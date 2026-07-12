"""
One-time Canva session capture for Canva team auto-invite.

Run this on YOUR OWN PC (not the server):

    pip install playwright
    python -m playwright install chromium
    python tools/canva_login.py

A real browser window opens:
  1) Log in to the Canva account that owns the team
  2) Wait until the People page (canva.com/settings/people) is fully loaded
  3) Come back to this terminal and press ENTER

It saves canva_session.json in the current folder.
Then send that file to the bot as a document:
  Admin Panel -> 🎨 Canva Auto-Invite -> 🔑 Upload Session

⚠️ Keep canva_session.json PRIVATE — it grants access to your Canva account.
   Never commit it to git.
"""

from playwright.sync_api import sync_playwright

OUTPUT_FILE = "canva_session.json"
PEOPLE_URL = "https://www.canva.com/settings/people"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.new_page()
        page.goto(PEOPLE_URL)

        print()
        print("1) Log in to your Canva account in the opened browser.")
        print("2) Wait until the People page (settings/people) is fully visible.")
        input("3) Then press ENTER here to save the session... ")

        context.storage_state(path=OUTPUT_FILE)
        browser.close()

    print()
    print(f"✅ Saved {OUTPUT_FILE}")
    print("Now send this file to the bot as a document:")
    print("  Admin Panel -> 🎨 Canva Auto-Invite -> 🔑 Upload Session")
    print()
    print("⚠️ Keep this file PRIVATE. Delete it after uploading if you want.")


if __name__ == "__main__":
    main()
