from config import STORE_NAME, OWNER_USERNAME, GROUP_LINK, CHANNEL_LINK


def welcome_text(name):
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


def deposit_text(balance):
    return f"""💰 <b>Add Funds</b>

Current balance: <code>{balance:.2f} USDT</code>

Send the amount you want to deposit.

Minimum: <code>0.1 USDT</code>
Maximum: <code>5000 USDT</code>

Payment method:
USDT BEP20 / BNB Smart Chain

After you enter the amount, the bot will create an automatic payment invoice."""
