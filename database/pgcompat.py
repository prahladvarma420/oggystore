"""
SQLite <-> PostgreSQL compatibility shim for OGGY Premium Store.

Why this exists
---------------
The bot and the web admin were written for SQLite (placeholders use "?",
upserts use INSERT OR IGNORE/REPLACE, ids come from cur.lastrowid, dates use
DATE('now')/strftime, schema uses AUTOINCREMENT, etc).

To host the web admin on Render while the bot stays on your VPS, both must talk
to ONE shared database over the network -> PostgreSQL.

Instead of rewriting ~400 SQL statements, we swap ONLY the two places that open
a database connection (database/db.py and web_admin/app.py::get_db) to call
connect() below.

- If DATABASE_URL is NOT set  -> behaves EXACTLY like the old sqlite3 (zero
  change to your current VPS/local setup).
- If DATABASE_URL starts with postgres -> opens PostgreSQL via psycopg (v3) and
  transparently translates each SQL statement from SQLite dialect to Postgres.

The translation is intentionally conservative and covers every construct used
in this codebase. See _translate() for the full list.
"""

import os
import re
import threading
from contextlib import contextmanager
from itertools import count

DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()
_SAVEPOINT_COUNTER = count(1)


def use_postgres() -> bool:
    return DATABASE_URL.lower().startswith(("postgres://", "postgresql://"))


@contextmanager
def atomic(conn):
    """Run a multi-statement unit atomically on SQLite or PostgreSQL.

    PostgreSQL normally runs in autocommit mode in this compatibility layer;
    psycopg's transaction context temporarily creates a real transaction.
    SQLite uses BEGIN IMMEDIATE for a top-level financial write and a unique
    SAVEPOINT when nested inside an existing transaction.
    """
    if isinstance(conn, _PgConn):
        with conn._lock:
            conn._transaction_depth += 1
            try:
                with conn._pg.transaction():
                    yield
            finally:
                conn._transaction_depth -= 1
        return

    nested = bool(getattr(conn, "in_transaction", False))
    savepoint = None
    if nested:
        savepoint = "oggy_atomic_{}_{}".format(
            threading.get_ident(), next(_SAVEPOINT_COUNTER)
        )
        conn.execute("SAVEPOINT " + savepoint)
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        if savepoint:
            conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            conn.execute("RELEASE SAVEPOINT " + savepoint)
        else:
            conn.rollback()
        raise
    else:
        if savepoint:
            conn.execute("RELEASE SAVEPOINT " + savepoint)
        else:
            conn.commit()


def is_unique_violation(exc) -> bool:
    """Backend-aware unique-constraint classification."""
    if use_postgres():
        try:
            import psycopg
            return isinstance(exc, psycopg.errors.UniqueViolation)
        except Exception:
            return False
    try:
        import sqlite3
        return isinstance(exc, sqlite3.IntegrityError) and "unique" in str(exc).lower()
    except Exception:
        return False


# Tables whose primary key is an auto-increment "id" column. Plain INSERTs into
# these get "RETURNING id" appended so cur.lastrowid keeps working on Postgres.
ID_TABLES = {
    "products", "product_stock", "orders", "deposits", "admin_logs",
    "tickets", "ticket_messages", "referrals", "withdrawal_requests", "product_price_tiers",
    "restock_notifications", "scheduled_broadcasts", "web_login_log",
    "web_admins", "m365_invites",
    # Reseller system (Phase 1)
    "resellers", "reseller_api_keys", "reseller_product_access",
    "reseller_pricing_rules", "reseller_wallet_transactions",
    "reseller_webhooks", "reseller_api_logs", "reseller_security_events",
}

# Conflict targets for INSERT OR REPLACE -> ON CONFLICT (...) DO UPDATE.
REPLACE_CONFLICT = {
    "settings": ["key"],
    "coupons": ["code"],
    "user_coupons": ["user_id"],
    "product_price_tiers": ["product_id", "quantity"],
    "web_sessions": ["sid"],
    # Reseller system (Phase 1) — upsert targets for INSERT OR REPLACE
    "reseller_product_access": ["reseller_id", "product_id"],
    "reseller_pricing_rules": ["reseller_id", "product_id"],
    "reseller_webhooks": ["reseller_id", "url"],
}


# ---------------------------------------------------------------- SQL translate
def _split_cols(coltext: str):
    return [c.strip() for c in coltext.split(",") if c.strip()]


def _apply_upserts(s: str) -> str:
    # INSERT OR IGNORE INTO t(...) ... -> INSERT INTO t(...) ... ON CONFLICT DO NOTHING
    m = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)", s, re.I)
    if m:
        s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.I)
        s = s.rstrip().rstrip(";")
        if "ON CONFLICT" not in s.upper():
            s = s + " ON CONFLICT DO NOTHING"
        return s

    # INSERT OR REPLACE INTO t(cols) VALUES(...) -> ON CONFLICT (pk) DO UPDATE SET ...
    m = re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", s, re.I)
    if m:
        table = m.group(1)
        cols = _split_cols(m.group(2))
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.I)
        s = s.rstrip().rstrip(";")
        target = REPLACE_CONFLICT.get(table)
        if target:
            setters = [c for c in cols if c not in target]
            if setters:
                set_sql = ", ".join("{0}=EXCLUDED.{0}".format(c) for c in setters)
                s = "{0} ON CONFLICT ({1}) DO UPDATE SET {2}".format(
                    s, ", ".join(target), set_sql)
            else:
                s = "{0} ON CONFLICT ({1}) DO NOTHING".format(s, ", ".join(target))
        else:
            s = s + " ON CONFLICT DO NOTHING"
        return s
    return s


