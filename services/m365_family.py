"""
Microsoft 365 Family auto-invite service.

Automatically sends a Microsoft 365 Family sharing invite to a customer's
email after they buy an enabled product.

How it works
------------
Microsoft has NO official API for consumer Family invites, so this drives the
real account.microsoft.com "Microsoft 365 sharing" page in a headless
Chromium browser (Playwright), logged in with the owner's saved session.

Session setup (one time, on the owner's PC)
-------------------------------------------
1) pip install playwright && python -m playwright install chromium
2) python tools/m365_login.py   -> log in, press ENTER -> m365_session.json
3) Send that file to the bot: Admin Panel -> 📩 M365 Auto-Invite -> 🔑 Upload Session

The session (cookies) is stored ENCRYPTED in the settings table
(key: m365_session_state). Every successful run re-saves refreshed cookies so
the session stays alive as long as invites keep being sent.

If anything fails (expired session, changed page layout, Microsoft check),
the customer is told the invite is being processed and the ADMIN gets an
alert with the email, the error and a page screenshot + a 🔁 Retry button.
"""

import asyncio
import html
import io
import json
import os
import subprocess
import sys
import tempfile
import time

from telegram import InlineKeyboardMarkup
from utils.button_styles import AutoStyledButton as InlineKeyboardButton

from database.db import get_setting, set_setting
from utils.crypto import encrypt_text, decrypt_text
from utils.error_logger import log_error
from config import ADMIN_ID

SESSION_KEY = "m365_session_state"

SHARING_URL = "https://account.microsoft.com/services/microsoft365/sharing"

PAGE_TIMEOUT_MS = 60000


# ---------------------------------------------------------------- session

def has_session() -> bool:
    return bool(get_setting(SESSION_KEY))


def save_session(state_json: str):
    # Validate JSON before storing so a broken file can never be saved.
    json.loads(state_json)
    set_setting(SESSION_KEY, encrypt_text(state_json))


def load_session():
    raw = get_setting(SESSION_KEY)
    if not raw:
        return None
    value = decrypt_text(raw)
    if not value or value.startswith("[encrypted:"):
        return None
    return value


# ---------------------------------------------------------------- browser

_RUNTIME_INSTALL_DONE = False

# Tracks the last automation phase reached, so a hard timeout in the async
# wrapper can still report WHERE the flow got stuck (the worker thread cannot
# return a value once it is killed by asyncio.wait_for).
_LAST_PHASE = "not started"
# Last screenshot captured during a run. Stored at module level so the async
# timeout handler can still attach an image even if the worker thread is killed
# mid-run (e.g. browser.close() or a screenshot call hangs).
_LAST_SCREENSHOT = None

# Lean Chromium flags: keep memory low so the browser does not get OOM-killed
# on Railway while loading Microsoft's heavy account SPA (an OOM kill makes
# every following Playwright call hang on a dead browser, ignoring timeouts).
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-setuid-sandbox",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI",
    "--mute-audio",
    "--hide-scrollbars",
    "--no-first-run",
]

# Resource types to block (images/media/fonts) to slash memory + load time.
# CSS/stylesheets are kept so element visibility checks stay accurate.
_BLOCK_RESOURCE_TYPES = ("image", "media", "font")


def _log(msg: str):
    """Server-side breadcrumb, visible in Railway deploy logs."""
    try:
        print(f"[M365] {msg}", flush=True)
    except Exception:
        pass


def _block_heavy_resources(route):
    try:
        if route.request.resource_type in _BLOCK_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
    except Exception:
        try:
            route.continue_()
        except Exception:
            pass


def _install_chromium(with_deps: bool = False):
    """Last-resort runtime install if the build step did not install Chromium.

    with_deps=True also installs the system libraries Chromium needs
    (libnss3, libatk, libgbm, ...) — required when the browser binary exists
    but crashes instantly on launch because the runtime image lacks them.

    Guarded so it runs AT MOST ONCE per process and with tight timeouts —
    `playwright install-deps` shells out to apt-get, which can hang for many
    minutes on some hosts. The real install happens at build time via
    nixpacks.toml; this is only an emergency fallback.
    """
    global _RUNTIME_INSTALL_DONE
    if _RUNTIME_INSTALL_DONE:
        return
    _RUNTIME_INSTALL_DONE = True
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            timeout=180,
            capture_output=True,
        )
        if with_deps:
            subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                check=False,
                timeout=180,
                capture_output=True,
            )
    except Exception as exc:
        log_error("Playwright chromium install failed", exc)


# ---------------------------------------------------------------- core automation

