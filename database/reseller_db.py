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
import ipaddress
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit
from decimal import Decimal, ROUND_HALF_UP

from database import pgcompat as _pgcompat
from database.db import (  # thread-local, pg-aware
    get_conn,
    get_cursor,
    _escape_like,
    _safe_add_column,
)
from utils.crypto import encrypt_text, decrypt_text
from utils.timezone import now, now_text

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
MAX_WALLET_BALANCE_CENTS = 9223372036854775807
_WALLET_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")

_PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # gift-code convention

# Column orders (match the CREATE TABLE definitions / SELECT lists below).
_RESELLER_COLS = ("id", "public_id", "telegram_user_id", "name", "username",
                  "status", "created", "updated")
_API_KEY_COLS = ("id", "key_id", "secret_hash", "reseller_id", "environment",
                 "status", "created", "last_used_at", "revoked_at", "enabled",
                 "expires_at", "rotated_to")   # Phase 13 P5 lifecycle fields
_ACCESS_COLS = ("id", "reseller_id", "product_id", "enabled", "created", "updated")
_PRICING_COLS = ("id", "reseller_id", "product_id", "rule_type", "value_cents",
                 "value_bps", "enabled", "created", "updated")
_WALLET_COLS = ("reseller_id", "balance_cents", "currency", "created", "updated")
_LEDGER_COLS = ("id", "reseller_id", "tx_type", "amount_cents",
                "balance_after_cents", "reference", "description", "created",
                "currency", "related_order_reference")
