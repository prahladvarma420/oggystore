"""
One-time Canva session capture for Canva team auto-invite.

Run this on YOUR OWN PC (not the server):

    pip install playwright
    python tools/canva_login.py

It opens your REAL Chrome (or Edge) browser — not the automated Chromium —
so Canva's Cloudflare protection does not show "Just a moment...".

  1) Log in to the Canva account that owns the team
  2) Wait until the People page (canva.com/settings/people) is fully loaded
  3) Come back to this terminal and press ENTER

It saves canva_session.json in the current folder.
Then send that file to the bot as a document:
  Admin Panel -> 🎨 Canva Auto-Invite -> 🔑 Upload Session

⚠️ Keep canva_session.json PRIVATE — it grants access to your Canva account.
   Never commit it to git.
"""

import os

from playwright.sync_api import sync_playwright

OUTPUT_FILE = "canva_session.json"
PEOPLE_URL = "https://www.canva.com/settings/people"
PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".canva_login_profile")

# Flags that hide the "this browser is automated" fingerprints Cloudflare
# looks for.
_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

_STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def _proxy_from_env():
    """Optional CANVA_PROXY for the login run.

    When the server uses a STICKY proxy, capture the session through the SAME
    proxy IP so Canva sees one consistent address for login + automation:
        CANVA_PROXY=http://user:pass@host:port python tools/canva_login.py
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
        print(f"Using proxy {cfg['server']} for this login run")
        return cfg
    except Exception:
        return {"server": raw}


def _launch(p):
    """Try the user's real Chrome first, then Edge, then bundled Chromium."""
    for channel in ("chrome", "msedge", None):
        try:
            context = p.chromium.launch_persistent_context(
                PROFILE_DIR,
                channel=channel,
                headless=False,
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1366, "height": 900},
                locale="en-US",
                proxy=_proxy_from_env(),
            )
            label = channel or "bundled chromium"
            print(f"✅ Opened browser: {label}")
            return context
        except Exception as exc:
            label = channel or "bundled chromium"
            print(f"⚠️ Could not open {label}: {str(exc)[:120]}")
            if channel is None:
                print()
                print("If the error says the browser executable doesn't exist, run:")
                print("    python -m playwright install chromium")
                raise
    return None


def main():
    with sync_playwright() as p:
        context = _launch(p)
        context.add_init_script(_STEALTH_JS)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(PEOPLE_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        print()
        print("1) Log in to your Canva account in the opened browser.")
        print("   (If you still see a 'Just a moment...' check, wait a few")
        print("    seconds — with your real Chrome it should pass by itself.)")
        print("2) Wait until the People page (settings/people) is fully visible.")
        input("3) Then press ENTER here to save the session... ")

        context.storage_state(path=OUTPUT_FILE)
        context.close()

    print()
    print(f"✅ Saved {OUTPUT_FILE}")
    print("Now send this file to the bot as a document:")
    print("  Admin Panel -> 🎨 Canva Auto-Invite -> 🔑 Upload Session")
    print()
    print("⚠️ Keep this file PRIVATE. Delete it after uploading if you want.")


if __name__ == "__main__":
    main()
