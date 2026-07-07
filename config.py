import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
OWNER_USERNAME = os.getenv("OWNER_USERNAME")
GROUP_LINK = "t.me/oggypremiumchat"
CHANNEL_LINK = "t.me/oggypremiumstore"
USDT_BEP20_ADDRESS = os.getenv("USDT_BEP20_ADDRESS")

STORE_NAME = "OGGY PREMIUM STORE"
DB_PATH = "shopbot.db"

# ================== AUTO PAYMENT SETTINGS: RPC / WEB3 ==================
# No private key needed. Never put private key in this bot.

# Main RPC
RPC_URL = "https://bsc.publicnode.com"

# Backup RPCs
BACKUP_RPC_URLS = [
    "https://bsc-dataseed.binance.org/",
    "https://bsc-dataseed1.bnbchain.org",
    "https://bsc-rpc.publicnode.com",
]

BSC_CHAIN_ID = 56

# USDT BEP20 contract
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
USDT_DECIMALS = 18

# Check payments every 30 seconds
PAYMENT_CHECK_INTERVAL_SECONDS = 30

# Deposit expires in 6 hours
DEPOSIT_EXPIRE_HOURS = 6

# Safer confirmation count
PAYMENT_REQUIRED_CONFIRMATIONS = 5

# Scan less blocks to avoid RPC rate-limit
# 1200 blocks = about 1 hour on BSC
PAYMENT_SCAN_BLOCKS = 1200

# Decimal tolerance
PAYMENT_AMOUNT_TOLERANCE = 0.000001
# ==================================================

# ================== BACKUP SETTINGS ==================
BACKUP_DIR = "backups"

# Keep only latest 20 database backups
MAX_BACKUPS = 20

# Automatic backup every 6 hours
BACKUP_INTERVAL_SECONDS = 6 * 60 * 60
# =====================================================


# ================== STOCK ALERT SETTINGS ==================
# Admin gets alert when product stock is this number or lower.
LOW_STOCK_ALERT_THRESHOLD = 2
# ==========================================================

# Bot display / business timezone
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