_WEBHOOK_COLS = ("id", "reseller_id", "url", "enabled", "created", "updated")
_API_LOG_COLS = ("id", "reseller_id", "api_key_id", "endpoint", "method",
                 "status_code", "request_id", "response_ms", "created",
                 "reseller_public_id", "key_id", "ip", "user_agent")
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
        revoked_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
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
        currency TEXT NOT NULL DEFAULT 'USDT',
        related_order_reference TEXT,
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
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        reseller_public_id TEXT,
        key_id TEXT,
        ip TEXT,
        user_agent TEXT
    )""")

    # Atomic aggregate counters. Request fields are enforced in Phase 4;
    # order fields are reserved for the later order-quota implementation.
    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_api_quota_counters(
        reseller_id INTEGER PRIMARY KEY REFERENCES resellers(id) ON DELETE CASCADE,
        request_minute_bucket TEXT,
        request_minute_count INTEGER NOT NULL DEFAULT 0,
        request_day_bucket TEXT,
        request_day_count INTEGER NOT NULL DEFAULT 0,
        request_month_bucket TEXT,
        request_month_count INTEGER NOT NULL DEFAULT 0,
        order_minute_bucket TEXT,
        order_minute_count INTEGER NOT NULL DEFAULT 0,
        order_day_bucket TEXT,
        order_day_count INTEGER NOT NULL DEFAULT 0,
        order_month_bucket TEXT,
        order_month_count INTEGER NOT NULL DEFAULT 0,
        last_api_key_id INTEGER REFERENCES reseller_api_keys(id) ON DELETE SET NULL,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # Per-key counters provide key-level identity/visibility while the actual
    # configured request limits remain reseller-wide to prevent key hopping.
    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_api_key_usage_counters(
        api_key_id INTEGER PRIMARY KEY REFERENCES reseller_api_keys(id) ON DELETE CASCADE,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE CASCADE,
        request_minute_bucket TEXT,
        request_minute_count INTEGER NOT NULL DEFAULT 0,
        request_day_bucket TEXT,
        request_day_count INTEGER NOT NULL DEFAULT 0,
        request_month_bucket TEXT,
        request_month_count INTEGER NOT NULL DEFAULT 0,
        order_minute_bucket TEXT,
        order_minute_count INTEGER NOT NULL DEFAULT 0,
        order_day_bucket TEXT,
        order_day_count INTEGER NOT NULL DEFAULT 0,
        order_month_bucket TEXT,
        order_month_count INTEGER NOT NULL DEFAULT 0,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
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
        "CREATE INDEX IF NOT EXISTS idx_reseller_key_usage_reseller ON reseller_api_key_usage_counters(reseller_id)",
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
    _safe_add_column("reseller_api_keys", "enabled", "INTEGER NOT NULL DEFAULT 1")
    # Phase 13 P5: optional key expiry (shop-time "YYYY-MM-DD HH:MM:SS"; NULL
    # = never expires) and rotation linkage (old key -> its successor).
    _safe_add_column("reseller_api_keys", "expires_at", "TEXT")
    _safe_add_column("reseller_api_keys", "rotated_to", "TEXT")
    _safe_add_column("reseller_api_logs", "reseller_public_id", "TEXT")
    _safe_add_column("reseller_api_logs", "key_id", "TEXT")
    _safe_add_column("reseller_api_logs", "ip", "TEXT")
    _safe_add_column("reseller_api_logs", "user_agent", "TEXT")
    _safe_add_column("reseller_wallet_transactions", "currency", "TEXT NOT NULL DEFAULT 'USDT'")
    _safe_add_column("reseller_wallet_transactions", "related_order_reference", "TEXT")
    # Phase 9: event subscription list for each webhook (JSON array of event type strings).
    _safe_add_column("reseller_webhooks", "events", "TEXT")
    # Phase 9 Prompt 3: create the transactional outbox and worker_jobs tables.
    from reseller_api.services.webhook_outbox import ensure_webhook_outbox_table
    ensure_webhook_outbox_table()
    from reseller_api.services.webhook_delivery import ensure_delivery_log_table
    ensure_delivery_log_table()
    if _pgcompat.use_postgres():
        cur.execute("CREATE OR REPLACE FUNCTION oggy_protect_reseller_ledger() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'reseller wallet ledger is immutable'; END; $$ LANGUAGE plpgsql")
        cur.execute("DROP TRIGGER IF EXISTS protect_reseller_ledger ON reseller_wallet_transactions")
        cur.execute("CREATE TRIGGER protect_reseller_ledger BEFORE UPDATE OR DELETE ON reseller_wallet_transactions FOR EACH ROW EXECUTE FUNCTION oggy_protect_reseller_ledger()")
    else:
        cur.execute("CREATE TRIGGER IF NOT EXISTS protect_reseller_ledger_update BEFORE UPDATE ON reseller_wallet_transactions BEGIN SELECT RAISE(ABORT, 'reseller wallet ledger is immutable'); END")
        cur.execute("CREATE TRIGGER IF NOT EXISTS protect_reseller_ledger_delete BEFORE DELETE ON reseller_wallet_transactions BEGIN SELECT RAISE(ABORT, 'reseller wallet ledger is immutable'); END")

    # Phase 7: reseller order metadata columns on the master orders table
    from reseller_api.services.order_service import ensure_order_metadata_columns
    ensure_order_metadata_columns()

    # Phase 15 P2: invoice document tables (draft/finalized/void + triggers)
    from database.invoice_db import ensure_invoice_tables
    ensure_invoice_tables()

    # Phase 15 P3: statement snapshot tables (open/finalized + triggers)
    from database.statement_db import ensure_statement_tables
    ensure_statement_tables()

    # Phase 15 P5: adjustment/refund business records (+ triggers)
    from database.adjustment_db import ensure_adjustment_tables
    ensure_adjustment_tables()

    # Phase 15 P6: reconciliation runs + discrepancies (append-only)
    from database.reconciliation_db import ensure_reconciliation_tables
    ensure_reconciliation_tables()

    # Phase 10: IP-based auth-failure counters (brute-force protection)
    ensure_ip_auth_counter_table()

    # Phase 10 P3: endpoint-specific quotas
    ensure_endpoint_quota_table()

    # Phase 10 P4: financial events audit log
    ensure_financial_event_table()

    # Phase 10 P7: reseller quota settings
    ensure_quota_settings_table()

    # Phase 10 Prompt 3: endpoint-specific quota counters
    ensure_endpoint_quota_table()

    # Phase 11 P3: reseller product mapping table
    ensure_product_mapping_table()

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
    reseller = get_reseller(reseller_id)
    if not reseller or reseller["status"] != "active":
        raise PermissionError("an active reseller account is required")
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
    """True when the presented secret matches the stored hash of an ACTIVE,
    unexpired key."""
    cur = get_cursor()
    cur.execute(
        "SELECT secret_hash, status, expires_at FROM reseller_api_keys WHERE key_id=?",
        (key_id,),
    )
    row = cur.fetchone()
    if not row or row[1] != "active":
        return False
    if row[2] and str(row[2]) <= now_text():   # Phase 13 P5: expiry enforced
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


def revoke_api_key_for_reseller(reseller_id: int, key_id: str) -> bool:
    """Ownership-scoped, permanent revocation for reseller-facing surfaces."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET status='revoked', revoked_at=? "
        "WHERE key_id=? AND reseller_id=? AND status='active'",
        (now_text(), key_id, reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_api_key_enabled_for_reseller(reseller_id: int, key_id: str,
                                     enabled: bool) -> bool:
    """Temporarily enable/disable an active key without ever reviving a
    revoked key. This helper is ownership-scoped for future management UIs."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET enabled=? "
        "WHERE key_id=? AND reseller_id=? AND status='active'",
        (1 if enabled else 0, key_id, reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_api_key_expiry_for_reseller(reseller_id: int, key_id: str,
                                    expires_at) -> bool:
    """Ownership-scoped expiry management for an ACTIVE key.

    expires_at: "YYYY-MM-DD HH:MM:SS" (shop timezone) or None to clear.
    A past timestamp is allowed — it simply disables the key immediately.
    Never revives or extends a revoked key.
    """
    if expires_at is not None:
        import re as _re
        if not isinstance(expires_at, str) or not _re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", expires_at):
            raise ValueError("expires_at must be 'YYYY-MM-DD HH:MM:SS' or None")
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET expires_at=? "
        "WHERE key_id=? AND reseller_id=? AND status='active'",
        (expires_at, key_id, reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0


def rotate_api_key_for_reseller(reseller_id: int, old_key_id: str,
                                grace_hours: float = 24.0):
    """Rotate an API key with a controlled overlap window.

    Creates a fresh key in the same environment and sets the old key's
    expires_at to now + grace_hours, so the reseller can migrate without
    losing access — and the old key retires automatically even if nobody
    remembers to revoke it manually.

    Rotation is exempt from the MAX_ACTIVE_KEYS cap *by design*: the window
    is temporary and self-closing.  The cap still applies to plain creation.

    Returns (new_key_id, new_secret, old_expires_at) on success.
    Returns None when the old key is missing, foreign, or not active.
    Raises PermissionError when the reseller is not active (key creation
    contract).  Never logs or stores the new secret in plaintext.
    """
    old = get_api_key(old_key_id)
    if not old or int(old["reseller_id"]) != int(reseller_id):
        return None
    if old["status"] != "active":
        return None
    new_key_id, new_secret = create_api_key(int(reseller_id), old["environment"])
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    expiry = (_now() + _td(hours=float(grace_hours))).strftime("%Y-%m-%d %H:%M:%S")
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET expires_at=?, rotated_to=? "
        "WHERE key_id=? AND reseller_id=? AND status='active'",
        (expiry, new_key_id, old_key_id, int(reseller_id)),
    )
    conn.commit()
    try:
        log_security_event(
            int(reseller_id), "api_key_rotated",
            description=(f"key {old_key_id} rotated -> {new_key_id}; "
                         f"old key expires {expiry}"),
        )
    except Exception:
        pass
    return new_key_id, new_secret, expiry


def get_api_auth_record(key_id: str):
    """Load the exact server-side fields needed for API authentication.

    The returned hash is internal-only. Callers must never serialize or log
    this record. No plaintext API secret exists in the database.
    """
    cur = get_cursor()
    cur.execute(
        "SELECT k.id, k.key_id, k.secret_hash, k.reseller_id, k.environment, "
        "k.status, k.enabled, k.last_used_at, k.expires_at, "
        "r.public_id, r.status, r.api_enabled, r.telegram_user_id, "
        "COALESCE(u.blocked, 0) "
        "FROM reseller_api_keys k "
        "JOIN resellers r ON r.id=k.reseller_id "
        "LEFT JOIN users u ON u.user_id=r.telegram_user_id "
        "WHERE k.key_id=?",
        (key_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = (
        "api_key_id", "key_id", "secret_hash", "reseller_id", "environment",
        "key_status", "key_enabled", "last_used_at", "expires_at",
        "reseller_public_id",
        "reseller_status", "api_enabled", "telegram_user_id", "user_blocked",
    )
    return _d(cols, row)


def get_api_authorization_snapshot(reseller_id: int, api_key_id: int):
    """Current key/reseller policy state scoped by BOTH authenticated IDs.

    A mismatched key/reseller pair returns no row, which is the database-level
    IDOR backstop for authorization decisions.
    """
    cur = get_cursor()
    cur.execute(
        "SELECT k.id, k.key_id, k.reseller_id, k.environment, k.status, k.enabled, "
        "r.public_id, r.status, r.api_enabled, r.rate_limit_per_min, "
        "r.orders_per_min, r.daily_request_limit, r.monthly_request_limit, "
        "r.daily_order_limit, r.monthly_order_limit, "
        "COALESCE(u.blocked, 0) "
        "FROM reseller_api_keys k "
        "JOIN resellers r ON r.id=k.reseller_id "
        "LEFT JOIN users u ON u.user_id=r.telegram_user_id "
        "WHERE k.id=? AND k.reseller_id=?",
        (api_key_id, reseller_id),
    )
    cols = (
        "api_key_id", "key_id", "reseller_id", "key_environment",
        "key_status", "key_enabled", "reseller_public_id", "reseller_status",
        "api_enabled", "rate_limit_per_min", "orders_per_min",
        "daily_request_limit", "monthly_request_limit", "daily_order_limit",
        "monthly_order_limit", "user_blocked",
    )
    return _d(cols, cur.fetchone())


def get_reseller_product_access_state(reseller_id: int, product_id: int):
    """Return master-product and reseller grant state without trusting a
    client ownership claim. Missing access rows are deny-by-default."""
    cur = get_cursor()
    cur.execute(
        "SELECT p.id, p.active, COALESCE(a.enabled, 0) "
        "FROM products p "
        "LEFT JOIN reseller_product_access a "
        "ON a.product_id=p.id AND a.reseller_id=? "
        "WHERE p.id=?",
        (reseller_id, product_id),
    )
    return _d(("product_id", "product_active", "access_enabled"), cur.fetchone())


def touch_api_key_used_throttled(api_key_id: int, min_interval_seconds: int = 60) -> bool:
    """Atomically update last_used_at at most once per interval.

    The conditional UPDATE avoids a write on every request and is safe when
    concurrent workers authenticate the same key.
    """
    interval = max(1, min(int(min_interval_seconds), 3600))
    stamp = now_text()
    cutoff = (now() - timedelta(seconds=interval)).strftime("%Y-%m-%d %H:%M:%S")
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_api_keys SET last_used_at=? "
        "WHERE id=? AND status='active' AND enabled=1 "
        "AND (last_used_at IS NULL OR last_used_at < ?)",
        (stamp, api_key_id, cutoff),
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


def ensure_product_public_id(product_id: int) -> str:
    """Return one immutable opaque public id for an existing master product.

    Product creation is spread across legacy Telegram and Web Admin surfaces,
    so assigning on first API exposure is the only additive approach that
    covers every existing insert path without changing customer behavior.
    The conditional UPDATE and unique index make concurrent assignment safe.
    """
    product_id = int(product_id)
    conn, cur = get_conn(), get_cursor()
    cur.execute("SELECT public_id FROM products WHERE id=?", (product_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError("product does not exist")
    if row[0]:
        return str(row[0])

    for _attempt in range(12):
        candidate = "PRD-" + "".join(
            secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(12)
        )
        try:
            cur.execute(
                "UPDATE products SET public_id=? "
                "WHERE id=? AND (public_id IS NULL OR public_id='')",
                (candidate, product_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            continue
        cur.execute("SELECT public_id FROM products WHERE id=?", (product_id,))
        row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    raise RuntimeError("could not allocate product public id")


def backfill_product_public_ids(batch_size: int = 500) -> int:
    """Assign opaque ids outside request handling in bounded startup batches.

    Existing products created before Phase 5 may have NULL public ids. Doing
    this during the idempotent database initialization prevents the first
    paginated API request from performing one write per returned product.
    Products created later by legacy admin surfaces still use the safe lazy
    fallback in ``ensure_product_public_id``.
    """
    batch_size = max(1, min(int(batch_size), 2000))
    conn, cur = get_conn(), get_cursor()
    updated = 0
    while True:
        cur.execute(
            "SELECT id FROM products "
            "WHERE public_id IS NULL OR public_id='' ORDER BY id ASC LIMIT ?",
            (batch_size,),
        )
        ids = [int(row[0]) for row in cur.fetchall()]
        if not ids:
            return updated
        try:
            for product_id in ids:
                candidate = "PRD-" + "".join(
                    secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(12)
                )
                cur.execute(
                    "UPDATE products SET public_id=? "
                    "WHERE id=? AND (public_id IS NULL OR public_id='')",
                    (candidate, product_id),
                )
                if cur.rowcount > 0:
                    updated += 1
            conn.commit()
        except Exception:
            # An extraordinarily unlikely random collision rolls back only one
            # bounded batch; the single-row helper then retries each id safely.
            conn.rollback()
            for product_id in ids:
                ensure_product_public_id(product_id)
                updated += 1


def list_reseller_api_products(reseller_id: int, *, page: int, limit: int,
                               search: str = "", category: str = "",
                               availability: str = ""):
    """Paginated, deny-by-default view over the existing master catalog.

    This is a reseller-scoped repository query, not a second catalog. Only an
    enabled access row can produce a result. Product activity follows the
    existing customer rule (products.active=1). Inactive categories are not
    exposed as metadata, while the active product remains searchable exactly
    as it is in the customer search flow.
    """
    reseller_id = int(reseller_id)
    page, limit = int(page), int(limit)
    where = ["a.reseller_id=?", "a.enabled=1", "p.active=1"]
    params = [reseller_id]

    term = str(search or "").strip()
    if term:
        pattern = f"%{_escape_like(term.lower())}%"
        where.append(
            "(LOWER(p.name) LIKE ? ESCAPE '!' OR "
            "LOWER(COALESCE(p.description, '')) LIKE ? ESCAPE '!')"
        )
        params.extend((pattern, pattern))

    category_name = str(category or "").strip()
    if category_name:
        if category_name.lower() == "uncategorized":
            where.append("p.category_id IS NULL")
        else:
            where.extend(("c.active=1", "LOWER(c.name)=?"))
            params.append(category_name.lower())

    joins = (
        " FROM products p "
        "JOIN reseller_product_access a ON a.product_id=p.id "
        "LEFT JOIN categories c ON c.id=p.category_id "
    )
    predicate = " WHERE " + " AND ".join(where)

    # Availability HAVING clause applied to the computed stock expression so
    # filtering is consistent with the value returned in the response.
    avail_filter = str(availability or "").strip().lower()
    if avail_filter == "in_stock":
        having = " HAVING CASE WHEN COUNT(ps.id)>0 THEN COUNT(ps.id) ELSE q.fallback_stock END > 0"
    elif avail_filter == "out_of_stock":
        having = " HAVING CASE WHEN COUNT(ps.id)>0 THEN COUNT(ps.id) ELSE q.fallback_stock END = 0"
    else:
        having = ""

    cur = get_cursor()
    if avail_filter:
        # The availability filter lives in a HAVING over the stock join, so a
        # plain COUNT(*) would overcount. Count the grouped rows instead so
        # pagination metadata (total/pages) matches the filtered result set.
        count_having = having.replace("q.fallback_stock", "COALESCE(p.stock, 0)")
        cur.execute(
            "SELECT COUNT(*) FROM (SELECT p.id" + joins
            + "LEFT JOIN product_stock ps ON ps.product_id=p.id AND ps.sold=0"
            + predicate
            + " GROUP BY p.id, p.stock" + count_having + ")",
            tuple(params),
        )
    else:
        cur.execute("SELECT COUNT(*)" + joins + predicate, tuple(params))
    count_row = cur.fetchone()
    total = int(count_row[0] or 0) if count_row else 0

    # Pagination happens in the inner query before stock rows are joined. This
    # keeps stock aggregation bounded to at most ``limit`` products instead of
    # grouping the entire product_stock table for every request.
    inner_sql = (
        "SELECT p.id AS product_id, p.public_id, p.name, "
        "COALESCE(p.description, '') AS description, "
        "COALESCE(p.validity, '') AS validity, "
        "COALESCE(p.warranty, '') AS warranty, "
        "COALESCE(p.max_per_order, 1) AS max_per_order, "
        "COALESCE(p.featured, 0) AS featured, "
        "CASE WHEN c.active=1 THEN c.name ELSE NULL END AS category_name, "
        "COALESCE(p.stock, 0) AS fallback_stock, p.price AS master_price, "
        "pr.rule_type, pr.value_cents, pr.value_bps "
        + joins
        + "LEFT JOIN reseller_pricing_rules pr ON pr.product_id=p.id "
          "AND pr.reseller_id=a.reseller_id AND pr.enabled=1 "
        + predicate
        + " ORDER BY COALESCE(p.featured, 0) DESC, p.id DESC LIMIT ? OFFSET ?"
    )
    grouped = (
        "q.product_id, q.public_id, q.name, q.description, q.validity, "
        "q.warranty, q.max_per_order, q.featured, q.category_name, "
        "q.fallback_stock, q.master_price, q.rule_type, q.value_cents, q.value_bps"
    )
    # ``having`` was computed above (shared with the total-count query so the
    # filtered result set and pagination metadata stay consistent).
    select_sql = (
        "SELECT " + grouped + ", "
        "CASE WHEN COUNT(ps.id)>0 THEN COUNT(ps.id) ELSE q.fallback_stock END "
        "FROM (" + inner_sql + ") q "
        "LEFT JOIN product_stock ps ON ps.product_id=q.product_id AND ps.sold=0 "
        "GROUP BY " + grouped
        + having
        + " ORDER BY q.featured DESC, q.product_id DESC"
    )
    offset = (page - 1) * limit
    cur.execute(select_sql, tuple(params + [limit, offset]))
    cols = (
        "product_id", "public_id", "name", "description", "validity",
        "warranty", "max_per_order", "featured", "category_name",
        "fallback_stock", "master_price", "rule_type", "value_cents",
        "value_bps", "available_quantity",
    )
    rows = _ds(cols, cur.fetchall())
    for item in rows:
        if not item["public_id"]:
            item["public_id"] = ensure_product_public_id(item["product_id"])
    return rows, total


def get_reseller_api_product(reseller_id: int, public_id: str):
    """Return one accessible active master product by opaque public id.

    The reseller predicate and enabled access grant are part of the lookup so
    callers cannot distinguish a missing product from another reseller's or
    an inactive product. Only explicitly selected API-safe catalog columns
    are returned; the internal id is included solely for server-side policy
    and pricing resolution and must never be serialized.
    """
    cur = get_cursor()
    cur.execute(
        "SELECT p.id, p.public_id, p.name, COALESCE(p.description, ''), "
        "COALESCE(p.validity, ''), COALESCE(p.warranty, ''), "
        "COALESCE(p.max_per_order, 1), COALESCE(p.featured, 0), "
        "CASE WHEN c.active=1 THEN c.name ELSE NULL END, "
        "CASE WHEN COUNT(ps.id)>0 THEN COUNT(ps.id) "
        "ELSE COALESCE(p.stock, 0) END, "
        "p.price, pr.rule_type, pr.value_cents, pr.value_bps "
        "FROM products p "
        "JOIN reseller_product_access a ON a.product_id=p.id "
        "LEFT JOIN categories c ON c.id=p.category_id "
        "LEFT JOIN reseller_pricing_rules pr ON pr.product_id=p.id "
        "AND pr.reseller_id=a.reseller_id AND pr.enabled=1 "
        "LEFT JOIN product_stock ps ON ps.product_id=p.id AND ps.sold=0 "
        "WHERE p.public_id=? AND p.active=1 "
        "AND a.reseller_id=? AND a.enabled=1 "
        "GROUP BY p.id, p.public_id, p.name, p.description, p.validity, "
        "p.warranty, p.max_per_order, p.featured, c.active, c.name, p.stock, "
        "p.price, pr.rule_type, pr.value_cents, pr.value_bps "
        "LIMIT 1",
        (str(public_id), int(reseller_id)),
    )
    cols = (
        "product_id", "public_id", "name", "description", "validity",
        "warranty", "max_per_order", "featured", "category_name",
        "available_quantity", "master_price", "rule_type", "value_cents",
        "value_bps",
    )
    return _d(cols, cur.fetchone())


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
                            reference: str = None, description: str = "",
                            currency: str = "USDT", related_order_reference: str = None):
    """Atomically move balance and append exactly one immutable ledger row."""
    if tx_type not in TX_TYPES:
        raise ValueError("invalid tx_type")
    if isinstance(reseller_id, bool) or not isinstance(reseller_id, int) or reseller_id <= 0:
        raise ValueError("invalid reseller_id")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise TypeError("amount_cents must be an integer")
    if abs(amount_cents) > MAX_WALLET_BALANCE_CENTS:
        raise ValueError("amount_cents exceeds wallet limit")
    if amount_cents == 0:
        raise ValueError("amount_cents must be non-zero")
    if tx_type in {"topup_credit", "refund_credit", "adjustment_credit"} and amount_cents < 0:
        raise ValueError("credit amount must be positive")
    if tx_type in {"purchase_debit", "adjustment_debit"} and amount_cents > 0:
        raise ValueError("debit amount must be negative")
    if not isinstance(reference, str) or not _WALLET_REFERENCE_RE.fullmatch(reference):
        raise ValueError("valid idempotency reference required")
    if related_order_reference is not None and (
        not isinstance(related_order_reference, str)
        or not _WALLET_REFERENCE_RE.fullmatch(related_order_reference)
    ):
        raise ValueError("invalid related order reference")
    if not isinstance(description, str) or len(description) > 500:
        raise ValueError("invalid description")
    if currency != "USDT":
        raise ValueError("unsupported currency")
    conn, cur = get_conn(), get_cursor()
    stamp = now_text()

    def classify_existing(row):
        """Return duplicate only when the stored request is exactly equivalent."""
        existing = _d(_LEDGER_COLS, row)
        same_request = (
            int(existing["amount_cents"]) == amount_cents
            and existing["currency"] == currency
            and (existing["related_order_reference"] or None)
                == (related_order_reference or None)
            and (existing["description"] or "") == (description or "")
        )
        return ("duplicate" if same_request else "idempotency_conflict",
                int(existing["id"]))

    try:
        with _pgcompat.atomic(conn):
            cur.execute("SELECT id FROM resellers WHERE id=?", (reseller_id,))
            if not cur.fetchone():
                raise ValueError("reseller does not exist")
            cur.execute("INSERT OR IGNORE INTO reseller_wallet(reseller_id) VALUES(?)", (reseller_id,))
            if reference is not None:
                # Fetch the complete immutable request so a reused key with a
                # different payload is rejected rather than silently accepted.
                cur.execute("SELECT * FROM reseller_wallet_transactions WHERE reseller_id=? AND tx_type=? AND reference=?", (reseller_id, tx_type, reference))
                row = cur.fetchone()
                if row:
                    return classify_existing(row)
            if amount_cents > 0:
                cur.execute("UPDATE reseller_wallet SET balance_cents=balance_cents+?, updated=? WHERE reseller_id=? AND balance_cents<=?", (amount_cents, stamp, reseller_id, MAX_WALLET_BALANCE_CENTS - amount_cents))
            else:
                cur.execute("UPDATE reseller_wallet SET balance_cents=balance_cents+?, updated=? WHERE reseller_id=? AND balance_cents+?>=0", (amount_cents, stamp, reseller_id, amount_cents))
            if cur.rowcount == 0:
                cur.execute("SELECT balance_cents FROM reseller_wallet WHERE reseller_id=?", (reseller_id,))
                row = cur.fetchone()
                return ("balance_limit_exceeded" if amount_cents > 0 else "insufficient_funds"), int(row[0] if row else 0)
            cur.execute("SELECT balance_cents FROM reseller_wallet WHERE reseller_id=?", (reseller_id,))
            balance_after = int(cur.fetchone()[0])
            cur.execute("INSERT INTO reseller_wallet_transactions(reseller_id,tx_type,amount_cents,balance_after_cents,reference,description,created,currency,related_order_reference) VALUES(?,?,?,?,?,?,?,?,?)", (reseller_id, tx_type, amount_cents, balance_after, reference, description, stamp, currency, related_order_reference))
            txid = cur.lastrowid
    except Exception as exc:
        if reference is not None and _pgcompat.is_unique_violation(exc):
            cur.execute("SELECT * FROM reseller_wallet_transactions WHERE reseller_id=? AND tx_type=? AND reference=?", (reseller_id, tx_type, reference))
            row = cur.fetchone()
            if row:
                return classify_existing(row)
        raise
    return "posted", txid


# ---------------------------------------------------------------------------
# Phase 14 P6 — webhook, delivery & fulfillment analytics
#
# Sources of truth: webhook_outbox (business events), webhook_delivery_log
# (per-attempt HTTP results), reseller_webhooks (configured endpoints),
# orders (fulfillment lifecycle per the P14 P3 status model).  Read-only;
# no external calls; no secrets or payloads anywhere in the output.
#
# Latency notes: the schema stores second-resolution timestamps, so latency
# is reported in whole seconds (outbox created_at -> processed_at) — honest
# precision, no fabricated millisecond numbers.
# ---------------------------------------------------------------------------

def _webhook_analytics(where: str, params: tuple, days: int,
                       since: str = None) -> dict:
    cur = get_cursor()
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    if since is None:
        since = (_now() - _td(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")

    scope = f"{where} AND created_at >= ?"
    cur.execute(
        f"""
        SELECT COUNT(*),
         SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END),
         SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),
         SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),
         SUM(CASE WHEN attempts > 1 THEN 1 ELSE 0 END),
         AVG(CASE WHEN status='delivered' AND processed_at IS NOT NULL
             THEN CAST(strftime('%s', processed_at) AS INTEGER)
                  - CAST(strftime('%s', created_at) AS INTEGER) END)
        FROM webhook_outbox WHERE {scope}
        """,
        params + (since,),
    )
    total, delivered, pending, dead, retried, avg_lat = cur.fetchone()

    cur.execute(
        f"SELECT event_type, COUNT(*) FROM webhook_outbox WHERE {scope}"
        " GROUP BY event_type ORDER BY 2 DESC",
        params + (since,),
    )
    by_type = {t: int(c) for t, c in cur.fetchall()}

    dscope = f"{where} AND created_at >= ?"
    cur.execute(
        f"""
        SELECT COUNT(*),
         SUM(CASE WHEN success=1 THEN 1 ELSE 0 END),
         SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),
         SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END)
        FROM webhook_delivery_log WHERE {dscope}
        """,
        params + (since,),
    )
    d_total, d_ok, d_fail, d_retries = cur.fetchone()

    cur.execute(
        f"SELECT COALESCE(http_status,0) code, COUNT(*) FROM webhook_delivery_log"
        f" WHERE {dscope} GROUP BY code ORDER BY 2 DESC",
        params + (since,),
    )
    http_dist = {}
    for code, c in cur.fetchall():
        key = str(int(code)) if code else "no_response"
        http_dist[key] = int(c)

    return {
        "window_days": max(1, int(days)),
        "events_total": int(total or 0),
        "delivered": int(delivered or 0),
        "pending": int(pending or 0),
        "dead_lettered": int(dead or 0),
        "events_retried": int(retried or 0),
        "avg_delivery_seconds": (round(float(avg_lat), 1)
                                 if avg_lat is not None else None),
        "by_event_type": by_type,
        "delivery_attempts": int(d_total or 0),
        "attempts_succeeded": int(d_ok or 0),
        "attempts_failed": int(d_fail or 0),
        "retry_attempts": int(d_retries or 0),
        "http_status_distribution": http_dist,
    }


def get_platform_webhook_analytics(days: int = 7, since: str = None) -> dict:
    """Owner view: platform webhook + fulfillment operational metrics."""
    cur = get_cursor()
    out = _webhook_analytics("1=1", (), days, since=since)
    cur.execute(
        "SELECT COUNT(*), SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END)"
        " FROM reseller_webhooks"
    )
    total_wh, enabled_wh = cur.fetchone()
    out["webhooks_configured"] = int(total_wh or 0)
    out["webhooks_enabled"] = int(enabled_wh or 0)

    # fulfillment lifecycle (reseller-API orders, same window)
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    if since is None:
        since = (_now() - _td(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        SELECT COUNT(*),
         SUM(CASE WHEN COALESCE(master_status,'') LIKE '%Failed%' THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(master_status,'') LIKE '%Cancelled%' THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(master_status,'') NOT LIKE '%Failed%'
                   AND COALESCE(master_status,'') NOT LIKE '%Cancelled%'
                   AND (delivery IS NULL OR delivery='') THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(master_status,'') NOT LIKE '%Failed%'
                   AND COALESCE(master_status,'') NOT LIKE '%Cancelled%'
                   AND delivery IS NOT NULL AND delivery<>'' THEN 1 ELSE 0 END)
        FROM orders
        WHERE source='reseller_api' AND reseller_id IS NOT NULL AND created >= ?
        """,
        (since,),
    )
    f_total, f_failed, f_cancelled, f_pending, f_completed = cur.fetchone()
    out["fulfillment"] = {
        "orders": int(f_total or 0),
        "completed": int(f_completed or 0),
        "pending": int(f_pending or 0),
        "failed": int(f_failed or 0),
        "cancelled": int(f_cancelled or 0),
        "success_rate": (round(int(f_completed or 0) / int(f_total), 4)
                         if f_total else None),
    }
    return out


