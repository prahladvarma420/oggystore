from datetime import datetime, timedelta
import re as _re
import sqlite3
from config import DB_PATH, ADMIN_ID
import os
from utils.crypto import encrypt_text, decrypt_text
from utils.timezone import now_text, now as ist_now

from database.pgcompat import connect as _compat_connect  # SQLite/Postgres switch
conn = _compat_connect(DB_PATH)
cur = conn.cursor()


def _safe_add_column(table: str, column: str, definition: str):
    # Do not fail fresh installs if a migration is listed before the table is created.
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if cur.fetchone() is None:
        return
    cur.execute(f"PRAGMA table_info({table})")
    existing = [row[1] for row in cur.fetchall()]
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        username TEXT DEFAULT 'No username',
        balance REAL DEFAULT 0,
        binance_pay TEXT DEFAULT 'not set',
        notifications INTEGER DEFAULT 1,
        blocked INTEGER DEFAULT 0,
        joined TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        stock INTEGER NOT NULL,
        delivery TEXT NOT NULL,
        active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_stock(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        delivery TEXT NOT NULL,
        sold INTEGER DEFAULT 0,
        sold_to INTEGER,
        sold_at TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_name TEXT,
        price REAL,
        delivery TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS deposits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        unique_amount REAL,
        status TEXT DEFAULT 'pending',
        tx_hash TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        paid_at TEXT
    )
    """)
    try:
        cur.execute("ALTER TABLE deposits ADD COLUMN method TEXT")
    except Exception:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS processed_txs(
        tx_hash TEXT PRIMARY KEY,
        deposit_id INTEGER,
        user_id INTEGER,
        amount REAL,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT,
        details TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins(
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        subject TEXT,
        message TEXT,
        status TEXT DEFAULT 'open',
        admin_reply TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER UNIQUE,
        earned REAL DEFAULT 0,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawal_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        wallet TEXT,
        status TEXT DEFAULT 'pending',
        admin_note TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    _safe_add_column("users", "referral_earnings", "REAL DEFAULT 0")
    _safe_add_column("users", "referral_pending", "REAL DEFAULT 0")
    _safe_add_column("products", "featured", "INTEGER DEFAULT 0")
    _safe_add_column("products", "image_file_id", "TEXT")
    _safe_add_column("users", "username", "TEXT DEFAULT 'No username'")
    _safe_add_column("users", "blocked", "INTEGER DEFAULT 0")
    _safe_add_column("users", "loyalty_points", "REAL DEFAULT 0")
    _safe_add_column("users", "vip_rank", "TEXT DEFAULT 'Bronze'")
    _safe_add_column("users", "last_marketing_sent", "TEXT")
    _safe_add_column("deposits", "tx_hash", "TEXT")
    _safe_add_column("deposits", "paid_at", "TEXT")
    _safe_add_column("deposits", "checkout_product_id", "INTEGER")
    _safe_add_column("deposits", "checkout_quantity", "INTEGER DEFAULT 0")
    _safe_add_column("deposits", "checkout_done", "INTEGER DEFAULT 0")
    _safe_add_column("products", "description", "TEXT DEFAULT ''")
    _safe_add_column("products", "validity", "TEXT DEFAULT 'Not mentioned'")
    _safe_add_column("products", "warranty", "TEXT DEFAULT 'No warranty'")
    _safe_add_column("products", "rules", "TEXT DEFAULT 'No rules mentioned'")
    _safe_add_column("products", "redeem_link", "TEXT DEFAULT 'Not required'")
    # Repair rows saved by an old Add Product bug that stored NULL redeem links.
    try:
        cur.execute("UPDATE products SET redeem_link='Not required' WHERE redeem_link IS NULL OR redeem_link=''")
        conn.commit()
    except Exception:
        pass
    _safe_add_column("products", "max_per_order", "INTEGER DEFAULT 1")
    _safe_add_column("products", "flash_price", "REAL")
    _safe_add_column("products", "flash_ends_at", "TEXT")
    _safe_add_column("products", "low_stock_notified", "INTEGER DEFAULT 0")
    # Always keep main owner as admin
    cur.execute("INSERT OR IGNORE INTO admins(user_id, added_by) VALUES(?, ?)", (ADMIN_ID, ADMIN_ID))
    # Encrypt any product codes / delivered codes still stored in plaintext.
    encrypt_existing_secrets()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_price_tiers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(product_id, quantity)
    )
    """)
    try:
        cur.execute("ALTER TABLE product_price_tiers ADD COLUMN updated TEXT")
    except Exception:
        pass

    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupons(
        code TEXT PRIMARY KEY,
        discount_type TEXT NOT NULL DEFAULT 'percent',
        value REAL NOT NULL DEFAULT 0,
        active INTEGER DEFAULT 1,
        max_uses INTEGER DEFAULT 0,
        used_count INTEGER DEFAULT 0,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    _safe_add_column("coupons", "expires_at", "TEXT")
    _safe_add_column("coupons", "reseller", "INTEGER DEFAULT 0")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_coupons(
        user_id INTEGER PRIMARY KEY,
        code TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS restock_notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        notified INTEGER DEFAULT 0,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, product_id)
    )
    """)

    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS message_templates(
        key TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_broadcasts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        run_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        sent INTEGER DEFAULT 0,
        failed INTEGER DEFAULT 0,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bundles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL,
        active INTEGER DEFAULT 1,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bundle_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bundle_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER DEFAULT 1
    )
    """)

    # One-time cleanup for existing installs: in the delivery message only
    # the codes should be copyable. Order id / paid / balance become bold.
    try:
        cur.execute(
            "UPDATE message_templates SET body = REPLACE(REPLACE(REPLACE(body, "
            "'<b>#{order_id}</b>', '<b>#{order_id}</b>'), "
            "'<b>${paid}</b>', '<b>${paid}</b>'), "
            "'<b>${balance}</b>', '<b>${balance}</b>') "
            "WHERE key='delivery_success'"
        )
        conn.commit()
    except Exception:
        pass

    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('referral_percent', '5')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('low_stock_alert_threshold', '3')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('low_stock_alert_levels', '5,1')")
    # Migration: drop the old level-10 alert (only 5 and 1 remain)
    cur.execute("UPDATE settings SET value='5,1' WHERE key='low_stock_alert_levels' AND value='10,5,1'")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('min_withdrawal', '1')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('loyalty_percent', '2')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('force_join_enabled', '1')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('force_join_channels', '@oggypremiumstore,@oggypremiumchat')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('first_purchase_discount_percent', '0')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('buy_x_qty', '0')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('buy_x_free', '0')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auto_welcome_coupon', '')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('inactive_days', '7')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('inactive_coupon', '')")

    # One-time upgrade: old deposit_success default -> full detailed version
    try:
        cur.execute(
            "UPDATE message_templates SET body=? WHERE key='deposit_success' AND body IN (?,?,?,?)",
            ('✅ <b>DEPOSIT CONFIRMED</b>\n\n🧾 <b>Invoice:</b> <code>#{invoice_id}</code>\n💵 <b>Paid:</b> <code>{paid} USDT</code>\n💰 <b>Balance Added:</b> <code>{amount} USDT</code>\n💼 <b>New Balance:</b> <code>{balance} USDT</code>\n🔗 <b>TX:</b> {tx}\n\n🛍 <i>You can now buy products from Shop.</i>',
             '✅ <b>Deposit Confirmed</b>\n\n💵 Amount: <code>{amount} USDT</code>\n\nYour wallet balance has been updated.',
             '✅ <b>Deposit Confirmed</b>\n\n💵 Amount: <code>{amount} USDT</code>\n\nYour wallet balance has been updated.',
             '✅ Deposit Confirmed\n\n💵 Amount: {amount} USDT\n\nYour wallet balance has been updated.',
             '✅ Deposit Confirmed\n\n💵 Amount: {amount} USDT\n\nYour wallet balance has been updated.'),
        )
        conn.commit()
    except Exception:
        pass

    default_templates = {
        "welcome": (
            "Welcome Message",
            "👋 <b>Welcome to {store_name}</b>\n\nChoose an option below to continue."
        ),
        "delivery_success": (
            "Delivery Success Message",
            "✅ <b>Payment Successful!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🧾 <b>Order ID:</b> <b>#{order_id}</b>\n\n📦 <b>{product_name}</b>\n\n🔑 <b>Code(s):</b>\n<code>{delivery}</code>\n\n━━━━━━━━━━━━━━━━━━━━\n\n💰 <b>Paid:</b> <b>${paid}</b>\n💼 <b>Remaining Balance:</b> <b>${balance}</b>\n\n━━━━━━━━━━━━━━━━━━━━\nThank you for your purchase! 🎉\n\nUse /orders to view your order history."
        ),
        "coupon_broadcast": (
            "Coupon Broadcast Message",
            "🎟 <b>NEW COUPON AVAILABLE!</b>\n\n💝 <b>Discount:</b> <code>{discount}</code>\n🎫 <b>Coupon Code:</b> <code>{code}</code>\n⏳ <b>Validity:</b> <code>{validity}</code>\n🔥 <b>Limit:</b> <code>{limit}</code>\n\nApply this coupon before checkout and save on your next purchase 👇"
        ),
        "flash_sale_broadcast": (
            "Flash Sale Broadcast Message",
            "🧨 <b>FLASH SALE — LIMITED TIME!</b>\n\n📦 <b>{product_name}</b>\n\n💵 <b>Was:</b> <s>${old_price}</s>\n⚡ <b>Now:</b> <code>${new_price} USDT</code> <b>({discount_percent}% OFF)</b>\n\n🏃 <b>Hurry — {validity}</b>\nGrab it before the timer runs out 👇"
        ),
        "restock_alert": (
            "Restock Alert Message",
            "🔔 <b>Back In Stock!</b>\n\n📦 <b>{product_name}</b>\n📊 Available: <code>{stock}</code>\n\nTap below to buy now 👇"
        ),
        "new_product_broadcast": (
            "New Product Broadcast Message",
            "🆕 <b>New Product Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ New product is now available!</i>"
        ),
        "stock_added_broadcast": (
            "Stock Added Broadcast Message",
            "🆕 <b>New Stock Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ Grab yours before they’re gone!</i>"
        ),
        "deposit_prompt": (
            "Deposit Menu Message",
            "💰 <b>Add Funds</b>\n\nCurrent balance: <b>{balance} USDT</b>\n\nSend the amount you want to deposit.\n\nMinimum: <b>{min} USDT</b>\nMaximum: <b>{max} USDT</b>\n\nPayment method:\nUSDT BEP20 / BNB Smart Chain\n\nAfter you enter the amount, the bot will create an automatic payment invoice."
        ),
        "profile_message": (
            "My Profile Message",
            "👤 <b>My Profile</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💼 <b>Wallet Balance:</b> <b>{balance} USDT</b>\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💰 <b>Total Spent:</b> <b>${spent}</b>\n\n💳 <b>Paid Deposits:</b> <b>{deposits}</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n\n🎁 <b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n\n🎟 <b>Coupon:</b> {coupon}\n🔔 <b>Notifications:</b> {notifications}"
        ),
        "wallet_message": (
            "My Wallet Message",
            "💼 <b>My Wallet</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💰 <b>Available Balance:</b> <b>{balance} USDT</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n🧾 <b>Paid Deposits:</b> <b>{deposits}</b>\n\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💸 <b>Total Spent:</b> <b>{spent} USDT</b>\n\n��� <b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n⭐ <b>Loyalty Points:</b> <b>{loyalty_points}</b>\n🏆 <b>VIP Rank:</b> <b>{vip_rank}</b>\n\nUse Deposit to add balance, then pay directly from wallet during checkout."
        ),
        "support_prompt": (
            "Support Ticket Message",
            "��� <b>Create Support Ticket</b>\n\nSend your issue/message now.\n\nExample:\nPayment done but balance not added."
        ),
        "deposit_success": (
            "Deposit Success Message",
            "✅ <b>DEPOSIT CONFIRMED</b>\n\n🧾 <b>Invoice:</b> <code>#{invoice_id}</code>\n💵 <b>Paid:</b> <code>{paid} USDT</code>\n💰 <b>Balance Added:</b> <code>{amount} USDT</code>\n💼 <b>New Balance:</b> <code>{balance} USDT</code>\n🔗 <b>TX:</b> {tx}\n\n🛍 <i>You can now buy products from Shop.</i>"
        ),
        "new_offer_broadcast": (
            "New Offer Alert Broadcast",
            "🔥 <b>NEW OFFER ALERT!</b>\n\n📦 <b>{product}</b>\n💰 <b>Price:</b> <code>{price}</code> / code\n✅ <b>Available:</b> <code>{available}</code>\n\n🎁 <b>BULK DISCOUNT ACTIVE</b>\n{offers}\n\n⚡ <i>Grab this deal before it’s gone!</i>"
        ),
        "insufficient_balance": (
            "Insufficient Balance (Checkout)",
            "❌ <b>Insufficient Balance</b>\n\n📦 <b>Product:</b> {product}\n🔢 <b>Quantity:</b> {quantity}\n💰 <b>Total:</b> {total} USDT\n👛 <b>Your Balance:</b> {balance} USDT\n➕ <b>Need More:</b> {need} USDT\n\nDeposit exactly what's missing with one tap below."
        )
    }

    for key, (title, body) in default_templates.items():
        cur.execute(
            "INSERT OR IGNORE INTO message_templates(key, title, body) VALUES(?,?,?)",
            (key, title, body)
        )


    conn.commit()