def _translate(sql: str, has_params: bool = True):
    """Translate one SQLite statement to Postgres. Returns (sql, returns_id).

    has_params tells us whether psycopg will be given a parameter
    sequence. That decides whether literal % must be escaped -- see the
    escaping step at the end of this function.
    """
    s = sql
    is_ddl = re.match(r"\s*(CREATE|ALTER)\b", s, re.I) is not None

    # --- schema / DDL ---
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bAUTOINCREMENT\b", "", s, flags=re.I)
    # Telegram IDs exceed 32-bit INTEGER; money needs full precision.
    if is_ddl:
        s = re.sub(r"\bINTEGER\b", "BIGINT", s, flags=re.I)
        s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s, flags=re.I)
    # datetime(CURRENT_TIMESTAMP) -> CURRENT_TIMESTAMP (used inside DEFAULTs)
    s = re.sub(r"datetime\(\s*CURRENT_TIMESTAMP\s*\)", "CURRENT_TIMESTAMP", s, flags=re.I)
    # TEXT columns default to a text timestamp, not timestamptz
    s = re.sub(r"DEFAULT\s*\(\s*CURRENT_TIMESTAMP\s*\)",
               "DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')", s, flags=re.I)
    s = re.sub(r"DEFAULT\s+CURRENT_TIMESTAMP",
               "DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')", s, flags=re.I)

    # --- sqlite_master existence checks (used by _safe_add_column) ---
    s = re.sub(r"FROM\s+sqlite_master\s+WHERE\s+type='table'\s+AND\s+name=\?",
               "FROM information_schema.tables WHERE table_name=?", s, flags=re.I)

    # --- last_insert_rowid() (SQLite) -> lastval() (Postgres) ---
    s = re.sub(r"last_insert_rowid\s*\(\s*\)", "lastval()", s, flags=re.I)

    # --- date / time functions (order matters) ---
    s = re.sub(r"DATE\(\s*'now'\s*,\s*\?\s*\)", "(CURRENT_DATE + (?)::interval)", s, flags=re.I)
    s = re.sub(r"DATE\(\s*'now'\s*\)", "CURRENT_DATE", s, flags=re.I)
    s = re.sub(r"datetime\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", s, flags=re.I)
    s = re.sub(r"strftime\(\s*'%H'\s*,\s*([A-Za-z_][\w\.]*)\s*\)",
               r"EXTRACT(HOUR FROM (\1)::timestamp)::int", s, flags=re.I)
    # DATE(COALESCE(...)) -> (COALESCE(...))::date
    s = re.sub(r"DATE\(\s*(COALESCE\([^()]*\))\s*\)", r"(\1)::date", s, flags=re.I)
    # DATE(<identifier>) -> (<identifier>)::date
    s = re.sub(r"DATE\(\s*([A-Za-z_][\w\.]*)\s*\)", r"(\1)::date", s, flags=re.I)
    # DATE(?) -> (?)::date
    s = re.sub(r"DATE\(\s*\?\s*\)", "(?)::date", s, flags=re.I)

    # --- upserts ---
    s = _apply_upserts(s)

    # --- RETURNING id for plain inserts into id-tables (for lastrowid) ---
    returns_id = False
    mi = re.match(r"\s*INSERT\s+INTO\s+(\w+)", s, re.I)
    if mi and mi.group(1) in ID_TABLES and "ON CONFLICT" not in s.upper() \
            and "RETURNING" not in s.upper():
        s = s.rstrip().rstrip(";") + " RETURNING id"
        returns_id = True

    # --- escape literal % (e.g. LIKE 'enc:%') BEFORE introducing %s placeholders ---
    # Only when parameters are supplied. psycopg unescapes %% -> % as part of
    # interpolation, but with no parameters it does not interpolate at all, so
    # escaping unconditionally left a literal '%%' in the SQL and corrupted
    # every parameterless query containing a percent sign.
    if has_params:
        s = s.replace("%", "%%")
    # --- placeholders ? -> %s ---
    s = s.replace("?", "%s")

    return s, returns_id


