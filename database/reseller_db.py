"""
Reseller system — database foundation (Phase 1).

Additive-only layer: 9 new tables + data-access helpers. NOTHING in the
customer ordering, product, stock, payment or delivery flows is touched.
The reseller system REUSES the existing masters:
  - products ............ product catalog (reseller_product_access /
                          reseller_pricing_rules reference products(id))
  - product_stock ....... stock units (Phase 2 claims via reserve_stock_items)
  - orders .............. order records (Phase 2 inserts with a reseller marker)
  - users ............... Telegram identity (resellers.telegram_user_id)

Conventions followed (see database/db.py):
  - CREATE TABLE IF NOT EXISTS inside an ensure_*() called from init_db()
  - TEXT timestamps ('YYYY-MM-DD HH:MM:SS', now_text() / DEFAULT CURRENT_TIMESTAMP)
  - sqlite dialect; Postgres handled transparently by database/pgcompat.py
    (new id-tables registered in pgcompat.ID_TABLES, upsert targets in
    pgcompat.REPLACE_CONFLICT)
  - secrets at rest encrypted with utils.crypto.encrypt_text

Read helpers return plain DICTS so callers behave identically on both
backends (sqlite3 rows are tuples; pgcompat rows are _Row — dicts are the
only shape that is the same everywhere, and they JSON-serialize directly
for the Phase 2 API).

Money is INTEGER minor units (cents) everywhere — NEVER float.
USDT 19.99 is stored as 1999. Percentages are INTEGER basis points
(10000 bps = 100.00%).

The wallet ledger (reseller_wallet_transactions) is IMMUTABLE by design:
this module deliberately exposes NO update/delete helpers for it, and every
row stores the running balance_after_cents so the ledger is self-auditing.
"""

import hashlib
import secrets
import threading
from decimal import Decimal, ROUND_HALF_UP

from database import pgcompat as _pgcompat
from database.db import get_conn, get_cursor, _safe_add_column  # thread-local, pg-aware
from utils.crypto import encrypt_text, decrypt_text
from utils.timezone import now_text

# ------------------------------------------------------------------ constants

RESELLER_STATUSES = ("pending", "active", "suspended", "closed")
KEY_ENVIRONMENTS = ("test", "production")
KEY_STATUSES = ("active", "revoked")
RULE_TYPES = ("fixed_price", "discount_percent", "markup_percent")
TX_TYPES = (
    "topup_credit",       # admin/system credits the reseller wallet
    "purchase_debit",     # API order charge
    "refund_credit",      # order failure / cancellation refund
    "adjustment_credit",  # manual admin correction (+)
    "adjustment_debit",   # manual admin correction (-)
)

_PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # gift-code convention

# Serializes only the legacy fallback path (ancient SQLite builds without
# UPDATE ... RETURNING). The RETURNING path needs no lock: the balance read
# happens inside the same atomic statement as the update.
_WALLET_LOCK = threading.Lock()

# Column orders (match the CREATE TABLE definitions / SELECT lists below).
_RESELLER_COLS = ("id", "public_id", "telegram_user_id", "name", "username",
                  "status", "created", "updated")
_API_KEY_COLS = ("id", "key_id", "secret_hash", "reseller_id", "environment",
                 "status", "created", "last_used_at", "revoked_at")
_ACCESS_COLS = ("id", "reseller_id", "product_id", "enabled", "created", "updated")
_PRICING_COLS = ("id", "reseller_id", "product_id", "rule_type", "value_cents",
                 "value_bps", "enabled", "created", "updated")
_WALLET_COLS = ("reseller_id", "balance_cents", "currency", "created", "updated")
_LEDGER_COLS = ("id", "reseller_id", "tx_type", "amount_cents",
                "balance_after_cents", "reference", "description", "created")
_WEBHOOK_COLS = ("id", "reseller_id", "url", "enabled", "created", "updated")
_API_LOG_COLS = ("id", "reseller_id", "api_key_id", "endpoint", "method",
                 "status_code", "request_id", "response_ms", "created")
_SEC_EVENT_COLS = ("id", "reseller_id", "event_type", "ip", "user_agent",
                   "description", "created")


def _d(cols, row):
    """Row -> dict. Works on sqlite3 tuples AND pgcompat _Row (both backends)."""
    return dict(zip(cols, list(row))) if row is not None else None