def get_reseller_webhook_analytics(reseller_id: int, days: int = 30) -> dict:
    """One reseller's webhook/delivery activity (reseller-safe)."""
    cur = get_cursor()
    out = _webhook_analytics("reseller_id=?", (int(reseller_id),), days)
    cur.execute(
        "SELECT COUNT(*), SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END)"
        " FROM reseller_webhooks WHERE reseller_id=?",
        (int(reseller_id),),
    )
    total_wh, enabled_wh = cur.fetchone()
    out["webhooks_configured"] = int(total_wh or 0)
    out["webhooks_enabled"] = int(enabled_wh or 0)
    return out


# ---------------------------------------------------------------------------
# Phase 14 P5 — product sales, stock & inventory analytics
#
# Sources of truth: master `products` + `product_stock` (code rows with
# sold flags) + `reseller_product_mappings` (adoption) + `orders` (sales
# via the Phase 14 P3 status model).  Read-only; index-bounded; integer
# cents end to end.  No duplicate stock is tracked — availability is read
# from the master inventory at query time.
#
# "units_sold" follows the P3 kept-units definition (non-terminal orders
# minus partially-refunded units).  "out-of-stock events" are not a tracked
# event stream in the current architecture — we report the truthful
# per-product stockout STATE (availability == 0) instead of fabricating
# history.
# ---------------------------------------------------------------------------