# ---------------------------------------------------------------- Row wrapper
class _Row:
    """Behaves like sqlite3.Row: supports r[0], r['name'], dict(r), list(r)."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = list(vals)

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return self._vals[self._cols.index(k)]

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return list(self._cols)


# ---------------------------------------------------------------- PG cursor
class _PgCursor:
    def __init__(self, conn):
        self._conn = conn
        self._pg = conn._pg.cursor()
        self.lastrowid = None
        self._rows = None
        self._idx = 0

    @property
    def rowcount(self):
        try:
            return self._pg.rowcount
        except Exception:
            return -1

    def _cols(self):
        if self._pg.description:
            return [d[0] for d in self._pg.description]
        return []

    def _raw_execute(self, tsql, p):
        """Execute with automatic reconnect.

        Railway's public proxy silently drops idle TCP connections, which
        makes psycopg raise 'the connection is lost' on the next query.
        Recover by retrying on a fresh cursor, then on a fresh connection.
        """
        try:
            self._pg.execute(tsql, p)
            return
        except Exception as exc:
            if not self._conn._is_connection_error(exc):
                raise
            # Retrying a single statement on a new autocommit connection would
            # split an atomic financial unit. Let the transaction fail so the
            # database rolls it back; the caller may safely retry the whole
            # idempotent operation instead.
            if self._conn._transaction_depth:
                raise
        # Stale cursor (connection was already refreshed elsewhere): retry
        # once on a fresh cursor from the current connection.
        try:
            self._pg = self._conn._pg.cursor()
            self._pg.execute(tsql, p)
            return
        except Exception as exc:
            if not self._conn._is_connection_error(exc):
                raise
        # Connection itself is dead: reconnect once and retry.
        self._conn._reconnect()
        self._pg = self._conn._pg.cursor()
        self._pg.execute(tsql, p)

    def execute(self, sql, params=()):
        up = sql.strip().upper()
        # PRAGMA table_info(x) -> information_schema lookup (cid,name,type,...)
        if up.startswith("PRAGMA TABLE_INFO"):
            m = re.search(r"TABLE_INFO\(\s*([^)]+?)\s*\)", sql, re.I)
            table = (m.group(1).strip().strip('"').strip("'") if m else "")
            with self._conn._lock:
                self._raw_execute(
                    "SELECT (ordinal_position-1) AS cid, column_name AS name, "
                    "data_type AS type, 0 AS notnull, column_default AS dflt_value, "
                    "0 AS pk FROM information_schema.columns "
                    "WHERE table_name=%s ORDER BY ordinal_position", (table,))
                self._load()
            return self
        # Other PRAGMAs (journal_mode, busy_timeout, ...) -> no-op
        if up.startswith("PRAGMA"):
            self._rows = []
            self._idx = 0
            return self

        p = tuple(params) if params else None
        tsql, returns_id = _translate(sql, p is not None)
        with self._conn._lock:
            self._raw_execute(tsql, p)
            if returns_id:
                try:
                    row = self._pg.fetchone()
                    self.lastrowid = row[0] if row else None
                except Exception:
                    self.lastrowid = None
                self._rows = []
                self._idx = 0
            else:
                self._load()
        return self

    def executemany(self, sql, seq):
        for params in seq:
            self.execute(sql, params)
        return self

    def _load(self):
        self._idx = 0
        if self._pg.description:
            cols = self._cols()
            try:
                self._rows = [_Row(cols, r) for r in self._pg.fetchall()]
            except Exception:
                self._rows = []
        else:
            self._rows = []

    def fetchone(self):
        if not self._rows or self._idx >= len(self._rows):
            return None
        r = self._rows[self._idx]
        self._idx += 1
        return r

    def fetchall(self):
        rest = self._rows[self._idx:] if self._rows else []
        self._idx = len(self._rows) if self._rows else 0
        return rest

    def close(self):
        try:
            self._pg.close()
        except Exception:
            pass

    def __iter__(self):
        return iter(self.fetchall())


# ---------------------------------------------------------------- PG connection
class _PgConn:
    def __init__(self, dsn):
        import psycopg  # lazy import: only needed when using Postgres
        self._psycopg = psycopg
        self._dsn = dsn
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._pg = self._open()
        # accepted but ignored (sqlite3 compatibility):
        object.__setattr__(self, "row_factory", None)

    def _open(self):
        # TCP keepalives stop Railway's proxy from silently dropping the
        # connection while the bot/panel sits idle.
        return self._psycopg.connect(
            self._dsn,
            autocommit=True,
            connect_timeout=15,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )

    def _reconnect(self):
        try:
            self._pg.close()
        except Exception:
            pass
        self._pg = self._open()

    def _is_connection_error(self, exc) -> bool:
        if isinstance(
            exc,
            (self._psycopg.OperationalError, self._psycopg.InterfaceError),
        ):
            return True
        try:
            return bool(self._pg.closed or self._pg.broken)
        except Exception:
            return False

    def cursor(self):
        return _PgCursor(self)

    def execute(self, sql, params=()):
        cur = _PgCursor(self)
        cur.execute(sql, params)
        return cur

    def commit(self):
        # autocommit is on; nothing to do
        return None

    def rollback(self):
        try:
            self._pg.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self._pg.close()
        except Exception:
            pass

    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)


# ---------------------------------------------------------------- public connect
def connect(db_path, timeout: int = 15):
    """Drop-in replacement for sqlite3.connect().

    Postgres when DATABASE_URL is set, otherwise plain sqlite3 (unchanged).
    """
    if use_postgres():
        return _PgConn(DATABASE_URL)
    import sqlite3
    return sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