def _ds(cols, rows):
    return [dict(zip(cols, list(r))) for r in rows]


# ------------------------------------------------------------------ money helpers

def to_cents(amount) -> int:
    """Exact decimal -> integer cents. to_cents('19.99') == 1999.

    Accepts str/int/Decimal. Float input is rejected on purpose: floats are
    how money bugs start (0.1 + 0.2 != 0.3)."""
    if isinstance(amount, float):
        raise TypeError("float money is not allowed — pass str/int/Decimal")
    try:
        parsed = (Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except Exception:
        raise ValueError(f"invalid money amount: {amount!r}")
    return int(parsed)


def from_cents(cents: int) -> str:
    """Integer cents -> display string '19.99'."""
    c = int(cents)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"


# ------------------------------------------------------------------ schema

def ensure_reseller_tables():
    """Create the 9 reseller tables + indexes. Idempotent; called from init_db().

    Deletion policy ("safe deletion"):
      - RESTRICT on money/audit references (wallet, ledger, api keys): a reseller
        with financial history can never be hard-deleted — deactivate instead.
      - CASCADE on pure configuration (product access, pricing rules, webhooks):
        deleting a product/reseller cleans up its config rows automatically.
      - SET NULL on log/security-event references: history survives cleanup.
    """
    conn = get_conn()
    cur = get_cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS resellers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        telegram_user_id INTEGER UNIQUE,
        name TEXT DEFAULT '',
        username TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','active','suspended','closed')),
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_api_keys(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id TEXT NOT NULL UNIQUE,
        secret_hash TEXT NOT NULL UNIQUE,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE RESTRICT,
        environment TEXT NOT NULL DEFAULT 'test'
            CHECK (environment IN ('test','production')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','revoked')),
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        last_used_at TEXT,
        revoked_at TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_product_access(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 1,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(reseller_id, product_id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_pricing_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        rule_type TEXT NOT NULL
            CHECK (rule_type IN ('fixed_price','discount_percent','markup_percent')),
        value_cents INTEGER,
        value_bps INTEGER,
        enabled INTEGER NOT NULL DEFAULT 1,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(reseller_id, product_id),
        CHECK (
            (rule_type = 'fixed_price'
                AND value_cents IS NOT NULL AND value_cents >= 0)
            OR (rule_type IN ('discount_percent','markup_percent')
                AND value_bps IS NOT NULL AND value_bps BETWEEN 0 AND 10000)
        )
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_wallet(
        reseller_id INTEGER PRIMARY KEY REFERENCES resellers(id) ON DELETE RESTRICT,
        balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
        currency TEXT NOT NULL DEFAULT 'USDT',
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_wallet_transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE RESTRICT,
        tx_type TEXT NOT NULL CHECK (tx_type IN (
            'topup_credit','purchase_debit','refund_credit',
            'adjustment_credit','adjustment_debit')),
        amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
        balance_after_cents INTEGER NOT NULL CHECK (balance_after_cents >= 0),
        reference TEXT,
        description TEXT DEFAULT '',
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(reseller_id, tx_type, reference)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_webhooks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        secret_encrypted TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(reseller_id, url)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_api_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER REFERENCES resellers(id) ON DELETE SET NULL,
        api_key_id INTEGER REFERENCES reseller_api_keys(id) ON DELETE SET NULL,
        endpoint TEXT NOT NULL,
        method TEXT NOT NULL,
        status_code INTEGER,
        request_id TEXT,
        response_ms INTEGER,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_security_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER REFERENCES resellers(id) ON DELETE SET NULL,
        event_type TEXT NOT NULL,
        ip TEXT,
        user_agent TEXT,
        description TEXT DEFAULT '',
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_resellers_status ON resellers(status)",
        "CREATE INDEX IF NOT EXISTS idx_resellers_telegram ON resellers(telegram_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_keys_reseller ON reseller_api_keys(reseller_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_access_product ON reseller_product_access(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_pricing_product ON reseller_pricing_rules(product_id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_ledger_reseller ON reseller_wallet_transactions(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_ledger_ref ON reseller_wallet_transactions(reference)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_api_logs_reseller ON reseller_api_logs(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_api_logs_request ON reseller_api_logs(request_id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_api_logs_created ON reseller_api_logs(created)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_security_reseller ON reseller_security_events(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_reseller_security_type ON reseller_security_events(event_type, created)",
    ):
        cur.execute(stmt)

    # Limits configuration columns (Phase 2) — additive, idempotent. These are
    # CONFIG only: the enforcement engine arrives with the public API.
    _safe_add_column("resellers", "rate_limit_per_min", "INTEGER NOT NULL DEFAULT 60")
    _safe_add_column("resellers", "orders_per_min", "INTEGER NOT NULL DEFAULT 10")
    _safe_add_column("resellers", "daily_request_limit", "INTEGER NOT NULL DEFAULT 10000")
    _safe_add_column("resellers", "monthly_request_limit", "INTEGER NOT NULL DEFAULT 250000")
    _safe_add_column("resellers", "daily_order_limit", "INTEGER NOT NULL DEFAULT 500")
    _safe_add_column("resellers", "monthly_order_limit", "INTEGER NOT NULL DEFAULT 10000")
    _safe_add_column("resellers", "api_enabled", "INTEGER NOT NULL DEFAULT 1")

    conn.commit()


# ------------------------------------------------------------------ resellers

def generate_reseller_public_id() -> str:
    return "RSR-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(10))


def create_reseller(name: str, telegram_user_id=None, username: str = "") -> dict:
    """Create a reseller in 'pending' status. Returns dict(id, public_id)."""
    conn, cur = get_conn(), get_cursor()
    stamp = now_text()
    for _attempt in range(5):  # public_id UNIQUE collision retry (gift-code pattern)
        public_id = generate_reseller_public_id()
        try:
            cur.execute(
                "INSERT INTO resellers(public_id, telegram_user_id, name, username, status, created, updated) "
                "VALUES(?,?,?,?,'pending',?,?)",
                (public_id, telegram_user_id, name or "", username or "", stamp, stamp),
            )
            conn.commit()
            return {"id": cur.lastrowid, "public_id": public_id}
        except Exception as exc:
            if "public_id" in str(exc).lower() and "unique" in str(exc).lower():
                continue
            raise
    raise RuntimeError("could not allocate a unique reseller public_id")


def get_reseller(reseller_id: int):
    cur = get_cursor()
    cur.execute("SELECT * FROM resellers WHERE id=?", (reseller_id,))
    return _d(_RESELLER_COLS, cur.fetchone())


def get_reseller_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM resellers WHERE public_id=?", (public_id,))
    return _d(_RESELLER_COLS, cur.fetchone())


def get_reseller_by_telegram(telegram_user_id: int):
    cur = get_cursor()
    cur.execute("SELECT * FROM resellers WHERE telegram_user_id=?", (telegram_user_id,))
    return _d(_RESELLER_COLS, cur.fetchone())


def list_resellers(status=None, limit: int = 100, offset: int = 0):
    cur = get_cursor()
    if status:
        cur.execute(
            "SELECT * FROM resellers WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        cur.execute("SELECT * FROM resellers ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    return _ds(_RESELLER_COLS, cur.fetchall())


def set_reseller_status(reseller_id: int, status: str) -> bool:
    """Activate/suspend/close. Rowcount-verified like the existing code."""
    if status not in RESELLER_STATUSES:
        raise ValueError(f"invalid reseller status: {status}")
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE resellers SET status=?, updated=? WHERE id=?",
        (status, now_text(), reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ API keys

def _hash_secret(secret: str) -> str:
    """SHA-256 of the secret. Secrets are high-entropy random tokens; only the
    hash is stored, so a DB leak does not leak usable credentials."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_api_key(reseller_id: int, environment: str = "test"):
    """Create an API key. Returns (key_id, secret) — the secret is shown ONCE
    and never stored; only its hash is persisted.

    key_id  : public identifier, safe to log/store, e.g. 'rk_live_8F3K...'
    secret  : private part the reseller sends alongside key_id.
    """
    if environment not in KEY_ENVIRONMENTS:
        raise ValueError(f"invalid environment: {environment}")
    if not get_reseller(reseller_id):
        raise ValueError(f"reseller {reseller_id} does not exist")
    conn, cur = get_conn(), get_cursor()
    prefix = "rk_live_" if environment == "production" else "rk_test_"
    for _attempt in range(5):
        key_id = prefix + "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(8))
        secret = secrets.token_urlsafe(32)
        try:
            cur.execute(
                "INSERT INTO reseller_api_keys(key_id, secret_hash, reseller_id, environment, status, created) "
                "VALUES(?,?,?,?,'active',?)",
                (key_id, _hash_secret(secret), reseller_id, environment, now_text()),
            )
            conn.commit()
            return key_id, secret
        except Exception as exc:
            if "unique" in str(exc).lower():
                continue
            raise
    raise RuntimeError("could not allocate a unique api key")


def verify_api_key_secret(key_id: str, secret: str) -> bool:
    """True when the presented secret matches the stored hash of an ACTIVE key."""
    cur = get_cursor()
    cur.execute(
        "SELECT secret_hash, status FROM reseller_api_keys WHERE key_id=?", (key_id,)
    )
    row = cur.fetchone()
    if not row or row[1] != "active":
        return False
    return secrets.compare_digest(row[0], _hash_secret(secret))


def get_api_key(key_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_api_keys WHERE key_id=?", (key_id,))
    return _d(_API_KEY_COLS, cur.fetchone())


def list_api_keys(reseller_id: int, include_revoked: bool = False):
    cur = get_cursor()
    if include_revoked:
        cur.execute(
            "SELECT * FROM reseller_api_keys WHERE reseller_id=? ORDER BY id DESC",
            (reseller_id,),
        )
    else:
        cur.execute(
            "SELECT * FROM reseller_api_keys WHERE reseller_id=? AND status='active' ORDER BY id DESC",
            (reseller_id,),
        )
    return _ds(_API_KEY_COLS, cur.fetchall())


def touch_api_key_used(key_id: str):
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET last_used_at=? WHERE key_id=?",
        (now_text(), key_id),
    )
    conn.commit()


def revoke_api_key(key_id: str) -> bool:
    """Soft-delete: the key row (and its audit trail) is kept forever."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET status='revoked', revoked_at=? "
        "WHERE key_id=? AND status='active'",
        (now_text(), key_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ product access

def set_product_access(reseller_id: int, product_id: int, enabled: bool = True):
    """Grant/revoke a reseller's access to an EXISTING product (idempotent
    upsert — never creates duplicate reseller/product rows)."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT OR REPLACE INTO reseller_product_access(reseller_id, product_id, enabled, created, updated) "
        "VALUES(?,?,?,COALESCE((SELECT created FROM reseller_product_access WHERE reseller_id=? AND product_id=?), ?), ?)",
        (reseller_id, product_id, 1 if enabled else 0,
         reseller_id, product_id, now_text(), now_text()),
    )
    conn.commit()


def has_product_access(reseller_id: int, product_id: int) -> bool:
    cur = get_cursor()
    cur.execute(
        "SELECT enabled FROM reseller_product_access WHERE reseller_id=? AND product_id=?",
        (reseller_id, product_id),
    )
    row = cur.fetchone()
    return bool(row and row[0])


def list_product_access(reseller_id: int, enabled_only: bool = False):
    cur = get_cursor()
    sql = ("SELECT a.*, p.name, p.price FROM reseller_product_access a "
           "JOIN products p ON p.id = a.product_id WHERE a.reseller_id=?")
    if enabled_only:
        sql += " AND a.enabled=1"
    cur.execute(sql + " ORDER BY a.id DESC", (reseller_id,))
    return _ds(_ACCESS_COLS + ("name", "price"), cur.fetchall())


# ------------------------------------------------------------------ pricing rules

def set_pricing_rule(reseller_id: int, product_id: int, rule_type: str,
                     value: int, enabled: bool = True):
    """One rule per (reseller, product) — enforced by UNIQUE + upsert.

    value is INTEGER: cents for fixed_price, basis points for the percent
    rules (1500 bps = 15.00%). No floats anywhere.
    """
    if rule_type not in RULE_TYPES:
        raise ValueError(f"invalid rule_type: {rule_type}")
    value = int(value)
    if rule_type == "fixed_price":
        if value < 0:
            raise ValueError("fixed_price value (cents) must be >= 0")
        value_cents, value_bps = value, None
    else:
        if not 0 <= value <= 10000:
            raise ValueError("percent rule value (basis points) must be 0..10000")
        value_cents, value_bps = None, value
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT OR REPLACE INTO reseller_pricing_rules"
        "(reseller_id, product_id, rule_type, value_cents, value_bps, enabled, created, updated) "
        "VALUES(?,?,?,?,?,?,COALESCE((SELECT created FROM reseller_pricing_rules "
        "WHERE reseller_id=? AND product_id=?), ?), ?)",
        (reseller_id, product_id, rule_type, value_cents, value_bps,
         1 if enabled else 0, reseller_id, product_id, now_text(), now_text()),
    )
    conn.commit()


def get_pricing_rule(reseller_id: int, product_id: int):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_pricing_rules WHERE reseller_id=? AND product_id=? AND enabled=1",
        (reseller_id, product_id),
    )
    return _d(_PRICING_COLS, cur.fetchone())


def list_pricing_rules(reseller_id: int):
    cur = get_cursor()
    cur.execute(
        "SELECT r.*, p.name FROM reseller_pricing_rules r "
        "JOIN products p ON p.id = r.product_id WHERE r.reseller_id=? ORDER BY r.id DESC",
        (reseller_id,),
    )
    return _ds(_PRICING_COLS + ("name",), cur.fetchall())


def delete_pricing_rule(reseller_id: int, product_id: int) -> bool:
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "DELETE FROM reseller_pricing_rules WHERE reseller_id=? AND product_id=?",
        (reseller_id, product_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ wallet + ledger

def get_or_create_wallet(reseller_id: int):
    """1:1 wallet per reseller (PK = reseller_id). Idempotent."""
    conn, cur = get_conn(), get_cursor()
    cur.execute("INSERT OR IGNORE INTO reseller_wallet(reseller_id) VALUES(?)", (reseller_id,))
    conn.commit()
    cur.execute("SELECT * FROM reseller_wallet WHERE reseller_id=?", (reseller_id,))
    return _d(_WALLET_COLS, cur.fetchone())


def get_wallet_balance_cents(reseller_id: int) -> int:
    row = get_or_create_wallet(reseller_id)
    return int(row["balance_cents"])


def post_wallet_transaction(reseller_id: int, tx_type: str, amount_cents: int,
                            reference: str = None, description: str = ""):
    """Post ONE immutable ledger entry and update the balance atomically.

    Returns (status, data):
      ('posted',    transaction_id)
      ('duplicate', existing_transaction_id)  — same (reseller, type, reference)
      ('insufficient_funds', current_balance_cents)

    amount_cents is SIGNED: credits positive, debits negative. The balance
    update is a conditional single-statement UPDATE (the project's FIX43/44
    pattern): balance can never go negative and concurrent posts can't
    double-spend. The UNIQUE(reseller_id, tx_type, reference) constraint is
    the idempotency backstop — a retried API call with the same reference
    can never post twice. reference may be NULL (manual adjustments); NULL
    references are never deduplicated (standard SQL semantics).

    There is intentionally NO update/delete for this table — the ledger is
    append-only. Corrections are new adjustment entries.
    """
    if tx_type not in TX_TYPES:
        raise ValueError(f"invalid tx_type: {tx_type}")
    amount_cents = int(amount_cents)
    if amount_cents == 0:
        raise ValueError("amount_cents must be non-zero")

    conn, cur = get_conn(), get_cursor()
    get_or_create_wallet(reseller_id)

    # Idempotency short-circuit (fast path; the UNIQUE constraint is the guard).
    if reference is not None:
        cur.execute(
            "SELECT id FROM reseller_wallet_transactions "
            "WHERE reseller_id=? AND tx_type=? AND reference=?",
            (reseller_id, tx_type, reference),
        )
        row = cur.fetchone()
        if row:
            return "duplicate", row[0]

    stamp = now_text()
    # Conditional atomic balance move — the same discipline as the customer
    # wallet deduction in products/product_handlers.py — but the new balance is
    # read in the SAME statement via RETURNING, so the ledger's balance_after
    # snapshot is race-free even under Postgres autocommit (a separate SELECT
    # could observe another connection's commit and store a wrong snapshot).
    try:
        cur.execute(
            "UPDATE reseller_wallet SET balance_cents = balance_cents + ?, updated=? "
            "WHERE reseller_id=? AND balance_cents + ? >= 0 RETURNING balance_cents",
            (amount_cents, stamp, reseller_id, amount_cents),
        )
        rows = cur.fetchall()
    except Exception:
        rows = None  # ancient SQLite without RETURNING — legacy fallback below
    if rows is not None:
        if not rows:
            conn.rollback()
            return "insufficient_funds", get_wallet_balance_cents(reseller_id)
        balance_after = int(rows[0][0])
    else:
        with _WALLET_LOCK:
            cur.execute(
                "UPDATE reseller_wallet SET balance_cents = balance_cents + ?, updated=? "
                "WHERE reseller_id=? AND balance_cents + ? >= 0",
                (amount_cents, stamp, reseller_id, amount_cents),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return "insufficient_funds", get_wallet_balance_cents(reseller_id)
            cur.execute("SELECT balance_cents FROM reseller_wallet WHERE reseller_id=?", (reseller_id,))
            balance_after = int(cur.fetchone()[0])
    try:
        cur.execute(
            "INSERT INTO reseller_wallet_transactions"
            "(reseller_id, tx_type, amount_cents, balance_after_cents, reference, description, created) "
            "VALUES(?,?,?,?,?,?,?)",
            (reseller_id, tx_type, amount_cents, balance_after, reference, description or "", stamp),
        )
    except Exception as exc:
        # Lost an idempotency race: another connection posted the same
        # reference first. Undo our balance move and report the duplicate.
        if reference is not None and "unique" in str(exc).lower():
            # sqlite (legacy mode): the balance UPDATE is still uncommitted in
            # this connection's transaction -> rollback() alone undoes it.
            # Postgres (autocommit): the UPDATE is already durable -> compensate
            # with a reverse move. Doing BOTH (the old code) double-undid the
            # balance on sqlite — caught by the P15 threaded race test.
            conn.rollback()
            if _pgcompat.use_postgres():
                cur.execute(
                    "UPDATE reseller_wallet SET balance_cents = balance_cents - ?, updated=? "
                    "WHERE reseller_id=?",
                    (amount_cents, now_text(), reseller_id),
                )
                conn.commit()
            cur.execute(
                "SELECT id FROM reseller_wallet_transactions "
                "WHERE reseller_id=? AND tx_type=? AND reference=?",
                (reseller_id, tx_type, reference),
            )
            row = cur.fetchone()
            return "duplicate", (row[0] if row else None)
        conn.rollback()
        raise
    conn.commit()
    return "posted", cur.lastrowid


def list_wallet_transactions(reseller_id: int, limit: int = 50, offset: int = 0):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_wallet_transactions WHERE reseller_id=? "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (reseller_id, limit, offset),
    )
    return _ds(_LEDGER_COLS, cur.fetchall())


def get_wallet_transaction_by_reference(reseller_id: int, tx_type: str, reference: str):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type=? AND reference=?",
        (reseller_id, tx_type, reference),
    )
    return _d(_LEDGER_COLS, cur.fetchone())


# ------------------------------------------------------------------ webhooks

def upsert_webhook(reseller_id: int, url: str, secret: str, enabled: bool = True):
    """Register/replace a webhook endpoint. The signing secret is stored
    ENCRYPTED (same convention as sessions/delivery content)."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT OR REPLACE INTO reseller_webhooks(reseller_id, url, secret_encrypted, enabled, created, updated) "
        "VALUES(?,?,?,?,COALESCE((SELECT created FROM reseller_webhooks WHERE reseller_id=? AND url=?), ?), ?)",
        (reseller_id, url, encrypt_text(secret), 1 if enabled else 0,
         reseller_id, url, now_text(), now_text()),
    )
    conn.commit()


def get_webhook_secret(reseller_id: int, url: str):
    cur = get_cursor()
    cur.execute(
        "SELECT secret_encrypted FROM reseller_webhooks WHERE reseller_id=? AND url=?",
        (reseller_id, url),
    )
    row = cur.fetchone()
    return decrypt_text(row[0]) if row else None


def list_webhooks(reseller_id: int, enabled_only: bool = False):
    cur = get_cursor()
    sql = ("SELECT id, reseller_id, url, enabled, created, updated "
           "FROM reseller_webhooks WHERE reseller_id=?")
    if enabled_only:
        sql += " AND enabled=1"
    cur.execute(sql + " ORDER BY id DESC", (reseller_id,))
    return _ds(_WEBHOOK_COLS, cur.fetchall())


def set_webhook_enabled(webhook_id: int, enabled: bool) -> bool:
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_webhooks SET enabled=?, updated=? WHERE id=?",
        (1 if enabled else 0, now_text(), webhook_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_webhook(webhook_id: int) -> bool:
    conn, cur = get_conn(), get_cursor()
    cur.execute("DELETE FROM reseller_webhooks WHERE id=?", (webhook_id,))
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ logs + security events

def log_api_request(reseller_id, api_key_id, endpoint: str, method: str,
                    status_code: int, request_id: str = None, response_ms: int = None):
    """Append-only API request log (no update/delete by design)."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT INTO reseller_api_logs"
        "(reseller_id, api_key_id, endpoint, method, status_code, request_id, response_ms, created) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (reseller_id, api_key_id, endpoint, method, status_code,
         request_id, response_ms, now_text()),
    )
    conn.commit()
    return cur.lastrowid


def list_api_logs(reseller_id: int = None, limit: int = 50, offset: int = 0):
    cur = get_cursor()
    if reseller_id is not None:
        cur.execute(
            "SELECT * FROM reseller_api_logs WHERE reseller_id=? "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (reseller_id, limit, offset),
        )
    else:
        cur.execute(
            "SELECT * FROM reseller_api_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return _ds(_API_LOG_COLS, cur.fetchall())


def log_security_event(reseller_id, event_type: str, ip: str = None,
                       user_agent: str = None, description: str = ""):
    """Append-only security event log: auth failures, rate-limit hits,
    replay attempts, suspicious payloads, etc."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT INTO reseller_security_events"
        "(reseller_id, event_type, ip, user_agent, description, created) "
        "VALUES(?,?,?,?,?,?)",
        (reseller_id, event_type, ip, user_agent, description or "", now_text()),
    )
    conn.commit()
    return cur.lastrowid


def list_security_events(reseller_id: int = None, event_type: str = None,
                         limit: int = 50, offset: int = 0):
    cur = get_cursor()
    sql, params = "SELECT * FROM reseller_security_events WHERE 1=1", []
    if reseller_id is not None:
        sql += " AND reseller_id=?"
        params.append(reseller_id)
    if event_type:
        sql += " AND event_type=?"
        params.append(event_type)
    cur.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
    return _ds(_SEC_EVENT_COLS, cur.fetchall())


# ------------------------------------------------------------------ admin lifecycle actions

# Allowed admin actions -> (allowed current statuses, resulting status).
# This map is the SINGLE source of truth, validated server-side everywhere
# (web admin today; the public API never gets these actions).
RESELLER_ACTIONS = {
    "approve":    (("pending",), "active"),
    "reject":     (("pending",), "closed"),
    "suspend":    (("active",), "suspended"),
    "block":      (("active", "suspended"), "closed"),
    "reactivate": (("suspended", "closed"), "active"),
}


def apply_reseller_action(reseller_id: int, action: str):
    """Validate + apply an admin lifecycle action.

    Returns (ok, message, new_status). Never raises on bad input — callers
    map ok=False to a 400. The actual write goes through set_reseller_status
    (whitelist + rowcount-verified).
    """
    action = (action or "").strip().lower()
    spec = RESELLER_ACTIONS.get(action)
    if not spec:
        return False, f"Unknown action '{action}'.", None
    row = get_reseller(reseller_id)
    if not row:
        return False, "Reseller not found.", None
    allowed_from, target = spec
    current = row["status"]
    if current not in allowed_from:
        return False, f"Cannot {action} a reseller whose status is '{current}'.", current
    set_reseller_status(reseller_id, target)
    return True, f"Reseller is now {target}.", target


# ------------------------------------------------------------------ price resolution

def percent_to_bps(value) -> int:
    """'15' / '12.5' -> 1500 / 1250 basis points. Decimal-exact; strings/ints
    only, floats rejected (same no-float rule as money)."""
    if isinstance(value, float):
        raise TypeError("float percents are not allowed — pass str/int")
    try:
        pct = Decimal(str(value).strip())
    except Exception:
        raise ValueError(f"invalid percent value: {value!r}")
    if not (0 <= pct <= 100):
        raise ValueError("percent must be between 0 and 100")
    return int((pct * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def get_master_unit_price_cents(product_id: int, quantity: int = 1):
    """Master-catalog unit price in integer cents. Returns (cents, used_tier).

    Reuses the project's existing quantity-tier architecture
    (product_price_tiers): a tier's price is the TOTAL for that many units
    (unit = tier_total / tier_qty), the cheapest tier with qty <= requested
    quantity wins, and products.price is the fallback — the same math the
    customer shop uses. Float master prices are converted via Decimal(str())
    and integer-divided — no float arithmetic on money. Flash-sale pricing is
    intentionally NOT applied here (promotions are customer-facing; reseller
    pricing is contractual). (None, False) when the product does not exist.
    """
    quantity = max(1, int(quantity or 1))
    cur = get_cursor()
    cur.execute("SELECT price FROM products WHERE id=?", (product_id,))
    row = cur.fetchone()
    if not row:
        return None, False
    base_cents = to_cents(str(row[0]))
    if quantity <= 1:
        return base_cents, False
    cur.execute("SELECT quantity, price FROM product_price_tiers WHERE product_id=?",
                (product_id,))
    best, used = base_cents, False
    for tq, tp in cur.fetchall():
        tq = int(tq)
        if tq > 1 and tq <= quantity:
            tier_unit = to_cents(str(tp)) // tq
            if 0 < tier_unit < best:
                best, used = tier_unit, True
    return best, used


def resolve_reseller_price_cents(reseller_id: int, product_id: int, quantity: int = 1):
    """THE server-side price source for the future reseller API.

    Returns (unit_price_cents, source). Never accepts a client-supplied price.
    Fallback order: enabled reseller rule -> master quantity tier -> master
    product price. Percent rules use integer basis-point math with half-up
    rounding: delta = (cents * bps + 5000) // 10000.
    """
    base, used_tier = get_master_unit_price_cents(product_id, quantity)
    if base is None:
        return None, None
    rule = get_pricing_rule(reseller_id, product_id)  # enabled rules only
    if not rule:
        return base, ("master_tier" if used_tier else "master")
    rt = rule["rule_type"]
    if rt == "fixed_price":
        return int(rule["value_cents"]), "rule:fixed_price"
    bps = int(rule["value_bps"])
    delta = (base * bps + 5000) // 10000
    if rt == "discount_percent":
        return max(0, base - delta), "rule:discount_percent"
    return base + delta, "rule:markup_percent"


# ------------------------------------------------------------------ API limits configuration

# Phase-2 configuration ONLY — the enforcement engine ships with the public
# API. Field -> (min, max). A limit of 0 means "none allowed"; api_enabled is
# the master on/off switch for the reseller's future API access.
LIMIT_FIELDS = {
    "api_enabled":           (0, 1),
    "rate_limit_per_min":    (0, 100000),
    "orders_per_min":        (0, 10000),
    "daily_request_limit":   (0, 10000000),
    "monthly_request_limit": (0, 100000000),
    "daily_order_limit":     (0, 1000000),
    "monthly_order_limit":   (0, 10000000),
}


def get_reseller_limits(reseller_id: int):
    """All limit fields for one reseller as a plain dict, or None."""
    cur = get_cursor()
    cols = ", ".join(LIMIT_FIELDS)
    cur.execute(f"SELECT {cols} FROM resellers WHERE id=?", (reseller_id,))
    row = cur.fetchone()
    if not row:
        return None
    return {k: int(v) for k, v in zip(LIMIT_FIELDS.keys(), list(row))}


def set_reseller_limits(reseller_id: int, **updates) -> dict:
    """Validate + persist limit fields. Only whitelisted keys, integer values
    inside their configured ranges; rowcount-verified. Returns the full
    updated limits dict."""
    if not updates:
        raise ValueError("no limit fields provided")
    clean = {}
    for k, v in updates.items():
        if k not in LIMIT_FIELDS:
            raise ValueError(f"unknown limit field: {k}")
        if isinstance(v, bool):
            v = int(v)
        try:
            v = int(str(v).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be an integer")
        lo, hi = LIMIT_FIELDS[k]
        if not (lo <= v <= hi):
            raise ValueError(f"{k} must be between {lo} and {hi}")
        clean[k] = v
    conn, cur = get_conn(), get_cursor()
    sets = ", ".join(f"{k}=?" for k in clean)   # keys come from the constant only
    cur.execute(f"UPDATE resellers SET {sets}, updated=? WHERE id=?",
                (*clean.values(), now_text(), reseller_id))
    if cur.rowcount == 0:
        raise ValueError("reseller not found")
    conn.commit()
    return get_reseller_limits(reseller_id)
