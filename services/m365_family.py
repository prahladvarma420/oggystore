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


# ------------------------------------------------- persistence heuristic

# Microsoft auth cookies. ESTSAUTHPERSISTENT is the durable "Stay signed in"
# token: when it is present with a real future expiry the login survives
# browser restarts for weeks. ESTSAUTH alone (session-only, expires == -1)
# usually dies within ~24h.
_MS_PERSISTENT_COOKIE = "ESTSAUTHPERSISTENT"
_MS_AUTH_COOKIES = ("ESTSAUTHPERSISTENT", "ESTSAUTH", "ESTSAUTHLIGHT")
_MS_AUTH_DOMAIN_HINTS = ("login.microsoftonline.com", "login.live.com", "microsoft", "live.com")


def analyze_session_persistence(state_json):
    """Inspect an uploaded Playwright storage_state and decide whether the
    Microsoft login looks *persistent* (survives browser close, lasts weeks)
    or *session-only* (no "Stay signed in" token -> usually dies within ~24h).

    Returns a dict:
        {
          "persistent": bool,
          "reason": str,
          "warning_html": str | None,  # admin-facing warning (None if durable)
        }
    Never raises: on any parsing problem it returns persistent=True with no
    warning so it can never block a valid upload.
    """
    try:
        data = json.loads(state_json) if isinstance(state_json, str) else (state_json or {})
        cookies = data.get("cookies") or []
    except Exception:
        return {"persistent": True, "reason": "unparseable", "warning_html": None}

    now = time.time()
    day = 24 * 3600

    def _ms_auth(c):
        name = str(c.get("name") or "")
        if name not in _MS_AUTH_COOKIES:
            return None
        dom = str(c.get("domain") or "").lower()
        if dom and not any(h in dom for h in _MS_AUTH_DOMAIN_HINTS):
            return None
        try:
            exp = float(c.get("expires", -1))
        except Exception:
            exp = -1.0
        return name, exp

    best = {}  # cookie name -> furthest-future expiry seen
    for c in cookies:
        try:
            got = _ms_auth(c)
        except Exception:
            got = None
        if not got:
            continue
        name, exp = got
        if name not in best or exp > best[name]:
            best[name] = exp

    persistent_exp = best.get(_MS_PERSISTENT_COOKIE)
    durable = persistent_exp is not None and persistent_exp > now + day

    if durable:
        days_left = max(1, int((persistent_exp - now) // day))
        return {
            "persistent": True,
            "reason": f"ESTSAUTHPERSISTENT valid ~{days_left}d",
            "warning_html": None,
        }

    if not best:
        reason = "no Microsoft auth cookies found in this file"
    elif _MS_PERSISTENT_COOKIE not in best:
        reason = "no 'Stay signed in' token (ESTSAUTHPERSISTENT missing)"
    elif persistent_exp is not None and persistent_exp <= 0:
        reason = "'Stay signed in' token is session-only (expires == -1)"
    else:
        reason = "'Stay signed in' token expires within 24h"

    warning_html = (
        "\u26a0\ufe0f <b>Heads up \u2014 this session looks NON-PERSISTENT.</b>\n\n"
        f"Reason: <i>{html.escape(reason, quote=False)}</i>\n\n"
        "This login has <b>no long-term \"Stay signed in\" token</b>, so Microsoft "
        "will most likely drop it <b>within ~24 hours</b> and the ID will show as expired.\n\n"
        "\u2705 <b>To fix it:</b> re-export the session and make sure you <b>TICK "
        "\"Stay signed in\" (Keep me signed in)</b> on the Microsoft sign-in page "
        "<i>before</i> saving <code>m365_session.json</code>, then upload it again.\n\n"
        "The ID has still been saved for now \u2014 but expect it to expire soon unless re-uploaded."
    )
    return {"persistent": False, "reason": reason, "warning_html": warning_html}


# ------------------------------------------------- multi-session rotation

_ROTATION_NOTE = None


def pop_rotation_note():
    """Return and clear the last session-rotation alert (HTML), if any."""
    global _ROTATION_NOTE
    note = _ROTATION_NOTE
    _ROTATION_NOTE = None
    return note


def _load_active_session(skip_ids=None, warranty=None):
    """Return (session_id, label, state_json) for the first usable Microsoft ID
    of the requested warranty group ('6m'/'1y'; None/'' = any active ID).
    (active status + real uploaded cookies), skipping any ids in `skip_ids`.

    `skip_ids` powers the order safety-net: when one ID turns out to be expired
    mid-purchase, the caller adds it to the skip set and asks for the next ID.
    Falls back to (and migrates) the legacy single-session settings key.
    Returns None when no usable session is left.
    """
    skip_ids = set(skip_ids or ())
    try:
        from database.db import (
            list_active_m365_sessions,
            get_m365_session,
            list_m365_sessions,
            add_m365_session,
        )
        for sid, label, _used in list_active_m365_sessions(warranty):
            if sid in skip_ids:
                continue
            row = get_m365_session(sid)
            if not row:
                continue
            _sid, _label, state, _u = row
            if state and not str(state).startswith("[encrypted:"):
                return _sid, str(_label or f"ID #{_sid}"), state
        # No usable rows in the sessions table -> migrate a legacy upload.
        if not list_m365_sessions() and not warranty:
            legacy = load_session()
            if legacy:
                new_sid = add_m365_session("ID #1", legacy)
                try:
                    set_setting(SESSION_KEY, "")
                except Exception:
                    pass
                _log("migrated legacy session into sessions table as ID #1")
                if new_sid not in skip_ids:
                    return new_sid, "ID #1", legacy
        return None
    except Exception:
        legacy = load_session()
        if legacy:
            return None, "legacy session", legacy
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
# Order safety-net breadcrumbs: the failover driver reads these after each run
# to decide whether the session that just ran is dead (skip it, try the next).
_LAST_SESSION_ID = None
_LAST_SESSION_EXPIRED = False

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
    "--start-maximized",
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
    "--disable-infobars",
]

# Injected into every page/frame BEFORE any site script runs. Removes the
# obvious "this is an automated browser" tells that Microsoft's login checks
# for (navigator.webdriver, missing plugins, headless language list, etc.).
_STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]}); } catch (e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch (e) {}
  try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch (e) {}
  try { window.chrome = window.chrome || { runtime: {} }; } catch (e) {}
  try {
    const orig = navigator.permissions && navigator.permissions.query;
    if (orig) {
      navigator.permissions.query = (p) => (
        p && p.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : orig(p)
      );
    }
  } catch (e) {}
  try {
    const gp = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (param) {
      if (param === 37445) return 'Intel Inc.';
      if (param === 37446) return 'Intel Iris OpenGL Engine';
      return gp.call(this, param);
    };
  } catch (e) {}
})();
"""

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

def _confirmation_screenshot(page):
    """Screenshot of JUST the 'Your invite has been sent' confirmation dialog,
    NOT the whole page — so the subscription/expiry details behind it are never
    shown to the customer.
    """
    try:
        # Prefer the modal dialog container if Microsoft rendered one.
        for sel in ('div[role="dialog"]', '[role="dialog"]', 'div[role="alertdialog"]'):
            try:
                dlg = page.locator(sel).first
                if dlg.is_visible(timeout=2000):
                    shot = dlg.screenshot(timeout=5000)
                    if shot:
                        return shot
            except Exception:
                continue
        # Fallback: locate the confirmation text and climb to its box.
        el = page.get_by_text("invite has been sent", exact=False).first
        if not el.is_visible(timeout=3000):
            return None
        for xp in (
            "xpath=ancestor::*[@role='dialog'][1]",
            "xpath=ancestor::*[.//button][1]",
            "xpath=..",
        ):
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


def send_family_invite_sync(email: str, forced_session=None, warranty=None):
    """Send a Microsoft 365 Family invite to `email`.

    Returns (ok: bool, message: str, screenshot: bytes | None).
    Runs blocking Playwright code — call via send_family_invite() from async code.

    `forced_session` lets the failover driver run a specific (session_id, label,
    state_json) tuple instead of auto-picking the first active ID.
    """
    global _ROTATION_NOTE, _LAST_SESSION_ID, _LAST_SESSION_EXPIRED
    _ROTATION_NOTE = None
    _LAST_SESSION_EXPIRED = False
    _session = forced_session or _load_active_session(warranty=warranty)
    if not _session:
        try:
            from database.db import list_m365_sessions
            _all_full = bool(list_m365_sessions())
        except Exception:
            _all_full = False
        if _all_full:
            return False, "ALL Microsoft IDs are FULL or EXPIRED — upload a new session or reset one: Admin Panel → 📩 M365 Auto-Invite → 📂 Sessions.", None
        return False, "No Microsoft session uploaded. Admin Panel → 📩 M365 Auto-Invite → 🔑 Upload Session.", None
    session_id, session_label, state_json = _session
    _LAST_SESSION_ID = session_id

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "Playwright is not installed on the server (add `playwright` to requirements + install chromium).", None

    # Load the uploaded session cookies into a throwaway storage-state file.
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
                    # nixpacks.toml (phases.setup.aptPkgs) and the browser binary
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
                screen={"width": 1920, "height": 1080},
                device_scale_factor=1,
                is_mobile=False,
                has_touch=False,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Chromium";v="149", "Not.A/Brand";v="24", "Google Chrome";v="149"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
            )
            # Apply the stealth patches to every page/frame before site JS runs.
            try:
                context.add_init_script(_STEALTH_JS)
            except Exception as _sj_exc:
                _log(f"stealth init_script failed (non-fatal): {repr(_sj_exc)[:120]}")
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
            login_dead = False
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
                    verdict = _login_interstitial_verdict(page)
                    _log(f"login interstitial verdict: {verdict}")
                    if verdict == "dead":
                        login_dead = True
                        break
                    if verdict == "unknown":
                        break
                    continue  # alive: bounced back to the account page - re-probe
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

            # Login page shown -> the uploaded session cookies have expired.
            # Flag it so the failover driver skips this ID and tries the next,
            # and so the keep-alive job can alert the owner early.
            if login_dead or (
                ("login.live.com" in cur_url or "login.microsoftonline" in cur_url)
                and _login_interstitial_verdict(page) == "dead"
            ):
                _LAST_SESSION_EXPIRED = True
                _log(f"session '{session_label}' expired (login screen shown)")
                browser.close()
                return False, (
                    f"Microsoft session '" + str(session_label) + "' has EXPIRED (it now shows a login screen). "
                    "Upload a fresh session: Admin → 📩 M365 Auto-Invite → 🔑 Upload Session."
                ), screenshot

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

            if ok:
                # Customer proof: JUST the 'Your invite has been sent' box, so
                # the subscription/expiry info behind it is never exposed.
                cropped = _confirmation_screenshot(page)
                screenshot = cropped or _snap()
            else:
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


def _invite_with_failover_sync(email: str, warranty=None):
    """Order safety-net driver: try each active Microsoft ID in turn.

    If an ID's cookies turn out to be expired, mark it expired (so the owner is
    alerted) and automatically fall through to the next ID. Only when every
    usable ID is exhausted do we give up.

    Returns (ok, message, screenshot, queueable) where `queueable` is True when
    the failure was 'no usable session right now' — meaning the order should be
    parked in the queue and auto-delivered once a fresh session is available
    (rather than shown to the owner as a hard error needing a manual Retry).
    """
    from database.db import mark_m365_session_expired
    tried = set()
    last_msg = None
    last_shot = None
    for _ in range(8):
        sess = _load_active_session(skip_ids=tried, warranty=warranty)
        if not sess:
            break
        sid = sess[0]
        ok, msg, shot = send_family_invite_sync(email, forced_session=sess)
        last_msg, last_shot = msg, shot
        if ok:
            return True, msg, shot, False
        if _LAST_SESSION_EXPIRED and sid is not None:
            tried.add(sid)
            try:
                mark_m365_session_expired(sid)
            except Exception:
                pass
            _log(f"failover: session #{sid} expired; trying next ID")
            continue
        # Real, non-expiry failure on a live session -> surface it (manual Retry).
        return False, msg, shot, False
    # Every usable ID is exhausted (all expired, or none uploaded) -> queue it.
    if last_msg is None:
        _wl = {"6m": "6-Month warranty", "1y": "1-Year warranty"}.get(str(warranty or ""), "")
        _who = (
            f"No usable Microsoft session in the {_wl} group right now. "
            if _wl else "No usable Microsoft session right now. "
        )
        last_msg = (
            _who
            + "Upload a fresh session: Admin → 📩 M365 Auto-Invite → 🔑 Upload Session."
        )
    return False, last_msg, last_shot, True


def _login_interstitial_verdict(page):
    """When a page lands on login.live.com / login.microsoftonline, decide
    whether the session is really dead or just passing through a silent SSO
    redirect or a "Stay signed in?" / passkey interstitial.

    Microsoft routinely bounces VALID sessions through login.live.com, so a
    login URL alone must never be treated as an expiry.
    Returns 'alive' | 'dead' | 'unknown'.
    """
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        try:
            low = (page.url or "").lower()
        except Exception:
            low = ""
        if low and "login.live.com" not in low and "login.microsoftonline" not in low:
            return "alive"  # silent SSO bounce finished on its own
        # "Stay signed in?" and passkey nags appear on login.* while the
        # session is perfectly alive - click through them and re-check.
        clicked = False
        for sel in (
            "#idSIButton9",
            "#acceptButton",
            "#iShowSkip",
            "#iCancel",
            "button:has-text('Yes')",
            "button:has-text('Skip for now')",
            "button:has-text('Not now')",
        ):
            try:
                el = page.locator(sel).first
                if el.is_visible():
                    el.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass
            continue
        # Only a real credential form means the cookies are genuinely dead.
        for sel in ("input[name='loginfmt']", "input[name='passwd']", "#i0116", "#i0118"):
            try:
                if page.locator(sel).first.is_visible():
                    return "dead"
            except Exception:
                continue
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
    return "unknown"


def _refresh_session_sync(session_id, state_json):
    """Keep-alive: open a session's cookies, visit the account page and either
    re-save refreshed cookies (Microsoft extends a session every time it is
    used) or detect that it has died. Returns 'alive' | 'expired' | 'error'.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return "error"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    tmp.write(state_json)
    tmp.close()
    result = "error"
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, channel="chromium", args=_CHROMIUM_ARGS)
            except Exception as _first_exc:
                try:
                    browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
                except Exception as _launch_exc:
                    # Same self-heal the purchase path already had. Without this
                    # the keepalive job could never recover from a missing browser
                    # binary and just logged "check inconclusive" forever.
                    err_text = str(_launch_exc)
                    if (
                        "Executable doesn't exist" in err_text
                        or "has been closed" in err_text
                        or "Failed to launch" in err_text
                        or "error while loading shared libraries" in err_text
                    ):
                        _log("chromium missing during keepalive - attempting runtime install")
                        _install_chromium(with_deps=False)
                        browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
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
            try:
                context.add_init_script(_STEALTH_JS)
            except Exception:
                pass
            try:
                context.route("**/*", _block_heavy_resources)
            except Exception:
                pass
            page = context.new_page()
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(30000)
            nav_ok = False
            for nav_url in (
                "https://account.microsoft.com/services/microsoft365/details",
                SHARING_URL,
            ):
                try:
                    page.goto(nav_url, wait_until="commit", timeout=25000)
                    nav_ok = True
                    break
                except Exception:
                    continue
            if not nav_ok:
                browser.close()
                return "error"
            try:
                page.wait_for_load_state("domcontentloaded", timeout=12000)
            except Exception:
                pass
            expired = False
            alive = False
            for _ in range(8):
                try:
                    cur_url = page.url
                except Exception:
                    cur_url = ""
                low = (cur_url or "").lower()
                if "login.live.com" in low or "login.microsoftonline" in low:
                    verdict = _login_interstitial_verdict(page)
                    if verdict == "dead":
                        expired = True
                        break
                    if verdict == "alive":
                        continue  # bounced back - re-probe the account page
                    break  # unknown -> inconclusive, never mark expired
                for probe in ("Share", "Start sharing", "Invite", "Manage sharing"):
                    try:
                        if (
                            page.get_by_role("button", name=probe, exact=False).first.is_visible()
                            or page.get_by_role("link", name=probe, exact=False).first.is_visible()
                        ):
                            alive = True
                            break
                    except Exception:
                        continue
                if alive:
                    break
                if low and "login." not in low and "account.microsoft.com" in low:
                    alive = True
                    break
                page.wait_for_timeout(1500)
            if expired:
                result = "expired"
            elif alive:
                try:
                    _persist_session_state(session_id, json.dumps(context.storage_state()))
                except Exception:
                    pass
                result = "alive"
            else:
                result = "error"
            browser.close()
    except Exception as exc:
        _log(f"refresh session #{session_id} error: {repr(exc)[:150]}")
        result = "error"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    return result


