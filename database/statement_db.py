"""Reseller financial statement storage — Phase 15 Prompt 3.

Statements are chronological SNAPSHOTS of ledger activity over a billing
period — a reporting/document layer, never a second source of truth.  All
figures derive from the immutable wallet ledger; statement rows store only
derived snapshots (refreshable while open, frozen once finalized) and line
items REFERENCE ledger rows (ledger_id), never duplicate mutable money data.

Period model: half-open [period_start, period_end) in shop time
(utils.timezone) — the same clock the ledger stamps with.  A transaction at
exactly period_start belongs to this period; one at period_end belongs to
the next.  No boundary record can belong to two periods.

Lifecycle: open (refreshable snapshot) → finalized (frozen, terminal).
Corrections after finalization arrive via the ledger (adjustments/refunds)
and appear in LATER statements — finalized history is never rewritten.
"""

import json
import secrets

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor
from utils.timezone import now_text

STATEMENT_STATUSES = ("open", "finalized")
PERIOD_TYPES = ("monthly", "weekly", "custom", "invoice")

_PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_statement_public_id() -> str:
    """STMT-XXXXXXXXXXXX — opaque, unguessable, unique-enforced at the table."""
    return "STMT-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(12))


def ensure_statement_tables():
    """Create statement tables + indexes + immutability triggers (idempotent)."""
    conn, cur = get_conn(), get_cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reseller_statements(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        statement_number TEXT NOT NULL UNIQUE,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE RESTRICT,
        period_type TEXT NOT NULL CHECK (period_type IN ('monthly','weekly','custom','invoice')),
        period_start TEXT NOT NULL,
        period_end   TEXT NOT NULL,
        timezone TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','finalized')),
        currency TEXT NOT NULL DEFAULT 'USDT',
        opening_balance_cents INTEGER NOT NULL DEFAULT 0,
        closing_balance_cents INTEGER NOT NULL DEFAULT 0,
        total_credits_cents INTEGER NOT NULL DEFAULT 0,
        total_debits_cents INTEGER NOT NULL DEFAULT 0,
        purchases_cents INTEGER NOT NULL DEFAULT 0,
        refunds_cents INTEGER NOT NULL DEFAULT 0,
        adjustment_credit_cents INTEGER NOT NULL DEFAULT 0,
        adjustment_debit_cents INTEGER NOT NULL DEFAULT 0,
        topup_cents INTEGER NOT NULL DEFAULT 0,
        transactions_count INTEGER NOT NULL DEFAULT 0,
        orders_count INTEGER NOT NULL DEFAULT 0,
        invoice_ids TEXT DEFAULT '[]',
        reseller_public_id TEXT,
        reseller_name TEXT,
        generated_by TEXT DEFAULT '',
        finalized_at TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reseller_statement_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        statement_id INTEGER NOT NULL
            REFERENCES reseller_statements(id) ON DELETE CASCADE,
        ledger_id INTEGER NOT NULL,
        reference TEXT NOT NULL,
        tx_type TEXT NOT NULL,
        direction TEXT NOT NULL CHECK (direction IN ('debit','credit')),
        amount_cents INTEGER NOT NULL,
        balance_after_cents INTEGER NOT NULL,
        description TEXT DEFAULT '',
        order_public_id TEXT,
        created TEXT NOT NULL,
        UNIQUE(statement_id, ledger_id)
    )""")

    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_statements_reseller ON reseller_statements(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_statements_status ON reseller_statements(status, created)",
        "CREATE INDEX IF NOT EXISTS idx_statement_lines_stmt ON reseller_statement_lines(statement_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_statement_lines_ledger ON reseller_statement_lines(ledger_id)",
    ):
        cur.execute(stmt)

    # One statement per reseller per period+type (open or finalized).
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_statements_period_unique
        ON reseller_statements(reseller_id, period_start, period_end, period_type)
    """)

    if _pgcompat.use_postgres():
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_statement_guard() RETURNS trigger AS $$
            BEGIN
              IF TG_OP = 'UPDATE' THEN
                IF OLD.status='finalized' AND NEW.status='finalized' AND (
                    NEW.opening_balance_cents IS DISTINCT FROM OLD.opening_balance_cents OR
                    NEW.closing_balance_cents IS DISTINCT FROM OLD.closing_balance_cents OR
                    NEW.total_credits_cents IS DISTINCT FROM OLD.total_credits_cents OR
                    NEW.total_debits_cents IS DISTINCT FROM OLD.total_debits_cents OR
                    NEW.purchases_cents IS DISTINCT FROM OLD.purchases_cents OR
                    NEW.refunds_cents IS DISTINCT FROM OLD.refunds_cents OR
                    NEW.adjustment_credit_cents IS DISTINCT FROM OLD.adjustment_credit_cents OR
                    NEW.adjustment_debit_cents IS DISTINCT FROM OLD.adjustment_debit_cents OR
                    NEW.topup_cents IS DISTINCT FROM OLD.topup_cents OR
                    NEW.reseller_id IS DISTINCT FROM OLD.reseller_id OR
                    NEW.period_start IS DISTINCT FROM OLD.period_start OR
                    NEW.period_end IS DISTINCT FROM OLD.period_end OR
                    NEW.period_type IS DISTINCT FROM OLD.period_type OR
                    NEW.statement_number IS DISTINCT FROM OLD.statement_number OR
                    NEW.public_id IS DISTINCT FROM OLD.public_id) THEN
                    RAISE EXCEPTION 'finalized statements are immutable';
                END IF;
                IF OLD.status='finalized' AND NEW.status<>'finalized' THEN
                    RAISE EXCEPTION 'finalized statements are terminal';
                END IF;
              END IF;
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'statements cannot be deleted';
              END IF;
              RETURN COALESCE(NEW, OLD);
            END; $$ LANGUAGE plpgsql""")
        cur.execute("DROP TRIGGER IF EXISTS statement_guard ON reseller_statements")
        cur.execute("CREATE TRIGGER statement_guard BEFORE UPDATE OR DELETE ON reseller_statements FOR EACH ROW EXECUTE FUNCTION oggy_statement_guard()")
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_statement_line_guard() RETURNS trigger AS $$
            DECLARE st TEXT;
            BEGIN
              SELECT status INTO st FROM reseller_statements
              WHERE id = COALESCE(OLD.statement_id, NEW.statement_id);
              IF st = 'finalized' THEN
                RAISE EXCEPTION 'lines of finalized statements are immutable';
              END IF;
              RETURN COALESCE(NEW, OLD);
            END; $$ LANGUAGE plpgsql""")
        cur.execute("DROP TRIGGER IF EXISTS statement_line_guard ON reseller_statement_lines")
        cur.execute("CREATE TRIGGER statement_line_guard BEFORE INSERT OR UPDATE OR DELETE ON reseller_statement_lines FOR EACH ROW EXECUTE FUNCTION oggy_statement_line_guard()")
    else:
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS statement_immutable_update
            BEFORE UPDATE ON reseller_statements
            WHEN OLD.status='finalized' AND (
                NEW.opening_balance_cents IS NOT OLD.opening_balance_cents OR
                NEW.closing_balance_cents IS NOT OLD.closing_balance_cents OR
                NEW.total_credits_cents IS NOT OLD.total_credits_cents OR
                NEW.total_debits_cents IS NOT OLD.total_debits_cents OR
                NEW.purchases_cents IS NOT OLD.purchases_cents OR
                NEW.refunds_cents IS NOT OLD.refunds_cents OR
                NEW.adjustment_credit_cents IS NOT OLD.adjustment_credit_cents OR
                NEW.adjustment_debit_cents IS NOT OLD.adjustment_debit_cents OR
                NEW.topup_cents IS NOT OLD.topup_cents OR
                NEW.reseller_id IS NOT OLD.reseller_id OR
                NEW.period_start IS NOT OLD.period_start OR
                NEW.period_end IS NOT OLD.period_end OR
                NEW.period_type IS NOT OLD.period_type OR
                NEW.statement_number IS NOT OLD.statement_number OR
                NEW.public_id IS NOT OLD.public_id)
            BEGIN SELECT RAISE(ABORT, 'finalized statements are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS statement_terminal
            BEFORE UPDATE ON reseller_statements
            WHEN OLD.status='finalized' AND NEW.status<>'finalized'
            BEGIN SELECT RAISE(ABORT, 'finalized statements are terminal'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS statement_no_delete
            BEFORE DELETE ON reseller_statements
            BEGIN SELECT RAISE(ABORT, 'statements cannot be deleted'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS statement_line_frozen_ins
            BEFORE INSERT ON reseller_statement_lines
            WHEN (SELECT status FROM reseller_statements WHERE id=NEW.statement_id)='finalized'
            BEGIN SELECT RAISE(ABORT, 'lines of finalized statements are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS statement_line_frozen_upd
            BEFORE UPDATE ON reseller_statement_lines
            WHEN (SELECT status FROM reseller_statements WHERE id=OLD.statement_id)='finalized'
            BEGIN SELECT RAISE(ABORT, 'lines of finalized statements are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS statement_line_frozen_del
            BEFORE DELETE ON reseller_statement_lines
            WHEN (SELECT status FROM reseller_statements WHERE id=OLD.statement_id)='finalized'
            BEGIN SELECT RAISE(ABORT, 'lines of finalized statements are immutable'); END""")
    conn.commit()


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

_STMT_COLS = ("id", "public_id", "statement_number", "reseller_id",
              "period_type", "period_start", "period_end", "timezone",
              "status", "currency", "opening_balance_cents",
              "closing_balance_cents", "total_credits_cents",
              "total_debits_cents", "purchases_cents", "refunds_cents",
              "adjustment_credit_cents", "adjustment_debit_cents",
              "topup_cents", "transactions_count", "orders_count",
              "invoice_ids", "reseller_public_id", "reseller_name",
              "generated_by", "finalized_at", "created", "updated")

_LINE_COLS = ("id", "statement_id", "ledger_id", "reference", "tx_type",
              "direction", "amount_cents", "balance_after_cents",
              "description", "order_public_id", "created")


def _d(cols, row):
    return dict(zip(cols, row)) if row else None


def next_statement_number() -> str:
    """Concurrency-safe sequential ST-<year>-<seq> via the settings counter
    inside one atomic block (same proven pattern as invoice numbering)."""
    conn = get_conn()
    with _pgcompat.atomic(conn):
        cur = get_cursor()
        cur.execute(
            "INSERT INTO settings(key, value) VALUES('statement_number_seq','0')"
            " ON CONFLICT(key) DO NOTHING"
            if _pgcompat.use_postgres() else
            "INSERT OR IGNORE INTO settings(key, value) VALUES('statement_number_seq','0')"
        )
        cur.execute(
            "UPDATE settings SET value=CAST(value AS INTEGER)+1"
            " WHERE key='statement_number_seq'"
        )
        cur.execute("SELECT value FROM settings WHERE key='statement_number_seq'")
        seq = int(cur.fetchone()[0])
        return f"ST-{now_text()[:4]}-{seq:06d}"


def insert_statement(fields: dict) -> int:
    """Insert one statement row; caller owns the transaction boundary."""
    cur = get_cursor()
    cols = tuple(fields.keys())
    cur.execute(
        f"INSERT INTO reseller_statements({','.join(cols)})"
        f" VALUES({','.join('?' for _ in cols)})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def replace_statement_lines(statement_id: int, lines: list):
    """Open-only line rebuild inside the caller's atomic block; the
    UNIQUE(statement_id, ledger_id) index is the duplicate backstop."""
    cur = get_cursor()
    cur.execute("DELETE FROM reseller_statement_lines WHERE statement_id=?",
                (int(statement_id),))
    for line in lines:
        line = {**line, "statement_id": int(statement_id)}
        cols = tuple(line.keys())
        cur.execute(
            f"INSERT INTO reseller_statement_lines({','.join(cols)})"
            f" VALUES({','.join('?' for _ in cols)})",
            tuple(line[c] for c in cols),
        )


def get_statement_by_id(statement_id: int):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_statements WHERE id=?",
                (int(statement_id),))
    return _d(_STMT_COLS, cur.fetchone())


def get_statement_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_statements WHERE public_id=?",
                (str(public_id),))
    return _d(_STMT_COLS, cur.fetchone())


def get_statement_for_reseller(reseller_id: int, public_id: str):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_statements WHERE public_id=? AND reseller_id=?",
        (str(public_id), int(reseller_id)),
    )
    return _d(_STMT_COLS, cur.fetchone())


def find_period_statement(reseller_id: int, period_type: str,
                          period_start: str, period_end: str):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_statements"
        " WHERE reseller_id=? AND period_type=? AND period_start=? AND period_end=?",
        (int(reseller_id), str(period_type), str(period_start), str(period_end)),
    )
    return _d(_STMT_COLS, cur.fetchone())


def list_statements(reseller_id: int = None, status: str = None,
                    period_type: str = None, limit: int = 50,
                    after_id: int = None, period_start: str = None,
                    number_search: str = None, date_from: str = None,
                    date_to: str = None, currency: str = None):
    where, params = " WHERE 1=1", []
    if reseller_id is not None:
        where += " AND reseller_id=?"
        params.append(int(reseller_id))
    if status in STATEMENT_STATUSES:
        where += " AND status=?"
        params.append(status)
    if period_type in PERIOD_TYPES:
        where += " AND period_type=?"
        params.append(period_type)
    if period_start:
        where += " AND period_start >= ?"
        params.append(str(period_start))
    # Phase 15 P7: date filters use OVERLAP semantics — a statement matches
    # when any covered day intersects [date_from, date_to].  period_end is a
    # half-open exclusive bound, so overlap ⇔ start < day_after(date_to)
    # AND end > date_from.
    if date_from:
        where += " AND period_end > ?"
        params.append(str(date_from) + " 00:00:00")
    if date_to:
        from datetime import datetime as _dt2, timedelta as _td2
        nxt = (_dt2.strptime(str(date_to), "%Y-%m-%d") + _td2(days=1)
               ).strftime("%Y-%m-%d 00:00:00")
        where += " AND period_start < ?"
        params.append(nxt)
    if currency:
        where += " AND currency=?"
        params.append(str(currency))
    if after_id:
        where += " AND id < ?"
        params.append(int(after_id))
    if number_search:
        where += " AND (public_id LIKE ? OR statement_number LIKE ?)"
        params += [f"%{number_search}%"] * 2
    params.append(min(100, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_statements" + where +
        " ORDER BY id DESC LIMIT ?", tuple(params))
    return [_d(_STMT_COLS, r) for r in cur.fetchall()]


def get_statement_lines(statement_id: int, limit: int = 500,
                        after_id: int = None):
    """Paginated line reads — large histories never load unbounded."""
    where = "statement_id=?"
    params = [int(statement_id)]
    if after_id:
        where += " AND id > ?"
        params.append(int(after_id))
    params.append(min(1000, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_statement_lines WHERE " + where +
        " ORDER BY id LIMIT ?", tuple(params))
    return [_d(_LINE_COLS, r) for r in cur.fetchall()]


def finalize_statement(statement_id: int) -> bool:
    """open -> finalized, guarded at the SQL level (rowcount proof)."""
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reseller_statements SET status='finalized', finalized_at=?,"
        " updated=? WHERE id=? AND status='open'",
        (now_text(), now_text(), int(statement_id)),
    )
    conn.commit()
    return cur.rowcount > 0


def invoices_overlapping(reseller_id: int, first_day: str, last_day: str):
    """Invoices overlapping the statement window.  Invoice periods are plain
    dates (YYYY-MM-DD); the service passes the statement's first/last covered
    DAY as dates, so the comparison is a clean inclusive date overlap:
    overlap ⇔ invoice.start <= last_day AND invoice.end >= first_day."""
    cur = get_cursor()
    cur.execute(
        "SELECT public_id, invoice_number, period_start, period_end, status,"
        " total_cents FROM reseller_invoices"
        " WHERE reseller_id=? AND period_start <= ? AND period_end >= ?"
        " ORDER BY id",
        (int(reseller_id), str(last_day), str(first_day)),
    )
    cols = ("public_id", "invoice_number", "period_start", "period_end",
            "status", "total_cents")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


__all__ = [
    "STATEMENT_STATUSES", "PERIOD_TYPES",
    "generate_statement_public_id", "ensure_statement_tables",
    "next_statement_number", "insert_statement", "replace_statement_lines",
    "get_statement_by_id", "get_statement_by_public_id",
    "get_statement_for_reseller", "find_period_statement",
    "list_statements", "get_statement_lines", "finalize_statement",
    "invoices_overlapping",
]