def ensure_user(user):
    username = user.username if getattr(user, "username", None) else "No username"
    cur.execute(
        "INSERT OR IGNORE INTO users(user_id, first_name, username) VALUES(?, ?, ?)",
        (user.id, user.first_name or "User", username),
    )
    cur.execute(
        "UPDATE users SET first_name=?, username=? WHERE user_id=?",
        (user.first_name or "User", username, user.id),
    )

    default_templates = {
        "welcome": (
            "Welcome Message",
            "👋 <b>Welcome to {store_name}</b>\n\nChoose an option below to continue."
        ),
        "delivery_success": (
            "Delivery Success Message",
            "✅ <b>Payment Successful!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🧾 <b>Order ID:</b> <b>#{order_id}</b>\n\n📦 <b>{product_name}</b>\n\n🔑 <b>Code(s):</b>\n<code>{delivery}</code>\n\n━━━━━━━━━━━━━━━━━━━━\n\n💰 <b>Paid:</b> <b>${paid}</b>\n💼 <b>Remaining Balance:</b> <b>${balance}</b>\n\n━━━━━━━━━━━━━━━━━━━━\nThank you for your purchase! 🎉\n\nUse /orders to view your order history."
        ),
        "coupon_broadcast": (
            "Coupon Broadcast Message",
            "🎟 <b>NEW COUPON AVAILABLE!</b>\n\n💝 <b>Discount:</b> <code>{discount}</code>\n🎫 <b>Coupon Code:</b> <code>{code}</code>\n⏳ <b>Validity:</b> <code>{validity}</code>\n🔥 <b>Limit:</b> <code>{limit}</code>\n\nApply this coupon before checkout and save on your next purchase 👇"
        ),
        "flash_sale_broadcast": (
            "Flash Sale Broadcast Message",
            "🧨 <b>FLASH SALE — LIMITED TIME!</b>\n\n📦 <b>{product_name}</b>\n\n💵 <b>Was:</b> <s>${old_price}</s>\n⚡ <b>Now:</b> <code>${new_price} USDT</code> <b>({discount_percent}% OFF)</b>\n\n🏃 <b>Hurry — {validity}</b>\nGrab it before the timer runs out 👇"
        ),
        "restock_alert": (
            "Restock Alert Message",
            "🔔 <b>Back In Stock!</b>\n\n📦 <b>{product_name}</b>\n📊 Available: <code>{stock}</code>\n\nTap below to buy now 👇"
        ),
        "new_product_broadcast": (
            "New Product Broadcast Message",
            "🆕 <b>New Product Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ New product is now available!</i>"
        ),
        "stock_added_broadcast": (
            "Stock Added Broadcast Message",
            "🆕 <b>New Stock Added!</b>\n\n📦 <b>{product}</b>\n➕ <b>Added:</b> <code>{added}</code>\n✅ <b>Available:</b> <code>{available}</code>\n💰 <b>Price:</b> <code>{price}</code>\n\n<i>⚡ Grab yours before they’re gone!</i>"
        ),
        "deposit_prompt": (
            "Deposit Menu Message",
            "💰 <b>Add Funds</b>\n\nCurrent balance: <b>{balance} USDT</b>\n\nSend the amount you want to deposit.\n\nMinimum: <b>{min} USDT</b>\nMaximum: <b>{max} USDT</b>\n\nPayment method:\nUSDT BEP20 / BNB Smart Chain\n\nAfter you enter the amount, the bot will create an automatic payment invoice."
        ),
        "profile_message": (
            "My Profile Message",
            "👤 <b>My Profile</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💼 <b>Wallet Balance:</b> <b>{balance} USDT</b>\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💰 <b>Total Spent:</b> <b>${spent}</b>\n\n💳 <b>Paid Deposits:</b> <b>{deposits}</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n\n🎁 <b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n\n🎟 <b>Coupon:</b> {coupon}\n🔔 <b>Notifications:</b> {notifications}"
        ),
        "wallet_message": (
            "My Wallet Message",
            "💼 <b>My Wallet</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💰 <b>Available Balance:</b> <b>{balance} USDT</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n🧾 <b>Paid Deposits:</b> <b>{deposits}</b>\n\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💸 <b>Total Spent:</b> <b>{spent} USDT</b>\n\n🎁 <b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n⭐ <b>Loyalty Points:</b> <b>{loyalty_points}</b>\n🏆 <b>VIP Rank:</b> <b>{vip_rank}</b>\n\nUse Deposit to add balance, then pay directly from wallet during checkout."
        ),
        "support_prompt": (
            "Support Ticket Message",
            "🎫 <b>Create Support Ticket</b>\n\nSend your issue/message now.\n\nExample:\nPayment done but balance not added."
        ),
        "deposit_success": (
            "Deposit Success Message",
            "✅ <b>DEPOSIT CONFIRMED</b>\n\n🧾 <b>Invoice:</b> <code>#{invoice_id}</code>\n💵 <b>Paid:</b> <code>{paid} USDT</code>\n💰 <b>Balance Added:</b> <code>{amount} USDT</code>\n💼 <b>New Balance:</b> <code>{balance} USDT</code>\n🔗 <b>TX:</b> {tx}\n\n🛍 <i>You can now buy products from Shop.</i>"
        ),
        "new_offer_broadcast": (
            "New Offer Alert Broadcast",
            "🔥 <b>NEW OFFER ALERT!</b>\n\n📦 <b>{product}</b>\n💰 <b>Price:</b> <code>{price}</code> / code\n✅ <b>Available:</b> <code>{available}</code>\n\n🎁 <b>BULK DISCOUNT ACTIVE</b>\n{offers}\n\n⚡ <i>Grab this deal before it’s gone!</i>"
        ),
        "insufficient_balance": (
            "Insufficient Balance (Checkout)",
            "❌ <b>Insufficient Balance</b>\n\n📦 <b>Product:</b> {product}\n🔢 <b>Quantity:</b> {quantity}\n💰 <b>Total:</b> {total} USDT\n👛 <b>Your Balance:</b> {balance} USDT\n➕ <b>Need More:</b> {need} USDT\n\nDeposit exactly what's missing with one tap below."
        )
    }

    for key, (title, body) in default_templates.items():
        cur.execute(
            "INSERT OR IGNORE INTO message_templates(key, title, body) VALUES(?,?,?)",
            (key, title, body)
        )


    conn.commit()


