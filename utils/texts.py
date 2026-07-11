from config import STORE_NAME, OWNER_USERNAME, GROUP_LINK, CHANNEL_LINK


def welcome_text(name):
    try:
        from database.db import get_setting
        custom = (get_setting("welcome_message", "") or "").strip()
    except Exception:
        custom = ""
    if custom:
        return (custom.replace("{name}", str(name))
                      .replace("{store}", STORE_NAME)
                      .replace("{owner}", OWNER_USERNAME)
                      .replace("{group}", GROUP_LINK)
                      .replace("{channel}", CHANNEL_LINK))
    return f"""🙏 <b>Jai Shree Ram {name}</b>

Welcome to <b>{STORE_NAME}</b>

Premium digital products with instant delivery.

🛍 <b>Shop</b> — Browse products
💰 <b>Deposit</b> — Add USDT balance
👤 <b>Profile</b> — Balance and orders
🆘 <b>Support</b> — Contact admin

👑 <b>Owner:</b> {OWNER_USERNAME}
👥 <b>Group:</b> {GROUP_LINK}
📢 <b>Channel:</b> {CHANNEL_LINK}

⚡ Fast • Secure • Automated
"""


DEPOSIT_PROMPT_DEFAULT = (
    "💰 <b>Add Funds</b>\n\nCurrent balance: <b>{balance} USDT</b>\n\nSend the amount you want to deposit.\n\nMinimum: <b>{min} USDT</b>\nMaximum: <b>{max} USDT</b>\n\nPayment method:\nUSDT BEP20 / BNB Smart Chain\n\nAfter you enter the amount, the bot will create an automatic payment invoice."
)


def deposit_text(balance):
    values = {"balance": f"{float(balance or 0):.2f}", "min": "0.1", "max": "5000"}
    try:
        from database.db import render_message_template
        return render_message_template("deposit_prompt", DEPOSIT_PROMPT_DEFAULT, **values)
    except Exception:
        return DEPOSIT_PROMPT_DEFAULT.format(**values)
