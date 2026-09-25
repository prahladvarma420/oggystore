from datetime import datetime, timedelta
import re as _re
import sqlite3
from config import DB_PATH, ADMIN_ID
import os
from utils.crypto import encrypt_text, decrypt_text
from utils.timezone import now_text, today_prefix, now as ist_now

import threading as _threading

from database.pgcompat import connect as _compat_connect  # SQLite/Postgres switch

# ---------------------------------------------------------------- FIX42
# `conn` / `cur` used to be ONE module-global connection and ONE cursor shared
# by the bot thread, the PTB job-queue threads and all 8 gunicorn threads.
#
# That is not safe. _PgCursor buffers its result set into self._rows, so two
# threads running execute() on the same cursor clobber each other's rows AND
# each other's .rowcount / .lastrowid. In the payment path that meant a
# `cur.rowcount` balance check could read another thread's result.
#
# Fix: give every thread its own connection + cursor, kept behind proxy objects
# so that `from database.db import conn, cur` (done by 8 modules) and all ~380
# existing `cur.execute(...)` / `conn.commit()` call sites keep working
# unchanged.
_tls = _threading.local()


def _new_conn():
    c = _compat_connect(DB_PATH)
    # WAL lets SQLite readers run concurrently with a writer instead of
    # serialising every access. No-op on Postgres (pgcompat ignores PRAGMA).
    try:
        c.cursor().execute("PRAGMA journal_mode=WAL")
        c.cursor().execute("PRAGMA busy_timeout=15000")
    except Exception:
        pass
    return c


def get_conn():
    """The calling thread's own database connection."""
    c = getattr(_tls, "conn", None)
    if c is None:
        c = _new_conn()
        _tls.conn = c
    return c


def get_cursor():
    """The calling thread's own cursor."""
    k = getattr(_tls, "cur", None)
    if k is None:
        k = get_conn().cursor()
        _tls.cur = k
    return k