def ensure_user_id(user_id: int):
    cur.execute("INSERT OR IGNORE INTO users(user_id, first_name) VALUES(?, ?)", (user_id, "User"))
    conn.commit()


def get_balance(user_id: int) -> float:
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else 0.0


def add_balance(user_id: int, amount: float):
    ensure_user_id(user_id)
    cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
    conn.commit()


def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True

    cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
    return cur.fetchone() is not None


def add_admin(user_id: int, added_by: int):
    ensure_user_id(user_id)
    cur.execute(
        "INSERT OR IGNORE INTO admins(user_id, added_by) VALUES(?, ?)",
        (user_id, added_by)
    )
    conn.commit()


def remove_admin(user_id: int):
    if user_id == ADMIN_ID:
        return False

    cur.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    return True


def get_admins():
    cur.execute("""
        SELECT a.user_id, u.first_name, u.username, a.added_by, a.added_at
        FROM admins a
        LEFT JOIN users u ON a.user_id = u.user_id
        ORDER BY a.added_at ASC
    """)
    return cur.fetchall()


def encrypt_existing_secrets():
    """One-time, idempotent migration: encrypt any product codes / delivered
    codes still stored in plaintext. Safe to run on every startup -- rows
    already encrypted (or when no key is set) are left untouched."""
    if not os.getenv("DATA_ENCRYPTION_KEY"):
        return
    targets = [
        ("product_stock", "delivery"),
        ("orders", "delivery"),
        ("withdrawal_requests", "wallet"),
        ("tickets", "subject"),
        ("tickets", "message"),
        ("tickets", "admin_reply"),
    ]
    try:
        for table, col in targets:
            try:
                rows = cur.execute(
                    'SELECT id, "%s" FROM %s WHERE "%s" IS NOT NULL '
                    'AND "%s" NOT LIKE \'enc:%%\'' % (col, table, col, col)
                ).fetchall()
            except Exception:
                continue
            for rid, val in rows:
                enc = encrypt_text(val)
                if enc != val:
                    cur.execute('UPDATE %s SET "%s"=? WHERE id=?' % (table, col), (enc, rid))
        conn.commit()
    except Exception:
        pass


def add_stock_items(product_id: int, delivery_text: str):
    items = []
    for part in delivery_text.replace(";;", "\n").splitlines():
        item = part.strip()
        if item:
            items.append(item)

    if not items and delivery_text.strip():
        items = [delivery_text.strip()]

    for item in items:
        cur.execute(
            "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)",
            (product_id, encrypt_text(item))
        )

    cur.execute("UPDATE products SET stock=? WHERE id=?", (len(items), product_id))
    # Re-arm low-stock alerts after restock so the next drop alerts again.
    cur.execute("UPDATE products SET low_stock_notified=0 WHERE id=?", (product_id,))
    conn.commit()
    return len(items)


def get_available_stock_count(product_id: int) -> int:
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=0", (product_id,))
    row = cur.fetchone()
    return row[0] if row else 0