def send_family_invite_sync(email: str):
    """Send a Microsoft 365 Family invite to `email`.

    Returns (ok: bool, message: str, screenshot: bytes | None).
    Runs blocking Playwright code — call via send_family_invite() from async code.
    """
    state_json = load_session()
    if not state_json:
        return False, "No Microsoft session uploaded. Admin Panel → 📩 M365 Auto-Invite → 🔑 Upload Session.", None

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "Playwright is not installed on the server (add `playwright` to requirements + install chromium).", None

    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    tmp.write(state_json)
    tmp.close()

    screenshot = None
    global _LAST_PHASE, _LAST_SCREENSHOT
    _LAST_PHASE = "launching browser"
    _LAST_SCREENSHOT = None
    # Internal deadline: return a real result + screenshot BEFORE the async
    # wrapper's hard timeout kills this thread (which would give no info).
    _deadline = time.monotonic() + 100

    _log(f"invite start -> {email}")
    try:
        with sync_playwright() as p:
            _log("playwright started, launching chromium")
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=_CHROMIUM_ARGS,
                )
                _log("chromium launched OK")
            except Exception as launch_exc:
                err_text = str(launch_exc)
                _log(f"launch failed: {err_text[:300]}")
                if (
                    "Executable doesn't exist" in err_text
                    or "has been closed" in err_text
                    or "Failed to launch" in err_text
                    or "error while loading shared libraries" in err_text
                ):
                    # Browser missing OR it crashed on startup. The system
                    # libraries are baked into the image at build time via
                    # railpack.json (deploy.aptPackages) and the browser binary
                    # via the build install step, so this is only a rare
                    # fallback. Install the browser binary ONLY (never apt at
                    # runtime — apt-get hangs on the Railway runtime image).
                    _install_chromium(with_deps=False)
                    browser = p.chromium.launch(
                        headless=True,
                        args=_CHROMIUM_ARGS,
                    )
                    _log("chromium launched OK after reinstall")
                else:
                    raise

            context = browser.new_context(
                storage_state=tmp.name,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            # Block heavy resources to keep memory low (prevents OOM hangs).
            try:
                context.route("**/*", _block_heavy_resources)
            except Exception:
                pass
            page = context.new_page()
            _log("context + page created")
            # Cap every individual page operation so nothing can hang forever.
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(30000)

            # Microsoft's account portal keeps background connections open, so
            # goto() can time out even though the page rendered fine. Navigate
            # with a tolerant strategy ("commit" = don't wait for full load),
            # then give the app time to render and verify content ourselves.
            # The /sharing URL redirects to /details, so try /details first.
            _LAST_PHASE = "navigating to subscription page"
            for nav_url in (
                "https://account.microsoft.com/services/microsoft365/details",
                SHARING_URL,
            ):
                try:
                    _log(f"goto start: {nav_url}")
                    page.goto(nav_url, wait_until="commit", timeout=25000)
                    _log(f"goto committed: {page.url[:120]}")
                    break
                except Exception as nav_exc:
                    _log(f"goto failed: {repr(nav_exc)[:300]}")
                    continue

            # Wait for the sharing UI (it's a slow single-page app): poll for up
            # to ~18s until a Share/invite control or a login redirect appears.
            _LAST_PHASE = "waiting for the subscription page to render"
            # Let the heavy single-page app settle before probing.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=12000)
                _log("domcontentloaded reached")
            except Exception as ls_exc:
                _log(f"load_state timeout/error: {repr(ls_exc)[:200]}")

            # Capture a viewport screenshot and stash it at module level so even
            # a hard timeout later can still attach an image. Viewport (NOT
            # full_page) screenshots are fast and never hang on tall pages.
            def _snap():
                global _LAST_SCREENSHOT
                try:
                    shot = page.screenshot(timeout=8000)
                    _LAST_SCREENSHOT = shot
                    _log(f"screenshot captured ({len(shot)} bytes)")
                    return shot
                except Exception as shot_exc:
                    _log(f"screenshot FAILED: {repr(shot_exc)[:200]}")
                    return _LAST_SCREENSHOT
            screenshot = _snap()

            # is_visible() returns immediately (no waiting), so this poll is
            # fast and bounded: ~10 x 1.5s = 15s max, capped by the deadline.
            sharing_ui_found = False
            for poll_i in range(10):
                if time.monotonic() > _deadline:
                    _log("poll aborted: internal deadline")
                    break
                try:
                    cur_url = page.url
                except Exception:
                    cur_url = ""
                _log(f"poll {poll_i}: url={cur_url[:120]}")
                if "login.live.com" in cur_url or "login.microsoftonline" in cur_url:
                    break
                for probe in ("Share", "Start sharing", "Invite", "Manage sharing"):
                    try:
                        if page.get_by_role("button", name=probe, exact=False).first.is_visible():
                            sharing_ui_found = True
                            break
                        if page.get_by_role("link", name=probe, exact=False).first.is_visible():
                            sharing_ui_found = True
                            break
                    except Exception:
                        continue
                if sharing_ui_found:
                    break
                page.wait_for_timeout(1500)

            _log(f"poll done: sharing_ui_found={sharing_ui_found}")
            # Refresh the screenshot with the final page state.
            screenshot = _snap()
            try:
                cur_url = page.url
            except Exception:
                cur_url = ""

            # Session expired -> Microsoft redirects to the login page.
            if "login.live.com" in cur_url or "login.microsoftonline" in cur_url:
                browser.close()
                return False, "Microsoft session EXPIRED — run tools/m365_login.py again and upload a fresh session.", screenshot

            # The Share/Manage control never appeared: return the screenshot +
            # URL + on-page text so we can see EXACTLY what the page shows
            # (consent wall, error, empty page, different button label, etc.).
            if not sharing_ui_found:
                snippet = ""
                try:
                    snippet = page.inner_text("body", timeout=4000)[:300].replace("\n", " ").strip()
                except Exception:
                    pass
                _log(f"share UI not found; url={cur_url[:120]} | text={snippet[:150]}")
                _log("closing browser (not-found path)")
                browser.close()
                _log("browser closed (not-found path)")
                return (
                    False,
                    "Loaded the account page but the Share/Manage button never appeared. "
                    f"URL: {cur_url or 'unknown'} | Page text: {snippet or '(empty)'}",
                    screenshot,
                )

            # 1) Open the invite dialog (several possible button labels).
            _LAST_PHASE = "clicking the blue Share button"
            _log("share UI found; opening invite dialog")
            opened = False
            for label in ("Start sharing", "Invite people", "Invite someone", "Add people", "Share", "Invite"):
                try:
                    btn = page.get_by_role("button", name=label, exact=False).first
                    if btn.is_visible(timeout=2500):
                        btn.click()
                        opened = True
                        break
                except Exception:
                    continue

            # Fallback: some layouts use a link (e.g. "Manage sharing" at the top).
            if not opened:
                for label in ("Manage sharing", "Share"):
                    try:
                        lnk = page.get_by_role("link", name=label, exact=False).first
                        if lnk.is_visible(timeout=2000):
                            lnk.click()
                            opened = True
                            break
                    except Exception:
                        continue

            page.wait_for_timeout(2500)

            # 1b) "Share your benefits" chooser dialog: pick "Email invite"
            #     (the other option is "Copy link").
            _LAST_PHASE = "choosing 'Email invite'"
            _log(f"dialog open attempt done (opened={opened}); choosing Email invite")
            for label in ("Email invite", "Invite via email", "Invite by email", "Email"):
                clicked_chooser = False
                for role in ("button", "link"):
                    try:
                        el = page.get_by_role(role, name=label, exact=True).first
                        if el.is_visible(timeout=1200):
                            el.click()
                            clicked_chooser = True
                            break
                    except Exception:
                        continue
                if clicked_chooser:
                    break

            page.wait_for_timeout(2000)

            # 2) Fill the customer's email into the "Add email" box.
            _LAST_PHASE = "entering the email address"
            _log("entering email address")
            filled = False
            for selector in (
                'input[placeholder="Add email" i]',
                'input[type="email"]',
                'input[name="email"]',
                'input[placeholder*="mail" i]',
                'input[aria-label*="mail" i]',
                'input[type="text"]',
            ):
                try:
                    box = page.locator(selector).first
                    if box.is_visible(timeout=2000):
                        box.click()
                        box.fill(email)
                        filled = True
                        break
                except Exception:
                    continue

            if not filled:
                screenshot = _snap()
                browser.close()
                return False, "Could not find the email box in the Microsoft invite dialog (layout may have changed).", screenshot

            # Commit the email chip (some layouts require Enter/Tab to register
            # it before the Share button becomes enabled).
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            page.wait_for_timeout(1200)

            # 3) Click the dialog's "Share" button to send the invite.
            #    It stays disabled until a valid email is entered.
            _LAST_PHASE = "clicking 'Share' to send the invite"
            _log("clicking Share to send")
            clicked = False
            for label in ("Share", "Send invite", "Send invitation", "Send", "Invite"):
                try:
                    btn = page.get_by_role("button", name=label, exact=True).first
                    if btn.is_visible(timeout=2000) and btn.is_enabled():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                # Retry: re-press Enter to register email, then try Share again.
                try:
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(1000)
                    btn = page.get_by_role("button", name="Share", exact=True).first
                    if btn.is_visible(timeout=1500) and btn.is_enabled():
                        btn.click()
                        clicked = True
                except Exception:
                    pass

            # Wait for the "Your invite has been sent" confirmation screen.
            _LAST_PHASE = "waiting for the 'invite sent' confirmation"
            _log("waiting for confirmation")
            page.wait_for_timeout(6000)

            body_text = ""
            try:
                body_text = page.inner_text("body", timeout=5000).lower()
            except Exception:
                pass

            success_markers = (
                "your invite has been sent",
                "invite has been sent",
                "invite sent",
                "invitation sent",
                "invitation was sent",
                "sent an invite",
                "sent an invitation",
            )
            ok = any(marker in body_text for marker in success_markers)
            _log(f"confirmation check: ok={ok}")

            screenshot = _snap()

            # Persist refreshed cookies so the session lives longer.
            try:
                save_session(json.dumps(context.storage_state()))
            except Exception:
                pass

            browser.close()

            if ok:
                return True, "Invite sent successfully.", screenshot
            return False, "Could not confirm the invite was sent (check the screenshot).", screenshot

    except Exception as exc:
        log_error("M365 invite automation failed", exc, extra=f"email={email}")
        return False, f"Automation error: {str(exc).strip()[:400]}", screenshot

    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


async def send_family_invite(email: str):
    """Async wrapper — runs the blocking Playwright flow in a worker thread.

    Wrapped in an overall timeout so the caller (and the Telegram UI) can never
    hang indefinitely, even if the browser or a page operation stalls.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(send_family_invite_sync, email),
            timeout=120,
        )
    except asyncio.TimeoutError:
        _log(f"HARD TIMEOUT 120s; last phase: {_LAST_PHASE}")
        return (
            False,
            f"Timed out. Last step reached: {_LAST_PHASE}. "
            "(A screenshot of where it stalled is attached if one was captured.)",
            _LAST_SCREENSHOT,
        )


# ---------------------------------------------------------------- orchestration

async def process_invite(bot, invite_id: int):
    """Run one invite end-to-end: automation + customer + admin notifications."""
    from database.db import get_m365_invite, set_m365_invite_status

    invite = get_m365_invite(invite_id)
    if not invite:
        return

    _id, user_id, order_id, email, _status, _error, _created = invite
    safe_email = html.escape(str(email), quote=False)

    try:
        ok, message, screenshot = await send_family_invite(email)
    except Exception as exc:
        ok, message, screenshot = False, f"Unexpected error: {str(exc).strip()[:400]}", None

    if ok:
        set_m365_invite_status(invite_id, "sent", None)

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "�� <b>Microsoft 365 Invite Sent!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📧 <b>Email:</b> <code>{safe_email}</code>\n\n"
                    "1️⃣ Open that inbox (check <b>Spam</b> too)\n"
                    "2️⃣ Open the email from <b>Microsoft</b>\n"
                    "3️⃣ Tap <b>Accept invitation</b> and sign in with THIS email\n\n"
                    "Enjoy Microsoft 365! 🎉"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "✅ <b>M365 Invite Auto-Sent</b>\n\n"
                    f"📧 <code>{safe_email}</code>\n"
                    f"👤 User: <code>{user_id}</code> | 🛒 Order: <b>#{order_id}</b>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ---------------- failure path ----------------
    set_m365_invite_status(invite_id, "failed", str(message)[:500])

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⏳ <b>Your Microsoft 365 invite is being processed.</b>\n\n"
                f"📧 It will arrive at <code>{safe_email}</code> shortly.\n\n"
                "If you don't receive it within a few hours, contact support from the main menu."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "❌ <b>M365 Auto-Invite FAILED</b>\n\n"
                f"📧 <b>Email:</b> <code>{safe_email}</code>\n"
                f"👤 <b>User:</b> <code>{user_id}</code> | 🛒 <b>Order:</b> <b>#{order_id}</b>\n\n"
                f"⚠️ <code>{html.escape(str(message)[:300], quote=False)}</code>\n\n"
                "👉 Send the invite manually OR fix the session and tap Retry."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Retry Invite", callback_data=f"admin_m365_retry_{invite_id}")],
                [InlineKeyboardButton("📩 M365 Menu", callback_data="admin_m365")],
            ]),
        )
    except Exception:
        pass

    if screenshot:
        try:
            await bot.send_photo(
                chat_id=ADMIN_ID,
                photo=io.BytesIO(screenshot),
                caption=f"📸 Page state for failed invite #{invite_id} ({safe_email})",
            )
        except Exception:
            pass