def _keepalive_sessions_sync():
    """Refresh every active session; return the labels of any that just died."""
    from database.db import list_active_m365_sessions, get_m365_session, mark_m365_session_expired
    newly_expired = []
    for sid, label, _used in list_active_m365_sessions():
        row = get_m365_session(sid)
        if not row:
            continue
        _s, _l, state, _u = row
        if not state or str(state).startswith("[encrypted:"):
            continue
        status = _refresh_session_sync(sid, state)
        if status == "expired":
            # Never kill a session on a single verdict - a Microsoft login
            # bounce can look like an expiry. Re-check once from scratch and
            # only mark it dead if BOTH checks agree.
            status = _refresh_session_sync(sid, state)
        if status == "expired":
            try:
                mark_m365_session_expired(sid)
            except Exception:
                pass
            newly_expired.append(str(label or f"ID #{sid}"))
            _log(f"keepalive: session #{sid} EXPIRED")
        elif status == "alive":
            _log(f"keepalive: session #{sid} refreshed OK")
        else:
            _log(f"keepalive: session #{sid} check inconclusive")
    return newly_expired


async def send_family_invite_ex(email: str, warranty=None):
    """Async wrapper around the failover driver.

    Returns (ok, message, screenshot, queueable). Runs the blocking Playwright
    work in a worker thread under an overall timeout so the Telegram UI can
    never hang, even if the browser stalls.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_invite_with_failover_sync, email, warranty),
            timeout=300,
        )
    except asyncio.TimeoutError:
        _log(f"HARD TIMEOUT; last phase: {_LAST_PHASE}")
        return (
            False,
            f"Timed out. Last step reached: {_LAST_PHASE}. "
            "(A screenshot of where it stalled is attached if one was captured.)",
            _LAST_SCREENSHOT,
            True,
        )


async def send_family_invite(email: str):
    """Back-compat 3-tuple wrapper (used by the admin Test Invite)."""
    ok, msg, shot, _queueable = await send_family_invite_ex(email)
    return ok, msg, shot


# ---------------------------------------------------------------- orchestration

async def process_invite(bot, invite_id: int):
    """Run one invite end-to-end: automation + customer + admin notifications."""
    from database.db import get_m365_invite, set_m365_invite_status

    invite = get_m365_invite(invite_id)
    if not invite:
        return

    _id, user_id, order_id, email, _status, _error, _created = invite
    safe_email = html.escape(str(email), quote=False)

    # Route the invite through the session group matching the product's warranty.
    try:
        from database.db import get_m365_invite_warranty
        warranty = get_m365_invite_warranty(invite_id)
    except Exception:
        warranty = ""

    try:
        ok, message, screenshot, queueable = await send_family_invite_ex(email, warranty)
    except Exception as exc:
        ok, message, screenshot, queueable = False, f"Unexpected error: {str(exc).strip()[:400]}", None, True

    if ok:
        set_m365_invite_status(invite_id, "sent", None)

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "<b>Microsoft 365 Invite Sent!</b>\n"
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

        # Send the customer a proof screenshot cropped to JUST the
        # 'Your invite has been sent' box (no subscription/expiry details).
        if screenshot:
            try:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=io.BytesIO(screenshot),
                    caption="📸 Proof — your Microsoft 365 invite was sent.",
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

    # ---------------- safety-net: no usable session right now -> QUEUE ----
    # The order is parked and auto-delivered by the keep-alive job as soon as a
    # fresh session is available, so the customer is never handed a dead invite.
    if queueable:
        set_m365_invite_status(invite_id, "queued", str(message)[:500])

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "⏳ <b>Your Microsoft 365 invite is being processed.</b>\n\n"
                    f"📧 It will be sent to <code>{safe_email}</code> shortly.\n\n"
                    "You'll get a confirmation here as soon as it's on its way. "
                    "If you don't receive it within a few hours, contact support from the main menu."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            from database.db import count_queued_m365_invites
            queued_total = count_queued_m365_invites()
        except Exception:
            queued_total = 0

        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🛟 <b>M365 Order Queued (no live session)</b>\n\n"
                    f"📧 <b>Email:</b> <code>{safe_email}</code>\n"
                    f"👤 <b>User:</b> <code>{user_id}</code> | 🛒 <b>Order:</b> <b>#{order_id}</b>\n"
                    f"📦 <b>Orders waiting in queue:</b> <b>{queued_total}</b>\n\n"
                    f"⚠️ <code>{html.escape(str(message)[:200], quote=False)}</code>\n\n"
                    "👉 Upload a fresh session and the queued order(s) auto-deliver within minutes — "
                    "no manual Retry needed."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Upload Session", callback_data="admin_m365_upload")],
                    [InlineKeyboardButton("📩 M365 Menu", callback_data="admin_m365")],
                ]),
            )
        except Exception:
            pass
        return

    # ---------------- hard failure path (live session, real error) --------
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


# ---------------------------------------------------------------- keep-alive job

async def run_m365_maintenance(context):
    """Background job (runs every few hours):

    1) 🔄 Keep-alive — quietly refresh every uploaded session so Microsoft keeps
       extending its life (prevents most expiries).
    2) 🔔 Early warning — DM the owner the instant a session is found dead, so it
       gets re-uploaded before a customer ever hits it.
    3) 🛟 Auto-deliver — flush any queued orders the moment a healthy session
       exists again.
    """
    bot = context.bot

    # 1 + 2: refresh sessions and alert on any that just died.
    try:
        newly_expired = await asyncio.wait_for(
            asyncio.to_thread(_keepalive_sessions_sync),
            timeout=600,
        )
    except Exception as exc:
        _log(f"keepalive job error: {repr(exc)[:150]}")
        newly_expired = []

    for label in newly_expired:
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "🔔 <b>M365 Session Expired</b>\n\n"
                    f"🔴 <b>{html.escape(str(label), quote=False)}</b> is no longer signed in.\n\n"
                    "Upload a fresh session now so invites keep working — any orders that come in "
                    "meanwhile are safely queued and auto-delivered."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Upload Session", callback_data="admin_m365_upload")],
                    [InlineKeyboardButton("📂 Sessions", callback_data="admin_m365_sessions")],
                ]),
            )
        except Exception:
            pass

    # 3: auto-deliver queued orders if a usable session is available.
    try:
        from database.db import has_healthy_m365_session, list_queued_m365_invites
        if has_healthy_m365_session():
            queued = list_queued_m365_invites(25)
            if queued:
                _log(f"queue flush: {len(queued)} queued invite(s) to retry")
            for inv in queued:
                try:
                    # Only retry when a session of the invite's warranty group exists,
                    # so customers don't get repeated 'queued' notices meanwhile.
                    try:
                        from database.db import get_m365_invite_warranty, list_active_m365_sessions
                        _w = get_m365_invite_warranty(inv[0])
                        if _w and not list_active_m365_sessions(_w):
                            continue
                    except Exception:
                        pass
                    await process_invite(bot, inv[0])
                except Exception as pexc:
                    _log(f"queue flush invite #{inv[0]} error: {repr(pexc)[:120]}")
    except Exception as exc:
        _log(f"queue flush error: {repr(exc)[:150]}")
