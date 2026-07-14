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
import re
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
    try:
        from database.db import count_active_m365_sessions
        if count_active_m365_sessions() > 0:
            return True
    except Exception:
        pass
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


# ------------------------------------------------- multi-session rotation

_ROTATION_NOTE = None


def pop_rotation_note():
    """Return and clear the last session-rotation alert (HTML), if any."""
    global _ROTATION_NOTE
    note = _ROTATION_NOTE
    _ROTATION_NOTE = None
    return note


def _click_any(page, labels, timeout=8000):
    """Click the first visible button/submit matching any of the given labels."""
    for label in labels:
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.is_visible(timeout=max(1200, timeout // max(1, len(labels)))):
                btn.click()
                return True
        except Exception:
            pass
        try:
            inp = page.locator(f"input[type='submit'][value*='{label}' i]").first
            if inp.is_visible(timeout=1200):
                inp.click()
                return True
        except Exception:
            pass
    return False


def _click_link_any(page, labels, timeout=2000):
    """Click the first visible link/button/text matching any label (regex-escaped)."""
    for label in labels:
        pat = re.compile(re.escape(label), re.I)
        for getter in (
            lambda: page.get_by_role("link", name=pat).first,
            lambda: page.get_by_role("button", name=pat).first,
            lambda: page.get_by_text(pat).first,
        ):
            try:
                el = getter()
                if el.is_visible(timeout=timeout):
                    el.click()
                    return True
            except Exception:
                continue
    return False


def _extract_totp_secret(page):
    """Scrape a base32 TOTP setup key from the current page, if one is shown.

    Microsoft's "different authenticator app" screen prints the key on its own
    line, so we scan line-by-line (most reliable) and only fall back to a
    grouped inline match that stays within a single line.
    """
    try:
        body = page.inner_text("body", timeout=4000)
    except Exception:
        return None
    import re as _re
    for raw in body.splitlines():
        line = raw.strip()
        if ":" in line:
            line = line.split(":")[-1].strip()
        compact = _re.sub(r"[\s-]+", "", line)
        if 16 <= len(compact) <= 64 and _re.fullmatch(r"[A-Za-z2-7]+", compact) and _re.search(r"[2-7]", compact):
            return compact.upper()
    # Fallback: 4-char groups separated by spaces/tabs WITHIN one line only
    # (never across newlines), and only if the result contains a base32 digit.
    m = _re.search(r"((?:[A-Za-z2-7]{4}[ \t-]+){3,}[A-Za-z2-7]{4})", body)
    if m:
        cand = _re.sub(r"[\s-]+", "", m.group(1)).upper()
        if _re.search(r"[2-7]", cand):
            return cand
    return None


def _short_url(page):
    try:
        return (page.url or "")[:100]
    except Exception:
        return "?"


def _page_text(page, n=220):
    try:
        return page.inner_text("body", timeout=2500)[:n].replace("\n", " ").strip()
    except Exception:
        return ""


def _do_login(page, email, password, totp_secret):
    """Re-entrant Microsoft consumer sign-in. Returns (ok, message, discovered_secret).

    Microsoft bounces between login.microsoftonline.com/common and
    login.live.com and sometimes RE-SHOWS the email page after a redirect.
    So instead of a fixed email->password->done order, we loop: each pass we
    detect whichever screen is actually visible (email / verify / password /
    passkey / forced Authenticator setup / OTP / stay-signed-in) and act on it.
    """
    discovered_secret = None
    EMAIL_SEL = "input[type=\'email\'], input[name=\'loginfmt\']"
    PW_SEL = "input[type=\'password\'], input[name=\'passwd\']"
    OTP_SEL = "input[name=\'otc\'], input[autocomplete=\'one-time-code\'], input[inputmode=\'numeric\'], input[type=\'tel\']"

    def _vis(selector, timeout=600):
        try:
            return page.locator(selector).first.is_visible(timeout=timeout)
        except Exception:
            return False

    def _text_vis(pattern, timeout=600):
        try:
            return page.get_by_text(re.compile(pattern, re.I)).first.is_visible(timeout=timeout)
        except Exception:
            return False

    emails = 0
    passwords = 0
    otps = 0
    try:
        _log(f"login: start url={_short_url(page)}")
        for step in range(18):
            try:
                low = (page.url or "").lower()
            except Exception:
                low = ""

            # A) Keep-signed-in prompt -> Yes (we are basically in).
            if _text_vis("Stay signed in"):
                _click_any(page, ["Yes"], timeout=5000)
                _log("login: clicked 'Stay signed in? -> Yes'")
                page.wait_for_timeout(2000)
                continue

            # B) Success: left the sign-in hosts entirely.
            on_login = ("login.live.com" in low or "login.microsoftonline" in low or "login.microsoft.com" in low)
            if low and not on_login:
                _log(f"login: left sign-in host, now at {_short_url(page)}")
                break

            # C) Account picker -> use another account.
            try:
                tile = page.get_by_role("button", name=re.compile("another account|use another", re.I)).first
                if tile.is_visible(timeout=500):
                    tile.click(); page.wait_for_timeout(1200); continue
            except Exception:
                pass

            # D) Forced "set up Microsoft Authenticator" (QR) -> switch to text key.
            if not discovered_secret and _text_vis("set up the Microsoft Authenticator|to sign in to Microsoft Authenticator|scan this QR code|Scan the QR code|Start by getting the app", 600):
                _log("login: forced Authenticator setup detected")
                _click_link_any(page, [
                    "I want to use a different authenticator app", "use a different authenticator app",
                    "different authenticator app", "Set up a different Authenticator app",
                    "I can\'t scan the QR code", "Can\'t scan the image", "Can\'t scan", "enter the code manually", "Next",
                ], timeout=2500)
                page.wait_for_timeout(2500)
                secret = _extract_totp_secret(page)
                if secret:
                    discovered_secret = secret
                    _log(f"login: scraped setup key len={len(secret)}; registering")
                    _click_any(page, ["Next"]); page.wait_for_timeout(2500)
                    if _vis(OTP_SEL, 4000):
                        try:
                            from utils.totp import totp_now
                            page.locator(OTP_SEL).first.fill(totp_now(secret), timeout=8000)
                            _click_any(page, ["Next", "Verify", "Done", "Sign in", "Submit"])
                            page.wait_for_timeout(2500)
                        except Exception:
                            pass
                    continue
                return False, ("Microsoft is FORCING Microsoft Authenticator (QR) setup for this ID and offered no text "
                               "setup key. Set up 2FA once manually via 'I want to use a different authenticator app' to "
                               "get the SETUP KEY, paste it into Add Login, then retry."), discovered_secret

            # E) Passkey / "use your phone" nudge -> Cancel / Skip.
            if "fido" in low or "passkey" in low or _text_vis("use your phone or tablet|sign in faster with a passkey|create a passkey", 500):
                if _click_link_any(page, ["Cancel", "Skip for now", "Not now", "Maybe later", "No thanks", "Back"], timeout=1500):
                    _log("login: dismissed passkey prompt"); page.wait_for_timeout(1500); continue

            # F) OTP / 2FA code prompt (only when no password box is showing).
            if (totp_secret or discovered_secret) and _vis(OTP_SEL, 500) and not _vis(PW_SEL, 250):
                if otps < 3:
                    try:
                        from utils.totp import totp_now
                        page.locator(OTP_SEL).first.fill(totp_now(discovered_secret or totp_secret), timeout=8000)
                        _click_any(page, ["Verify", "Sign in", "Next", "Submit"])
                        otps += 1; _log("login: entered TOTP code"); page.wait_for_timeout(2500); continue
                    except Exception:
                        pass

            # G) "Verify your email" / passwordless -> switch to password.
            if _click_link_any(page, [
                "Use your password", "Use my password", "Sign in with your password",
                "Sign in with a password", "Enter your password instead", "Use password",
            ], timeout=700):
                _log("login: clicked 'Use your password'"); page.wait_for_timeout(2000); continue

            email_vis = _vis(EMAIL_SEL, 600)

            # H) Password field (only when the email field is NOT the one showing).
            if _vis(PW_SEL, 600) and not email_vis:
                if passwords >= 3:
                    return False, "password step kept looping (likely wrong password)", discovered_secret
                try:
                    box = page.locator(PW_SEL).first
                    box.fill(password, timeout=12000)
                    if not _click_any(page, ["Sign in", "Next", "Submit"]):
                        try: box.press("Enter")
                        except Exception: pass
                    passwords += 1; page.wait_for_timeout(3500)
                    _post_pw = _page_text(page, 320)
                    _log(f"login: password submitted (#{passwords}) url={_short_url(page)} :: {_post_pw[:200]}")
                    _low_pw = _post_pw.lower()
                    if any(k in _low_pw for k in [
                        "your account or password is incorrect", "password is incorrect",
                        "that password is incorrect", "incorrect password",
                    ]):
                        return False, "Microsoft says the PASSWORD is incorrect for this ID. Update it in Admin → M365 → Sessions.", discovered_secret
                    if any(k in _low_pw for k in [
                        "unusual", "verify your identity", "help us protect", "protect your account",
                        "verify it's you", "we need to verify", "prove you're not a robot",
                        "let's prove", "solve the puzzle", "press and hold",
                    ]):
                        return False, ("Microsoft is asking for EXTRA VERIFICATION after the password (unusual-activity / "
                                       "identity or robot check) — automated login cannot pass this. Use the session-upload "
                                       "method for this ID: sign in once manually, then upload its session file."), discovered_secret
                    continue
                except Exception as e:
                    return False, f"could not enter password ({repr(e)[:90]})", discovered_secret

            # I) Email field -> fill / advance (handles redirect re-prompts).
            if email_vis:
                # If we already did a full email+password cycle and got bounced
                # back here, the credentials were rejected.
                if passwords >= 1:
                    return False, ("Entered the email and password but Microsoft bounced back to the sign-in screen. "
                                   "This almost always means the PASSWORD is wrong or the account needs extra "
                                   "verification. Re-check the password in Admin → M365 → Sessions."), discovered_secret
                if emails >= 3:
                    return False, ("email step kept looping across redirects (page: " + (_page_text(page) or "?") + ")"), discovered_secret
                try:
                    box = page.locator(EMAIL_SEL).first
                    try:
                        cur_val = box.input_value(timeout=500)
                    except Exception:
                        cur_val = ""
                    if email.lower() not in (cur_val or "").lower():
                        box.fill(email, timeout=10000)
                    if not _click_any(page, ["Next", "Sign in"]):
                        try: box.press("Enter")
                        except Exception: pass
                    emails += 1; page.wait_for_timeout(3500)
                    _log(f"login: email submitted #{emails} url={_short_url(page)}")
                    if any(k in _page_text(page, 400).lower() for k in [
                        "that microsoft account doesn\'t exist", "couldn\'t find an account", "we couldn\'t find",
                        "enter a valid email", "this username may be incorrect", "no account found",
                    ]):
                        return False, "Microsoft rejected the login EMAIL (account not found / typo). Fix it in Admin → M365 → Sessions.", discovered_secret
                    continue
                except Exception as e:
                    return False, f"could not enter email ({repr(e)[:90]})", discovered_secret

            # Nothing recognized this pass; wait and re-scan.
            page.wait_for_timeout(1500)

        # Final keep-signed-in safety click.
        if _text_vis("Stay signed in"):
            _click_any(page, ["Yes"], timeout=5000); page.wait_for_timeout(2000)

        try:
            low = (page.url or "").lower()
        except Exception:
            low = ""
        if "login.live.com" in low or "login.microsoftonline" in low or "login.microsoft.com" in low:
            return False, f"still on the sign-in page after submitting (page said: {_page_text(page) or 'unknown'})", discovered_secret
        return True, "signed in", discovered_secret
    except Exception as e:
        return False, f"login error: {repr(e)[:140]}", discovered_secret


def _load_active_session():
    """Return (session_id, label, state_json_or_None, creds_or_None) for the
    first non-full Microsoft ID.

    - state_json is the saved cookies (None when only credentials are stored).
    - creds is (email, password, totp_secret) when a saved login exists.
    Falls back to (and migrates) the legacy single-session settings key.
    Returns None when no usable session exists.
    """
    try:
        from database.db import (
            get_current_m365_session,
            list_m365_sessions,
            add_m365_session,
            get_m365_credentials,
        )
        row = get_current_m365_session()
        if row:
            sid, label, state, _used = row
            try:
                creds = get_m365_credentials(sid)
            except Exception:
                creds = None
            state_ok = bool(state) and not str(state).startswith("[encrypted:")
            if state_ok:
                return sid, str(label or f"ID #{sid}"), state, creds
            if creds:
                # No usable cookies yet, but we can log in with credentials.
                return sid, str(label or f"ID #{sid}"), None, creds
            return None
        if not list_m365_sessions():
            legacy = load_session()
            if legacy:
                sid = add_m365_session("ID #1", legacy)
                try:
                    set_setting(SESSION_KEY, "")
                except Exception:
                    pass
                _log("migrated legacy session into sessions table as ID #1")
                return sid, "ID #1", legacy, None
        return None
    except Exception:
        legacy = load_session()
        if legacy:
            return None, "legacy session", legacy, None
        return None


def _persist_session_state(session_id, state_json):
    """Save refreshed cookies back to the right storage slot."""
    if session_id is None:
        save_session(state_json)
        return
    from database.db import update_m365_session_state
    update_m365_session_state(session_id, state_json)


def _handle_rotation(session_id, session_label):
    """Count a successful invite; when this ID is full, rotate to the next one."""
    global _ROTATION_NOTE
    if session_id is None:
        return
    from database.db import increment_m365_session_used, mark_m365_session_full, get_current_m365_session
    used = increment_m365_session_used(session_id)
    try:
        limit = int(float(get_setting("m365_invites_per_session", "5") or 5))
    except Exception:
        limit = 5
    if limit <= 0 or used < limit:
        return
    mark_m365_session_full(session_id)
    safe_label = html.escape(str(session_label), quote=False)
    nxt = get_current_m365_session()
    if nxt:
        nxt_label = html.escape(str(nxt[1]), quote=False)
        _ROTATION_NOTE = (
            f"Microsoft ID <b>{safe_label}</b> is now FULL (<code>{used}/{limit}</code> invites used).\n\n"
            f"➡️ Switched to the next ID: <b>{nxt_label}</b>"
        )
    else:
        _ROTATION_NOTE = (
            f"🚨 Microsoft ID <b>{safe_label}</b> is now FULL (<code>{used}/{limit}</code> invites used) "
            "and there are <b>NO more IDs left</b>!\n\nUpload a new session now or the next invites will FAIL:\n"
            "Admin Panel → 📩 M365 Auto-Invite → 🔑 Upload Session"
        )
    _log(f"rotation: session #{session_id} full ({used}/{limit}); next={'yes' if nxt else 'NONE'}")


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
    global _ROTATION_NOTE
    _ROTATION_NOTE = None
    _session = _load_active_session()
    if not _session:
        try:
            from database.db import list_m365_sessions
            _all_full = bool(list_m365_sessions())
        except Exception:
            _all_full = False
        if _all_full:
            return False, "ALL Microsoft IDs are FULL — upload a new session or reset one: Admin Panel → 📩 M365 Auto-Invite → 📂 Sessions.", None
        return False, "No Microsoft session uploaded. Admin Panel → 📩 M365 Auto-Invite → 🔑 Upload Session.", None
    session_id, session_label, state_json, session_creds = _session

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "Playwright is not installed on the server (add `playwright` to requirements + install chromium).", None

    # A credential-only ID (no saved cookies yet) starts with a fresh browser
    # and signs in automatically below.
    tmp = None
    if state_json:
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

    # --- Network preflight: can this container even reach Microsoft? ---
    _LAST_PHASE = "checking network connectivity to Microsoft"
    try:
        import socket as _socket
        infos = _socket.getaddrinfo("account.microsoft.com", 443)
        addrs = sorted({i[4][0] for i in infos})
        _log(f"preflight DNS -> {addrs[:6]}")
    except Exception as dns_exc:
        _log(f"preflight DNS FAILED: {repr(dns_exc)[:200]}")
    try:
        import urllib.request as _urlreq
        _req = _urlreq.Request(
            "https://account.microsoft.com",
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with _urlreq.urlopen(_req, timeout=12) as _resp:
            _log(f"preflight HTTPS from python: status {_resp.status}")
    except Exception as pf_exc:
        _log(f"preflight HTTPS from python FAILED: {repr(pf_exc)[:200]}")

    try:
        with sync_playwright() as p:
            _log("playwright started, launching chromium")
            try:
                try:
                    # New headless mode (full Chromium binary): a much closer
                    # fingerprint to real Chrome than the default headless
                    # shell, which Microsoft/Akamai bot detection tarpits
                    # (accepts the connection but never responds).
                    browser = p.chromium.launch(
                        headless=True,
                        channel="chromium",
                        args=_CHROMIUM_ARGS,
                    )
                    _log("chromium launched OK (new headless mode)")
                except Exception as ch_exc:
                    _log(f"new-headless launch failed: {repr(ch_exc)[:200]}; falling back to headless shell")
                    browser = p.chromium.launch(
                        headless=True,
                        args=_CHROMIUM_ARGS,
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

            _ctx_kwargs = {"storage_state": tmp.name} if tmp else {}
            context = browser.new_context(
                **_ctx_kwargs,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
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

            # Canary navigation: proves whether Chromium's networking works at
            # all in this container (separates "network broken" from
            # "Microsoft blocking us").
            try:
                _log("canary goto: https://example.com")
                page.goto("https://example.com", wait_until="commit", timeout=10000)
                _log(f"canary OK: {page.url[:80]}")
            except Exception as can_exc:
                _log(f"canary goto FAILED: {repr(can_exc)[:200]}")
            # Second canary: does Microsoft's MAIN site answer this browser?
            # Separates "all of Microsoft blocks us" from "only the account
            # portal blocks us".
            try:
                _log("canary2 goto: https://www.microsoft.com")
                page.goto("https://www.microsoft.com", wait_until="commit", timeout=12000)
                _log(f"canary2 OK: {page.url[:80]}")
            except Exception as can2_exc:
                _log(f"canary2 goto FAILED: {repr(can2_exc)[:200]}")

            # Microsoft's account portal keeps background connections open, so
            # goto() can time out even though the page rendered fine. Navigate
            # with a tolerant strategy ("commit" = don't wait for full load),
            # then give the app time to render and verify content ourselves.
            # The /sharing URL redirects to /details, so try /details first.
            _LAST_PHASE = "navigating to subscription page"
            nav_ok = False
            for nav_url in (
                "https://account.microsoft.com/services/microsoft365/details",
                SHARING_URL,
            ):
                try:
                    _log(f"goto start: {nav_url}")
                    page.goto(nav_url, wait_until="commit", timeout=25000)
                    _log(f"goto committed: {page.url[:120]}")
                    nav_ok = True
                    break
                except Exception as nav_exc:
                    _log(f"goto failed: {repr(nav_exc)[:300]}")
                    continue

            if not nav_ok:
                # The site never started responding. Do NOT touch the page
                # object again: with a hung pending navigation, page/locator
                # calls block forever (this caused the silent 120s timeouts).
                # Return a clear error immediately instead.
                _log("navigation never committed; returning early (site unreachable/blocked)")
                _LAST_PHASE = "Microsoft's site did not respond (network blocked?)"
                try:
                    browser.close()
                    _log("browser closed (unreachable path)")
                except Exception as close_exc:
                    _log(f"browser close failed: {repr(close_exc)[:120]}")
                return (
                    False,
                    "Could not reach account.microsoft.com from the server — the page never "
                    "started responding (both URLs timed out at the network level). Chromium "
                    "itself is working fine. This usually means Microsoft is blocking the "
                    "datacenter IP. Check Deploy Logs (filter 'M365') for preflight + canary "
                    "results.",
                    None,
                )

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

            # Login page shown -> either the cookies expired, or this is a
            # credential-only ID signing in for the first time.
            if "login.live.com" in cur_url or "login.microsoftonline" in cur_url:
                if session_creds and session_creds[0] and session_creds[1]:
                    _LAST_PHASE = "auto-signing in with saved credentials"
                    _log(f"login page detected; auto sign-in for '{session_label}'")
                    login_ok, login_msg, _new_secret = _do_login(page, session_creds[0], session_creds[1], session_creds[2])
                    _log(f"auto sign-in result: ok={login_ok} msg={login_msg} new_secret={'yes' if _new_secret else 'no'}")
                    if _new_secret and session_id is not None:
                        try:
                            from database.db import set_m365_credentials
                            set_m365_credentials(session_id, session_creds[0], session_creds[1], _new_secret)
                            _log("saved auto-registered TOTP secret to this session")
                        except Exception as _sec_exc:
                            _log(f"could not save auto-registered secret: {repr(_sec_exc)[:120]}")
                    screenshot = _snap()
                    if not login_ok:
                        browser.close()
                        return False, f"Auto sign-in failed for '" + str(session_label) + "': " + str(login_msg) + ". Check the email/password/2FA secret in Admin → M365 → Sessions.", screenshot
                    # Re-open the subscription page now that we are signed in.
                    for nav_url in (
                        "https://account.microsoft.com/services/microsoft365/details",
                        SHARING_URL,
                    ):
                        try:
                            page.goto(nav_url, wait_until="commit", timeout=25000)
                            break
                        except Exception:
                            continue
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=12000)
                    except Exception:
                        pass
                    # Persist the fresh cookies so future invites skip the login.
                    try:
                        _persist_session_state(session_id, json.dumps(context.storage_state()))
                        _log("fresh login cookies saved to session")
                    except Exception as save_exc:
                        _log(f"could not save fresh cookies: {repr(save_exc)[:120]}")
                    # Re-poll for the sharing UI after signing in.
                    sharing_ui_found = False
                    screenshot = _snap()
                    for poll_i in range(10):
                        if time.monotonic() > _deadline:
                            break
                        try:
                            cur_url = page.url
                        except Exception:
                            cur_url = ""
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
                    screenshot = _snap()
                    _log(f"post-login poll: sharing_ui_found={sharing_ui_found}")
                else:
                    browser.close()
                    return False, f"Microsoft session '" + str(session_label) + "' EXPIRED — add a saved login (email + password + 2FA secret) so it can refresh itself, or upload a fresh session. Admin → 📩 M365 Auto-Invite → 📂 Sessions.", screenshot

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
                _persist_session_state(session_id, json.dumps(context.storage_state()))
            except Exception:
                pass

            browser.close()

            if ok:
                try:
                    _handle_rotation(session_id, session_label)
                except Exception as rot_exc:
                    _log(f"rotation bookkeeping failed: {rot_exc}")
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

        rotation_note = pop_rotation_note()
        if rotation_note:
            try:
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text="🔄 <b>M365 Session Rotation</b>\n\n" + rotation_note,
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
