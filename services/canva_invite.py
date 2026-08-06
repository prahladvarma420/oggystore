"""
Canva team auto-invite service.

Automatically sends a Canva team invite to a customer's email after they
buy an enabled product.

How it works
------------
Drives the real canva.com/settings/people page in a headless Chromium
browser (Playwright), logged in with the owner's saved session:
  Invite people -> enter email -> Send invitations -> wait for the
  "Invitation sent to <email>" confirmation and screenshot JUST that message.

Session setup (one time, on the owner's PC)
-------------------------------------------
1) pip install playwright && python -m playwright install chromium
2) python tools/canva_login.py   -> log in, press ENTER -> canva_session.json
3) Send that file to the bot: Admin Panel -> 🎨 Canva Auto-Invite -> 🔑 Upload Session

The session (cookies) is stored ENCRYPTED in the settings table
(key: canva_session_state). Every successful run re-saves refreshed cookies so
the session stays alive as long as invites keep being sent.

If anything fails (expired session, changed page layout), the customer is told
the invite is being processed and the ADMIN gets an alert with the email, the
error and a page screenshot + a 🔁 Retry button.
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

SESSION_KEY = "canva_session_state"

PEOPLE_URL = "https://www.canva.com/settings/people"

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
# wrapper can still report WHERE the flow got stuck.
from services import pw_lane

_LAST_PHASE = "not started"
# Last screenshot captured during a run (see services/m365_family.py for why).
_LAST_SCREENSHOT = None

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-quic",
    "--disable-blink-features=AutomationControlled",
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


def _proxy_config():
    """Optional proxy for when Cloudflare blocks the server's datacenter IP.

    Set the CANVA_PROXY environment variable on the worker, e.g.:
        http://user:pass@host:port    or    http://host:port
    A residential/ISP proxy works best against Cloudflare.
    """
    raw = os.environ.get("CANVA_PROXY", "").strip()
    if not raw:
        return None
    try:
        from urllib.parse import urlparse
        u = urlparse(raw if "://" in raw else "http://" + raw)
        cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg
    except Exception:
        return {"server": raw}


# Text markers of Cloudflare's challenge / block pages.
_CF_MARKERS = (
    "verify you are human",
    "just a moment",
    "designing again soon",
    "needs to review the security",
)


def _looks_like_cloudflare(page) -> bool:
    try:
        body = page.inner_text("body", timeout=3000).lower()
    except Exception:
        return False
    return any(m in body for m in _CF_MARKERS)


def _try_cloudflare_challenge(page) -> bool:
    """Try to pass Cloudflare's 'Verify you are human' checkbox.

    Returns True when the challenge page is no longer visible.
    """
    for round_i in range(6):
        if not _looks_like_cloudflare(page):
            return True
        _log(f"cloudflare challenge visible (round {round_i + 1}) — trying checkbox")

        # The Turnstile checkbox lives in a cross-origin iframe.
        try:
            for frame in page.frames:
                if "challenges.cloudflare.com" in (frame.url or ""):
                    for sel in ("input[type='checkbox']", "label", "#challenge-stage", "body"):
                        try:
                            frame.click(sel, timeout=2000)
                            _log(f"clicked challenge frame '{sel}'")
                            break
                        except Exception:
                            continue
                    break
        except Exception as fr_exc:
            _log(f"challenge frame click failed: {repr(fr_exc)[:150]}")

        # Also try clicking where the widget renders on the main page.
        try:
            page.mouse.click(640, 320)
        except Exception:
            pass

        page.wait_for_timeout(5000)
        if not _looks_like_cloudflare(page):
            _log("cloudflare challenge cleared")
            return True

        # Halfway through, try a fresh load — sometimes it passes silently.
        if round_i == 2:
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
                _log("reloaded page during challenge")
            except Exception:
                pass

    return not _looks_like_cloudflare(page)

# Block heavy resources (images/media/fonts) to slash memory + load time.
# CSS/stylesheets are kept so element visibility checks stay accurate.
_BLOCK_RESOURCE_TYPES = ("image", "media", "font")


def _log(msg: str):
    """Server-side breadcrumb, visible in Railway deploy logs."""
    try:
        print(f"[CANVA] {msg}", flush=True)
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
    """Last-resort runtime install if the build step did not install Chromium."""
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

def _toast_screenshot(page):
    """Screenshot of JUST the 'Invitation sent to ...' confirmation message
    (the small black toast), not the whole page."""
    try:
        el = page.get_by_text("Invitation sent to", exact=False).first
        if not el.is_visible(timeout=3000):
            return None
        # Climb to the toast container (the box that also holds the Ok button).
        for xp in ("xpath=ancestor::*[.//button][1]", "xpath=.."):
            try:
                box = el.locator(xp).first
                shot = box.screenshot(timeout=5000)
                if shot:
                    return shot
            except Exception:
                continue
        return el.screenshot(timeout=5000)
    except Exception:
        return None


def send_canva_invite_sync(email: str):
    """Lane-guarded entry: never run two Chromium jobs at the same time."""
    if not pw_lane.acquire(f"Canva invite {email}", wait_seconds=60):
        _log(f"browser lane busy ({pw_lane.busy_info()}) -> Canva invite deferred")
        return (
            False,
            "Another browser task is still running. Retry this invite in a "
            "minute.",
            None,
        )
    try:
        return _send_canva_invite_locked(email)
    finally:
        pw_lane.release()


def _send_canva_invite_locked(email: str):
    """Send a Canva team invite to `email`.

    Returns (ok: bool, message: str, screenshot: bytes | None).
    On success the screenshot is JUST the 'Invitation sent to <email>' toast.
    Runs blocking Playwright code — call via send_canva_invite() from async code.
    """
    state_json = load_session()
    if not state_json:
        return False, "No Canva session uploaded. Admin Panel → 🎨 Canva Auto-Invite → 🔑 Upload Session.", None

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
    _deadline = time.monotonic() + 100

    _log(f"invite start -> {email}")

    try:
        with sync_playwright() as p:
            _log("playwright started, launching chromium")
            _proxy = _proxy_config()
            if _proxy:
                _log(f"using proxy {_proxy.get('server')}")
            try:
                try:
                    browser = p.chromium.launch(
                        headless=True,
                        channel="chromium",
                        args=_CHROMIUM_ARGS,
                        proxy=_proxy,
                    )
                    _log("chromium launched OK (new headless mode)")
                except Exception as ch_exc:
                    _log(f"new-headless launch failed: {repr(ch_exc)[:200]}; falling back to headless shell")
                    browser = p.chromium.launch(
                        headless=True,
                        args=_CHROMIUM_ARGS,
                        proxy=_proxy,
                    )
                    _log("chromium launched OK (headless shell)")
            except Exception as launch_exc:
                err_text = str(launch_exc)
                _log(f"launch failed: {err_text[:300]}")
                if (
                    "Executable doesn't exist" in err_text
                    or "has been closed" in err_text
                    or "Failed to launch" in err_text
                    or "error while loading shared libraries" in err_text
                ):
                    _install_chromium(with_deps=False)
                    browser = p.chromium.launch(
                        headless=True,
                        args=_CHROMIUM_ARGS,
                        proxy=_proxy,
                    )
                    _log("chromium launched OK after reinstall")
                else:
                    raise

            context = browser.new_context(
                storage_state=tmp.name,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            # Hide the navigator.webdriver automation flag from Cloudflare.
            try:
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            except Exception:
                pass
            try:
                context.route("**/*", _block_heavy_resources)
            except Exception:
                pass
            page = context.new_page()
            _log("context + page created")
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(30000)

            # Navigate to the People page (tolerant strategy: don't wait for
            # full load — Canva keeps background connections open).
            _LAST_PHASE = "opening canva.com/settings/people"
            nav_ok = False
            for attempt in range(2):
                try:
                    _log(f"goto start (attempt {attempt + 1}): {PEOPLE_URL}")
                    page.goto(PEOPLE_URL, wait_until="commit", timeout=25000)
                    _log(f"goto committed: {page.url[:120]}")
                    nav_ok = True
                    break
                except Exception as nav_exc:
                    _log(f"goto failed: {repr(nav_exc)[:300]}")
                    continue

            if not nav_ok:
                _log("navigation never committed; returning early")
                _LAST_PHASE = "Canva's site did not respond"
                try:
                    browser.close()
                except Exception:
                    pass
                return (
                    False,
                    "Could not reach canva.com from the server — the page never started "
                    "responding. Check Deploy Logs (filter 'CANVA') for details.",
                    None,
                )

            _LAST_PHASE = "waiting for the People page to render"
            try:
                page.wait_for_load_state("domcontentloaded", timeout=12000)
                _log("domcontentloaded reached")
            except Exception as ls_exc:
                _log(f"load_state timeout/error: {repr(ls_exc)[:200]}")

            # Cloudflare sometimes challenges datacenter IPs with a
            # "Verify you are human" checkbox — try to pass it.
            if _looks_like_cloudflare(page):
                _LAST_PHASE = "passing the Cloudflare human check"
                _log("cloudflare challenge detected after load")
                _try_cloudflare_challenge(page)

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

            # Poll until the 'Invite people' button or a login redirect appears.
            invite_ui_found = False
            for poll_i in range(10):
                if time.monotonic() > _deadline:
                    _log("poll aborted: internal deadline")
                    break
                try:
                    cur_url = page.url
                except Exception:
                    cur_url = ""
                _log(f"poll {poll_i}: url={cur_url[:120]}")
                if "/login" in cur_url or "/signup" in cur_url:
                    break
                for probe in ("Invite people", "Invite"):
                    try:
                        if page.get_by_role("button", name=probe, exact=False).first.is_visible():
                            invite_ui_found = True
                            break
                        if page.get_by_role("link", name=probe, exact=False).first.is_visible():
                            invite_ui_found = True
                            break
                    except Exception:
                        continue
                if invite_ui_found:
                    break
                page.wait_for_timeout(1500)

            _log(f"poll done: invite_ui_found={invite_ui_found}")
            screenshot = _snap()
            try:
                cur_url = page.url
            except Exception:
                cur_url = ""

            # Session expired -> Canva redirects to the login page.
            if "/login" in cur_url or "/signup" in cur_url:
                browser.close()
                return False, "Canva session EXPIRED — run tools/canva_login.py again and upload a fresh session.", screenshot

            if not invite_ui_found:
                snippet = ""
                try:
                    snippet = page.inner_text("body", timeout=4000)[:300].replace("\n", " ").strip()
                except Exception:
                    pass
                _log(f"invite button not found; url={cur_url[:120]} | text={snippet[:150]}")
                browser.close()
                if any(m in snippet.lower() for m in _CF_MARKERS):
                    return (
                        False,
                        "Cloudflare is blocking the server's IP with a 'Verify you are human' "
                        "check, and the automatic pass attempt failed. Your Canva session is fine — "
                        "the server's datacenter IP is the problem. Fix: set the CANVA_PROXY "
                        "environment variable on the Railway worker to a residential proxy "
                        "(format: http://user:pass@host:port), redeploy, then Retry.",
                        screenshot,
                    )
                return (
                    False,
                    "Loaded the Canva People page but the 'Invite people' button never appeared. "
                    f"URL: {cur_url or 'unknown'} | Page text: {snippet or '(empty)'}",
                    screenshot,
                )

            # 1) Click 'Invite people' to open the invite dialog.
            _LAST_PHASE = "clicking 'Invite people'"
            _log("clicking Invite people")
            opened = False
            for label in ("Invite people", "Invite"):
                for role in ("button", "link"):
                    try:
                        el = page.get_by_role(role, name=label, exact=False).first
                        if el.is_visible(timeout=2500):
                            el.click()
                            opened = True
                            break
                    except Exception:
                        continue
                if opened:
                    break

            page.wait_for_timeout(2500)

            # 2) Fill the customer's email into the 'Enter email address...' box
            #    inside the invite dialog (NOT the page's member search box).
            _LAST_PHASE = "entering the email address"
            _log(f"entering email (dialog opened={opened})")
            candidates = (
                'input[placeholder*="Enter email" i]',
                '[role="dialog"] input[placeholder*="email" i]',
                '[role="dialog"] input[type="email"]',
                'input[type="email"]',
            )
            filled = False
            for selector in candidates:
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
                return False, "Could not find the email box in the Canva invite dialog (layout may have changed).", screenshot

            # Commit the address (some layouts turn it into a chip on Enter).
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            page.wait_for_timeout(1200)

            # 3) Click 'Send invitations'.
            _LAST_PHASE = "clicking 'Send invitations'"
            _log("clicking Send invitations")
            clicked = False
            for label in ("Send invitations", "Send invitation", "Send"):
                try:
                    btn = page.get_by_role("button", name=label, exact=False).first
                    if btn.is_visible(timeout=2000) and btn.is_enabled():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                try:
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(1000)
                    btn = page.get_by_role("button", name="Send invitations", exact=False).first
                    if btn.is_visible(timeout=1500) and btn.is_enabled():
                        btn.click()
                        clicked = True
                except Exception:
                    pass

            if not clicked:
                screenshot = _snap()
                browser.close()
                return False, "Could not click 'Send invitations' in the Canva dialog (layout may have changed).", screenshot

            # 4) Wait for the 'Invitation sent to <email>' confirmation toast.
            _LAST_PHASE = "waiting for the 'Invitation sent' confirmation"
            _log("waiting for confirmation toast")
            page.wait_for_timeout(4000)

            body_text = ""
            try:
                body_text = page.inner_text("body", timeout=5000).lower()
            except Exception:
                pass

            success_markers = (
                "invitation sent to",
                "invitations sent",
                "invitation sent",
                "invite sent",
            )
            ok = any(marker in body_text for marker in success_markers)
            _log(f"confirmation check: ok={ok}")

            if ok:
                # Screenshot of JUST the confirmation message, as requested.
                toast = _toast_screenshot(page)
                screenshot = toast or _snap()
            else:
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
        log_error("Canva invite automation failed", exc, extra=f"email={email}")
        return False, f"Automation error: {str(exc).strip()[:400]}", screenshot

    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


async def send_canva_invite(email: str):
    """Async wrapper — runs the blocking Playwright flow in a worker thread,
    capped with an overall timeout so the Telegram UI can never hang."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(send_canva_invite_sync, email),
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
    from database.db import get_canva_invite, set_canva_invite_status

    invite = get_canva_invite(invite_id)
    if not invite:
        return

    _id, user_id, order_id, email, _status, _error, _created = invite
    safe_email = html.escape(str(email), quote=False)

    try:
        ok, message, screenshot = await send_canva_invite(email)
    except Exception as exc:
        ok, message, screenshot = False, f"Unexpected error: {str(exc).strip()[:400]}", None

    if ok:
        set_canva_invite_status(invite_id, "sent", None)

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "🎨 <b>Canva Invite Sent!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📧 <b>Email:</b> <code>{safe_email}</code>\n\n"
                    "1️⃣ Open that inbox (check <b>Spam</b> too)\n"
                    "2️⃣ Open the email from <b>Canva</b>\n"
                    "3️⃣ Tap the button inside and sign in with THIS email\n\n"
                    "Enjoy Canva! 🎉"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "✅ <b>Canva Invite Auto-Sent</b>\n\n"
                    f"📧 <code>{safe_email}</code>\n"
                    f"👤 User: <code>{user_id}</code> | 🛒 Order: <b>#{order_id}</b>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Proof screenshot: JUST the 'Invitation sent to <email>' message.
        if screenshot:
            try:
                await bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=io.BytesIO(screenshot),
                    caption=f"📸 Canva confirmation — invite #{invite_id} ({safe_email})",
                )
            except Exception:
                pass
        return

    # ---------------- failure path ----------------
    set_canva_invite_status(invite_id, "failed", str(message)[:500])

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "⏳ <b>Your Canva invite is being processed.</b>\n\n"
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
                "❌ <b>Canva Auto-Invite FAILED</b>\n\n"
                f"📧 <b>Email:</b> <code>{safe_email}</code>\n"
                f"👤 <b>User:</b> <code>{user_id}</code> | 🛒 <b>Order:</b> <b>#{order_id}</b>\n\n"
                f"⚠️ <code>{html.escape(str(message)[:300], quote=False)}</code>\n\n"
                "👉 Send the invite manually OR fix the session and tap Retry."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Retry Invite", callback_data=f"admin_canva_retry_{invite_id}")],
                [InlineKeyboardButton("🎨 Canva Menu", callback_data="admin_canva")],
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
