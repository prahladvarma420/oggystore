from datetime import datetime
import sqlite3
from config import DB_PATH, ADMIN_ID
import os
from utils.crypto import encrypt_text, decrypt_text
from utils.timezone import now_text

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

    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('referral_percent', '5')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('low_stock_alert_threshold', '3')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('low_stock_alert_levels', '10,5,1')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('min_withdrawal', '1')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('loyalty_percent', '2')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('first_purchase_discount_percent', '0')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('buy_x_qty', '0')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('buy_x_free', '0')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('auto_welcome_coupon', '')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('inactive_days', '7')")
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('inactive_coupon', '')")

    default_templates = {
        "welcome": (
            "Welcome Message",
            "👋 <b>Welcome to {store_name}</b>\\n\\nChoose an option below to continue."
        ),
        "delivery_success": (
            "Delivery Success Message",
            "✅ <b>Payment Successful!</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n🧾 <b>Order ID:</b> <code>#{order_id}</code>\\n\\n📦 <b>{product_name}</b>\\n\\n🔑 <b>Code(s):</b>\\n<code>{delivery}</code>\\n\\n━━━━━━━━━━━━━━━━━━━━\\n\\n💰 <b>Paid:</b> <code>${paid}</code>\\n💼 <b>Remaining Balance:</b> <code>${balance}</code>\\n\\n━━━━━━━━━━━━━━━━━━━━\\nThank you for your purchase! 🎉\\n\\nUse /orders to view your order history."
        ),
        "coupon_broadcast": (
            "Coupon Broadcast Message",
            "🎟 <b>NEW COUPON AVAILABLE!</b>\\n\\n💝 <b>Discount:</b> <code>{discount}</code>\\n🎫 <b>Coupon Code:</b> <code>{code}</code>\\n⏳ <b>Validity:</b> <code>{validity}</code>\\n🔥 <b>Limit:</b> <code>{limit}</code>\\n\\nApply this coupon before checkout and save on your next purchase 👇"
        ),
        "flash_sale_broadcast": (
            "Flash Sale Broadcast Message",
            "🧨 <b>FLASH SALE — LIMITED TIME!</b>\\n\\n📦 <b>{product_name}</b>\\n\\n💵 <b>Was:</b> <s>${old_price}</s>\\n⚡ <b>Now:</b> <code>${new_price} USDT</code> <b>({discount_percent}% OFF)</b>\\n\\n🏃 <b>Hurry — {validity}</b>\\nGrab it before the timer runs out 👇"
        ),
        "restock_alert": (
            "Restock Alert Message",
            "🔔 <b>Back In Stock!</b>\\n\\n📦 <b>{product_name}</b>\\n📊 Available: <code>{stock}</code>\\n\\nTap below to buy now 👇"
        ),
        "deposit_success": (
            "Deposit Success Message",
            "✅ <b>Deposit Confirmed</b>\\n\\n💵 Amount: <code>{amount} USDT</code>\\n\\nYour wallet balance has been updated."
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
            "👋 <b>Welcome to {store_name}</b>\\n\\nChoose an option below to continue."
        ),
        "delivery_success": (
            "Delivery Success Message",
            "✅ <b>Payment Successful!</b>\\n━━━━━━━━━━━━━━━━━━━━\\n\\n🧾 <b>Order ID:</b> <code>#{order_id}</code>\\n\\n📦 <b>{product_name}</b>\\n\\n🔑 <b>Code(s):</b>\\n<code>{delivery}</code>\\n\\n━━━━━━━━━━━━━━━━━━━━\\n\\n💰 <b>Paid:</b> <code>${paid}</code>\\n💼 <b>Remaining Balance:</b> <code>${balance}</code>\\n\\n━━━━━━━━━━━━━━━━━━━━\\nThank you for your purchase! 🎉\\n\\nUse /orders to view your order history."
        ),
        "coupon_broadcast": (
            "Coupon Broadcast Message",
            "🎟 <b>NEW COUPON AVAILABLE!</b>\\n\\n💝 <b>Discount:</b> <code>{discount}</code>\\n🎫 <b>Coupon Code:</b> <code>{code}</code>\\n⏳ <b>Validity:</b> <code>{validity}</code>\\n🔥 <b>Limit:</b> <code>{limit}</code>\\n\\nApply this coupon before checkout and save on your next purchase 👇"
        ),
        "flash_sale_broadcast": (
            "Flash Sale Broadcast Message",
            "🧨 <b>FLASH SALE — LIMITED TIME!</b>\\n\\n📦 <b>{product_name}</b>\\n\\n💵 <b>Was:</b> <s>${old_price}</s>\\n⚡ <b>Now:</b> <code>${new_price} USDT</code> <b>({discount_percent}% OFF)</b>\\n\\n🏃 <b>Hurry — {validity}</b>\\nGrab it before the timer runs out 👇"
        ),
        "restock_alert": (
            "Restock Alert Message",
            "🔔 <b>Back In Stock!</b>\\n\\n📦 <b>{product_name}</b>\\n📊 Available: <code>{stock}</code>\\n\\nTap below to buy now 👇"
        ),
        "deposit_success": (
            "Deposit Success Message",
            "✅ <b>Deposit Confirmed</b>\\n\\n💵 Amount: <code>{amount} USDT</code>\\n\\nYour wallet balance has been updated."
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
    conn.commit()
    return len(items)


def get_available_stock_count(product_id: int) -> int:
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=0", (product_id,))
    row = cur.fetchone()
    return row[0] if row else 0


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


def set_product_price_tier(product_id: int, quantity: int, price: float):
    quantity = int(quantity)
    price = float(price)
    cur.execute("""
        INSERT OR REPLACE INTO product_price_tiers(product_id, quantity, price)
        VALUES(?,?,?)
    """, (product_id, quantity, price))
    conn.commit()


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
    }
    body = get_message_template(key, default)
    try:
        return body.format(**{**kwargs, **aliases})
    except Exception:
        return body


def simplify_default_message_templates():
    simple_templates = {
        "welcome": "👋 Welcome to {store}\n\nChoose an option below 👇",
        "delivery_success": "✅ Payment Successful!\n\n🧾 Order ID: #{order_id}\n📦 Product: {product}\n\n🔑 Code:\n{code}\n\n💰 Paid: {paid}\n💼 Balance: {balance}\n\nThank you for your purchase ❤️",
        "coupon_broadcast": "🎟 New Coupon Available!\n\n💝 Discount: {discount}\n🎫 Code: {code}\n⏳ Validity: {validity}\n🔥 Limit: {limit}\n\nUse it before checkout 👇",
        "flash_sale_broadcast": "🔥 FLASH SALE!\n\n📦 {product}\n\n💰 Was: {old_price}\n⚡ Now: {new_price}\n🎁 Discount: {discount}\n\n⏳ {validity}\n\nBuy before it ends 👇",
        "restock_alert": "🔔 Back In Stock!\n\n📦 {product}\n📊 Available: {stock}\n\nTap below to buy now 👇",
        "deposit_success": "✅ Deposit Confirmed\n\n💵 Amount: {amount} USDT\n\nYour wallet balance has been updated."
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
    try:
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE DATE(created)=DATE('now')")
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
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders")
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

