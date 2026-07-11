"""
One-time Microsoft session capture for M365 Family auto-invite.

Run this on YOUR OWN PC (not the server):

    pip install playwright
    python -m playwright install chromium
    python tools/m365_login.py

A real browser window opens:
  1) Log in to the Microsoft account that owns the M365 Family plan
     -> IMPORTANT: tick "Keep me signed in" / "Stay signed in? Yes"
  2) Wait until the Microsoft 365 sharing page is fully loaded
  3) Come back to this terminal and press ENTER

It saves m365_session.json in the current folder.
Then send that file to the bot as a document:
  Admin Panel -> 📩 M365 Auto-Invite -> 🔑 Upload Session

⚠️ Keep m365_session.json PRIVATE — it grants access to your Microsoft account.
   Never commit it to git.
"""

from playwright.sync_api import sync_playwright

OUTPUT_FILE = "m365_session.json"
SHARING_URL = "https://account.microsoft.com/services/microsoft365/sharing"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        page.goto(SHARING_URL)

        print()
        print("1) Log in to your Microsoft account in the opened browser.")
        print("   -> Tick 'Keep me signed in' / answer 'Yes' to 'Stay signed in?'")
        print("2) Wait until the Microsoft 365 sharing page is fully visible.")
        input("3) Then press ENTER here to save the session... ")

        context.storage_state(path=OUTPUT_FILE)
        browser.close()

    print()
    print(f"✅ Saved {OUTPUT_FILE}")
    print("Now send this file to the bot as a document:")
    print("  Admin Panel -> 📩 M365 Auto-Invite -> 🔑 Upload Session")
    print()
    print("⚠️ Keep this file PRIVATE. Delete it after uploading if you want.")


if __name__ == "__main__":
    main()