def close_thread_connection():
    """Release this thread's connection (call from long-lived worker teardown)."""
    k = getattr(_tls, "cur", None)
    if k is not None:
        try:
            k.close()
        except Exception:
            pass
        _tls.cur = None
    c = getattr(_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _tls.conn = None


class _ConnProxy:
    """Forwards every attribute access to the current thread's connection."""
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(get_conn(), name)

    def __setattr__(self, name, value):
        setattr(get_conn(), name, value)

    def __enter__(self):
        return get_conn().__enter__()

    def __exit__(self, *a):
        return get_conn().__exit__(*a)


class _CursorProxy:
    """Forwards every attribute access to the current thread's cursor."""
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(get_cursor(), name)

    def __setattr__(self, name, value):
        setattr(get_cursor(), name, value)

    def __iter__(self):
        return iter(get_cursor())


conn = _ConnProxy()
cur = _CursorProxy()


def _table_columns(table: str):
    """Column names for `table` on either backend."""
    try:
        from database.pgcompat import use_postgres
    except Exception:
        def use_postgres():
            return False

    if use_postgres():
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=?",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]

    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _safe_add_column(table: str, column: str, definition: str):
    # Do not fail fresh installs if a migration is listed before the table is created.
    # NOTE: the sqlite_master probe is rewritten to information_schema by pgcompat;
    # it is scoped to the public schema so a same-named table in another schema
    # cannot produce a false positive.
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if cur.fetchone() is None:
        return
    existing = _table_columns(table)
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ---- Editable deposit button labels + per-method invoice messages ----
# These are seeded into message_templates so the owner can edit them from the
# web admin. Button labels are plain text (Telegram buttons can't render HTML
# or premium emoji). Invoice messages support full HTML and premium emoji via
# <tg-emoji emoji-id="...">\U0001f4b0</tg-emoji> tags.
BTN_BEP20_DEFAULT = "\U0001f7e1 USDT \u2014 BEP20 (BNB Chain)"
BTN_TRC20_DEFAULT = "\U0001f534 USDT \u2014 TRC20 (Tron)"
BTN_BINANCE_DEFAULT = "\U0001f7e8 USDT \u2014 Binance Pay"

# Checkout-screen pay buttons (editable; buttons are plain text only).
BTN_CHECKOUT_BEP20_DEFAULT = "\u26a1 Pay USDT BEP20"
BTN_CHECKOUT_TRC20_DEFAULT = "\u26a1 Pay USDT TRC20"
BTN_CHECKOUT_BINANCE_DEFAULT = "\U0001f7e8 Pay via Binance Pay"

# Price-change broadcasts (editable). Placeholders: {product} {old_price}
# {new_price} {pct} {stock} {bar} {tail}. {tail} is filled automatically
# ("grab it now" when in stock / "restocking soon" when out of stock).
PRICE_UP_BROADCAST_DEFAULT = (
    "\U0001f4e2 <b>PRICE UPDATE!</b>\n{bar}\n\n"
    "\U0001f4e6 <b>{product}</b>\n\n"
    "\U0001f4b0 <b>Old Price:</b> <s>{old_price}</s>\n"
    "\u2728 <b>New Price:</b> <b>{new_price}</b>\n"
    "\U0001f4c8 <b>Price Increased:</b> <b>+{pct}%</b>\n"
    "\U0001f4ca <b>Available Stock:</b> <b>{stock}</b>\n\n"
    "{bar}\n{tail}"
)
PRICE_DOWN_BROADCAST_DEFAULT = (
    "\U0001f525 <b>PRICE DROP ALERT!</b>\n{bar}\n\n"
    "\U0001f4e6 <b>{product}</b>\n\n"
    "\U0001f4b0 <b>Old Price:</b> <s>{old_price}</s>\n"
    "\u2728 <b>New Price:</b> <b>{new_price}</b>\n"
    "\U0001f4c9 <b>Price Dropped:</b> <b>-{pct}%</b> \U0001f389\n"
    "\U0001f4ca <b>Available Stock:</b> <b>{stock}</b>\n\n"
    "{bar}\n{tail}"
)

INVOICE_BEP20_DEFAULT = (
    "\U0001fa99 <b>USDT BEP20</b>\n"
    "<b>Auto-verification enabled</b>\n\n"
    "\u269b\ufe0f Order: <b>${amount} USDT</b>\n\n"
    "\U0001f7e2 Send EXACTLY: <code>{unique_amount}</code> <b>USDT</b>\n"
    "<b>To Address (BEP20 (BNB Smart Chain)):</b>\n"
    "<code>{address}</code>\n\n"
    "\u26a0\ufe0f Send <b>exactly</b> this amount on the <b>BEP20 (BNB Smart Chain)</b> network \u2014 it must match to the <b>last decimal</b>, or your payment will NOT be matched automatically.\n"
    "Expires in <b>{expire_hours} hours</b>.\n\n"
    "Payment ID: <b>#{invoice_id}</b>\n"
    "Payment will be verified and credited automatically."
)

INVOICE_TRC20_DEFAULT = (
    "\U0001fa99 <b>USDT TRC20</b>\n"
    "<b>Auto-verification enabled</b>\n\n"
    "\u269b\ufe0f Order: <b>${amount} USDT</b>\n\n"
    "\U0001f7e2 Send EXACTLY: <code>{unique_amount}</code> <b>USDT</b>\n"
    "<b>To Address (TRC20 (Tron Network)):</b>\n"
    "<code>{address}</code>\n\n"
    "Works with <b>Trust Wallet</b>, Binance withdrawal, or any TRC20 wallet.\n\n"
    "\u26a0\ufe0f Send <b>exactly</b> this amount on the <b>TRC20 (Tron)</b> network \u2014 it must match to the <b>last decimal</b>, or your payment will NOT be matched automatically.\n"
    "Expires in <b>{expire_hours} hours</b>.\n\n"
    "Payment ID: <b>#{invoice_id}</b>\n"
    "Payment will be verified and credited automatically."
)

INVOICE_BINANCE_DEFAULT = (
    "\U0001f7e8 <b>USDT \u2014 Binance Pay</b>\n"
    "<b>Auto-verification enabled</b>\n\n"
    "\u269b\ufe0f Order: <b>${amount} USDT</b>\n\n"
    "<b>How to pay:</b>\n"
    "1\ufe0f\u20e3 Open <b>Binance App</b> \u2192 <b>Pay</b> \u2192 <b>Send</b>.\n"
    "2\ufe0f\u20e3 Send <b>{amount} USDT</b> to Binance ID: <code>{pay_id}</code>\n"
    "3\ufe0f\u20e3 In the <b>Note / Remark</b> field, enter EXACTLY this code:\n"
    "<code>{note_code}</code>\n\n"
    "\u26a0\ufe0f Pay in <b>USDT only</b> \u2014 any other coin will <b>NOT</b> be credited.\n"
    "\u26a0\ufe0f You must include the note code above, or your payment cannot be matched.\n"
    "Expires in <b>{expire_hours} hours</b>.\n\n"
    "Payment ID: <b>#{invoice_id}</b>\n"
    "Payment will be verified and credited automatically."
)

# Bundle details message (Bundle Deals -> a specific bundle). Supports full
# HTML and premium emoji. Product names may contain <tg-emoji> tags; they
# render correctly because this message goes through render_message_template,
# whose final step normalizes those tags.
BUNDLE_DETAILS_DEFAULT = (
    "\U0001f381 <b>{bundle_name}</b>\n\n"
    "{items}\n"
    "\U0001f4b5 Total value: <s>${total_value}</s>\n"
    "\U0001f48e Bundle price: <b>${bundle_price}</b>\n"
    "\U0001f911 You save: <b>${savings}</b>\n\n"
    "\U0001f45b Your balance: <b>{balance} USDT</b>"
)

# Shop (Products) & Bundle Deals screen buttons. Plain text — Telegram button
# labels can't render HTML or premium emoji (use the message templates above for
# premium emoji instead).
BTN_BUNDLE_DEALS_DEFAULT = "\U0001f381 Bundle Deals"
BTN_BACK_TO_PRODUCTS_DEFAULT = "\u2b05\ufe0f Back To Products"
BTN_BACK_TO_BUNDLES_DEFAULT = "\u2b05\ufe0f Back To Bundles"
BTN_BUY_BUNDLE_DEFAULT = "\U0001f49a Buy Bundle (${price})"

# Shop (Products) intro + Bundle Deals list/empty messages. Full HTML + premium
# emoji supported (they go through render_message_template).
SHOP_INTRO_DEFAULT = (
    "\U0001f6cd\ufe0f <b>Products</b>\n\n"
    "Browse products and get instant delivery.\n\n"
    "\u26a1 Delivery: <b>Automatic</b>\n"
    "\U0001f4b3 Payment: <b>Wallet Balance</b>\n\n"
    "Select a product below:"
)
BUNDLES_INTRO_DEFAULT = (
    "\U0001f381 <b>Bundle Deals</b>\n\n"
    "Get more for less \u2014 combo packs at a special price.\n\n"
    "Select a bundle below:"
)
BUNDLES_EMPTY_DEFAULT = (
    "\U0001f381 <b>Bundle Deals</b>\n\n"
    "No bundles available right now.\n\n"
    "Check back soon!"
)


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
    CREATE TABLE IF NOT EXISTS categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        icon TEXT DEFAULT '',
        sort_order INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created TEXT DEFAULT CURRENT_TIMESTAMP
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
    try:
        cur.execute("ALTER TABLE deposits ADD COLUMN note_code TEXT")
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

    # A ticket is the conversation header; every user/admin/system entry lives
    # in this child table. Legacy ticket columns remain intact for backwards
    # compatibility with the Telegram admin screens and old backups.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ticket_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        sender_type TEXT NOT NULL,
        sender_id INTEGER,
        message TEXT NOT NULL,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        read_by_admin INTEGER DEFAULT 0,
        legacy_slot TEXT,
        UNIQUE(ticket_id, legacy_slot)
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
    _safe_add_column("deposits", "reminded", "INTEGER DEFAULT 0")
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
    # Opaque identifier used by public APIs. The existing integer id remains
    # the internal primary/foreign key for every legacy flow. Values are
    # assigned lazily and atomically by database.reseller_db so products
    # created by any existing admin surface also receive an id before they are
    # exposed through the reseller API.
    _safe_add_column("products", "public_id", "TEXT")
    # Product category for the shop menu. NULL = Uncategorized ("📦 Other") --
    # existing products keep working with no manual editing.
    _safe_add_column("products", "category_id", "INTEGER")
    # Optional per-category emoji/icon shown in the bot shop picker.
    _safe_add_column("categories", "icon", "TEXT DEFAULT ''")
    # Indexes for the plain-text product/category search fallback (typing
    # "youtube" with no active conversation state) and the shop menus, which
    # both filter on active=1 / category_id first before any LIKE matching.
    # A leading-wildcard LIKE '%term%' can't use a plain B-tree index for the
    # match itself on either SQLite or Postgres (that would need FTS/trigram
    # indexes, which is out of scope here) -- these speed up the active/
    # category filtering these queries always do first, and cost nothing on
    # small catalogs. IF NOT EXISTS makes this a no-op on every later boot.
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_active_name ON products(active, name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_public_id ON products(public_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_active_name ON categories(active, name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stock_product_sold ON product_stock(product_id, sold)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposits_status_created ON deposits(status, created)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_deposits_user ON deposits(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket_id ON ticket_messages(ticket_id, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ticket_messages_unread ON ticket_messages(ticket_id, read_by_admin, sender_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawal_requests(status)")
        conn.commit()
    except Exception:
        pass

    # Backfill every legacy ticket exactly once. Existing data is preserved;
    # the old columns continue to mirror the initial/latest messages.
    try:
        cur.execute("SELECT id, user_id, message, admin_reply, created, updated FROM tickets")
        for tid, user_id, message, admin_reply, created, updated in cur.fetchall():
            if message:
                cur.execute(
                    "INSERT OR IGNORE INTO ticket_messages(ticket_id,sender_type,sender_id,message,created,read_by_admin,legacy_slot) VALUES(?,?,?,?,?,0,'initial')",
                    (tid, "user", user_id, message, created),
                )
            if admin_reply:
                cur.execute(
                    "INSERT OR IGNORE INTO ticket_messages(ticket_id,sender_type,sender_id,message,created,read_by_admin,legacy_slot) VALUES(?,?,?,?,?,1,'legacy_admin_reply')",
                    (tid, "admin", ADMIN_ID, admin_reply, updated or created),
                )
        conn.commit()
    except Exception:
        pass
    # Payment method shown in admin order search/detail ("Wallet",
    # "USDT BEP20", "USDT TRC20", "Binance Pay"). NULL on legacy rows --
    # those are derived from the funding checkout deposit at display time.
    _safe_add_column("orders", "payment_method", "TEXT")
    # Persistent 24h unresolved-reminder dedupe (survives restarts; NULL = never reminded).
    _safe_add_column("tickets", "last_reminder_at", "TEXT")
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

    # Editable deposit button labels + per-method invoice messages. These show
    # up in the web admin "Message Templates" editor so the owner can rename
    # the buttons and reword each payment invoice (incl. premium emoji).
    for _k, _title, _body in (
        ("btn_bep20", "Button \u2014 BEP20 Method Label", BTN_BEP20_DEFAULT),
        ("btn_trc20", "Button \u2014 TRC20 Method Label", BTN_TRC20_DEFAULT),
        ("btn_binance", "Button \u2014 Binance Pay Method Label", BTN_BINANCE_DEFAULT),
        ("invoice_bep20", "Invoice \u2014 BEP20 Deposit Message", INVOICE_BEP20_DEFAULT),
        ("invoice_trc20", "Invoice \u2014 TRC20 Deposit Message", INVOICE_TRC20_DEFAULT),
        ("invoice_binance", "Invoice \u2014 Binance Pay Deposit Message", INVOICE_BINANCE_DEFAULT),
        ("bundle_details", "Bundle \u2014 Details Message", BUNDLE_DETAILS_DEFAULT),
        ("shop_intro", "Message \u2014 Products (Shop) Screen", SHOP_INTRO_DEFAULT),
        ("bundles_intro", "Message \u2014 Bundle Deals List", BUNDLES_INTRO_DEFAULT),
        ("bundles_empty", "Message \u2014 Bundle Deals (Empty State)", BUNDLES_EMPTY_DEFAULT),
        ("btn_bundle_deals", "Button \u2014 Bundle Deals", BTN_BUNDLE_DEALS_DEFAULT),
        ("btn_back_to_products", "Button \u2014 Back To Products", BTN_BACK_TO_PRODUCTS_DEFAULT),
        ("btn_back_to_bundles", "Button \u2014 Back To Bundles", BTN_BACK_TO_BUNDLES_DEFAULT),
        ("btn_buy_bundle", "Button \u2014 Buy Bundle", BTN_BUY_BUNDLE_DEFAULT),
        ("price_up_broadcast", "Broadcast \u2014 Price Increase Alert", PRICE_UP_BROADCAST_DEFAULT),
        ("price_down_broadcast", "Broadcast \u2014 Price Drop Alert", PRICE_DOWN_BROADCAST_DEFAULT),
        ("btn_checkout_bep20", "Button \u2014 Checkout Pay BEP20", BTN_CHECKOUT_BEP20_DEFAULT),
        ("btn_checkout_trc20", "Button \u2014 Checkout Pay TRC20", BTN_CHECKOUT_TRC20_DEFAULT),
        ("btn_checkout_binance", "Button \u2014 Checkout Pay Binance", BTN_CHECKOUT_BINANCE_DEFAULT),
    ):
        cur.execute(
            "INSERT OR IGNORE INTO message_templates(key, title, body) VALUES(?,?,?)",
            (_k, _title, _body),
        )

    
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
    # Master kill switch for all protected reseller API routes. Read from the
    # database on every request; no process-local cache is used.
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('reseller_api_global_enabled', '1')")

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
            "💼 <b>My Wallet</b>\n\n🆔 <b>User ID:</b> <code>{user_id}</code>\n👤 <b>Name:</b> {name}\n🔗 <b>Username:</b> {username}\n\n💰 <b>Available Balance:</b> <b>{balance} USDT</b>\n📥 <b>Total Deposited:</b> <b>{deposited} USDT</b>\n🧾 <b>Paid Deposits:</b> <b>{deposits}</b>\n\n🛒 <b>Total Orders:</b> <b>{orders}</b>\n💸 <b>Total Spent:</b> <b>{spent} USDT</b>\n\n<b>Referral Earnings:</b> <b>{referral_earnings} USDT</b>\n⏳ <b>Pending Referral:</b> <b>{referral_pending} USDT</b>\n⭐ <b>Loyalty Points:</b> <b>{loyalty_points}</b>\n🏆 <b>VIP Rank:</b> <b>{vip_rank}</b>\n\nUse Deposit to add balance, then pay directly from wallet during checkout."
        ),
        "support_prompt": (
            "Support Ticket Message",
            "<b>Create Support Ticket</b>\n\nSend your issue/message now.\n\nExample:\nPayment done but balance not added."
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

    # Seed the default templates ONCE per process. This used to run 15
    # INSERT OR IGNORE statements plus a commit on every /start, every message
    # and every callback -- a large pointless write load on every interaction.
    global _TEMPLATES_SEEDED
    if not _TEMPLATES_SEEDED:
        for key, (title, body) in default_templates.items():
            cur.execute(
                "INSERT OR IGNORE INTO message_templates(key, title, body) VALUES(?,?,?)",
                (key, title, body)
            )
        _TEMPLATES_SEEDED = True

    # Idempotency table for Web Admin stock additions.
    # Each Add Stock click generates a UUID (op_id) on the frontend.
    # The backend records completed op_ids here and ignores replays.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock_op_log(
        op_id TEXT PRIMARY KEY,
        product_id INTEGER NOT NULL,
        added INTEGER NOT NULL,
        available INTEGER NOT NULL,
        admin_id INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Auto-cleanup: keep only the last 7 days of op records (they are only
    # needed to deduplicate in-flight retries, not for permanent audit).
    # created is stored as TEXT (ISO-8601) so we cast it for the comparison.
    try:
        from database.pgcompat import use_postgres as _use_pg
    except Exception:
        def _use_pg(): return False
    if _use_pg():
        cur.execute(
            "DELETE FROM stock_op_log"
            " WHERE created::timestamptz < NOW() - INTERVAL '7 days'"
        )
    else:
        cur.execute(
            "DELETE FROM stock_op_log"
            " WHERE created < datetime('now', '-7 days')"
        )

    conn.commit()

    # Base tables now exist -- (re)apply the gift/warranty migrations that
    # cannot safely run at import time on a fresh database.
    _apply_gift_warranty_migrations()
    _apply_m365_migrations()

    # Reseller system tables (Phase 1 — additive only; no existing table or
    # flow is touched). Lives in database/reseller_db.py; imported lazily to
    # keep module import order simple.
    from database.reseller_db import ensure_reseller_tables, backfill_product_public_ids
    ensure_reseller_tables()
    backfill_product_public_ids()



_TEMPLATES_SEEDED = False


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
            "✅ <b>Payment Successful!</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🧾 <b>Order ID:</b> <b>#{order_id}</b>\n\n📦 <b>{product_name}</b>\n\n🔑 <b>Code(s):</b>\n<code>{delivery}</code>\n\n━━━━━━━━━━━━━━━━━��━━\n\n💰 <b>Paid:</b> <b>${paid}</b>\n💼 <b>Remaining Balance:</b> <b>${balance}</b>\n\n━━━━━━━━━━━━━━━━━━━━\nThank you for your purchase! 🎉\n\nUse /orders to view your order history."
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

    if not items:
        return 0

    # Bulk insert: encrypt all codes first, then insert in one executemany
    # inside a single transaction. For 1000+ codes this is dramatically faster
    # than the previous one-INSERT-per-code loop.
    encrypted_rows = [(product_id, encrypt_text(item), 0) for item in items]
    cur.executemany(
        "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,?)",
        encrypted_rows
    )

    # Set stock to the REAL unsold count. This used to be `stock=len(items)`,
    # which discarded any existing unsold stock on every restock.
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=0", (product_id,))
    _avail = cur.fetchone()
    cur.execute("UPDATE products SET stock=? WHERE id=?",
                (int(_avail[0]) if _avail else len(items), product_id))
    # Re-arm low-stock alerts after restock so the next drop alerts again.
    cur.execute("UPDATE products SET low_stock_notified=0 WHERE id=?", (product_id,))
    conn.commit()
    return len(items)


def add_stock_file_item(product_id: int, delivery_text: str):
    """Add ONE stock item whose delivery text is an entire uploaded file.

    Unlike add_stock_items(), this never splits on newlines or ';;', so a
    multi-line account block (email login, passwords, activation link,
    notes...) is stored -- and later delivered -- exactly as uploaded.
    Returns the available (unsold) stock count after the insert.
    """
    item = str(delivery_text).strip()
    if not item:
        return 0
    cur.execute(
        "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,0)",
        (product_id, encrypt_text(item))
    )
    cur.execute("SELECT COUNT(*) FROM product_stock WHERE product_id=? AND sold=0", (product_id,))
    _avail = cur.fetchone()
    available = int(_avail[0]) if _avail else 1
    cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
    # Re-arm low-stock alerts after restock so the next drop alerts again.
    cur.execute("UPDATE products SET low_stock_notified=0 WHERE id=?", (product_id,))
    conn.commit()
    return available


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
    """Atomically claim ONE unsold code. See reserve_stock_items for why this
    uses a conditional UPDATE rather than SELECT ... FOR UPDATE (FIX44).
    """
    stamp = now_text()
    for _attempt in range(5):
        cur.execute(
            "SELECT id, delivery FROM product_stock "
            "WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT 10",
            (product_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        for stock_id, delivery in rows:
            cur.execute(
                "UPDATE product_stock SET sold=1, sold_to=?, sold_at=? "
                "WHERE id=? AND sold=0",
                (user_id, stamp, stock_id),
            )
            if cur.rowcount == 1:
                try:
                    conn.commit()
                except Exception:
                    pass
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


def _parse_account_blocks(accounts_text: str, delimiter: str = ":"):
    """Parse pasted account stock into a list of (email, password, twofa).

    delimiter:
      - a single-char separator (":", "|", ",") or the names "tab"/"space":
        one account per line, fields split by that separator. The password may
        itself contain the separator -- the email is taken from before the
        first separator and the 2FA key from after the last, so only the
        middle (password) can hold extra separators.
      - "lines": accounts are separated by blank lines (or, if none, grouped
        every 3 lines), with email / password / 2FA on their own lines.
    """
    accounts = []
    text = (accounts_text or "").replace("\r\n", "\n").replace("\r", "\n")

    if delimiter == "lines":
        lines = [ln.strip() for ln in text.split("\n")]
        blocks, current = [], []
        for ln in lines:
            if ln == "":
                if current:
                    blocks.append(current)
                    current = []
            else:
                current.append(ln)
        if current:
            blocks.append(current)
        # Everything pasted with no blank lines -> group every 3 lines.
        if len(blocks) == 1 and len(blocks[0]) > 3 and len(blocks[0]) % 3 == 0:
            flat = blocks[0]
            blocks = [flat[i:i + 3] for i in range(0, len(flat), 3)]
        for b in blocks:
            email = b[0] if len(b) > 0 else ""
            password = b[1] if len(b) > 1 else ""
            twofa = b[2] if len(b) > 2 else ""
            if email:
                accounts.append((email, password, twofa))
        return accounts

    delim_map = {"colon": ":", "pipe": "|", "comma": ",", "tab": "\t", "space": " "}
    delim = delim_map.get(delimiter, delimiter or ":")

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(delim)]
        if len(parts) >= 3:
            email, twofa = parts[0], parts[-1]
            password = delim.join(parts[1:-1]).strip()
        elif len(parts) == 2:
            email, password, twofa = parts[0], parts[1], ""
        else:
            email, password, twofa = parts[0], "", ""
        if email:
            accounts.append((email, password, twofa))
    return accounts


def format_account_delivery(email: str, password: str, twofa: str) -> str:
    """Build the multi-line block a customer receives for one account.

    Line 1 = email, line 2 = password, line 3 = 2FA key.
    """
    lines = [f"\U0001f4e7 Email: {email}", f"\U0001f511 Password: {password}"]
    if twofa:
        lines.append(f"\U0001f510 2FA Key: {twofa}")
    return "\n".join(lines)


def add_account_stock_items(product_id: int, accounts_text: str, delimiter: str = ":") -> int:
    """Add account-style stock (email + password + 2FA). Each account is stored
    as ONE stock unit whose delivery text keeps the three fields on separate
    lines, so the customer receives them exactly that way."""
    accounts = _parse_account_blocks(accounts_text, delimiter)
    if not accounts:
        return 0

    # Bulk insert: encrypt all account blocks first, then insert in one
    # executemany call. Same speed-up as add_stock_items().
    encrypted_rows = [
        (product_id, encrypt_text(format_account_delivery(email, password, twofa)), 0)
        for email, password, twofa in accounts
    ]
    cur.executemany(
        "INSERT INTO product_stock(product_id, delivery, sold) VALUES(?,?,?)",
        encrypted_rows
    )
    available = get_available_stock_count(product_id)
    cur.execute("UPDATE products SET stock=? WHERE id=?", (available, product_id))
    # Re-arm low-stock alerts after restock so the next drop alerts again.
    cur.execute("UPDATE products SET low_stock_notified=0 WHERE id=?", (product_id,))
    conn.commit()
    return len(accounts)


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


SUPPORT_RESOLUTION_MESSAGE = "Your support has been resolved. If you have any query contact admin @prahladvarma"


def append_ticket_message(ticket_id: int, sender_type: str, message: str,
                          sender_id=None, read_by_admin=None) -> int:
    """Append one message to an existing conversation without creating a ticket."""
    sender_type = str(sender_type or "").strip().lower()
    if sender_type not in ("user", "admin", "system"):
        raise ValueError("invalid sender type")
    message = str(message or "").strip()
    if not message:
        raise ValueError("empty message")
    cur.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,))
    if cur.fetchone() is None:
        raise ValueError("ticket not found")
    if read_by_admin is None:
        read_by_admin = 0 if sender_type == "user" else 1
    stamp = now_text()
    cur.execute(
        "INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,message,created,read_by_admin) VALUES(?,?,?,?,?,?)",
        (ticket_id, sender_type, sender_id, encrypt_text(message), stamp,
         1 if read_by_admin else 0),
    )
    message_id = cur.lastrowid
    if sender_type == "user":
        # A customer follow-up never resets the status: once an admin has
        # answered, the ticket stays UNRESOLVED until someone closes it.
        cur.execute("UPDATE tickets SET updated=? WHERE id=? AND status<>'closed'",
                    (stamp, ticket_id))
    elif sender_type == "admin":
        cur.execute("UPDATE tickets SET admin_reply=?, status='answered', updated=? WHERE id=? AND status<>'closed'",
                    (encrypt_text(message), stamp, ticket_id))
    else:
        cur.execute("UPDATE tickets SET updated=? WHERE id=?", (stamp, ticket_id))
    conn.commit()
    return message_id


def create_ticket(user_id: int, subject: str, message: str) -> int:
    cur.execute(
        "INSERT INTO tickets(user_id, subject, message, status) VALUES(?,?,?, 'open')",
        (user_id, encrypt_text(subject), encrypt_text(message))
    )
    conn.commit()
    ticket_id = cur.lastrowid
    append_ticket_message(ticket_id, "user", message, user_id, read_by_admin=False)
    return ticket_id


def add_user_ticket_reply(ticket_id: int, user_id: int, message: str) -> int:
    cur.execute("SELECT status FROM tickets WHERE id=? AND user_id=?", (ticket_id, user_id))
    row = cur.fetchone()
    if row is None:
        raise ValueError("ticket not found")
    if str(row[0]) == "closed":
        raise ValueError("ticket closed")
    return append_ticket_message(ticket_id, "user", message, user_id, read_by_admin=False)


def get_ticket_messages(ticket_id: int, limit: int = 100, before_id=None):
    limit = max(1, min(int(limit or 100), 100))
    sql = "SELECT id,ticket_id,sender_type,sender_id,message,created,read_by_admin FROM ticket_messages WHERE ticket_id=?"
    params = [ticket_id]
    if before_id:
        sql += " AND id<?"
        params.append(int(before_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit + 1)
    cur.execute(sql, tuple(params))
    rows = [list(r) for r in cur.fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row[4] = decrypt_text(row[4])
    rows.reverse()
    return [tuple(r) for r in rows], has_more


def mark_ticket_read(ticket_id: int):
    cur.execute("UPDATE ticket_messages SET read_by_admin=1 WHERE ticket_id=? AND sender_type='user' AND read_by_admin=0",
                (ticket_id,))
    conn.commit()


def get_ticket_conversations(status="all", limit=200):
    where = ""
    if status == "open":
        where = "WHERE t.status='open'"
    elif status == "answered":
        where = "WHERE t.status='answered'"
    elif status == "closed":
        where = "WHERE t.status='closed'"
    elif status == "unresolved":
        # Admin/system has responded at least once but the ticket is not
        # closed. Derived from message history (not the status string), so
        # legacy rows and edge cases are included.
        where = ("WHERE t.status<>'closed' AND EXISTS (SELECT 1 FROM ticket_messages tm "
                 "WHERE tm.ticket_id=t.id AND tm.sender_type IN ('admin','system'))")
    elif status == "unread":
        where = "WHERE COALESCE(ms.unread_count,0)>0"
    cur.execute("""
        SELECT t.id,t.user_id,u.first_name,u.username,t.subject,t.status,t.created,t.updated,
               lm.message,lm.sender_type,lm.created,
               COALESCE(ms.message_count,0),COALESCE(ms.unread_count,0)
        FROM tickets t
        LEFT JOIN users u ON u.user_id=t.user_id
        LEFT JOIN (
            SELECT ticket_id,COUNT(*) message_count,
                   SUM(CASE WHEN sender_type='user' AND read_by_admin=0 THEN 1 ELSE 0 END) unread_count,
                   MAX(id) last_id
            FROM ticket_messages GROUP BY ticket_id
        ) ms ON ms.ticket_id=t.id
        LEFT JOIN ticket_messages lm ON lm.id=ms.last_id
        """ + where + " ORDER BY COALESCE(lm.created,t.updated,t.created) DESC, t.id DESC LIMIT ?",
        (max(1, min(int(limit or 200), 500)),))
    out = []
    for r in cur.fetchall():
        r = list(r)
        r[4] = decrypt_text(r[4])
        r[8] = decrypt_text(r[8]) if r[8] is not None else ""
        out.append(tuple(r))
    return out


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


def count_user_tickets(user_id: int) -> int:
    cur.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (user_id,))
    try:
        return int(cur.fetchone()[0] or 0)
    except Exception:
        return 0


def get_user_tickets_page(user_id: int, limit: int = 5, offset: int = 0):
    """Paginated My Tickets list — ALL statuses, newest first."""
    cur.execute(
        "SELECT id, subject, status, created, updated FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, max(1, int(limit)), max(0, int(offset)))
    )
    out = []
    for r in cur.fetchall():
        r = list(r)
        r[1] = decrypt_text(r[1])
        out.append(tuple(r))
    return out


def get_tickets_due_for_reminder(hours: int = 24):
    """Unresolved tickets (an admin has replied, ticket not closed) whose last
    activity AND last reminder are both at least `hours` old.

    Timestamps are stored as 'YYYY-MM-DD HH:MM:SS' text in the store timezone,
    so lexicographic comparison is correct on both SQLite and Postgres.
    """
    cutoff = (ist_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        SELECT t.id, t.user_id, u.first_name, u.username, t.subject, t.status, t.updated
        FROM tickets t
        LEFT JOIN users u ON u.user_id = t.user_id
        WHERE t.status <> 'closed'
          AND EXISTS (SELECT 1 FROM ticket_messages tm
                      WHERE tm.ticket_id = t.id AND tm.sender_type IN ('admin','system'))
          AND COALESCE(t.updated, t.created) <= ?
          AND (t.last_reminder_at IS NULL OR t.last_reminder_at <= ?)
        ORDER BY t.id ASC
        """,
        (cutoff, cutoff)
    )
    out = []
    for r in cur.fetchall():
        r = list(r)
        r[4] = decrypt_text(r[4])
        out.append(tuple(r))
    return out


def mark_ticket_reminded(ticket_id: int):
    """Persist the reminder time so restarts/jobs never duplicate reminders."""
    cur.execute("UPDATE tickets SET last_reminder_at=? WHERE id=?", (now_text(), ticket_id))
    conn.commit()


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
    cur.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))
    row = cur.fetchone()
    if row is None:
        return False
    if str(row[0]) == "closed":
        return False
    append_ticket_message(ticket_id, "admin", admin_reply, ADMIN_ID, read_by_admin=True)
    return True


def close_ticket(ticket_id: int):
    stamp = now_text()
    cur.execute("UPDATE tickets SET status='closed', admin_reply=?, updated=? WHERE id=? AND status<>'closed'",
                (encrypt_text(SUPPORT_RESOLUTION_MESSAGE), stamp, ticket_id))
    changed = cur.rowcount > 0
    if changed:
        cur.execute(
            "INSERT OR IGNORE INTO ticket_messages(ticket_id,sender_type,sender_id,message,created,read_by_admin,legacy_slot) VALUES(?,?,?,?,?,1,'resolution')",
            (ticket_id, "system", ADMIN_ID, encrypt_text(SUPPORT_RESOLUTION_MESSAGE), stamp),
        )
    conn.commit()
    return changed



def _release_claims(stock_ids):
    """Undo our own claims when an order cannot be fully filled."""
    for sid in stock_ids:
        try:
            cur.execute(
                "UPDATE product_stock SET sold=0, sold_to=NULL, sold_at=NULL WHERE id=?",
                (sid,),
            )
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        pass


def reserve_stock_items(product_id: int, user_id: int, quantity: int):
    """Atomically claim `quantity` unsold codes. Returns None if it cannot.

    FIX43 - the old version did SELECT ... WHERE sold=0 and then a blind
    UPDATE ... WHERE id=?. Two buyers checking out at the same moment both
    passed the SELECT and both got handed the SAME codes.

    Note we deliberately do NOT use SELECT ... FOR UPDATE SKIP LOCKED:
    pgcompat opens Postgres with autocommit=True, so the row locks would be
    released the instant the SELECT statement finished and would protect
    nothing. Instead every row is claimed with a conditional single-statement
    UPDATE (... WHERE id=? AND sold=0), which is atomic on both backends.
    rowcount==1 means this thread won the row; rowcount==0 means somebody
    else took it and we move on to the next candidate.
    """
    quantity = int(quantity)
    if quantity <= 0:
        return None

    claimed = []  # list of (stock_id, delivery)
    stamp = now_text()
    stalls = 0

    for _attempt in range(12):
        if len(claimed) >= quantity:
            break
        need = quantity - len(claimed)
        # Over-fetch generously: under contention most candidates will already
        # have been taken by another thread by the time we try to claim them.
        cur.execute(
            "SELECT id, delivery FROM product_stock "
            "WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT ?",
            (product_id, need * 5 + 25),
        )
        rows = cur.fetchall()
        if not rows:
            break

        already = {sid for sid, _d in claimed}
        progressed = False
        for stock_id, delivery in rows:
            if len(claimed) >= quantity:
                break
            if stock_id in already:
                continue
            cur.execute(
                "UPDATE product_stock SET sold=1, sold_to=?, sold_at=? "
                "WHERE id=? AND sold=0",
                (user_id, stamp, stock_id),
            )
            if cur.rowcount == 1:
                claimed.append((stock_id, delivery))
                progressed = True

        if progressed:
            stalls = 0
            continue
        # Nothing claimed this round. On SQLite the competing writers' UPDATEs
        # are not committed yet, so our SELECT still sees those rows as sold=0
        # while our conditional UPDATE matches nothing. Backing off briefly
        # lets them commit and gives us a genuinely fresh candidate list.
        # Giving up immediately here (the first cut of this fix did) told
        # customers "out of stock" while plenty of stock existed.
        stalls += 1
        if stalls >= 4:
            break
        _threading.Event().wait(0.05 * stalls)

    if len(claimed) < quantity:
        _release_claims([sid for sid, _d in claimed])
        return None

    try:
        conn.commit()
    except Exception:
        pass
    return [decrypt_text(d) for _sid, d in claimed]


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


def validate_coupon(code: str, user_id: int = None):
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

    # FIX60: per-user redemption cap. Without this only the GLOBAL used_count
    # was enforced, so a single user could redeem the same coupon over and
    # over -- draining a limited coupon, or unlimited times when max_uses=0.
    if user_id is not None:
        try:
            per_user = int(get_setting_value("coupon_max_uses_per_user", "1") or 1)
        except Exception:
            per_user = 1
        if per_user > 0 and get_coupon_user_uses(code, user_id) >= per_user:
            return None, "You have already used this coupon"

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


_COUPON_REDEMPTIONS_READY = False


def _ensure_coupon_redemptions():
    """Lazily create the redemption log (mirrors the ensure_canva_table pattern).

    Created lazily rather than at import time so it never runs DDL before
    init_db() has set the database up.
    """
    global _COUPON_REDEMPTIONS_READY
    if _COUPON_REDEMPTIONS_READY:
        return
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupon_redemptions(
        code TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        redeemed_at TEXT
    )""")
    conn.commit()
    _COUPON_REDEMPTIONS_READY = True


def get_coupon_user_uses(code: str, user_id: int) -> int:
    """How many times this specific user has already redeemed this coupon."""
    _ensure_coupon_redemptions()
    cur.execute(
        "SELECT COUNT(*) FROM coupon_redemptions WHERE code=? AND user_id=?",
        (code.upper().strip(), user_id),
    )
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def mark_coupon_used(code: str, user_id: int = None):
    code = code.upper().strip()
    cur.execute("UPDATE coupons SET used_count=used_count+1 WHERE code=?", (code,))
    if user_id is not None:
        _ensure_coupon_redemptions()
        cur.execute(
            "INSERT INTO coupon_redemptions(code, user_id, redeemed_at) VALUES(?,?,?)",
            (code, user_id, now_text()),
        )
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


VIP_TIERS = [(0.0, "Bronze"), (100.0, "Silver"), (500.0, "Gold"), (2000.0, "Platinum")]


def add_loyalty_points(user_id: int, amount_spent: float):
    """Award loyalty points based on the loyalty_percent setting.

    Previously a stub returning 0.0, so the points shown in the wallet/profile
    screens never moved.
    """
    try:
        percent = float(get_setting("loyalty_percent", "0") or 0)
        points = round(float(amount_spent) * percent / 100.0, 6)
        if points <= 0:
            return 0.0
        cur.execute(
            "UPDATE users SET loyalty_points=COALESCE(loyalty_points,0)+? WHERE user_id=?",
            (points, user_id),
        )
        conn.commit()
        update_vip_rank(user_id)
        return points
    except Exception:
        return 0.0


def update_vip_rank(user_id: int):
    """Recompute VIP rank from lifetime spend. Previously always 'Bronze'."""
    try:
        cur.execute("SELECT COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        spent = float(row[0] or 0) if row else 0.0
        rank = "Bronze"
        for threshold, name in VIP_TIERS:
            if spent >= threshold:
                rank = name
        cur.execute("UPDATE users SET vip_rank=? WHERE user_id=?", (rank, user_id))
        conn.commit()
        return rank
    except Exception:
        return "Bronze"

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
        "flash_sale_broadcast": "🔥 FLASH SALE!\n\n📦 {product}\n\n💰 Was: {old_price}\n⚡ Now: {new_price}\n🎁 Discount: {discount}\n\n{validity}\n\nBuy before it ends 👇",
        "restock_alert": "🔔 Back In Stock!\n\n📦 {product}\n📊 Available: {stock}\n\nTap below to buy now 👇",
        "deposit_success": "✅ Deposit Confirmed\n\nAmount: {amount} USDT\n\nYour wallet balance has been updated."
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
        else:
            pct = round((new_price - old_price) / old_price * 100) if old_price > 0 else 0

        bar = "\u2501" * 18
        if available and int(available) > 0:
            tail = "\U0001f6cd <i>Open the Shop in the bot to grab it now!</i>"
        else:
            tail = "\u23f3 <i>Out of stock \u2014 restocking soon!</i> \U0001f514"
        msg = render_message_template(
            "price_down_broadcast" if new_price < old_price else "price_up_broadcast",
            PRICE_DOWN_BROADCAST_DEFAULT if new_price < old_price else PRICE_UP_BROADCAST_DEFAULT,
            product_name=name,
            old_price=f"${old_price:.2f}",
            new_price=f"${new_price:.2f}",
            pct=pct,
            stock=available,
            bar=bar,
            tail=tail,
        )

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



def _admin_user_ids():
    """User ids whose purchases count as ADMIN purchases (owner + promoted
    admins). Their orders are reported separately from customer revenue."""
    ids = set()
    try:
        from config import ADMIN_ID
        if ADMIN_ID:
            ids.add(int(ADMIN_ID))
    except Exception:
        pass
    try:
        cur.execute("SELECT user_id FROM admins")
        for row in cur.fetchall():
            try:
                ids.add(int(row[0]))
            except Exception:
                pass
    except Exception:
        pass
    return ids


def smart_dashboard_stats():
    reset_at = get_setting("revenue_reset_at", "") or "1970-01-01 00:00:00"
    admin_ids = sorted(_admin_user_ids())
    _ids = ",".join(str(i) for i in admin_ids)
    not_admin = (" AND user_id NOT IN (%s)" % _ids) if _ids else ""
    only_admin = (" AND user_id IN (%s)" % _ids) if _ids else " AND 1=0"
    try:
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders "
                    "WHERE created >= ? AND created >= ?" + not_admin, (today_prefix(), reset_at))
        today_revenue, today_orders = cur.fetchone()
    except Exception:
        today_revenue, today_orders = 0, 0

    try:
        cur.execute("SELECT COUNT(*) FROM users WHERE joined >= ?", (today_prefix(),))
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
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created >= ?" + not_admin, (reset_at,))
        total_revenue, total_orders = cur.fetchone()
    except Exception:
        total_revenue, total_orders = 0, 0

    try:
        cur.execute("SELECT COALESCE(SUM(price),0), COUNT(*) FROM orders WHERE created >= ?" + only_admin, (reset_at,))
        admin_revenue, admin_orders = cur.fetchone()
    except Exception:
        admin_revenue, admin_orders = 0, 0

    return {
        "today_revenue": float(today_revenue or 0),
        "today_orders": int(today_orders or 0),
        "new_users": int(new_users or 0),
        "open_tickets": int(open_tickets or 0),
        "low_stock": int(low_stock or 0),
        "total_revenue": float(total_revenue or 0),
        "total_orders": int(total_orders or 0),
        "admin_revenue": float(admin_revenue or 0),
        "admin_orders": int(admin_orders or 0),
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
        wrote_data = False
        db_file = Path(DB_PATH)
        if db_file.exists():
            z.write(db_file, db_file.name)
            wrote_data = True

        # On Postgres there is no .db file -- embed a portable JSON snapshot
        # instead, otherwise the zip contained no data at all.
        if not wrote_data:
            from utils.backup import create_backup
            snap = Path(create_backup())
            z.write(snap, snap.name)

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

        member = db_members[0]
        # Zip-slip guard: a crafted member like ../../etc/x.db previously
        # let an uploaded backup write anywhere on the filesystem.
        safe_name = Path(member).name
        if not safe_name or safe_name.startswith("."):
            return False, "Unsafe filename inside backup."

        extract_root = Path("restore_extract").resolve()
        extract_root.mkdir(parents=True, exist_ok=True)
        extracted = extract_root / safe_name
        with z.open(member) as src, open(extracted, "wb") as dst:
            dst.write(src.read())
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

_GIFT_WARRANTY_READY = False


def _apply_gift_warranty_migrations():
    """Create the gift/warranty tables and seed their settings rows.

    This block used to run at import time. On a brand new database that
    crashed the whole process, because db.py is imported long before
    init_db() creates the base tables -- so the INSERT INTO settings hit a
    table that did not exist yet. It is fully idempotent, so init_db() calls
    it after creating the base tables, and the import-time call below stays
    as a best-effort path for databases that are already initialised.
    """
    global _GIFT_WARRANTY_READY
    if _GIFT_WARRANTY_READY:
        return

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

    for _k, _v in (
        ("warranty_enabled", "1"),
        ("warranty_hours", "24"),
        ("warranty_max_week", "3"),
        ("gift_min_amount", "1"),
        ("gift_max_amount", "500"),
    ):
        cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (_k, _v))

    conn.commit()
    _GIFT_WARRANTY_READY = True


try:
    _apply_gift_warranty_migrations()
except Exception:
    # Fresh database: base tables do not exist yet. init_db() will create
    # them and then re-run these migrations.
    pass


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
            return None
        conn.commit()

    code = generate_gift_code()
    try:
        cur.execute(
            "INSERT INTO gift_codes(code, amount, created_by, created) VALUES(?,?,?,?)",
            (code, amount, user_id, now_text())
        )
        conn.commit()
    except Exception:
        # conn.rollback() is a no-op under Postgres autocommit, so the balance
        # deduction above is already durable. Refund explicitly instead of
        # silently keeping the customer money with no gift code issued.
        if not free:
            try:
                cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
                conn.commit()
            except Exception:
                pass
        return None

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
        return False, "This gift code has already been redeemed."
    conn.commit()

    try:
        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        conn.commit()
    except Exception:
        # The claim above is already durable (no-op rollback), so release it
        # explicitly -- otherwise the code is burned without crediting anyone.
        try:
            cur.execute(
                "UPDATE gift_codes SET redeemed_by=NULL, redeemed_at=NULL WHERE code=? AND redeemed_by=?",
                (code, user_id)
            )
            conn.commit()
        except Exception:
            pass
        return False, "Could not credit your balance. Please try again."

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

_M365_MIGRATIONS_READY = False


def _apply_m365_migrations():
    """Create the m365_sessions table and seed its settings row.

    Deferred for the same reason as the gift/warranty migrations: this used
    to run at import time and crashed a brand new database, because the
    `settings` table does not exist until init_db() runs. Idempotent.
    """
    global _M365_MIGRATIONS_READY
    if _M365_MIGRATIONS_READY:
        return

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

    _M365_MIGRATIONS_READY = True


try:
    _apply_m365_migrations()
except Exception:
    # Fresh database: init_db() will create the base tables and re-run this.
    pass


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


# ---------------- Admin order/user search (shared by bot admin + web admin) ----------------

def payment_method_label(value):
    """Display label for a stored payment-method key or label. New orders store
    the label directly; legacy checkout deposits stored the key."""
    v = str(value or "").strip()
    if not v:
        return "Wallet"
    return {
        "bep20": "USDT BEP20",
        "trc20": "USDT TRC20",
        "binance": "Binance Pay",
        "wallet": "Wallet",
    }.get(v.lower(), v)


def get_order_payment_method(order_id, user_id=None, product_id=None, stored=None):
    """Payment method label for one order. New orders carry it in
    orders.payment_method; legacy rows (NULL) are recovered from the paid
    direct-checkout deposit that funded the order (wallet purchases and
    bundles have none, so they show Wallet)."""
    if stored:
        return payment_method_label(stored)
    if user_id and product_id:
        try:
            cur.execute(
                "SELECT method FROM deposits WHERE user_id=? AND checkout_product_id=? AND status='paid' ORDER BY id DESC LIMIT 1",
                (user_id, product_id),
            )
            row = cur.fetchone()
            if row and row[0]:
                return payment_method_label(row[0])
        except Exception:
            pass
    return "Wallet"


def get_order_status_label(order_id):
    """Derived order status. Orders complete instantly on creation and refunded
    orders are deleted by design, so the only extra state is a warranty
    replacement linked to the order."""
    try:
        cur.execute(
            "SELECT status FROM replacements WHERE order_id=? ORDER BY id DESC LIMIT 1",
            (int(order_id),),
        )
        row = cur.fetchone()
    except Exception:
        row = None
    if row and row[0] in ("approved", "auto"):
        return "\U0001f6e1 Replaced"
    if row and row[0] == "pending":
        return "\U0001f6e1 Warranty review"
    return "\u2705 Completed"


def _order_search_clauses(order_id=None, user_id=None, username=None, product=None, date_from=None, date_to=None):
    """Parameterized WHERE for admin order search. Raises ValueError on a bad date."""
    where = []
    params = []
    if order_id is not None:
        where.append("o.id=?")
        params.append(int(order_id))
    if user_id is not None:
        where.append("o.user_id=?")
        params.append(int(user_id))
    if username:
        uname = str(username).strip().lstrip("@")
        if uname:
            where.append("LOWER(u.username)=LOWER(?)")
            params.append(uname)
    if product:
        where.append("o.product_name LIKE ?")
        params.append(f"%{product}%")
    for value, op in ((date_from, ">="), (date_to, "<=")):
        if value:
            d = str(value).strip()
            if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise ValueError("invalid date")
            where.append(f"o.created {op} ?")
            params.append(d + (" 00:00:00" if op == ">=" else " 23:59:59"))
    return (" WHERE " + " AND ".join(where), params) if where else ("", params)


def search_orders(order_id=None, user_id=None, username=None, product=None, date_from=None, date_to=None, limit=5, offset=0):
    """Paginated admin order search. All filtering runs in SQL with
    LIMIT/OFFSET -- the whole table is never loaded into memory."""
    where, params = _order_search_clauses(order_id, user_id, username, product, date_from, date_to)
    cur.execute(
        "SELECT o.id, o.user_id, o.product_name, o.price, o.created, o.payment_method, o.product_id, "
        "u.username, u.first_name "
        "FROM orders o LEFT JOIN users u ON u.user_id=o.user_id"
        + where + " ORDER BY o.id DESC LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    )
    return cur.fetchall()


def count_search_orders(order_id=None, user_id=None, username=None, product=None, date_from=None, date_to=None):
    """Total matches for the same filters (drives pagination)."""
    where, params = _order_search_clauses(order_id, user_id, username, product, date_from, date_to)
    cur.execute(
        "SELECT COUNT(*) FROM orders o LEFT JOIN users u ON u.user_id=o.user_id" + where,
        params,
    )
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def get_order_admin_detail(order_id):
    """One order + its owner for admin views (bot + web)."""
    cur.execute(
        "SELECT o.id, o.user_id, o.product_name, o.price, o.delivery, o.created, "
        "o.payment_method, o.product_id, u.username, u.first_name "
        "FROM orders o LEFT JOIN users u ON u.user_id=o.user_id WHERE o.id=?",
        (int(order_id),),
    )
    return cur.fetchone()


def find_user_by_id(user_id):
    """Read-only lookup by the canonical Telegram user ID. Never creates rows."""
    cur.execute(
        "SELECT user_id, first_name, username, balance, blocked, joined FROM users WHERE user_id=?",
        (int(user_id),),
    )
    return cur.fetchone()


def find_users_by_username(query, limit=10):
    """Exact case-insensitive username match first, then a partial match list.
    Users without a username are reachable only by their Telegram ID."""
    uname = str(query or "").strip().lstrip("@")
    if not uname:
        return []
    cur.execute(
        "SELECT user_id, first_name, username, balance, blocked, joined FROM users "
        "WHERE LOWER(username)=LOWER(?) LIMIT 1",
        (uname,),
    )
    row = cur.fetchone()
    if row:
        return [row]
    cur.execute(
        "SELECT user_id, first_name, username, balance, blocked, joined FROM users "
        "WHERE username LIKE ? AND username != 'No username' ORDER BY joined DESC LIMIT ?",
        (f"%{uname}%", int(limit)),
    )
    return cur.fetchall()


def get_user_overview(user_id):
    """Account row + order/deposit/referral aggregates for the admin user card.
    Read-only: searching must never create a user row."""
    cur.execute(
        "SELECT user_id, first_name, username, balance, blocked, joined, "
        "COALESCE(referral_earnings,0), COALESCE(referral_pending,0), "
        "COALESCE(loyalty_points,0), COALESCE(vip_rank,'Bronze') FROM users WHERE user_id=?",
        (int(user_id),),
    )
    user = cur.fetchone()
    if not user:
        return None
    # Plain tuple: safe on both SQLite and the Postgres pgcompat row wrapper
    # (whose __getitem__ supports int/str keys but NOT slices).
    user = tuple(user)
    cur.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM orders WHERE user_id=?", (int(user_id),))
    orders_count, total_spent = cur.fetchone()
    cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='paid'", (int(user_id),))
    dep_count, dep_total = cur.fetchone()
    return {
        "user": user,
        "orders_count": int(orders_count or 0),
        "total_spent": float(total_spent or 0),
        "deposits_count": int(dep_count or 0),
        "total_deposited": float(dep_total or 0),
        "referral_earnings": float(user[6] or 0),
        "referral_pending": float(user[7] or 0),
        "loyalty_points": float(user[8] or 0),
        "vip_rank": str(user[9] or "Bronze"),
    }


def get_user_deposits_page(user_id, limit=5, offset=0):
    """Paginated deposit history for one user (newest first) + total count."""
    cur.execute(
        "SELECT id, amount, method, status, created FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (int(user_id), int(limit), int(offset)),
    )
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM deposits WHERE user_id=?", (int(user_id),))
    row = cur.fetchone()
    return rows, (int(row[0] or 0) if row else 0)


# ---------------- Product categories (bot shop + web admin share these) ----------------

def get_categories(active_only=False):
    """All categories in display order (sort_order ASC, then id ASC).
    Rows: (id, name, sort_order, active, icon)."""
    sql = "SELECT id, name, sort_order, active, COALESCE(icon, '') FROM categories"
    if active_only:
        sql += " WHERE active=1"
    cur.execute(sql + " ORDER BY sort_order ASC, id ASC")
    return cur.fetchall()


def get_category(category_id):
    try:
        cur.execute(
            "SELECT id, name, sort_order, active, COALESCE(icon, '') FROM categories WHERE id=?",
            (int(category_id),),
        )
    except (TypeError, ValueError):
        return None
    return cur.fetchone()


def get_category_by_name(name):
    """Case-insensitive exact-name lookup. Premium-emoji (<tg-emoji>) tags are
    ignored, so "Gaming" and a premium-emoji "Gaming" are the same category."""
    def _plain(s):
        return _re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "", str(s or "")).strip().lower()
    target = _plain(name)
    if not target:
        return None
    cur.execute("SELECT id, name, sort_order, active, COALESCE(icon, '') FROM categories")
    for row in cur.fetchall():
        if _plain(row[1]) == target:
            return row
    return None


def create_category(name, icon=""):
    """Append a new category at the end of the display order.

    Returns the new row's id on BOTH backends. On PostgreSQL the pgcompat
    translation only adds RETURNING id for tables listed in ID_TABLES (and
    "categories" is not one of them), so cur.lastrowid comes back None -- or
    worse, stale from a previous INSERT on the shared cursor. That lost id is
    what made the admin create-flow fail with "Category not found". Never
    trust lastrowid blindly: verify it, else re-read the row we just wrote."""
    cur.execute("SELECT COALESCE(MAX(sort_order), 0) FROM categories")
    next_order = int(cur.fetchone()[0] or 0) + 1
    cur.execute(
        "INSERT INTO categories(name, icon, sort_order, active) VALUES(?, ?, ?, 1)",
        (str(name).strip(), str(icon or "").strip(), next_order),
    )
    conn.commit()
    cid = cur.lastrowid
    valid = False
    if cid:
        try:
            cur.execute("SELECT id FROM categories WHERE id=?", (int(cid),))
            valid = cur.fetchone() is not None
        except Exception:
            valid = False
    if not valid:
        cur.execute(
            "SELECT id FROM categories WHERE name=? ORDER BY id DESC LIMIT 1",
            (str(name).strip(),),
        )
        row = cur.fetchone()
        cid = row[0] if row else None
    return cid


def set_category_icon(category_id, icon):
    cur.execute(
        "UPDATE categories SET icon=? WHERE id=?",
        (str(icon or "").strip(), int(category_id)),
    )
    conn.commit()


def set_category_sort(category_id, sort_order):
    """Set an explicit display position for a category."""
    try:
        cur.execute(
            "UPDATE categories SET sort_order=? WHERE id=?",
            (int(sort_order), int(category_id)),
        )
        conn.commit()
    except (TypeError, ValueError):
        pass


def rename_category(category_id, name):
    cur.execute(
        "UPDATE categories SET name=? WHERE id=?",
        (str(name).strip(), int(category_id)),
    )
    conn.commit()


def set_category_active(category_id, active):
    cur.execute(
        "UPDATE categories SET active=? WHERE id=?",
        (1 if active else 0, int(category_id)),
    )
    conn.commit()


def delete_category(category_id):
    """Delete a category. Its products are NOT deleted -- they fall back to
    Uncategorized (category_id NULL) so they stay visible in the shop."""
    cur.execute(
        "UPDATE products SET category_id=NULL WHERE category_id=?",
        (int(category_id),),
    )
    cur.execute("DELETE FROM categories WHERE id=?", (int(category_id),))
    conn.commit()


def _normalize_category_order():
    """Rewrite sort_order as 1..n so swaps always work, even after deletes."""
    cur.execute("SELECT id FROM categories ORDER BY sort_order ASC, id ASC")
    ids = [row[0] for row in cur.fetchall()]
    for pos, cid in enumerate(ids, start=1):
        cur.execute("UPDATE categories SET sort_order=? WHERE id=?", (pos, cid))
    return ids


def move_category(category_id, direction):
    """Swap a category with its neighbor. direction: 'up' or 'down'."""
    ids = _normalize_category_order()
    try:
        idx = ids.index(int(category_id))
    except ValueError:
        return False
    swap_with = idx - 1 if direction == "up" else idx + 1
    if swap_with < 0 or swap_with >= len(ids):
        return False
    ids[idx], ids[swap_with] = ids[swap_with], ids[idx]
    for pos, cid in enumerate(ids, start=1):
        cur.execute("UPDATE categories SET sort_order=? WHERE id=?", (pos, cid))
    conn.commit()
    return True


def get_category_counts():
    """Active-product counts per category. Key 0 = uncategorized products."""
    cur.execute(
        "SELECT COALESCE(category_id, 0) AS cid, COUNT(*) FROM products "
        "WHERE active=1 GROUP BY COALESCE(category_id, 0)"
    )
    return {int(row[0]): int(row[1]) for row in cur.fetchall()}


def get_category_stock_map():
    """Return a dict mapping category_id -> bool (True = at least one product
    in the category has stock > 0, False = every product is out of stock).

    Stock is determined from the LIVE unsold codes in product_stock first,
    falling back to the products.stock integer for products whose stock is
    tracked manually (no uploaded codes) -- exactly the same two-source logic
    used everywhere else in the bot.

    Key 0 = uncategorized products.
    Categories with no products at all are NOT present in the dict; the
    caller should treat a missing key as False (no stock).
    """
    # Step 1: live stock from product_stock table (uploaded codes).
    cur.execute(
        "SELECT p.id, COALESCE(p.category_id, 0) AS cid, "
        "       COUNT(ps.id) AS live_stock "
        "FROM products p "
        "LEFT JOIN product_stock ps ON ps.product_id = p.id AND ps.sold = 0 "
        "WHERE p.active = 1 "
        "GROUP BY p.id, cid"
    )
    live_rows = cur.fetchall()  # (product_id, cid, live_count)

    # Step 2: For products that have zero live codes, fall back to products.stock.
    # Collect the product ids whose live count is 0 so we can re-check.
    zero_live_pids = [row[0] for row in live_rows if int(row[2]) == 0]
    manual_stock = {}
    if zero_live_pids:
        placeholders = ",".join("?" * len(zero_live_pids))
        cur.execute(
            f"SELECT id, stock FROM products WHERE id IN ({placeholders})",
            zero_live_pids,
        )
        manual_stock = {row[0]: int(row[1] or 0) for row in cur.fetchall()}

    # Step 3: Combine to decide per-category availability.
    category_has_stock: dict[int, bool] = {}
    for pid, cid, live_count in live_rows:
        cid = int(cid)
        live_count = int(live_count)
        effective_stock = live_count if live_count > 0 else manual_stock.get(pid, 0)
        if cid not in category_has_stock:
            category_has_stock[cid] = False
        if effective_stock > 0:
            category_has_stock[cid] = True  # At least one product is available

    return category_has_stock


def get_category_products(category_id):
    """Products of one category for the shop view. Existing ordering kept
    (featured first, then newest). category_id None/0 = Uncategorized."""
    if category_id:
        cur.execute(
            "SELECT id, name, price, stock, validity, featured, flash_price, flash_ends_at "
            "FROM products WHERE active=1 AND category_id=? "
            "ORDER BY featured DESC, id DESC",
            (int(category_id),),
        )
    else:
        cur.execute(
            "SELECT id, name, price, stock, validity, featured, flash_price, flash_ends_at "
            "FROM products WHERE active=1 AND category_id IS NULL "
            "ORDER BY featured DESC, id DESC"
        )
    return cur.fetchall()


def _escape_like(term: str) -> str:
    """Escape LIKE wildcard characters in user-controlled search text so a
    stray % or _ in what someone types can't change the match shape. The
    value is still passed through a bound parameter (never string-formatted
    into the SQL) -- this only neutralises wildcard *meaning* inside it.

    Uses "!" as the escape character (paired with ESCAPE '!' in the SQL)
    instead of a backslash: Postgres treats a backslash inside a plain
    '...' string literal differently depending on standard_conforming_strings,
    while "!" needs no special quoting in either SQLite or Postgres and is
    not a character LIKE gives meaning to on its own."""
    return term.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def search_products_and_categories(keyword: str, product_limit: int = 8, category_limit: int = 6, include_categories: bool = True):
    """Free-text, database-backed lookup used for the plain-text product
    search fallback (e.g. a user just types "youtube"). Case-insensitive,
    whitespace-trimmed, partial-match on products (and optionally
    categories), and shares the exact same tables/columns the shop menus
    already read from -- no separate in-memory catalog copy.

    Returns (products, categories):
      products   -> rows shaped exactly like get_category_products()/show_shop()
                    use: (id, name, price, stock, validity, featured,
                    flash_price, flash_ends_at) -- so callers can hand them
                    straight to the existing product-button renderer.
      categories -> rows shaped like get_categories(): (id, name, sort_order,
                    active, icon). Empty list, no query run, when
                    include_categories=False.
    Both lists are capped (LIMIT) so a broad keyword can never pull the whole
    catalog into memory.
    """
    term = (keyword or "").strip()
    if not term:
        return [], []

    like_term = f"%{_escape_like(term.lower())}%"

    cur.execute(
        """
        SELECT id, name, price, stock, validity, featured, flash_price, flash_ends_at
        FROM products
        WHERE active=1
          AND (
                LOWER(name) LIKE ? ESCAPE '!'
                OR LOWER(COALESCE(description, '')) LIKE ? ESCAPE '!'
              )
        ORDER BY featured DESC, id DESC
        LIMIT ?
        """,
        (like_term, like_term, int(product_limit)),
    )
    products = cur.fetchall()

    if not include_categories:
        return products, []

    cur.execute(
        """
        SELECT id, name, sort_order, active, COALESCE(icon, '')
        FROM categories
        WHERE active=1 AND LOWER(name) LIKE ? ESCAPE '!'
        ORDER BY sort_order ASC, id ASC
        LIMIT ?
        """,
        (like_term, int(category_limit)),
    )
    categories = cur.fetchall()

    return products, categories


def assign_product_category(product_id, category_id):
    """Assign/move a product to a category. None = Uncategorized."""
    if category_id is not None:
        if not get_category(category_id):
            return False
        cur.execute(
            "UPDATE products SET category_id=? WHERE id=?",
            (int(category_id), int(product_id)),
        )
    else:
        cur.execute(
            "UPDATE products SET category_id=NULL WHERE id=?",
            (int(product_id),),
        )
    conn.commit()
    return True