def get_display_stock(product_id: int) -> int:
    """Stock number for customer-facing broadcasts/messages.

    Uses the live unsold-codes count; falls back to products.stock for
    products whose stock is tracked manually (no uploaded codes), matching
    the behavior of the buy screen. Prevents 'Available Stock: 0' bugs in
    price/stock/offer broadcasts.
    """
    live = get_available_stock_count(product_id)
    if live > 0:
        return live
    try:
        cur.execute("SELECT stock FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def reserve_stock_item(product_id: int, user_id: int):
    cur.execute(
        "SELECT id, delivery FROM product_stock WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 1",
        (product_id,)
    )
    row = cur.fetchone()

    if row:
        stock_id, delivery = row
        cur.execute(
            "UPDATE product_stock SET sold=1, sold_to=?, sold_at=? WHERE id=?",
            (user_id, now_text(), stock_id)
        )
        return decrypt_text(delivery)

    return None


def get_sold_stock_count(product_id: int) -> int:
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=1", (product_id,))
    row = cur.fetchone()
    return row[0] if row else 0


def get_total_stock_count(product_id: int) -> int:
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=?", (product_id,))
    row = cur.fetchone()
    return row[0] if row else 0


def restock_product(product_id: int, delivery_text: str) -> int:
    added = add_stock_items(product_id, delivery_text)
    available = get_available_stock_count(product_id)
    cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
    conn.commit()
    return added


def is_blocked(user_id: int) -> bool:
    cur.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return bool(row and row[0] == 1)


def block_user(user_id: int):
    ensure_user_id(user_id)
    cur.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
    conn.commit()


def unblock_user(user_id: int):
    ensure_user_id(user_id)
    cur.execute("UPDATE users SET blocked=0 WHERE user_id=?", (user_id,))
    conn.commit()


def log_admin_action(admin_id: int, action: str, details: str = ""):
    cur.execute(
        "INSERT INTO admin_logs(admin_id, action, details) VALUES(?,?,?)",
        (admin_id, action, details)
    )
    conn.commit()


def get_admin_logs(limit: int = 25):
    cur.execute(
        "SELECT id, admin_id, action, details, created FROM admin_logs ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    return cur.fetchall()



def update_product_details(product_id: int, description: str, validity: str, warranty: str, rules: str, redeem_link: str, max_per_order: int):
    cur.execute(
        """
        UPDATE products
        SET description=?, validity=?, warranty=?, rules=?, redeem_link=?, max_per_order=?
        WHERE id=?
        """,
        (description, validity, warranty, rules, redeem_link, max_per_order, product_id)
    )
    conn.commit()


def create_ticket(user_id: int, subject: str, message: str) -> int:
    cur.execute(
        "INSERT INTO tickets(user_id, subject, message, status) VALUES(?,?,?, 'open')",
        (user_id, encrypt_text(subject), encrypt_text(message))
    )
    conn.commit()
    return cur.lastrowid


def get_user_tickets(user_id: int, limit: int = 10):
    cur.execute(
        "SELECT id, subject, message, status, admin_reply, created, updated FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    out = []
    for r in cur.fetchall():
        r = list(r)
        r[1] = decrypt_text(r[1])
        r[2] = decrypt_text(r[2])
        if r[4] is not None:
            r[4] = decrypt_text(r[4])
        out.append(tuple(r))
    return out


def get_open_tickets(limit: int = 20):
    cur.execute("""
        SELECT t.id, t.user_id, u.first_name, u.username, t.subject, t.message, t.status, t.created, t.updated
        FROM tickets t
        LEFT JOIN users u ON t.user_id = u.user_id
        WHERE t.status='open'
        ORDER BY t.id DESC
        LIMIT ?
    """, (limit,))
    out = []
    for r in cur.fetchall():
        r = list(r)
        r[4] = decrypt_text(r[4])
        r[5] = decrypt_text(r[5])
        out.append(tuple(r))
    return out


def get_ticket(ticket_id: int):
    cur.execute("""
        SELECT t.id, t.user_id, u.first_name, u.username, t.subject, t.message, t.status, t.admin_reply, t.created, t.updated
        FROM tickets t
        LEFT JOIN users u ON t.user_id = u.user_id
        WHERE t.id=?
    """, (ticket_id,))
    row = cur.fetchone()
    if row is None:
        return None
    row = list(row)
    row[4] = decrypt_text(row[4])
    row[5] = decrypt_text(row[5])
    if row[7] is not None:
        row[7] = decrypt_text(row[7])
    return tuple(row)


def reply_ticket(ticket_id: int, admin_reply: str):
    cur.execute(
        "UPDATE tickets SET admin_reply=?, status='answered', updated=? WHERE id=?",
        (encrypt_text(admin_reply), now_text(), ticket_id)
    )
    conn.commit()


def close_ticket(ticket_id: int):
    cur.execute(
        "UPDATE tickets SET status='closed', updated=? WHERE id=?",
        (now_text(), ticket_id)
    )
    conn.commit()



def reserve_stock_items(product_id: int, user_id: int, quantity: int):
    cur.execute(
        "SELECT id, delivery FROM product_stock WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT ?",
        (product_id, quantity)
    )
    rows = cur.fetchall()

    if len(rows) < quantity:
        return None

    deliveries = []
    for stock_id, delivery in rows:
        cur.execute(
            "UPDATE product_stock SET sold=1, sold_to=?, sold_at=? WHERE id=?",
            (user_id, now_text(), stock_id)
        )
        deliveries.append(decrypt_text(delivery))

    return deliveries


# ---------- Bundle deals ----------

def create_bundle(name, price, items):
    """items: list of (product_id, quantity). Returns new bundle id."""
    cur.execute(
        "INSERT INTO bundles(name, price, active, created) VALUES(?,?,1,?)",
        (name, float(price), now_text())
    )
    conn.commit()
    cur.execute("SELECT last_insert_rowid()")
    bundle_id = cur.fetchone()[0]

    for product_id, quantity in items:
        cur.execute(
            "INSERT INTO bundle_items(bundle_id, product_id, quantity) VALUES(?,?,?)",
            (bundle_id, int(product_id), int(quantity))
        )
    conn.commit()
    return bundle_id


def get_active_bundles():
    cur.execute("SELECT id, name, price FROM bundles WHERE active=1 ORDER BY id DESC")
    return cur.fetchall()


def get_all_bundles():
    cur.execute("SELECT id, name, price, active FROM bundles ORDER BY id DESC")
    return cur.fetchall()


def get_bundle(bundle_id: int):
    cur.execute("SELECT id, name, price, active FROM bundles WHERE id=?", (bundle_id,))
    return cur.fetchone()


def get_bundle_items(bundle_id: int):
    cur.execute(
        """
        SELECT bi.product_id, bi.quantity, p.name, p.price
        FROM bundle_items bi
        JOIN products p ON p.id = bi.product_id
        WHERE bi.bundle_id=?
        ORDER BY bi.id ASC
        """,
        (bundle_id,)
    )
    return cur.fetchall()


def delete_bundle(bundle_id: int):
    cur.execute("DELETE FROM bundle_items WHERE bundle_id=?", (bundle_id,))
    cur.execute("DELETE FROM bundles WHERE id=?", (bundle_id,))
    conn.commit()


# ---------- Referral / Withdrawal / Featured helpers ----------

def get_setting(key, default=None):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_setting(key, value):
    cur.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(value)))
    conn.commit()


def add_referral(referrer_id: int, referred_id: int):
    if not referrer_id or not referred_id or int(referrer_id) == int(referred_id):
        return False
    cur.execute("SELECT referred_id FROM referrals WHERE referred_id=?", (referred_id,))
    if cur.fetchone():
        return False
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,))
    if not cur.fetchone():
        return False
    cur.execute("INSERT OR IGNORE INTO referrals(referrer_id, referred_id) VALUES(?,?)", (referrer_id, referred_id))
    conn.commit()
    return cur.rowcount > 0


def add_referral_earning(buyer_id: int, order_amount: float):
    cur.execute("SELECT referrer_id FROM referrals WHERE referred_id=?", (buyer_id,))
    row = cur.fetchone()
    if not row:
        return 0.0, None
    referrer_id = row[0]
    percent = float(get_setting("referral_percent", "5") or 5)
    earning = round(float(order_amount) * percent / 100, 6)
    if earning <= 0:
        return 0.0, referrer_id
    cur.execute("UPDATE referrals SET earned=earned+? WHERE referred_id=?", (earning, buyer_id))
    cur.execute("UPDATE users SET referral_earnings=COALESCE(referral_earnings,0)+?, referral_pending=COALESCE(referral_pending,0)+? WHERE user_id=?", (earning, earning, referrer_id))
    conn.commit()
    return earning, referrer_id