def get_product_sales_analytics(days: int = 30, since: str = None) -> dict:
    """Owner view: per-product sales + inventory picture over a window.

    Returns per-product rows plus a per-category rollup.  All sales counts
    cover reseller-API orders in the window; availability/adoption reflect
    the current master inventory and mapping tables.
    """
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    cur = get_cursor()
    if since is None:
        since = (_now() - _td(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    else:
        # keep window_days truthful when the caller passed an explicit bound
        try:
            from datetime import datetime as _dt
            span = (_now().date() - _dt.strptime(since[:10], "%Y-%m-%d").date()).days + 1
            days = max(1, span)
        except Exception:
            pass

    # --- sales per product in window (reseller-API orders) ---------------
    cur.execute(
        """
        SELECT o.product_id,
               COUNT(*) orders,
               SUM(CASE WHEN COALESCE(o.master_status,'') NOT LIKE '%Failed%'
                         AND COALESCE(o.master_status,'') NOT LIKE '%Cancelled%'
                       THEN 1 ELSE 0 END) ok_orders,
               SUM(CASE WHEN COALESCE(o.master_status,'') LIKE '%Failed%'
                       THEN 1 ELSE 0 END) failed_orders,
               SUM(CASE WHEN COALESCE(o.master_status,'') LIKE '%Cancelled%'
                       THEN 1 ELSE 0 END) cancelled_orders,
               COALESCE(SUM(CASE WHEN COALESCE(o.master_status,'') NOT LIKE '%Failed%'
                                  AND COALESCE(o.master_status,'') NOT LIKE '%Cancelled%'
                            THEN o.quantity ELSE 0 END),0) units,
               COALESCE(SUM(o.unit_price_cents*o.quantity),0) gross,
               COUNT(DISTINCT o.reseller_id) resellers
        FROM orders o
        WHERE o.source='reseller_api' AND o.reseller_id IS NOT NULL
          AND o.created >= ?
        GROUP BY o.product_id
        """,
        (since,),
    )
    sales = {}
    for pid, orders, ok_o, f_o, c_o, units, gross, resellers in cur.fetchall():
        sales[int(pid)] = {
            "orders": int(orders or 0),
            "successful_orders": int(ok_o or 0),
            "failed_orders": int(f_o or 0),
            "cancelled_orders": int(c_o or 0),
            "units": int(units or 0),
            "gross_cents": int(gross or 0),
            "resellers": int(resellers or 0),
        }

    # partial-refunded units per product (kept-units correction, exact)
    cur.execute(
        """
        SELECT o.product_id,
               COALESCE(SUM(t.amount_cents / o.unit_price_cents),0) refunded_units
        FROM reseller_wallet_transactions t
        JOIN orders o ON t.reference = 'rapi.partial_refund.order.' || o.id
                  AND t.reseller_id = o.reseller_id
        WHERE t.tx_type='refund_credit' AND o.created >= ?
          AND o.source='reseller_api'
        GROUP BY o.product_id
        """,
        (since,),
    )
    for pid, ru in cur.fetchall():
        if int(pid) in sales:
            sales[int(pid)]["units"] = max(
                0, sales[int(pid)]["units"] - int(ru or 0))

    # --- master product + inventory + adoption ----------------------------
    cur.execute(
        """
        SELECT p.id, p.public_id, p.name, p.active, p.category_id,
               c.name AS category_name,
               (SELECT COUNT(*) FROM product_stock s
                 WHERE s.product_id=p.id AND s.sold=0) codes_available,
               (SELECT COUNT(*) FROM product_stock s
                 WHERE s.product_id=p.id AND s.sold=1) codes_consumed,
               COALESCE(p.stock,0) manual_stock,
               (SELECT COUNT(*) FROM reseller_product_mappings m
                 WHERE m.product_id=p.id AND m.enabled=1) adopted_by
        FROM products p
        LEFT JOIN categories c ON c.id=p.category_id
        """
    )
    products = []
    for (pid, pub, name, active, cat_id, cat_name, codes_avail,
         codes_consumed, manual_stock, adopted_by) in cur.fetchall():
        pid = int(pid)
        s = sales.get(pid, {})
        availability = int(codes_avail or 0)
        if availability == 0 and int(manual_stock or 0) > 0:
            availability = int(manual_stock)
        products.append({
            "product_id": str(pub or ensure_product_public_id(pid)),
            "name": str(name or ""),
            "active": bool(active),
            "category": cat_name or None,
            "orders": s.get("orders", 0),
            "successful_orders": s.get("successful_orders", 0),
            "failed_orders": s.get("failed_orders", 0),
            "cancelled_orders": s.get("cancelled_orders", 0),
            "units_sold": s.get("units", 0),
            "gross_sales": from_cents(s.get("gross_cents", 0)),
            "resellers": s.get("resellers", 0),
            "adopted_by": int(adopted_by or 0),   # enabled mappings
            "available_now": availability,
            "stock_consumed_codes": int(codes_consumed or 0),
            "out_of_stock": availability == 0,
        })
    products.sort(key=lambda r: (-r["units_sold"], r["name"]))

    # --- category rollup ---------------------------------------------------
    by_cat: dict = {}
    for row in products:
        key = row["category"] or "Uncategorized"
        agg = by_cat.setdefault(key, {"category": key, "products": 0,
                                      "orders": 0, "units_sold": 0,
                                      "gross_cents": 0})
        agg["products"] += 1
        agg["orders"] += row["orders"]
        agg["units_sold"] += row["units_sold"]
        agg["gross_cents"] += int(round(float(row["gross_sales"]) * 100))
    categories = []
    for agg in by_cat.values():
        agg["gross_sales"] = from_cents(agg.pop("gross_cents"))
        categories.append(agg)
    categories.sort(key=lambda r: -r["units_sold"])

    active_products = [r for r in products if r["active"]]
    return {
        "window_days": max(1, int(days)),
        "currency": "USDT",
        "products_total": len(products),
        "products_with_sales": sum(1 for r in products if r["orders"] > 0),
        "top_products": products[:10],
        "low_performers": [r for r in products
                           if r["active"] and r["orders"] == 0][:10],
        "out_of_stock_count": sum(1 for r in active_products if r["out_of_stock"]),
        "categories": categories,
        "products": products,
    }


# ---------------------------------------------------------------------------
# Phase 14 P4 — wallet & financial analytics
#
# Source of truth: the immutable reseller_wallet_transactions ledger +
# reseller_wallet balance.  Pure read-only aggregation — analytics never
# write and never join the transaction-processing path.  Integer cents
# everywhere; from_cents only for display strings.
#
# Ledger tx types (whitelisted at the schema level):
#   topup_credit, refund_credit, adjustment_credit (positive amounts)
#   purchase_debit, adjustment_debit (negative amounts)
#
# Definitions:
#   total_purchases — Σ |purchase_debit|  (orders only)
#   total_debits    — Σ |purchase_debit + adjustment_debit|
#   total_credits   — Σ topup + refund + adjustment credits
#   refunds         — Σ refund_credit
#   net_spend       — total_purchases − refunds
# ---------------------------------------------------------------------------

_LEDGER_SUMMARY_SQL = """
    SELECT
     COUNT(*),
     COALESCE(SUM(CASE WHEN tx_type='purchase_debit' THEN -amount_cents END),0),
     COALESCE(SUM(CASE WHEN tx_type='purchase_debit' THEN 1 END),0),
     COALESCE(SUM(CASE WHEN tx_type IN ('purchase_debit','adjustment_debit')
                   THEN -amount_cents END),0),
     COALESCE(SUM(CASE WHEN tx_type IN ('topup_credit','refund_credit',
                                        'adjustment_credit')
                   THEN amount_cents END),0),
     COALESCE(SUM(CASE WHEN tx_type='refund_credit' THEN amount_cents END),0),
     COALESCE(SUM(CASE WHEN tx_type='refund_credit' THEN 1 END),0)
    FROM reseller_wallet_transactions
"""


def get_wallet_financial_summary(reseller_id: int) -> dict:
    """One reseller's financial summary (reseller-safe)."""
    cur = get_cursor()
    rid = int(reseller_id)
    cur.execute(_LEDGER_SUMMARY_SQL + " WHERE reseller_id=?", (rid,))
    (tx_count, purchases, purchase_count, debits, credits,
     refunds, refund_count) = cur.fetchone()
    balance = get_wallet_balance_cents(rid)
    purchases = int(purchases or 0)
    refunds = int(refunds or 0)
    return {
        "balance_cents": balance,
        "balance": from_cents(balance),
        "currency": "USDT",
        "transaction_count": int(tx_count or 0),
        "total_purchases_cents": purchases,
        "total_purchases": from_cents(purchases),
        "purchase_count": int(purchase_count or 0),
        "total_debits_cents": int(debits or 0),
        "total_debits": from_cents(int(debits or 0)),
        "total_credits_cents": int(credits or 0),
        "total_credits": from_cents(int(credits or 0)),
        "refunds_cents": refunds,
        "refunds": from_cents(refunds),
        "refund_count": int(refund_count or 0),
        "net_spend_cents": purchases - refunds,
        "net_spend": from_cents(purchases - refunds),
    }


def get_platform_financial_summary() -> dict:
    """Owner view: platform totals + per-reseller spending board."""
    cur = get_cursor()
    cur.execute(_LEDGER_SUMMARY_SQL)
    (tx_count, purchases, purchase_count, debits, credits,
     refunds, refund_count) = cur.fetchone()
    purchases = int(purchases or 0)
    refunds = int(refunds or 0)

    cur.execute(
        "SELECT COALESCE(SUM(balance_cents),0), COUNT(*) FROM reseller_wallet"
    )
    total_balance, wallets = cur.fetchone()

    # per-reseller: balance, purchases, refunds, net spend (board)
    cur.execute(
        """
        SELECT w.reseller_id,
               w.balance_cents,
               COALESCE(SUM(CASE WHEN t.tx_type='purchase_debit'
                            THEN -t.amount_cents END),0) purchases,
               COALESCE(SUM(CASE WHEN t.tx_type='refund_credit'
                            THEN t.amount_cents END),0) refunds,
               COUNT(t.id) txs,
               r.public_id, r.name
        FROM reseller_wallet w
        LEFT JOIN reseller_wallet_transactions t ON t.reseller_id=w.reseller_id
        LEFT JOIN resellers r ON r.id=w.reseller_id
        GROUP BY w.reseller_id
        ORDER BY purchases DESC
        LIMIT 100
        """
    )
    board = []
    for rid, bal, pur, ref, txs, rpub, rname in cur.fetchall():
        row = (rpub, rname)
        board.append({
            "reseller_id": str(row[0] or ""),
            "name": str(row[1] or ""),
            "balance": from_cents(int(bal or 0)),
            "total_purchases": from_cents(int(pur or 0)),
            "refunds": from_cents(int(ref or 0)),
            "net_spend": from_cents(int(pur or 0) - int(ref or 0)),
            "transaction_count": int(txs or 0),
        })

    return {
        "currency": "USDT",
        "transaction_count": int(tx_count or 0),
        "total_purchases_cents": purchases,
        "total_purchases": from_cents(purchases),
        "purchase_count": int(purchase_count or 0),
        "total_debits_cents": int(debits or 0),
        "total_debits": from_cents(int(debits or 0)),
        "total_credits_cents": int(credits or 0),
        "total_credits": from_cents(int(credits or 0)),
        "refunds_cents": refunds,
        "refunds": from_cents(refunds),
        "refund_count": int(refund_count or 0),
        "net_spend_cents": purchases - refunds,
        "net_spend": from_cents(purchases - refunds),
        "balances_held_cents": int(total_balance or 0),
        "balances_held": from_cents(int(total_balance or 0)),
        "wallets": int(wallets or 0),
        "resellers": board,
    }


# ---------------------------------------------------------------------------
# Phase 14 P3 — reseller order & sales analytics
#
# Source of truth: the master `orders` table (reseller rows carry
# source='reseller_api', reseller_id, quantity, unit_price_cents,
# master_status) joined with the immutable wallet ledger for refunds
# (refund_credit rows referenced rapi.{refund|cancel_refund|partial_refund}
# .order.<id>).  Nothing is counted twice and nothing is recomputed from
# client-supplied data — every figure derives from authoritative records.
#
# Status model (mirrors the P12 lifecycle mapping):
#   failed    — master_status LIKE '%Failed%'
#   cancelled — master_status LIKE '%Cancelled%'
#   pending   — neither terminal, and no delivery content stored yet
#               (manual-fulfillment products awaiting delivery)
#   completed — everything else (code products deliver instantly at order)
#
# Financial definitions (integer cents end to end; from_cents only for
# display — Decimal-safe string output as elsewhere in the project):
#   gross_sales       — Σ unit_price*quantity over ALL orders in scope
#   refunds_full      — Σ full-failure refunds      (rapi.refund.order.*)
#   refunds_cancel    — Σ cancellation refunds      (rapi.cancel_refund.*)
#   refunds_partial   — Σ partial refunds           (rapi.partial_refund.*)
#   net_sales         — gross − all refunds
#   avg_order_value   — gross / total_orders
#   quantity_sold     — units charged and kept (non-terminal orders minus
#                       partially-refunded units)
# ---------------------------------------------------------------------------

_SALES_SCOPE_WHERE = (
    "o.reseller_id IS NOT NULL AND o.source='reseller_api'"
)


def _shop_day_bounds_to_utc(date_from: str = None, date_to: str = None):
    """Inclusive SHOP-timezone day bounds converted to UTC instants.

    The orders.created column stamps UTC (database default); the business
    day boundary the user means is shop-time (utils.timezone).  Converting
    is what makes "today" correct on both sides of the IST/UTC day flip
    (P15.10 finding: the P14 filter compared shop-day bounds to UTC
    timestamps and silently emptied the window after the flip)."""
    import re as _re
    from datetime import datetime, timezone
    from utils.timezone import TZ
    def _ok(v):
        return isinstance(v, str) and _re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
    lo = hi = None
    if _ok(date_from):
        lo = (datetime.strptime(date_from, "%Y-%m-%d")
              .replace(tzinfo=TZ).astimezone(timezone.utc)
              .strftime("%Y-%m-%d %H:%M:%S"))
    if _ok(date_to):
        hi = (datetime.strptime(date_to, "%Y-%m-%d")
              .replace(hour=23, minute=59, second=59, tzinfo=TZ)
              .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    return lo, hi


def _sales_day_bounds(date_from: str = None, date_to: str = None):
    """Order-scope bounds: shop-day semantics converted to the UTC clock of
    the orders.created column.  (Ledger bounds stay shop-time — see the
    refund scoping in _sales_analytics.)"""
    return _shop_day_bounds_to_utc(date_from, date_to)


def _sales_analytics(where: str, params: tuple, refund_where: str,
                     refund_params: tuple,
                     refund_window: tuple = None) -> dict:
    """Shared aggregation over a bounded, indexed order scope.

    refund_window (lo, hi) — optional SHOP-time bounds applied to refund
    ledger rows (the ledger stamps shop time; the order scope uses UTC
    after the P15.10 conversion).  Refunds count in the window they
    OCCURRED, matching invoice/statement semantics."""
    cur = get_cursor()
    cur = get_cursor()

    # master_status is NULL for instantly-completed orders — COALESCE to ''
    # so the NOT LIKE buckets include them (NULL LIKE anything is NULL).
    cur.execute(
        f"""
        SELECT COUNT(*),
         SUM(CASE WHEN COALESCE(o.master_status,'') LIKE '%Failed%' THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(o.master_status,'') LIKE '%Cancelled%' THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(o.master_status,'') NOT LIKE '%Failed%'
                   AND COALESCE(o.master_status,'') NOT LIKE '%Cancelled%'
                   AND (o.delivery IS NULL OR o.delivery='') THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(o.master_status,'') NOT LIKE '%Failed%'
                   AND COALESCE(o.master_status,'') NOT LIKE '%Cancelled%'
                   AND o.delivery IS NOT NULL AND o.delivery<>'' THEN 1 ELSE 0 END),
         SUM(CASE WHEN COALESCE(o.master_status,'') NOT LIKE '%Failed%'
                   AND COALESCE(o.master_status,'') NOT LIKE '%Cancelled%'
                   AND o.delivery IS NOT NULL AND o.delivery<>''
                 THEN o.unit_price_cents*o.quantity ELSE 0 END),
         COALESCE(SUM(o.unit_price_cents*o.quantity),0),
         COALESCE(SUM(CASE WHEN COALESCE(o.master_status,'') NOT LIKE '%Failed%'
                            AND COALESCE(o.master_status,'') NOT LIKE '%Cancelled%'
                      THEN o.quantity ELSE 0 END),0)
        FROM orders o WHERE {where}
        """,
        params,
    )
    (total, failed, cancelled, pending, completed, completed_cents,
     gross_cents, qty_sold) = cur.fetchone()
    total = int(total or 0)
    completed = int(completed or 0)
    gross_cents = int(gross_cents or 0)

    # --- refunds by kind (ledger is authoritative; prefix = refund kind) ---
    # When the order scope filters by product, the refund scope must follow
    # it — otherwise a per-product drilldown would leak sibling products'
    # refunds.  We then join the ledger through the scoped orders.
    product_scoped = "o.product_id=?" in where
    refunds = {}
    for kind, prefix in (("full", "rapi.refund.order."),
                         ("cancellation", "rapi.cancel_refund.order."),
                         ("partial", "rapi.partial_refund.order.")):
        refund_window_sql = ""
        refund_window_params = ()
        if refund_window and refund_window[0]:
            refund_window_sql += " AND t.created >= ?"
            refund_window_params += (refund_window[0],)
        if refund_window and refund_window[1]:
            refund_window_sql += " AND t.created <= ?"
            refund_window_params += (refund_window[1],)
        if product_scoped:
            cur.execute(
                "SELECT COALESCE(SUM(t.amount_cents),0), COUNT(*)"
                " FROM reseller_wallet_transactions t"
                " JOIN orders o ON t.reference = ? || o.id"
                "           AND t.reseller_id = o.reseller_id"
                f" WHERE t.tx_type='refund_credit' AND {where}",
                (prefix,) + params,
            )
        else:
            cur.execute(
                "SELECT COALESCE(SUM(amount_cents),0), COUNT(*)"
                " FROM reseller_wallet_transactions t"
                " WHERE t.tx_type='refund_credit' AND t.reference LIKE ?"
                + (f" AND {refund_where}" if refund_where else "")
                + refund_window_sql,
                (prefix + "%",) + tuple(refund_params) + refund_window_params,
            )
        amount, cnt = cur.fetchone()
        refunds[kind] = {"cents": int(amount or 0), "count": int(cnt or 0)}
    refunds_total_cents = sum(r["cents"] for r in refunds.values())

    # --- partially-refunded units: partial refunds are exactly
    # unit_price x failed_units (Phase 8 handler), so units are derivable
    # exactly from the ledger (integer division is exact by construction).
    cur.execute(
        f"""
        SELECT COALESCE(SUM(t.amount_cents / o.unit_price_cents),0)
        FROM reseller_wallet_transactions t
        JOIN orders o ON t.reference = 'rapi.partial_refund.order.' || o.id
                  AND t.reseller_id = o.reseller_id
        WHERE {where} AND t.tx_type='refund_credit'
        """,
        params,
    )
    partial_units = int(cur.fetchone()[0] or 0)

    # --- refunded orders: distinct orders with >=1 refund row -------------
    cur.execute(
        f"""
        SELECT COUNT(*) FROM orders o
        WHERE {where} AND EXISTS (
            SELECT 1 FROM reseller_wallet_transactions t
            WHERE t.reseller_id = o.reseller_id
              AND t.tx_type='refund_credit'
              AND t.reference IN (
                  'rapi.refund.order.' || o.id,
                  'rapi.cancel_refund.order.' || o.id,
                  'rapi.partial_refund.order.' || o.id))
        """,
        params,
    )
    refunded_orders = int(cur.fetchone()[0] or 0)

    return {
        "orders_total": total,
        "completed": completed,
        "pending": int(pending or 0),
        "failed": int(failed or 0),
        "cancelled": int(cancelled or 0),
        "refunded_orders": refunded_orders,
        # quantity sold = units charged and KEPT (partial-refunded units
        # subtract; failed/cancelled orders never entered the bucket)
        "quantity_sold": max(0, int(qty_sold or 0) - partial_units),
        "gross_sales_cents": gross_cents,
        "completed_sales_cents": int(completed_cents or 0),
        "refunds_full_cents": refunds["full"]["cents"],
        "refunds_cancellation_cents": refunds["cancellation"]["cents"],
        "refunds_partial_cents": refunds["partial"]["cents"],
        "refunds_total_cents": refunds_total_cents,
        "refund_events": sum(r["count"] for r in refunds.values()),
        "net_sales_cents": gross_cents - refunds_total_cents,
        "avg_order_value_cents": (gross_cents // total) if total else 0,
    }


def get_reseller_sales_analytics(reseller_id: int, date_from: str = None,
                                 date_to: str = None, days: int = 30,
                                 product_id: int = None) -> dict:
    """Sales analytics for ONE reseller (reseller-safe, index-bounded).

    product_id (optional, server-resolved): drill down to one product.
    product_id=-1 (inaccessible product, per the controller's non-oracle
    convention) yields a zeroed scope."""
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    rid = int(reseller_id)
    lo, hi = _sales_day_bounds(date_from, date_to)
    if lo is None:
        # default window on the UTC clock of orders.created
        from datetime import timezone as _tz_utc
        import datetime as _dtx
        lo = (_dtx.datetime.now(_tz_utc.utc) - _td(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    where = _SALES_SCOPE_WHERE + " AND o.reseller_id=?"
    params = [rid]
    if product_id is not None:
        where += " AND o.product_id=?"
        params.append(int(product_id))
    if lo:
        where += " AND o.created >= ?"
        params.append(lo)
    if hi:
        where += " AND o.created <= ?"
        params.append(hi)
    # refunds live on the ledger's SHOP clock — window them with raw
    # shop-day bounds (never the UTC-converted order bounds)
    ref_lo = f"{date_from} 00:00:00" if date_from else None
    ref_hi = f"{date_to} 23:59:59" if date_to else None
    if ref_lo is None:
        ref_lo = (_now() - _td(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    out = _sales_analytics(where, tuple(params), "t.reseller_id=?", (rid,),
                           refund_window=(ref_lo, ref_hi))

    cur = get_cursor()
    # top products by units sold (bounded scope)
    cur.execute(
        f"""
        SELECT o.product_id, COUNT(*) orders, COALESCE(SUM(o.quantity),0) units,
               COALESCE(SUM(o.unit_price_cents*o.quantity),0) gross
        FROM orders o WHERE {where}
        GROUP BY o.product_id ORDER BY units DESC LIMIT 10
        """,
        params,
    )
    top = []
    for pid, orders, units, gross in cur.fetchall():
        pub = ensure_product_public_id(int(pid)) if pid else None
        top.append({"product_id": pub, "orders": int(orders),
                    "units": int(units), "gross_sales": from_cents(int(gross))})
    out["top_products"] = top

    # orders by date (bounded)
    cur.execute(
        f"""
        SELECT substr(o.created,1,10) day, COUNT(*) c,
               COALESCE(SUM(o.unit_price_cents*o.quantity),0) gross
        FROM orders o WHERE {where}
        GROUP BY day ORDER BY day
        """,
        params,
    )
    out["orders_by_date"] = [
        {"date": str(d), "orders": int(c), "gross_sales": from_cents(int(g))}
        for d, c, g in cur.fetchall()
    ]
    # display-ready money strings (existing convention)
    for k in ("gross_sales", "completed_sales", "net_sales", "avg_order_value"):
        out[k] = from_cents(out[f"{k}_cents"])
    out["refunds_total"] = from_cents(out["refunds_total_cents"])
    out["currency"] = "USDT"
    return out


def get_platform_sales_analytics(date_from: str = None, date_to: str = None,
                                 product_id: int = None,
                                 reseller_id: int = None) -> dict:
    """Owner view: all-reseller sales analytics with optional filters."""
    where = _SALES_SCOPE_WHERE
    params = []
    if reseller_id:
        where += " AND o.reseller_id=?"
        params.append(int(reseller_id))
    if product_id:
        where += " AND o.product_id=?"
        params.append(int(product_id))
    lo, hi = _sales_day_bounds(date_from, date_to)
    if lo:
        where += " AND o.created >= ?"
        params.append(lo)
    if hi:
        where += " AND o.created <= ?"
        params.append(hi)
    refund_where = "1=1"
    refund_params: tuple = ()
    if reseller_id:
        refund_where = "t.reseller_id=?"
        refund_params = (int(reseller_id),)
    ref_lo = f"{date_from} 00:00:00" if date_from else None
    ref_hi = f"{date_to} 23:59:59" if date_to else None
    out = _sales_analytics(where, tuple(params), refund_where, refund_params,
                           refund_window=(ref_lo, ref_hi))

    cur = get_cursor()
    # per-reseller leaderboard (bounded scope, single JOIN — no N+1)
    cur.execute(
        f"""
        SELECT o.reseller_id, COUNT(*) orders,
               COALESCE(SUM(o.unit_price_cents*o.quantity),0) gross,
               r.public_id, r.name
        FROM orders o
        LEFT JOIN resellers r ON r.id=o.reseller_id
        WHERE {where}
        GROUP BY o.reseller_id ORDER BY gross DESC LIMIT 10
        """,
        params,
    )
    board = []
    for rid, orders, gross, rpub, rname in cur.fetchall():
        board.append({
            "reseller_id": str(rpub or ""),
            "name": str(rname or ""),
            "orders": int(orders), "gross_sales": from_cents(int(gross)),
        })
    out["resellers"] = board
    for k in ("gross_sales", "completed_sales", "net_sales", "avg_order_value"):
        out[k] = from_cents(out[f"{k}_cents"])
    out["refunds_total"] = from_cents(out["refunds_total_cents"])
    out["currency"] = "USDT"
    return out


def get_api_usage_platform(days: int = 7, since: str = None) -> dict:
    """Platform-wide API usage for the owner (Phase 14 P2).

    Read-only aggregation over reseller_api_logs — the existing append-only
    request log.  Time windows use RANGE predicates on `created` (served by
    idx_reseller_api_logs_created); the daily series is one grouped query
    over the bounded range, not N scans.  Clock note: log rows are stamped
    with shop-time now_text(), so day/month buckets use the shop clock —
    the same convention as get_reseller_api_usage().
    """
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    cur = get_cursor()
    now = _now()
    day_start = now.strftime("%Y-%m-%d 00:00:00")
    month_start = now.strftime("%Y-%m-01 00:00:00")
    # Phase 14 P7: optional explicit lower bound for dashboard date filters;
    # the series span follows the actual window, not the days default.
    if since:
        series_start = since
        try:
            from datetime import datetime as _dt
            span_days = (now.date() - _dt.strptime(since[:10], "%Y-%m-%d").date()).days + 1
            days = max(1, span_days)
        except Exception:
            pass
    else:
        series_start = (now - _td(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d 00:00:00")

    def _counts(since: str) -> dict:
        cur.execute(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status_code >= 400 AND status_code < 500 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN status_code = 429 THEN 1 ELSE 0 END),"
            " AVG(response_ms), MAX(response_ms)"
            " FROM reseller_api_logs WHERE created >= ?",
            (since,),
        )
        total, ok, e4, e5, e429, avg_ms, max_ms = cur.fetchone()
        total = int(total or 0)
        failed = int(e4 or 0) + int(e5 or 0)
        return {
            "total": total,
            "successful": int(ok or 0),
            "failed": failed,
            "errors_4xx": int(e4 or 0),
            "errors_5xx": int(e5 or 0),
            "rate_limited_429": int(e429 or 0),
            "error_rate": round(failed / total, 4) if total else 0.0,
            "avg_ms": round(float(avg_ms or 0), 1),
            "max_ms": int(max_ms or 0),
        }

    cur.execute(
        "SELECT endpoint, COUNT(*) c,"
        " SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) errors,"
        " AVG(response_ms) avg_ms"
        " FROM reseller_api_logs WHERE created >= ?"
        " GROUP BY endpoint ORDER BY c DESC LIMIT 10",
        (since or month_start,),
    )
    top_endpoints = [
        {"endpoint": e, "requests": int(c), "errors": int(er or 0),
         "avg_ms": round(float(a or 0), 1)}
        for e, c, er, a in cur.fetchall()
    ]

    cur.execute(
        "SELECT reseller_public_id, COUNT(*) c,"
        " SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) errors"
        " FROM reseller_api_logs WHERE created >= ? AND reseller_public_id IS NOT NULL"
        " GROUP BY reseller_public_id ORDER BY c DESC LIMIT 10",
        (since or month_start,),
    )
    top_resellers = [
        {"reseller_id": rid, "requests": int(c), "errors": int(er or 0)}
        for rid, c, er in cur.fetchall()
    ]

    cur.execute(
        "SELECT substr(created,1,10) day, COUNT(*) c,"
        " SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) errors,"
        " AVG(response_ms) avg_ms"
        " FROM reseller_api_logs WHERE created >= ?"
        " GROUP BY day ORDER BY day",
        (series_start,),
    )
    by_day = {str(d): (int(c), int(er or 0), round(float(a or 0), 1))
              for d, c, er, a in cur.fetchall()}
    series = []
    for i in range(max(1, int(days))):
        day = (now - _td(days=max(1, int(days)) - 1 - i)).strftime("%Y-%m-%d")
        c, er, a = by_day.get(day, (0, 0, 0.0))
        series.append({"date": day, "requests": c, "errors": er, "avg_ms": a})

    out = {
        "today": _counts(day_start),
        "this_month": _counts(month_start),
        "top_endpoints": top_endpoints,
        "top_resellers": top_resellers,
        "daily": series,
    }
    if since:
        out["window"] = _counts(since)
        out["window_since"] = since
    return out


def get_reseller_endpoint_usage(reseller_id: int, days: int = 30) -> list:
    """Per-endpoint usage breakdown for ONE reseller (Phase 14 P2).

    Reseller-safe: scoped to the caller's own log rows only, bounded window,
    counts and latency only (no IPs, agents, or request data).
    """
    from utils.timezone import now as _now
    from datetime import timedelta as _td
    cur = get_cursor()
    since = (_now() - _td(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT endpoint, method, COUNT(*) c,"
        " SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) ok,"
        " SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) errors,"
        " AVG(response_ms) avg_ms"
        " FROM reseller_api_logs WHERE reseller_id=? AND created >= ?"
        " GROUP BY endpoint, method ORDER BY c DESC LIMIT 20",
        (int(reseller_id), since),
    )
    return [
        {"endpoint": e, "method": m, "requests": int(c),
         "successful": int(o or 0), "failed": int(er or 0),
         "avg_ms": round(float(a or 0), 1)}
        for e, m, c, o, er, a in cur.fetchall()
    ]


def get_reseller_key_usage(reseller_id: int) -> list:
    """Per-API-key request usage for ONE reseller (public key ids only)."""
    cur = get_cursor()
    cur.execute(
        "SELECT k.key_id, COUNT(l.id) c,"
        " SUM(CASE WHEN l.status_code >= 400 THEN 1 ELSE 0 END) errors,"
        " MAX(l.created) last_at"
        " FROM reseller_api_keys k"
        " LEFT JOIN reseller_api_logs l ON l.api_key_id = k.id"
        " WHERE k.reseller_id=?"
        " GROUP BY k.id ORDER BY c DESC",
        (int(reseller_id),),
    )
    return [
        {"key_id": kid, "requests": int(c or 0), "errors": int(er or 0),
         "last_used_at": last}
        for kid, c, er, last in cur.fetchall()
    ]


def get_reseller_api_usage(reseller_id: int, day_bucket: str = None,
                           month_bucket: str = None) -> dict:
    """Aggregate API-usage metrics for ONE reseller (Phase 13 P6).

    Owner WebAdmin and the reseller-safe /api/v1/usage endpoint share this
    single aggregation.  All reads hit indexed columns or small per-reseller
    scans — no table sweeps, no per-request overhead (counters already exist:
    reseller_api_logs, reseller_security_events, reseller_financial_events,
    orders, webhook tables).

    Returns only reseller-safe numbers (counts, volume, timestamps) — no IPs,
    no user agents, no key material, no other reseller's data.
    """
    from utils.timezone import today_prefix, month_prefix
    day = day_bucket or today_prefix()
    month = month_bucket or month_prefix()
    cur = get_cursor()
    rid = int(reseller_id)

    cur.execute(
        "SELECT COUNT(*),"
        " SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status_code = 429 THEN 1 ELSE 0 END),"
        " AVG(response_ms), MAX(created)"
        " FROM reseller_api_logs WHERE reseller_id=?",
        (rid,),
    )
    total, ok, failed, rl429, avg_ms, last_req = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) FROM reseller_api_logs"
        " WHERE reseller_id=? AND substr(created,1,10)=?",
        (rid, day),
    )
    today_requests = int(cur.fetchone()[0])

    cur.execute(
        "SELECT event_type, COUNT(*) FROM reseller_security_events"
        " WHERE reseller_id=? AND event_type IN"
        " ('rate_limit_exceeded','quota_exceeded','auth_failure','brute_force_blocked')"
        " GROUP BY event_type",
        (rid,),
    )
    sec = {t: int(c) for t, c in cur.fetchall()}

    cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(unit_price_cents * quantity),0)"
        " FROM orders WHERE reseller_id=? AND source='reseller_api'",
        (rid,),
    )
    orders_total, volume_cents = cur.fetchone()

    quota = get_quota_settings(rid)
    used = count_orders_for_quota(rid, day, month)

    cur.execute(
        "SELECT COUNT(*), SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END)"
        " FROM reseller_webhooks WHERE reseller_id=?",
        (rid,),
    )
    wh_total, wh_enabled = cur.fetchone()

    cur.execute(
        "SELECT COUNT(*) FROM webhook_delivery_log"
        " WHERE reseller_id=? AND success=0",
        (rid,),
    )
    wh_failed = int(cur.fetchone()[0])

    cur.execute(
        "SELECT COUNT(*) FROM webhook_outbox"
        " WHERE reseller_id=? AND status='failed'",
        (rid,),
    )
    wh_dead = int(cur.fetchone()[0])

    cur.execute(
        "SELECT MAX(last_used_at) FROM reseller_api_keys WHERE reseller_id=?",
        (rid,),
    )
    last_key = cur.fetchone()[0]

    daily_q = int(quota.get("daily_order_quota") or 0)
    monthly_q = int(quota.get("monthly_order_quota") or 0)
    return {
        "requests_total": int(total or 0),
        "requests_ok": int(ok or 0),
        "requests_failed": int(failed or 0),
        "requests_today": today_requests,
        "rate_limited_429": int(rl429 or 0),
        "rate_limit_events": sec.get("rate_limit_exceeded", 0),
        "quota_exceeded_events": sec.get("quota_exceeded", 0),
        "auth_failure_events": sec.get("auth_failure", 0)
                               + sec.get("brute_force_blocked", 0),
        "avg_response_ms": round(float(avg_ms or 0)),
        "last_request_at": last_req,
        "last_key_used_at": last_key,
        "orders_total": int(orders_total or 0),
        "order_volume": from_cents(int(volume_cents or 0)),
        "orders_today": used["daily"],
        "orders_this_month": used["monthly"],
        "quota_daily": daily_q or None,          # None = unlimited
        "quota_monthly": monthly_q or None,
        "quota_daily_remaining": (max(0, daily_q - used["daily"])
                                  if daily_q else None),
        "quota_monthly_remaining": (max(0, monthly_q - used["monthly"])
                                    if monthly_q else None),
        "webhooks_configured": int(wh_total or 0),
        "webhooks_enabled": int(wh_enabled or 0),
        "webhook_deliveries_failed": wh_failed,
        "webhook_events_dead_lettered": wh_dead,
    }


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


def search_wallet_transactions(reseller_id: int, search: str = "",
                               tx_type: str = None, limit: int = 50,
                               offset: int = 0):
    """Search one reseller's immutable ledger with bounded pagination."""
    limit = min(100, max(1, int(limit)))
    offset = max(0, int(offset))
    sql = " FROM reseller_wallet_transactions WHERE reseller_id=?"
    params = [int(reseller_id)]
    if tx_type:
        if tx_type not in TX_TYPES:
            raise ValueError("invalid tx_type")
        sql += " AND tx_type=?"
        params.append(tx_type)
    search = str(search or "").strip()[:100]
    if search:
        pattern = "%" + _escape_like(search) + "%"
        sql += (" AND (reference LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!'"
                " OR related_order_reference LIKE ? ESCAPE '!')")
        params.extend([pattern, pattern, pattern])
    cur = get_cursor()
    cur.execute("SELECT COUNT(*)" + sql, tuple(params))
    total = int(cur.fetchone()[0])
    cur.execute("SELECT *" + sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset))
    return _ds(_LEDGER_COLS, cur.fetchall()), total


# ------------------------------------------------------------------ webhooks

_WEBHOOK_HOST_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$",
    re.IGNORECASE,
)


def _encrypt_webhook_secret(secret: str) -> str:
    """Webhook signing secrets fail closed when encryption is unavailable."""
    encrypted = encrypt_text(secret)
    if not encrypted or encrypted == secret or not encrypted.startswith("enc:"):
        raise RuntimeError("secure webhook secret storage is unavailable")
    return encrypted


def validate_webhook_url(url: str) -> str:
    """Validate and normalize a reseller webhook URL without making a request.

    Production webhooks are HTTPS-only. Localhost, IP-literal private/internal
    destinations, credentials and fragments are rejected. Delivery code must
    still resolve and re-check the destination IP immediately before every
    request to prevent DNS rebinding; delivery is intentionally not part of
    Phase 3.
    """
    raw = (url or "").strip()
    if not raw or len(raw) > 2048 or any(ord(ch) < 32 for ch in raw):
        raise ValueError("webhook URL is empty, too long, or contains control characters")
    try:
        parsed = urlsplit(raw)
        port = parsed.port  # also validates malformed/non-numeric ports
    except ValueError:
        raise ValueError("webhook URL is malformed")
    if parsed.scheme.lower() != "https":
        raise ValueError("webhook URL must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("webhook URL must have a host and cannot contain credentials")
    if parsed.fragment:
        raise ValueError("webhook URL cannot contain a fragment")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("webhook URL contains an invalid port")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("local webhook destinations are not allowed")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("webhook host is invalid")
        if not _WEBHOOK_HOST_RE.fullmatch(ascii_host):
            raise ValueError("webhook host is invalid")
        host = ascii_host
    else:
        if not ip.is_global:
            raise ValueError("private or internal webhook destinations are not allowed")
        host = "[{}]".format(ip.compressed) if ip.version == 6 else ip.compressed

    netloc = host + (f":{port}" if port is not None else "")
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def create_webhook(reseller_id: int, url: str, enabled: bool = True):
    """Create one endpoint and return (public row, one-time signing secret).

    The secret is encrypted at rest and never included in list/get results.
    """
    normalized = validate_webhook_url(url)
    reseller = get_reseller(reseller_id)
    if not reseller or reseller["status"] != "active":
        raise PermissionError("an active reseller account is required")
    secret = secrets.token_urlsafe(32)
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT INTO reseller_webhooks(reseller_id, url, secret_encrypted, enabled, created, updated) "
        "VALUES(?,?,?,?,?,?)",
        (reseller_id, normalized, _encrypt_webhook_secret(secret), 1 if enabled else 0,
         now_text(), now_text()),
    )
    conn.commit()
    cur.execute(
        "SELECT id, reseller_id, url, enabled, created, updated FROM reseller_webhooks "
        "WHERE reseller_id=? AND url=?",
        (reseller_id, normalized),
    )
    return _d(_WEBHOOK_COLS, cur.fetchone()), secret


def get_webhook_for_reseller(reseller_id: int, webhook_id: int):
    cur = get_cursor()
    cur.execute(
        "SELECT id, reseller_id, url, enabled, created, updated FROM reseller_webhooks "
        "WHERE id=? AND reseller_id=?",
        (webhook_id, reseller_id),
    )
    return _d(_WEBHOOK_COLS, cur.fetchone())


def update_webhook_url(reseller_id: int, webhook_id: int, url: str) -> bool:
    normalized = validate_webhook_url(url)
    reseller = get_reseller(reseller_id)
    if not reseller or reseller["status"] != "active":
        return False
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_webhooks SET url=?, updated=? WHERE id=? AND reseller_id=?",
        (normalized, now_text(), webhook_id, reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_webhook_enabled_for_reseller(reseller_id: int, webhook_id: int,
                                     enabled: bool) -> bool:
    reseller = get_reseller(reseller_id)
    if not reseller or reseller["status"] != "active":
        return False
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_webhooks SET enabled=?, updated=? WHERE id=? AND reseller_id=?",
        (1 if enabled else 0, now_text(), webhook_id, reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_webhook_for_reseller(reseller_id: int, webhook_id: int) -> bool:
    reseller = get_reseller(reseller_id)
    if not reseller or reseller["status"] != "active":
        return False
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "DELETE FROM reseller_webhooks WHERE id=? AND reseller_id=?",
        (webhook_id, reseller_id),
    )
    conn.commit()
    return cur.rowcount > 0

def rotate_webhook_secret(reseller_id: int, webhook_id: int) -> str | None:
    """Replace the signing secret for a webhook with a new random one.

    Returns the new plaintext secret (shown to the caller once), or None
    if the webhook does not exist / does not belong to reseller_id.

    Security:
    * The new secret is generated by generate_webhook_secret() (256-bit entropy).
    * It is encrypted before storage — the plaintext is never persisted.
    * The old secret is immediately invalidated; in-flight signed deliveries
      will fail receiver verification and be retried by the delivery worker.
    """
    from reseller_api.services.webhook_signature import generate_webhook_secret
    conn, cur = get_conn(), get_cursor()
    # Verify ownership before touching anything
    cur.execute(
        "SELECT id FROM reseller_webhooks WHERE id=? AND reseller_id=?",
        (webhook_id, reseller_id),
    )
    if not cur.fetchone():
        return None
    new_secret = generate_webhook_secret()
    cur.execute(
        "UPDATE reseller_webhooks SET secret_encrypted=?, updated=? WHERE id=? AND reseller_id=?",
        (_encrypt_webhook_secret(new_secret), now_text(), webhook_id, reseller_id),
    )
    conn.commit()
    return new_secret


def upsert_webhook(reseller_id: int, url: str, secret: str, enabled: bool = True):
    """Register/replace a webhook endpoint. The signing secret is stored
    ENCRYPTED (same convention as sessions/delivery content)."""
    normalized = validate_webhook_url(url)
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT OR REPLACE INTO reseller_webhooks(reseller_id, url, secret_encrypted, enabled, created, updated) "
        "VALUES(?,?,?,?,COALESCE((SELECT created FROM reseller_webhooks WHERE reseller_id=? AND url=?), ?), ?)",
        (reseller_id, normalized, _encrypt_webhook_secret(secret),
         1 if enabled else 0,
         reseller_id, normalized, now_text(), now_text()),
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
                    status_code: int, request_id: str = None, response_ms: int = None,
                    reseller_public_id: str = None, key_id: str = None,
                    ip: str = None, user_agent: str = None):
    """Append-only API request log (no update/delete by design)."""
    conn, cur = get_conn(), get_cursor()
    try:
        cur.execute(
            "INSERT INTO reseller_api_logs"
            "(reseller_id, api_key_id, endpoint, method, status_code, request_id, "
            "response_ms, created, reseller_public_id, key_id, ip, user_agent) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (reseller_id, api_key_id, endpoint, method, status_code,
             request_id, response_ms, now_text(), reseller_public_id, key_id,
             ip, user_agent),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def get_api_log_by_request_id(request_id: str):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_api_logs WHERE request_id=? ORDER BY id DESC LIMIT 1",
        (request_id,),
    )
    return _d(_API_LOG_COLS, cur.fetchone())


def _counter_values(row):
    if not row:
        return None
    cols = (
        "request_minute_bucket", "request_minute_count",
        "request_day_bucket", "request_day_count",
        "request_month_bucket", "request_month_count",
    )
    return _d(cols, row)


def consume_api_request_quota(reseller_id: int, api_key_id: int,
                              minute_bucket: str, day_bucket: str,
                              month_bucket: str, minute_limit: int,
                              daily_limit: int, monthly_limit: int):
    """Atomically consume one reseller-wide request quota unit.

    The conditional UPSERT is one database statement, so concurrent workers
    cannot all pass a read-then-write race. Multiple keys intentionally share
    the reseller aggregate limit; key hopping cannot increase the allowance.
    """
    minute_limit = max(0, int(minute_limit))
    daily_limit = max(0, int(daily_limit))
    monthly_limit = max(0, int(monthly_limit))
    stamp = now_text()
    conn, cur = get_conn(), get_cursor()
    sql = """
        INSERT INTO reseller_api_quota_counters(
            reseller_id, request_minute_bucket, request_minute_count,
            request_day_bucket, request_day_count,
            request_month_bucket, request_month_count,
            last_api_key_id, updated
        )
        SELECT ?, ?, 1, ?, 1, ?, 1, ?, ?
        WHERE ? >= 1 AND ? >= 1 AND ? >= 1
        ON CONFLICT(reseller_id) DO UPDATE SET
            request_minute_count=CASE
                WHEN reseller_api_quota_counters.request_minute_bucket=?
                THEN reseller_api_quota_counters.request_minute_count+1 ELSE 1 END,
            request_minute_bucket=?,
            request_day_count=CASE
                WHEN reseller_api_quota_counters.request_day_bucket=?
                THEN reseller_api_quota_counters.request_day_count+1 ELSE 1 END,
            request_day_bucket=?,
            request_month_count=CASE
                WHEN reseller_api_quota_counters.request_month_bucket=?
                THEN reseller_api_quota_counters.request_month_count+1 ELSE 1 END,
            request_month_bucket=?,
            last_api_key_id=?, updated=?
        WHERE
            (CASE WHEN reseller_api_quota_counters.request_minute_bucket=?
                THEN reseller_api_quota_counters.request_minute_count+1 ELSE 1 END) <= ?
            AND (CASE WHEN reseller_api_quota_counters.request_day_bucket=?
                THEN reseller_api_quota_counters.request_day_count+1 ELSE 1 END) <= ?
            AND (CASE WHEN reseller_api_quota_counters.request_month_bucket=?
                THEN reseller_api_quota_counters.request_month_count+1 ELSE 1 END) <= ?
    """
    params = (
        reseller_id, minute_bucket, day_bucket, month_bucket, api_key_id, stamp,
        minute_limit, daily_limit, monthly_limit,
        minute_bucket, minute_bucket,
        day_bucket, day_bucket,
        month_bucket, month_bucket,
        api_key_id, stamp,
        minute_bucket, minute_limit,
        day_bucket, daily_limit,
        month_bucket, monthly_limit,
    )
    try:
        cur.execute(sql, params)
        allowed = cur.rowcount > 0
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    cur.execute(
        "SELECT request_minute_bucket, request_minute_count, "
        "request_day_bucket, request_day_count, "
        "request_month_bucket, request_month_count "
        "FROM reseller_api_quota_counters WHERE reseller_id=?",
        (reseller_id,),
    )
    return allowed, _counter_values(cur.fetchone())


def record_api_key_request_usage(api_key_id: int, reseller_id: int,
                                 minute_bucket: str, day_bucket: str,
                                 month_bucket: str):
    """Atomically record allowed per-key usage for visibility only."""
    conn, cur = get_conn(), get_cursor()
    stamp = now_text()
    try:
        cur.execute("""
            INSERT INTO reseller_api_key_usage_counters(
                api_key_id, reseller_id,
                request_minute_bucket, request_minute_count,
                request_day_bucket, request_day_count,
                request_month_bucket, request_month_count, updated
            ) VALUES(?,?,?,1,?,1,?,1,?)
            ON CONFLICT(api_key_id) DO UPDATE SET
                reseller_id=excluded.reseller_id,
                request_minute_count=CASE
                    WHEN reseller_api_key_usage_counters.request_minute_bucket=excluded.request_minute_bucket
                    THEN reseller_api_key_usage_counters.request_minute_count+1 ELSE 1 END,
                request_minute_bucket=excluded.request_minute_bucket,
                request_day_count=CASE
                    WHEN reseller_api_key_usage_counters.request_day_bucket=excluded.request_day_bucket
                    THEN reseller_api_key_usage_counters.request_day_count+1 ELSE 1 END,
                request_day_bucket=excluded.request_day_bucket,
                request_month_count=CASE
                    WHEN reseller_api_key_usage_counters.request_month_bucket=excluded.request_month_bucket
                    THEN reseller_api_key_usage_counters.request_month_count+1 ELSE 1 END,
                request_month_bucket=excluded.request_month_bucket,
                updated=excluded.updated
        """, (api_key_id, reseller_id, minute_bucket, day_bucket,
               month_bucket, stamp))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def get_api_key_request_usage(api_key_id: int):
    cur = get_cursor()
    cur.execute(
        "SELECT request_minute_bucket, request_minute_count, "
        "request_day_bucket, request_day_count, request_month_bucket, "
        "request_month_count FROM reseller_api_key_usage_counters "
        "WHERE api_key_id=?",
        (api_key_id,),
    )
    return _counter_values(cur.fetchone())


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
    return resolve_reseller_price_from_values(
        base,
        rule["rule_type"] if rule else None,
        rule["value_cents"] if rule else None,
        rule["value_bps"] if rule else None,
        master_source=("master_tier" if used_tier else "master"),
        base_is_cents=True,
    )


def resolve_reseller_price_from_values(base_price, rule_type=None,
                                       value_cents=None, value_bps=None,
                                       *, master_source="master",
                                       base_is_cents=False):
    """Apply the existing reseller price policy to already-loaded values.

    Catalog queries use this to avoid two SQL statements per returned row.
    The arithmetic and fallback behavior remain centralized here and exactly
    match ``resolve_reseller_price_cents``.
    """
    base = int(base_price) if base_is_cents else to_cents(str(base_price))
    if not rule_type:
        return base, master_source
    rt = str(rule_type)
    if rt == "fixed_price":
        return int(value_cents), "rule:fixed_price"
    bps = int(value_bps)
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


# ---------------------------------------------------------------------------
# IP authentication-failure counters — Phase 10 Prompt 2
# ---------------------------------------------------------------------------

def ensure_ip_auth_counter_table() -> None:
    """Create reseller_ip_auth_counters (idempotent).

    Called from ensure_reseller_tables() so the table exists before any
    auth middleware runs.  One row per (ip_hash, minute_bucket).
    ip_hash is a 32-hex-char SHA-256 truncation; raw IPs are never stored.
    """
    conn = get_conn()
    cur  = get_cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reseller_ip_auth_counters (
            ip_hash       TEXT NOT NULL,
            minute_bucket TEXT NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            updated       TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            PRIMARY KEY (ip_hash, minute_bucket)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ip_auth_bucket"
        " ON reseller_ip_auth_counters(minute_bucket)"
    )
    conn.commit()


def get_ip_auth_failure_count(ip_hash: str, minute_bucket: str) -> int:
    """Return current failure count for this ip_hash/minute_bucket (0 if absent)."""
    cur = get_cursor()
    cur.execute(
        "SELECT failure_count FROM reseller_ip_auth_counters"
        " WHERE ip_hash=? AND minute_bucket=?",
        (str(ip_hash), str(minute_bucket)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def increment_ip_auth_failure(ip_hash: str, minute_bucket: str) -> int:
    """Atomically increment the failure counter; return the new count.

    Uses a single conditional UPSERT so concurrent workers on multiple
    Railway instances cannot race — the DB serialises the increment.
    """
    conn = get_conn()
    cur  = get_cursor()
    stamp = now_text()
    cur.execute(
        """
        INSERT INTO reseller_ip_auth_counters
            (ip_hash, minute_bucket, failure_count, updated)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(ip_hash, minute_bucket) DO UPDATE SET
            failure_count = reseller_ip_auth_counters.failure_count + 1,
            updated       = excluded.updated
        """,
        (str(ip_hash), str(minute_bucket), stamp),
    )
    conn.commit()
    cur.execute(
        "SELECT failure_count FROM reseller_ip_auth_counters"
        " WHERE ip_hash=? AND minute_bucket=?",
        (str(ip_hash), str(minute_bucket)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 1




# --------------------------------------------------------------------------- #
# Phase 10 Prompt 3: Endpoint-specific quota counters                         #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Phase 10 Prompt 9: Prune stale counter rows (unbounded table growth fix)    #
# --------------------------------------------------------------------------- #

def prune_stale_ip_auth_counters(keep_minutes: int = 60) -> int:
    """Delete reseller_ip_auth_counters rows older than keep_minutes.
    Safe to call from any worker concurrently. Returns rows deleted.
    """
    import datetime as _dt
    conn, cur = get_conn(), get_cursor()
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(minutes=keep_minutes)
              ).strftime("%Y-%m-%dT%H:%M")
    cur.execute("DELETE FROM reseller_ip_auth_counters WHERE minute_bucket < ?",
                (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    return deleted


def prune_stale_endpoint_quotas(keep_minutes: int = 10) -> int:
    """Delete reseller_endpoint_quotas rows whose bucket has expired.
    Returns rows deleted.
    """
    import datetime as _dt
    conn, cur = get_conn(), get_cursor()
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(minutes=keep_minutes)
              ).strftime("%Y-%m-%dT%H:%M")
    cur.execute(
        "DELETE FROM reseller_endpoint_quotas WHERE minute_bucket < ? AND minute_bucket != ''",
        (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    return deleted

# Phase 10 Prompt 3: Endpoint quota table + function                          #
# --------------------------------------------------------------------------- #

def ensure_endpoint_quota_table():
    """Create reseller_endpoint_quotas. Idempotent."""
    conn, cur = get_conn(), get_cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reseller_endpoint_quotas(
            reseller_id   INTEGER NOT NULL
                          REFERENCES resellers(id) ON DELETE CASCADE,
            category      TEXT NOT NULL,
            minute_bucket TEXT NOT NULL DEFAULT '',
            minute_count  INTEGER NOT NULL DEFAULT 0,
            updated       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (reseller_id, category)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ep_quota_reseller "
        "ON reseller_endpoint_quotas(reseller_id)"
    )
    conn.commit()


def consume_endpoint_quota(reseller_id: int, category: str,
                           minute_bucket: str, limit: int):
    """Atomically consume one unit from a per-category per-reseller counter.

    Returns (allowed: bool, used_count: int).
    Single-statement UPSERT prevents concurrent race conditions.
    """
    limit = max(1, int(limit))
    stamp = now_text()
    conn, cur = get_conn(), get_cursor()
    try:
        cur.execute("""
            INSERT INTO reseller_endpoint_quotas
                (reseller_id, category, minute_bucket, minute_count, updated)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(reseller_id, category) DO UPDATE SET
                minute_count = CASE
                    WHEN reseller_endpoint_quotas.minute_bucket = ?
                    THEN reseller_endpoint_quotas.minute_count + 1
                    ELSE 1 END,
                minute_bucket = ?,
                updated = ?
            WHERE
                (CASE
                    WHEN reseller_endpoint_quotas.minute_bucket = ?
                    THEN reseller_endpoint_quotas.minute_count + 1
                    ELSE 1 END) <= ?
        """, (
            reseller_id, category, minute_bucket, stamp,
            minute_bucket, minute_bucket, stamp,
            minute_bucket, limit,
        ))
        allowed = cur.rowcount > 0
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    cur.execute(
        "SELECT minute_count FROM reseller_endpoint_quotas "
        "WHERE reseller_id=? AND category=?",
        (reseller_id, category),
    )
    row = cur.fetchone()
    used = int(row[0]) if row else (1 if allowed else limit)
    return allowed, used


# --------------------------------------------------------------------------- #
# Phase 10 Prompt 4: Financial events audit log                               #
# --------------------------------------------------------------------------- #

def ensure_financial_event_table():
    """Create reseller_financial_events. Idempotent."""
    conn, cur = get_conn(), get_cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reseller_financial_events(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id INTEGER NOT NULL
                        REFERENCES resellers(id) ON DELETE CASCADE,
            event_type  TEXT NOT NULL,
            product_id  INTEGER,
            quantity    INTEGER,
            detail      TEXT DEFAULT '',
            created     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_fin_events_reseller "
        "ON reseller_financial_events(reseller_id, created)"
    )
    conn.commit()


def log_financial_event_db(reseller_id: int, event_type: str,
                           detail: str = '', product_id=None, quantity=None):
    """Insert one financial audit event row. Never raises."""
    try:
        conn, cur = get_conn(), get_cursor()
        cur.execute(
            "INSERT INTO reseller_financial_events "
            "(reseller_id, event_type, product_id, quantity, detail, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reseller_id, event_type, product_id, quantity, detail, now_text()),
        )
        conn.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "[fin_audit] DB error type=%s: %s", event_type, exc
        )


def count_recent_product_orders(reseller_id: int, product_id: int,
                                since_iso: str) -> int:
    """Count financial events of type 'order_placed' for a product since a timestamp."""
    try:
        cur = get_cursor()
        cur.execute(
            "SELECT COUNT(*) FROM reseller_financial_events "
            "WHERE reseller_id=? AND product_id=? AND event_type='order_placed' "
            "AND created >= ?",
            (reseller_id, product_id, since_iso),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Phase 10 Prompt 7: Reseller quota settings                                  #
# --------------------------------------------------------------------------- #

def ensure_quota_settings_table():
    """Create reseller_quota_settings. Idempotent."""
    conn, cur = get_conn(), get_cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reseller_quota_settings(
            reseller_id         INTEGER PRIMARY KEY
                                REFERENCES resellers(id) ON DELETE CASCADE,
            daily_order_quota   INTEGER NOT NULL DEFAULT 0,
            monthly_order_quota INTEGER NOT NULL DEFAULT 0,
            api_enabled         INTEGER NOT NULL DEFAULT 1,
            updated             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def get_quota_settings(reseller_id: int) -> dict:
    """Return quota settings for a reseller (defaults if not set)."""
    cur = get_cursor()
    cur.execute(
        "SELECT daily_order_quota, monthly_order_quota, api_enabled "
        "FROM reseller_quota_settings WHERE reseller_id=?",
        (reseller_id,),
    )
    row = cur.fetchone()
    if row:
        return {
            "daily_order_quota":   int(row[0]),
            "monthly_order_quota": int(row[1]),
            "api_enabled":         bool(row[2]),
        }
    # Default: unlimited (0 = disabled check)
    return {"daily_order_quota": 0, "monthly_order_quota": 0, "api_enabled": True}


def set_quota_settings(reseller_id: int, daily_order_quota: int = None,
                       monthly_order_quota: int = None, api_enabled: bool = None):
    """Upsert quota settings for a reseller."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "INSERT INTO reseller_quota_settings "
        "(reseller_id, daily_order_quota, monthly_order_quota, api_enabled, updated) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(reseller_id) DO UPDATE SET "
        "daily_order_quota=COALESCE(?, daily_order_quota), "
        "monthly_order_quota=COALESCE(?, monthly_order_quota), "
        "api_enabled=COALESCE(?, api_enabled), "
        "updated=?",
        (
            reseller_id,
            daily_order_quota or 0, monthly_order_quota or 0,
            1 if api_enabled is None else int(api_enabled),
            now_text(),
            daily_order_quota, monthly_order_quota,
            None if api_enabled is None else int(api_enabled),
            now_text(),
        ),
    )
    conn.commit()


def count_orders_for_quota(reseller_id: int, day_bucket: str,
                           month_bucket: str) -> dict:
    """Count financial order events for daily/monthly quota checks."""
    try:
        cur = get_cursor()
        cur.execute(
            "SELECT COUNT(*) FROM reseller_financial_events "
            "WHERE reseller_id=? AND event_type='order_placed' "
            "AND substr(created,1,10)=?",
            (reseller_id, day_bucket),
        )
        row = cur.fetchone()
        daily = int(row[0]) if row else 0
        cur.execute(
            "SELECT COUNT(*) FROM reseller_financial_events "
            "WHERE reseller_id=? AND event_type='order_placed' "
            "AND substr(created,1,7)=?",
            (reseller_id, month_bucket),
        )
        row = cur.fetchone()
        monthly = int(row[0]) if row else 0
        return {"daily": daily, "monthly": monthly}
    except Exception:
        return {"daily": 0, "monthly": 0}


# --------------------------------------------------------------------------- #
# Phase 10 Prompt 6: Key-identifier auth-failure tracking                     #
# --------------------------------------------------------------------------- #

def increment_key_auth_failure(key_prefix: str, bucket: str) -> int:
    """Increment auth failure counter for a key identifier prefix."""
    conn, cur = get_conn(), get_cursor()
    stamp = now_text()
    try:
        cur.execute("""
            INSERT INTO reseller_ip_auth_counters(ip_hash, minute_bucket, failure_count, updated)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(ip_hash, minute_bucket) DO UPDATE SET
                failure_count = failure_count + 1,
                updated = ?
        """, ("key:" + key_prefix, bucket, stamp, stamp))
        conn.commit()
        cur.execute(
            "SELECT failure_count FROM reseller_ip_auth_counters "
            "WHERE ip_hash=? AND minute_bucket=?",
            ("key:" + key_prefix, bucket),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 1
    except Exception:
        return 0


def get_key_auth_failure_count(key_prefix: str, bucket: str) -> int:
    """Return current auth failure count for a key identifier prefix."""
    try:
        cur = get_cursor()
        cur.execute(
            "SELECT failure_count FROM reseller_ip_auth_counters "
            "WHERE ip_hash=? AND minute_bucket=?",
            ("key:" + key_prefix, bucket),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ------------------------------------------------------------------ phase 11 p3: product mappings

def ensure_product_mapping_table() -> None:
    """Create reseller_product_mappings if it does not exist. Idempotent.

    Deletion policy:
      - ON DELETE CASCADE on reseller_id  -> cleaning up a reseller removes all mappings.
      - ON DELETE CASCADE on product_id   -> when the owner hard-deletes a master product
        the mapping is automatically removed; no orphan rows are ever left.
    When the owner disables a product (active=0) the mapping row stays but the
    product will not appear in the reseller catalog (filtered at query time).
    """
    conn = get_conn()
    cur = get_cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS reseller_product_mappings(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE CASCADE,
        product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        custom_name        TEXT,
        custom_description TEXT,
        reseller_sku       TEXT,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(reseller_id, product_id)
    )""")
    # Phase 11 P4: add reseller_sku if table was created before this column existed
    try:
        cur.execute("ALTER TABLE reseller_product_mappings ADD COLUMN reseller_sku TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_rpm_reseller "
        "ON reseller_product_mappings(reseller_id)"
    )
    conn.commit()


def import_reseller_product(
    reseller_id: int,
    product_id: int,
    custom_name: str = None,
    custom_description: str = None,
) -> dict:
    """Create a reseller product mapping.  Raises ValueError on conflict.

    - Only call after verifying the master product is active=1 and the reseller
      has an enabled access row (authorize_product_access).
    - The product_id is the INTERNAL integer id, never the public PRD- string.
    - Duplicate import (same reseller + product) raises ValueError('already_imported').
    """
    conn = get_conn()
    cur = get_cursor()
    try:
        cur.execute(
            "INSERT INTO reseller_product_mappings"
            "(reseller_id, product_id, custom_name, custom_description, enabled)"
            " VALUES(?,?,?,?,1)",
            (
                int(reseller_id),
                int(product_id),
                str(custom_name).strip() if custom_name else None,
                str(custom_description).strip() if custom_description else None,
            ),
        )
        conn.commit()
        mapping_id = cur.lastrowid
    except Exception as exc:
        # Roll the failed transaction back IMMEDIATELY. Without this the
        # implicit write transaction stays open on this thread's connection
        # and keeps holding the SQLite/Postgres write lock — under concurrent
        # imports the leaked lock blocks other writers until they hit the
        # busy timeout and surface raw "database is locked" errors (and the
        # lock can outlive the request on a reused worker-thread connection).
        try:
            conn.rollback()
        except Exception:
            pass
        msg = str(exc).lower()
        if "unique" in msg or "duplicate" in msg:
            raise ValueError("already_imported") from None
        raise
    return get_reseller_product_mapping(reseller_id, product_id) or {"id": mapping_id}


_MAPPING_COLS = (
    "id", "reseller_id", "product_id", "custom_name",
    "custom_description", "reseller_sku", "enabled", "created_at", "updated_at",
)
_MAPPING_SELECT = (
    "id, reseller_id, product_id, custom_name, custom_description,"
    " reseller_sku, enabled, created_at, updated_at"
)


def get_reseller_product_mapping(reseller_id: int, product_id: int) -> dict | None:
    """Return the mapping row (or None) for one reseller+product pair."""
    cur = get_cursor()
    cur.execute(
        f"SELECT {_MAPPING_SELECT} FROM reseller_product_mappings "
        "WHERE reseller_id=? AND product_id=?",
        (int(reseller_id), int(product_id)),
    )
    row = cur.fetchone()
    return dict(zip(_MAPPING_COLS, row)) if row else None


def get_reseller_product_mapping_by_public_id(
    reseller_id: int, public_id: str
) -> dict | None:
    """Resolve a PRD-* public id to a mapping row for this reseller."""
    cur = get_cursor()
    cur.execute(
        f"SELECT m.{', m.'.join(_MAPPING_COLS)} "
        "FROM reseller_product_mappings m "
        "JOIN products p ON p.id=m.product_id "
        "WHERE m.reseller_id=? AND p.public_id=?",
        (int(reseller_id), str(public_id)),
    )
    row = cur.fetchone()
    return dict(zip(_MAPPING_COLS, row)) if row else None


def has_enabled_product_mapping(reseller_id: int, product_id: int) -> bool:
    """True only when the reseller holds an ENABLED catalog mapping.

    Phase 12 P2: the orderable catalog is the reseller's own mapping list,
    not the raw owner access grant. The order-validation layer calls this
    after access authorization so a product that was never imported — or
    whose mapping the reseller disabled — cannot be ordered. Deny-by-default:
    a missing row reads as False.
    """
    cur = get_cursor()
    cur.execute(
        "SELECT enabled FROM reseller_product_mappings "
        "WHERE reseller_id=? AND product_id=?",
        (int(reseller_id), int(product_id)),
    )
    row = cur.fetchone()
    return bool(row and row[0])


def list_reseller_product_mappings(reseller_id: int) -> list:
    """Return all mapping rows for a reseller, joined with master product metadata.

    The join is intentionally read-only: only safe catalog columns are
    returned.  Internal product fields (cost, delivery, fulfillment, etc.)
    are excluded at this layer.
    """
    cur = get_cursor()
    cur.execute(
        "SELECT m.id, m.reseller_id, m.product_id, m.custom_name,"
        " m.custom_description, m.reseller_sku, m.enabled,"
        " m.created_at, m.updated_at,"
        " p.public_id, p.name AS master_name, p.active AS master_active,"
        " COALESCE(a.enabled, 0) AS access_enabled,"
        " COALESCE(p.stock, 0) AS fallback_stock,"
        " CASE WHEN COUNT(ps.id)>0 THEN COUNT(ps.id) ELSE COALESCE(p.stock,0) END"
        " AS available_quantity, p.price AS master_price,"
        " pr.rule_type, pr.value_cents, pr.value_bps,"
        " CASE WHEN c.active=1 THEN c.name ELSE NULL END AS category_name "
        "FROM reseller_product_mappings m "
        "JOIN products p ON p.id=m.product_id "
        "JOIN reseller_product_access a"
        "  ON a.reseller_id=m.reseller_id AND a.product_id=p.id "
        "LEFT JOIN categories c ON c.id=p.category_id "
        "LEFT JOIN reseller_pricing_rules pr"
        "  ON pr.product_id=p.id AND pr.reseller_id=m.reseller_id AND pr.enabled=1 "
        "LEFT JOIN product_stock ps ON ps.product_id=p.id AND ps.sold=0 "
        "WHERE m.reseller_id=? "
        "GROUP BY m.id, p.public_id, p.name, p.active, a.enabled, p.stock, p.price,"
        "  pr.rule_type, pr.value_cents, pr.value_bps, c.active, c.name "
        "ORDER BY m.id DESC",
        (int(reseller_id),),
    )
    extended_cols = (
        "id", "reseller_id", "product_id", "custom_name", "custom_description",
        "reseller_sku", "enabled", "created_at", "updated_at",
        "public_id", "master_name", "master_active", "access_enabled",
        "fallback_stock",
        "available_quantity", "master_price",
        "rule_type", "value_cents", "value_bps", "category_name",
    )
    return [dict(zip(extended_cols, row)) for row in cur.fetchall()]


def update_reseller_product_mapping(
    reseller_id: int,
    product_id: int,
    *,
    custom_name=...,
    custom_description=...,
    reseller_sku=...,
    enabled=...,
) -> dict | None:
    """Partial update of a reseller mapping.  Only supplied fields are changed.

    Pass ``Ellipsis`` (the default) to leave a field unchanged.
    Pass ``None`` to clear a text field.
    Returns the updated mapping row, or None if no mapping exists.
    """
    sets, params = [], []
    if custom_name is not ...:
        sets.append("custom_name=?")
        params.append(str(custom_name).strip() if custom_name is not None else None)
    if custom_description is not ...:
        sets.append("custom_description=?")
        params.append(
            str(custom_description).strip() if custom_description is not None else None
        )
    if reseller_sku is not ...:
        sets.append("reseller_sku=?")
        params.append(
            str(reseller_sku).strip() if reseller_sku is not None else None
        )
    if enabled is not ...:
        sets.append("enabled=?")
        params.append(1 if enabled else 0)
    if not sets:
        return get_reseller_product_mapping(reseller_id, product_id)
    sets.append("updated_at=?")
    params.append(now_text())
    params.extend([int(reseller_id), int(product_id)])
    conn = get_conn()
    cur = get_cursor()
    cur.execute(
        f"UPDATE reseller_product_mappings SET {', '.join(sets)} "
        "WHERE reseller_id=? AND product_id=?",
        tuple(params),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_reseller_product_mapping(reseller_id, product_id)


def remove_reseller_product_mapping(reseller_id: int, product_id: int) -> bool:
    """Hard-delete one reseller mapping. Returns True if a row was deleted.

    The master product is never touched. Pricing rules are LEFT in place so
    they can be reused if the reseller re-imports the product later.
    """
    conn = get_conn()
    cur = get_cursor()
    cur.execute(
        "DELETE FROM reseller_product_mappings WHERE reseller_id=? AND product_id=?",
        (int(reseller_id), int(product_id)),
    )
    conn.commit()
    return cur.rowcount > 0


# ── Product lookup helpers (used by tests and service layer) ─────────────────

def get_product_by_id(product_id: int) -> dict | None:
    """Return a minimal product row dict for the given internal product_id,
    or None if the product does not exist."""
    cur = get_cursor()
    cur.execute(
        "SELECT id, name, price, stock, active, description FROM products WHERE id=?",
        (int(product_id),),
    )
    row = cur.fetchone()
    if not row:
        return None
    return dict(zip(("id", "name", "price", "stock", "active", "description"), row))


def delete_product(product_id: int) -> bool:
    """Hard-delete a product row.  ON DELETE CASCADE will remove all child rows
    (stock, access rows, reseller mappings, etc.).
    Returns True if a row was deleted, False if the product did not exist.
    This is an owner-only administrative operation; never expose to resellers.
    """
    conn = get_conn()
    cur = get_cursor()
    cur.execute("DELETE FROM products WHERE id=?", (int(product_id),))
    conn.commit()
    return cur.rowcount > 0