def get_referral_stats(user_id: int):
    cur.execute("SELECT COUNT(*), COALESCE(SUM(earned),0) FROM referrals WHERE referrer_id=?", (user_id,))
    count, earned = cur.fetchone()
    cur.execute("SELECT COALESCE(referral_pending,0) FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    pending = row[0] if row else 0
    return count or 0, earned or 0, pending or 0


def create_withdrawal_request(user_id: int, amount: float, wallet: str):
    amount = float(amount)
    cur.execute("SELECT COALESCE(referral_pending,0) FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    pending = float(row[0] if row else 0)
    if amount <= 0 or pending < amount:
        return None
    cur.execute("UPDATE users SET referral_pending=referral_pending-? WHERE user_id=?", (amount, user_id))
    cur.execute("INSERT INTO withdrawal_requests(user_id, amount, wallet) VALUES(?,?,?)", (user_id, amount, encrypt_text(wallet)))
    conn.commit()
    return cur.lastrowid


def get_user_withdrawals(user_id: int, limit: int = 10):
    cur.execute("SELECT id, amount, wallet, status, admin_note, created, updated FROM withdrawal_requests WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
    out = []
    for r in cur.fetchall():
        r = list(r)
        if r[2] is not None:
            r[2] = decrypt_text(r[2])
        out.append(tuple(r))
    return out


def get_pending_withdrawals(limit: int = 20):
    cur.execute("""
        SELECT w.id, w.user_id, u.first_name, u.username, w.amount, w.wallet, w.status, w.created
        FROM withdrawal_requests w
        LEFT JOIN users u ON w.user_id=u.user_id
        WHERE w.status='pending'
        ORDER BY w.id ASC
        LIMIT ?
    """, (limit,))
    out = []
    for r in cur.fetchall():
        r = list(r)
        if r[5] is not None:
            r[5] = decrypt_text(r[5])
        out.append(tuple(r))
    return out


def update_withdrawal_status(withdrawal_id: int, status: str, note: str = ""):
    cur.execute("SELECT user_id, amount, status FROM withdrawal_requests WHERE id=?", (withdrawal_id,))
    row = cur.fetchone()
    if not row:
        return None
    user_id, amount, old_status = row
    if old_status != "pending":
        return (user_id, amount, old_status)
    if status == "rejected":
        cur.execute("UPDATE users SET referral_pending=COALESCE(referral_pending,0)+? WHERE user_id=?", (amount, user_id))
    cur.execute("UPDATE withdrawal_requests SET status=?, admin_note=?, updated=? WHERE id=?", (status, note, now_text(), withdrawal_id))
    conn.commit()
    return (user_id, amount, old_status)


def set_product_featured(product_id: int, featured: int):
    cur.execute("UPDATE products SET featured=? WHERE id=?", (int(featured), product_id))
    conn.commit()
    return cur.rowcount > 0


def set_product_image(product_id: int, file_id: str):
    cur.execute("UPDATE products SET image_file_id=? WHERE id=?", (file_id, product_id))
    conn.commit()
    return cur.rowcount > 0


def remove_product_image(product_id: int):
    cur.execute("UPDATE products SET image_file_id=NULL WHERE id=?", (product_id,))
    conn.commit()
    return cur.rowcount > 0


def get_product_price_tiers(product_id: int):
    cur.execute("""
        SELECT id, quantity, price
        FROM product_price_tiers
        WHERE product_id=?
        ORDER BY quantity ASC
    """, (product_id,))
    return cur.fetchall()


def set_product_price_tier(product_id: int, quantity: int, price: float, manual: bool = True):
    """Save one bulk offer. manual=True (hand-typed by an admin) stamps the
    row so base-price auto-scaling knows not to clobber freshly entered
    offers."""
    quantity = int(quantity)
    price = float(price)
    stamp = now_text() if manual else None
    cur.execute("""
        INSERT OR REPLACE INTO product_price_tiers(product_id, quantity, price, updated)
        VALUES(?,?,?,?)
    """, (product_id, quantity, price, stamp))
    conn.commit()


def tiers_recently_edited(product_id: int, minutes: int = 5) -> bool:
    """True if any bulk offer for this product was hand-edited within the
    last N minutes. Used to stop base-price auto-scaling from overwriting
    offers the admin just typed."""
    try:
        cur.execute("SELECT updated FROM product_price_tiers WHERE product_id=?", (product_id,))
        rows = cur.fetchall()
        cutoff = (ist_now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            if r and r[0] and str(r[0]).replace("T", " ")[:19] >= cutoff:
                return True
    except Exception:
        pass
    return False


def delete_product_price_tier(product_id: int, quantity: int):
    cur.execute("DELETE FROM product_price_tiers WHERE product_id=? AND quantity=?", (product_id, int(quantity)))
    conn.commit()
    return cur.rowcount > 0


def clear_product_price_tiers(product_id: int):
    cur.execute("DELETE FROM product_price_tiers WHERE product_id=?", (product_id,))
    conn.commit()


def get_setting_value(key: str, default: str = ""):
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else default


def set_user_coupon(user_id: int, code: str):
    cur.execute("INSERT OR REPLACE INTO user_coupons(user_id, code) VALUES(?,?)", (user_id, code.upper().strip()))
    conn.commit()


def get_user_coupon(user_id: int):
    cur.execute("SELECT code FROM user_coupons WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else None


def clear_user_coupon(user_id: int):
    cur.execute("DELETE FROM user_coupons WHERE user_id=?", (user_id,))
    conn.commit()


def validate_coupon(code: str):
    code = code.upper().strip()
    cur.execute("SELECT code, discount_type, value, active, max_uses, used_count, expires_at FROM coupons WHERE code=?", (code,))
    row = cur.fetchone()
    if not row:
        return None, "Coupon not found"

    code, discount_type, value, active, max_uses, used_count, expires_at = row

    if not active:
        return None, "Coupon is inactive"

    if expires_at:
        try:
            from utils.timezone import now
            end_time = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
            current_time = datetime.strptime(now().strftime("%Y-%m-%d %H:%M:%S"), "%Y-%m-%d %H:%M:%S")
            if end_time <= current_time:
                cur.execute("UPDATE coupons SET active=0 WHERE code=?", (code,))
                conn.commit()
                return None, "Coupon expired"
        except Exception:
            pass

    if max_uses and used_count >= max_uses:
        return None, "Coupon usage limit reached"

    return {
        "code": code,
        "discount_type": discount_type,
        "value": float(value),
        "max_uses": int(max_uses or 0),
        "used_count": int(used_count or 0),
        "expires_at": expires_at,
    }, None


def calculate_coupon_discount(total: float, coupon):
    if not coupon:
        return 0.0

    total = float(total)
    dtype = coupon.get("discount_type")
    value = float(coupon.get("value", 0))

    if dtype == "fixed":
        return round(min(total, value), 2)

    return round(min(total, total * value / 100), 2)


def mark_coupon_used(code: str):
    cur.execute("UPDATE coupons SET used_count=used_count+1 WHERE code=?", (code.upper().strip(),))
    conn.commit()



def add_restock_notification(user_id: int, product_id: int):
    cur.execute(
        "INSERT OR IGNORE INTO restock_notifications(user_id, product_id, notified) VALUES(?,?,0)",
        (user_id, product_id)
    )
    conn.commit()


def remove_restock_notification(user_id: int, product_id: int):
    cur.execute("DELETE FROM restock_notifications WHERE user_id=? AND product_id=?", (user_id, product_id))
    conn.commit()


def get_restock_wait_count(product_id: int):
    cur.execute("SELECT COUNT(*) FROM restock_notifications WHERE product_id=? AND notified=0", (product_id,))
    return cur.fetchone()[0]


def get_restock_waiters(product_id: int):
    cur.execute("SELECT user_id FROM restock_notifications WHERE product_id=? AND notified=0", (product_id,))
    return [r[0] for r in cur.fetchall()]


def mark_restock_notified(product_id: int):
    cur.execute("UPDATE restock_notifications SET notified=1 WHERE product_id=? AND notified=0", (product_id,))
    conn.commit()


def add_loyalty_points(user_id: int, amount_spent: float):
    return 0.0

def update_vip_rank(user_id: int):
    return 'Bronze'

def get_first_purchase_discount(user_id: int):
    cur.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (user_id,))
    count = cur.fetchone()[0]
    if count > 0:
        return 0.0
    return float(get_setting("first_purchase_discount_percent", "0") or 0)


def get_buy_x_get_y():
    try:
        x = int(get_setting("buy_x_qty", "0") or 0)
        y = int(get_setting("buy_x_free", "0") or 0)
        return x, y
    except Exception:
        return 0, 0



# Matches raw AND html-escaped tg-emoji tags (<tg-emoji ...> / &lt;tg-emoji ...&gt;).
_TG_EMOJI_TAG_RE = _re.compile(
    r'(?:<|&lt;)tg-emoji\s+emoji-id=(?:"|&quot;)(\d+)(?:"|&quot;)(?:>|&gt;)(.*?)(?:<|&lt;)/tg-emoji(?:>|&gt;)',
    _re.S,
)


def get_message_template(key: str, default: str = ""):
    cur.execute("SELECT body FROM message_templates WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row and row[0] else default


def set_message_template(key: str, body: str):
    cur.execute(
        "UPDATE message_templates SET body=?, updated=CURRENT_TIMESTAMP WHERE key=?",
        (body, key)
    )
    conn.commit()


def list_message_templates():
    cur.execute("SELECT key, title, updated FROM message_templates ORDER BY key")
    return cur.fetchall()


def render_message_template(key: str, default: str = "", **kwargs):
    aliases = {
        "store": kwargs.get("store_name", kwargs.get("store", "")),
        "product": kwargs.get("product_name", kwargs.get("product", "")),
        "code": kwargs.get("delivery", kwargs.get("code", "")),
        "discount": kwargs.get("discount_text", kwargs.get("discount", "")),
        "paid": kwargs.get("paid", ""),
        "balance": kwargs.get("balance", ""),
        "order_id": kwargs.get("order_id", ""),
        "user": kwargs.get("user", ""),
        "validity": kwargs.get("validity", ""),
        "limit": kwargs.get("limit", ""),
        "old_price": kwargs.get("old_price", ""),
        "new_price": kwargs.get("new_price", ""),
        "stock": kwargs.get("stock", ""),
        "amount": kwargs.get("amount", ""),
        "invoice_id": kwargs.get("invoice_id", ""),
        "invoice": kwargs.get("invoice_id", kwargs.get("invoice", "")),
        "tx": kwargs.get("tx", ""),
        "added": kwargs.get("added", ""),
        "available": kwargs.get("available", ""),
        "offers": kwargs.get("offers", ""),
        "price": kwargs.get("price", ""),
        "user_id": kwargs.get("user_id", ""),
        "username": kwargs.get("username", ""),
        "name": kwargs.get("name", kwargs.get("user", "")),
        "orders": kwargs.get("orders", ""),
        "spent": kwargs.get("spent", ""),
        "deposits": kwargs.get("deposits", ""),
        "deposited": kwargs.get("deposited", ""),
        "referral_earnings": kwargs.get("referral_earnings", ""),
        "referral_pending": kwargs.get("referral_pending", ""),
        "coupon": kwargs.get("coupon", ""),
        "notifications": kwargs.get("notifications", ""),
        "loyalty_points": kwargs.get("loyalty_points", ""),
        "vip_rank": kwargs.get("vip_rank", ""),
        "quantity": kwargs.get("quantity", ""),
        "total": kwargs.get("total", ""),
        "need": kwargs.get("need", ""),
        "min": kwargs.get("min", ""),
        "max": kwargs.get("max", ""),
    }
    body = get_message_template(key, default)
    try:
        rendered = body.format(**{**kwargs, **aliases})
    except Exception:
        rendered = body
    # Defense-in-depth: never let a broken/escaped <tg-emoji> tag reach users
    # as literal text. Normalizes raw AND html-escaped forms into one canonical
    # well-formed tag so Telegram renders the premium emoji (or its plain
    # fallback), and keeps all other HTML (<b>, <code>, ...) intact.
    return _TG_EMOJI_TAG_RE.sub(r'<tg-emoji emoji-id="\1">\2</tg-emoji>', rendered)


def simplify_default_message_templates():
    simple_templates = {
        "welcome": "👋 Welcome to {store}\n\nChoose an option below 👇",
        "delivery_success": "✅ Payment Successful!\n\n🧾 Order ID: #{order_id}\n📦 Product: {product}\n\n🔑 Code:\n{code}\n\n💰 Paid: {paid}\n💼 Balance: {balance}\n\nThank you for your purchase ❤️",
        "coupon_broadcast": "🎟 New Coupon Available!\n\n💝 Discount: {discount}\n🎫 Code: {code}\n⏳ Validity: {validity}\n🔥 Limit: {limit}\n\nUse it before checkout 👇",
        "flash_sale_broadcast": "🔥 FLASH SALE!\n\n📦 {product}\n\n💰 Was: {old_price}\n⚡ Now: {new_price}\n🎁 Discount: {discount}\n\n����� {validity}\n\nBuy before it ends 👇",
        "restock_alert": "🔔 Back In Stock!\n\n📦 {product}\n📊 Available: {stock}\n\nTap below to buy now 👇",
        "deposit_success": "✅ Deposit Confirmed\n\n���� Amount: {amount} USDT\n\nYour wallet balance has been updated."
    }
    for key, body in simple_templates.items():
        cur.execute("UPDATE message_templates SET body=? WHERE key=?", (body, key))
    conn.commit()




def create_scheduled_broadcast(message: str, run_at: str):
    cur.execute(
        "INSERT INTO scheduled_broadcasts(message, run_at, status) VALUES(?,?, 'pending')",
        (message, run_at)
    )
    conn.commit()
    return cur.lastrowid


def queue_price_change_broadcast(product_id: int, old_price: float, new_price: float):
    """Queue an automatic price-change announcement (the bot sends it within ~1 minute)."""
    try:
        cur.execute("SELECT name FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        if not row:
            return None
        from utils.premium_emoji import premium_safe
        name = premium_safe(row[0])
        available = get_display_stock(product_id)

        if new_price < old_price:
            pct = round((old_price - new_price) / old_price * 100) if old_price > 0 else 0
            title = "\U0001f525 <b>PRICE DROP ALERT!</b>"
            change_line = f"\U0001f4c9 <b>Price Dropped:</b> <b>-{pct}%</b> \U0001f389"
        else:
            pct = round((new_price - old_price) / old_price * 100) if old_price > 0 else 0
            title = "\U0001f4e2 <b>PRICE UPDATE!</b>"
            change_line = f"\U0001f4c8 <b>Price Increased:</b> <b>+{pct}%</b>"

        bar = "\u2501" * 18
        if available and int(available) > 0:
            tail = "\U0001f6cd <i>Open the Shop in the bot to grab it now!</i>"
        else:
            tail = "\u23f3 <i>Out of stock \u2014 restocking soon!</i> \U0001f514"
        msg = (f"{title}\n{bar}\n\n"
               f"\U0001f4e6 <b>{name}</b>\n\n"
               f"\U0001f4b0 <b>Old Price:</b> <s>${old_price:.2f}</s>\n"
               f"\u2728 <b>New Price:</b> <b>${new_price:.2f}</b>\n"
               f"{change_line}\n"
               f"\U0001f4ca <b>Available Stock:</b> <b>{available}</b>\n\n"
               f"{bar}\n"
               f"{tail}")

        return create_scheduled_broadcast(msg, now_text())
    except Exception:
        return None


def get_pending_scheduled_broadcasts(now_text: str):
    cur.execute(
        "SELECT id, message FROM scheduled_broadcasts WHERE status='pending' AND run_at<=? ORDER BY run_at ASC",
        (now_text,)
    )
    return cur.fetchall()


def mark_scheduled_broadcast_done(bid: int, sent: int, failed: int):
    cur.execute(
        "UPDATE scheduled_broadcasts SET status='sent', sent=?, failed=? WHERE id=?",
        (sent, failed, bid)
    )
    conn.commit()


def get_scheduled_broadcasts(limit: int = 10):
    cur.execute(
        "SELECT id, message, run_at, status, sent, failed FROM scheduled_broadcasts ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    return cur.fetchall()



def clear_admin_logs():
    cur.execute("DELETE FROM admin_logs")
    conn.commit()



def smart_dashboard_stats():
    reset_at = get_setting("revenue_reset_at", "") or "1970-01-01 00:00:00"
    try:
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE DATE(created)=DATE('now') AND created >= ?", (reset_at,))
        today_revenue, today_orders = cur.fetchone()
    except Exception:
        today_revenue, today_orders = 0, 0

    try:
        cur.execute("SELECT COUNT(*) FROM users WHERE DATE(joined)=DATE('now')")
        new_users = cur.fetchone()[0]
    except Exception:
        new_users = 0

    try:
        cur.execute("SELECT COUNT(*) FROM tickets WHERE status IN ('open','answered')")
        open_tickets = cur.fetchone()[0]
    except Exception:
        open_tickets = 0

    try:
        cur.execute("SELECT COUNT(*) FROM products WHERE active=1 AND COALESCE(stock,0)<=5")
        low_stock = cur.fetchone()[0]
    except Exception:
        low_stock = 0

    try:
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created >= ?", (reset_at,))
        total_revenue, total_orders = cur.fetchone()
    except Exception:
        total_revenue, total_orders = 0, 0

    return {
        "today_revenue": float(today_revenue or 0),
        "today_orders": int(today_orders or 0),
        "new_users": int(new_users or 0),
        "open_tickets": int(open_tickets or 0),
        "low_stock": int(low_stock or 0),
        "total_revenue": float(total_revenue or 0),
        "total_orders": int(total_orders or 0),
    }



def create_backup_zip():
    import zipfile
    import shutil
    from pathlib import Path
    from datetime import datetime
    try:
        from config import DB_PATH
    except Exception:
        DB_PATH = "shopbot.db"

    backup_dir = Path("backups")
    backup_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"shopbot_backup_{ts}.zip"

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as z:
        db_file = Path(DB_PATH)
        if db_file.exists():
            z.write(db_file, db_file.name)
        for p in Path(".").glob(".env*"):
            if p.is_file():
                z.write(p, p.name)

    return str(backup_path)


def restore_backup_zip(zip_path: str):
    import zipfile
    from pathlib import Path
    try:
        from config import DB_PATH
    except Exception:
        DB_PATH = "shopbot.db"

    with zipfile.ZipFile(zip_path, "r") as z:
        db_members = [m for m in z.namelist() if m.endswith(".db")]
        if not db_members:
            return False, "No .db file found in backup."
        z.extract(db_members[0], ".")
        extracted = Path(db_members[0])
        target = Path(DB_PATH)
        if extracted.resolve() != target.resolve():
            target.write_bytes(extracted.read_bytes())
            try:
                extracted.unlink()
            except Exception:
                pass
    return True, "Backup restored. Restart the bot."



# ---------------- Microsoft 365 Family auto-invite ----------------

_M365_TABLE_READY = False


def ensure_m365_table():
    global _M365_TABLE_READY
    if _M365_TABLE_READY:
        return
    cur.execute("""
    CREATE TABLE IF NOT EXISTS m365_invites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        order_id INTEGER,
        email TEXT,
        status TEXT DEFAULT 'pending',
        error TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    try:
        cur.execute("ALTER TABLE m365_invites ADD COLUMN warranty TEXT")
    except Exception:
        pass
    conn.commit()
    _M365_TABLE_READY = True


def create_m365_invite(user_id: int, order_id: int, email: str, warranty: str = "") -> int:
    ensure_m365_table()
    cur.execute(
        "INSERT INTO m365_invites(user_id, order_id, email, status, warranty) VALUES(?,?,?,'pending',?)",
        (user_id, order_id, email, str(warranty or "")),
    )
    conn.commit()
    return cur.lastrowid


def set_m365_invite_status(invite_id: int, status: str, error=None):
    ensure_m365_table()
    cur.execute("UPDATE m365_invites SET status=?, error=? WHERE id=?", (status, error, invite_id))
    conn.commit()


def get_m365_invite(invite_id: int):
    ensure_m365_table()
    cur.execute(
        "SELECT id, user_id, order_id, email, status, error, created FROM m365_invites WHERE id=?",
        (invite_id,),
    )
    return cur.fetchone()


def get_m365_invite_warranty(invite_id: int) -> str:
    """Warranty group recorded for an invite: '' (any), '6m' or '1y'."""
    ensure_m365_table()
    try:
        cur.execute("SELECT COALESCE(warranty,'') FROM m365_invites WHERE id=?", (invite_id,))
        row = cur.fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def list_m365_invites(limit: int = 10):
    ensure_m365_table()
    cur.execute(
        "SELECT id, user_id, order_id, email, status, error, created FROM m365_invites ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def get_m365_product_ids():
    raw = get_setting("m365_invite_products", "") or ""
    ids = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_m365_product(product_id) -> bool:
    try:
        return int(product_id) in get_m365_product_ids()
    except Exception:
        return False


def get_m365_product_warranty(product_id) -> str:
    """Warranty group an M365 product's invites should use: '' (any), '6m' or '1y'."""
    try:
        return str(get_setting(f"m365_warranty_{int(product_id)}", "") or "")
    except Exception:
        return ""


def set_m365_product_warranty(product_id, warranty):
    """Tag an M365 product so its invites are sent only from matching-warranty IDs."""
    set_setting(f"m365_warranty_{int(product_id)}", str(warranty or ""))


def toggle_m365_product(product_id: int) -> bool:
    ids = get_m365_product_ids()
    pid = int(product_id)
    if pid in ids:
        ids.discard(pid)
        enabled = False
    else:
        ids.add(pid)
        enabled = True
    set_setting("m365_invite_products", ",".join(str(x) for x in sorted(ids)))
    return enabled


# ---------------- Canva team auto-invite ----------------

_CANVA_TABLE_READY = False


def ensure_canva_table():
    global _CANVA_TABLE_READY
    if _CANVA_TABLE_READY:
        return
    cur.execute("""
    CREATE TABLE IF NOT EXISTS canva_invites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        order_id INTEGER,
        email TEXT,
        status TEXT DEFAULT 'pending',
        error TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    _CANVA_TABLE_READY = True


def create_canva_invite(user_id: int, order_id: int, email: str) -> int:
    ensure_canva_table()
    cur.execute(
        "INSERT INTO canva_invites(user_id, order_id, email, status) VALUES(?,?,?,'pending')",
        (user_id, order_id, email),
    )
    conn.commit()
    return cur.lastrowid


def set_canva_invite_status(invite_id: int, status: str, error=None):
    ensure_canva_table()
    cur.execute("UPDATE canva_invites SET status=?, error=? WHERE id=?", (status, error, invite_id))
    conn.commit()


def get_canva_invite(invite_id: int):
    ensure_canva_table()
    cur.execute(
        "SELECT id, user_id, order_id, email, status, error, created FROM canva_invites WHERE id=?",
        (invite_id,),
    )
    return cur.fetchone()


def list_canva_invites(limit: int = 10):
    ensure_canva_table()
    cur.execute(
        "SELECT id, user_id, order_id, email, status, error, created FROM canva_invites ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def get_canva_product_ids():
    raw = get_setting("canva_invite_products", "") or ""
    ids = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def is_canva_product(product_id) -> bool:
    try:
        return int(product_id) in get_canva_product_ids()
    except Exception:
        return False


def toggle_canva_product(product_id: int) -> bool:
    ids = get_canva_product_ids()
    pid = int(product_id)
    if pid in ids:
        ids.discard(pid)
        enabled = False
    else:
        ids.add(pid)
        enabled = True
    set_setting("canva_invite_products", ",".join(str(x) for x in sorted(ids)))
    return enabled


# ---------------- Gift Codes & Warranty Replacement ----------------

cur.execute("""
CREATE TABLE IF NOT EXISTS gift_codes(
    code TEXT PRIMARY KEY,
    amount REAL NOT NULL,
    created_by INTEGER,
    redeemed_by INTEGER,
    redeemed_at TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS replacements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    product_id INTEGER,
    old_delivery TEXT,
    new_delivery TEXT,
    status TEXT DEFAULT 'pending',
    created TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

_safe_add_column("orders", "product_id", "INTEGER")
_safe_add_column("users", "warranty_banned", "INTEGER DEFAULT 0")

cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('warranty_enabled', '1')")
cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('warranty_hours', '24')")
cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('warranty_max_week', '3')")
cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('gift_min_amount', '1')")
cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('gift_max_amount', '500')")
conn.commit()


def generate_gift_code() -> str:
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "GIFT-" + "".join(secrets.choice(alphabet) for _ in range(10))


def create_gift_code(user_id: int, amount: float, free: bool = False):
    """Create a gift code. Deducts the buyer's balance unless free (admin gift)."""
    amount = round(float(amount), 2)
    if amount <= 0:
        return None

    if not free:
        cur.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (amount, user_id, amount)
        )
        if cur.rowcount == 0:
            conn.rollback()
            return None

    code = generate_gift_code()
    cur.execute(
        "INSERT INTO gift_codes(code, amount, created_by, created) VALUES(?,?,?,?)",
        (code, amount, user_id, now_text())
    )
    conn.commit()
    return code


def redeem_gift_code(user_id: int, code: str):
    code = str(code).strip().upper()
    cur.execute("SELECT amount, redeemed_by, created_by FROM gift_codes WHERE code=?", (code,))
    row = cur.fetchone()

    if not row:
        return False, "Invalid gift code."

    amount, redeemed_by, created_by = row

    if redeemed_by:
        return False, "This gift code has already been redeemed."

    if created_by == user_id:
        return False, "You cannot redeem your own gift code. Share it with someone!"

    cur.execute(
        "UPDATE gift_codes SET redeemed_by=?, redeemed_at=? WHERE code=? AND redeemed_by IS NULL",
        (user_id, now_text(), code)
    )
    if cur.rowcount == 0:
        conn.rollback()
        return False, "This gift code has already been redeemed."

    cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
    conn.commit()
    return True, amount


def get_user_gift_codes(user_id: int, limit: int = 10):
    cur.execute(
        "SELECT code, amount, redeemed_by, created FROM gift_codes WHERE created_by=? ORDER BY created DESC LIMIT ?",
        (user_id, limit)
    )
    return cur.fetchall()


def list_gift_codes(limit: int = 15):
    cur.execute(
        "SELECT code, amount, created_by, redeemed_by, created FROM gift_codes ORDER BY created DESC LIMIT ?",
        (limit,)
    )
    return cur.fetchall()


def is_warranty_banned(user_id: int) -> bool:
    cur.execute("SELECT warranty_banned FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return bool(row and row[0] == 1)


def set_warranty_ban(user_id: int, banned: bool):
    ensure_user_id(user_id)
    cur.execute("UPDATE users SET warranty_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
    conn.commit()


def get_warranty_orders(user_id: int):
    """Orders inside the warranty window, with a known product and no replacement yet."""
    hours = int(float(get_setting("warranty_hours", "24") or 24))
    # Portable time filter (works on SQLite AND Postgres): compute the UTC
    # cutoff in Python and compare as text. orders.created is stored in UTC.
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT o.id, o.product_name, o.product_id, o.created FROM orders o "
        "WHERE o.user_id=? AND o.product_id IS NOT NULL "
        "AND o.created >= ? "
        "AND NOT EXISTS (SELECT 1 FROM replacements r WHERE r.order_id = o.id) "
        "ORDER BY o.id DESC LIMIT 10",
        (user_id, cutoff)
    )
    return cur.fetchall()


def get_order_for_warranty(order_id: int, user_id: int):
    hours = int(float(get_setting("warranty_hours", "24") or 24))
    # Portable time filter (works on SQLite AND Postgres). orders.created is UTC.
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT o.id, o.product_id, o.product_name, o.delivery FROM orders o "
        "WHERE o.id=? AND o.user_id=? AND o.product_id IS NOT NULL "
        "AND o.created >= ? "
        "AND NOT EXISTS (SELECT 1 FROM replacements r WHERE r.order_id = o.id)",
        (order_id, user_id, cutoff)
    )
    return cur.fetchone()


def count_recent_replacements(user_id: int, days: int = 7) -> int:
    # Portable time filter. replacements.created is written with now_text() (IST),
    # so compute the cutoff in IST as well.
    cutoff = (ist_now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT COUNT(*) FROM replacements WHERE user_id=? AND created >= ?",
        (user_id, cutoff)
    )
    row = cur.fetchone()
    return row[0] if row else 0


def create_replacement(order_id: int, user_id: int, product_id, old_delivery: str, new_delivery: str, status: str) -> int:
    cur.execute(
        "INSERT INTO replacements(order_id, user_id, product_id, old_delivery, new_delivery, status, created) VALUES(?,?,?,?,?,?,?)",
        (order_id, user_id, product_id, encrypt_text(old_delivery or ""), encrypt_text(new_delivery or ""), status, now_text())
    )
    conn.commit()
    return cur.lastrowid


def get_replacement(replacement_id: int):
    cur.execute(
        "SELECT id, order_id, user_id, product_id, old_delivery, new_delivery, status FROM replacements WHERE id=?",
        (replacement_id,)
    )
    return cur.fetchone()


def get_pending_replacements(limit: int = 10):
    cur.execute(
        "SELECT id, order_id, user_id, product_id, created FROM replacements WHERE status='pending' ORDER BY id ASC LIMIT ?",
        (limit,)
    )
    return cur.fetchall()


def set_replacement_status(replacement_id: int, status: str, new_delivery=None):
    if new_delivery is not None:
        cur.execute(
            "UPDATE replacements SET status=?, new_delivery=? WHERE id=?",
            (status, encrypt_text(new_delivery), replacement_id)
        )
    else:
        cur.execute("UPDATE replacements SET status=? WHERE id=?", (status, replacement_id))
    conn.commit()


# ---------------- M365 Multi-Session Rotation ----------------

cur.execute("""CREATE TABLE IF NOT EXISTS m365_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    state TEXT,
    invites_used INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created TEXT
)""")
conn.commit()

cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('m365_invites_per_session', '5')")
conn.commit()

# Optional saved login (email + password + 2FA secret) so a session can be
# refreshed automatically by logging in again when its cookies expire.
_safe_add_column("m365_sessions", "login_email", "TEXT")
_safe_add_column("m365_sessions", "login_password", "TEXT")
_safe_add_column("m365_sessions", "totp_secret", "TEXT")
# Warranty group tag: '' (any), '6m' (6-Month warranty IDs) or '1y' (1-Year warranty IDs).
_safe_add_column("m365_sessions", "warranty", "TEXT")
conn.commit()


def add_m365_session(label, state_json, warranty=""):
    """Store a new Microsoft session (encrypted). Returns the new session id.

    `warranty` tags the ID's group: '' (any), '6m' or '1y'."""
    cur.execute(
        "INSERT INTO m365_sessions (label, state, invites_used, status, created, warranty) VALUES (?, ?, 0, 'active', ?, ?)",
        (str(label or ""), encrypt_text(state_json), now_text(), str(warranty or "")),
    )
    conn.commit()
    return cur.lastrowid


def add_m365_credentials(label, email, password, totp_secret):
    """Store a Microsoft ID as login credentials (no cookie upload needed).

    The session starts with an empty cookie state; the bot signs in with these
    credentials automatically on first use and whenever the session expires.
    Returns the new session id.
    """
    cur.execute(
        "INSERT INTO m365_sessions (label, state, invites_used, status, created, login_email, login_password, totp_secret) VALUES (?, '', 0, 'active', ?, ?, ?, ?)",
        (
            str(label or ""),
            now_text(),
            encrypt_text(email or ""),
            encrypt_text(password or ""),
            encrypt_text(totp_secret or ""),
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_m365_credentials(session_id, email, password, totp_secret):
    """Attach/update saved login credentials on an existing session."""
    cur.execute(
        "UPDATE m365_sessions SET login_email=?, login_password=?, totp_secret=? WHERE id=?",
        (
            encrypt_text(email or ""),
            encrypt_text(password or ""),
            encrypt_text(totp_secret or ""),
            session_id,
        ),
    )
    conn.commit()


def get_m365_credentials(session_id):
    """Return (email, password, totp_secret) for a session, or None if not set."""
    try:
        cur.execute("SELECT login_email, login_password, totp_secret FROM m365_sessions WHERE id=?", (session_id,))
    except Exception:
        return None
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    email = decrypt_text(row[0]) if row[0] else ""
    if not email or str(email).startswith("[encrypted:"):
        return None
    password = decrypt_text(row[1]) if row[1] else ""
    totp = decrypt_text(row[2]) if row[2] else ""
    return (email, password, totp)


def rename_m365_session(session_id, label):
    cur.execute("UPDATE m365_sessions SET label=? WHERE id=?", (str(label), session_id))
    conn.commit()


def get_current_m365_session():
    """First active (non-full) session. Returns (id, label, state_json, invites_used) or None."""
    cur.execute("SELECT id, label, state, invites_used FROM m365_sessions WHERE status='active' ORDER BY id ASC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    state = decrypt_text(row[2]) if row[2] else None
    return (row[0], row[1], state, int(row[3] or 0))


def update_m365_session_state(session_id, state_json):
    """Persist refreshed cookies for a session."""
    cur.execute("UPDATE m365_sessions SET state=? WHERE id=?", (encrypt_text(state_json), session_id))
    conn.commit()


def increment_m365_session_used(session_id):
    cur.execute("UPDATE m365_sessions SET invites_used=COALESCE(invites_used,0)+1 WHERE id=?", (session_id,))
    conn.commit()
    cur.execute("SELECT invites_used FROM m365_sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def mark_m365_session_full(session_id):
    cur.execute("UPDATE m365_sessions SET status='full' WHERE id=?", (session_id,))
    conn.commit()


def reset_m365_session(session_id):
    """Zero the invite counter and reactivate (after freeing family slots)."""
    cur.execute("UPDATE m365_sessions SET invites_used=0, status='active' WHERE id=?", (session_id,))
    conn.commit()


def delete_m365_session(session_id):
    cur.execute("DELETE FROM m365_sessions WHERE id=?", (session_id,))
    conn.commit()


def list_m365_sessions():
    try:
        cur.execute("SELECT id, label, invites_used, status, created, CASE WHEN login_email IS NOT NULL AND login_email != '' THEN 1 ELSE 0 END, COALESCE(warranty,'') FROM m365_sessions ORDER BY id ASC")
        return cur.fetchall()
    except Exception:
        cur.execute("SELECT id, label, invites_used, status, created, 0, '' FROM m365_sessions ORDER BY id ASC")
        return cur.fetchall()


def count_active_m365_sessions():
    cur.execute("SELECT COUNT(*) FROM m365_sessions WHERE status='active'")
    return int(cur.fetchone()[0])


def get_m365_session(session_id):
    """Return one session (id, label, state_json, invites_used) or None."""
    cur.execute("SELECT id, label, state, invites_used FROM m365_sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    if not row:
        return None
    state = decrypt_text(row[2]) if row[2] else None
    if state and str(state).startswith("[encrypted:"):
        state = None
    return (row[0], row[1], state, int(row[3] or 0))


def list_active_m365_sessions(warranty=None):
    """Return [(id, label, invites_used), ...] for every non-full, non-expired ID.

    When `warranty` is given ('6m' or '1y'), only IDs tagged with that warranty
    group are returned, so 6-Month orders never consume a 1-Year ID and vice versa."""
    if warranty:
        cur.execute("SELECT id, label, invites_used FROM m365_sessions WHERE status='active' AND COALESCE(warranty,'')=? ORDER BY id ASC", (str(warranty),))
    else:
        cur.execute("SELECT id, label, invites_used FROM m365_sessions WHERE status='active' ORDER BY id ASC")
    return [(r[0], r[1], int(r[2] or 0)) for r in cur.fetchall()]


def set_m365_session_warranty(session_id, warranty):
    """Tag a Microsoft ID with a warranty group: '' (any), '6m' or '1y'."""
    cur.execute("UPDATE m365_sessions SET warranty=? WHERE id=?", (str(warranty or ""), session_id))
    conn.commit()


def mark_m365_session_expired(session_id):
    """Flag a session whose cookies died so it is skipped until re-uploaded."""
    cur.execute("UPDATE m365_sessions SET status='expired' WHERE id=?", (session_id,))
    conn.commit()


def has_healthy_m365_session():
    """True if at least one active (usable) Microsoft ID exists."""
    return count_active_m365_sessions() > 0


def list_queued_m365_invites(limit: int = 25):
    """Invites parked in the safety-net queue, waiting for a fresh session."""
    ensure_m365_table()
    cur.execute(
        "SELECT id, user_id, order_id, email, status, error, created FROM m365_invites WHERE status='queued' ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def count_queued_m365_invites():
    ensure_m365_table()
    cur.execute("SELECT COUNT(*) FROM m365_invites WHERE status='queued'")
    return int(cur.fetchone()[0])
