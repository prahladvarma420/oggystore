"""Reseller invoice storage layer — Phase 15 Prompt 2.

Invoices are financial DOCUMENTS over the authoritative ledger + orders —
never a second source of truth.  Schema follows the project's conventions:
integer cents, public opaque IDs, CHECK whitelists, idempotent ensure(),
immutability triggers on both backends (same pattern as the wallet ledger).

Lifecycle (prepaid wallet model — document semantics):
    draft      — preview; may be regenerated until finalized
    finalized  — frozen snapshot; charges were already settled via the
                 wallet at order time, so amount_paid == total and
                 amount_due == 0 by construction
    void       — terminal correction state (with reason); a voided invoice
                 is never reused — the next generation creates a new row

Transitions:  draft → finalized | void;  finalized → void.  Everything else
is rejected at the service layer AND by triggers at the database layer.
"""

import secrets

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor, _safe_add_column
from utils.timezone import now_text

INVOICE_STATUSES = ("draft", "finalized", "void")
LINE_SOURCE_TYPES = (
    "purchase",            # order charge (ledger purchase_debit)
    "refund_full",         # rapi.refund.order.*
    "refund_cancellation", # rapi.cancel_refund.order.*
    "refund_partial",      # rapi.partial_refund.order.*
    "adjustment_credit",   # owner credit
    "adjustment_debit",    # owner debit
    "topup",               # wallet funding (informational — not billed)
)

_PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_invoice_public_id() -> str:
    """INV-XXXXXXXXXXXX — opaque, unguessable, unique-enforced at the table."""
    return "INV-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET) for _ in range(12))


def ensure_invoice_tables():
    """Create invoice tables + indexes + immutability triggers. Idempotent."""
    conn, cur = get_conn(), get_cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reseller_invoices(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        invoice_number TEXT NOT NULL UNIQUE,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE RESTRICT,
        period_start TEXT NOT NULL,
        period_end   TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','finalized','void')),
        currency TEXT NOT NULL DEFAULT 'USDT',
        subtotal_cents INTEGER NOT NULL DEFAULT 0 CHECK (subtotal_cents >= 0),
        refund_cents INTEGER NOT NULL DEFAULT 0 CHECK (refund_cents >= 0),
        adjustment_credit_cents INTEGER NOT NULL DEFAULT 0 CHECK (adjustment_credit_cents >= 0),
        adjustment_debit_cents INTEGER NOT NULL DEFAULT 0 CHECK (adjustment_debit_cents >= 0),
        topup_cents INTEGER NOT NULL DEFAULT 0 CHECK (topup_cents >= 0),
        total_cents INTEGER NOT NULL DEFAULT 0,
        amount_paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_cents >= 0),
        amount_due_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_due_cents >= 0),
        orders_count INTEGER NOT NULL DEFAULT 0,
        transactions_count INTEGER NOT NULL DEFAULT 0,
        reseller_public_id TEXT,
        reseller_name TEXT,
        issued_by TEXT DEFAULT '',
        void_reason TEXT DEFAULT '',
        finalized_at TEXT,
        voided_at TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reseller_invoice_lines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL
            REFERENCES reseller_invoices(id) ON DELETE CASCADE,
        source_type TEXT NOT NULL CHECK (source_type IN (
            'purchase','refund_full','refund_cancellation','refund_partial',
            'adjustment_credit','adjustment_debit','topup')),
        source_reference TEXT NOT NULL,
        order_public_id TEXT,
        product_public_id TEXT,
        description TEXT DEFAULT '',
        quantity INTEGER NOT NULL DEFAULT 1,
        unit_amount_cents INTEGER NOT NULL DEFAULT 0,
        total_cents INTEGER NOT NULL DEFAULT 0,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(invoice_id, source_type, source_reference)
    )""")

    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_invoices_reseller ON reseller_invoices(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_status ON reseller_invoices(status, created)",
        "CREATE INDEX IF NOT EXISTS idx_invoices_period ON reseller_invoices(period_start, period_end)",
        "CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON reseller_invoice_lines(invoice_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_invoice_lines_source ON reseller_invoice_lines(source_type, source_reference)",
    ):
        cur.execute(stmt)

    # One non-void invoice per reseller per billing period (drafts are
    # regenerable; voided rows never block regeneration).
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_period_unique
        ON reseller_invoices(reseller_id, period_start, period_end)
        WHERE status IN ('draft','finalized')
    """)

    # --- immutability / transition triggers -------------------------------
    if _pgcompat.use_postgres():
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_invoice_guard() RETURNS trigger AS $$
            DECLARE current_status TEXT;
            BEGIN
              IF TG_OP = 'UPDATE' THEN
                -- finalized/void headers: financial + identity fields frozen
                IF OLD.status IN ('finalized','void') AND (
                    NEW.subtotal_cents IS DISTINCT FROM OLD.subtotal_cents OR
                    NEW.refund_cents IS DISTINCT FROM OLD.refund_cents OR
                    NEW.adjustment_credit_cents IS DISTINCT FROM OLD.adjustment_credit_cents OR
                    NEW.adjustment_debit_cents IS DISTINCT FROM OLD.adjustment_debit_cents OR
                    NEW.topup_cents IS DISTINCT FROM OLD.topup_cents OR
                    NEW.total_cents IS DISTINCT FROM OLD.total_cents OR
                    NEW.amount_paid_cents IS DISTINCT FROM OLD.amount_paid_cents OR
                    NEW.amount_due_cents IS DISTINCT FROM OLD.amount_due_cents OR
                    NEW.reseller_id IS DISTINCT FROM OLD.reseller_id OR
                    NEW.period_start IS DISTINCT FROM OLD.period_start OR
                    NEW.period_end IS DISTINCT FROM OLD.period_end OR
                    NEW.invoice_number IS DISTINCT FROM OLD.invoice_number OR
                    NEW.public_id IS DISTINCT FROM OLD.public_id) THEN
                    RAISE EXCEPTION 'finalized invoices are immutable';
                END IF;
                -- terminal/illegal transitions
                IF OLD.status='void' AND NEW.status<>'void' THEN
                    RAISE EXCEPTION 'void invoices are terminal';
                END IF;
                IF OLD.status='finalized' AND NEW.status='draft' THEN
                    RAISE EXCEPTION 'finalized invoices cannot return to draft';
                END IF;
                IF OLD.status='draft' AND NEW.status NOT IN ('draft','finalized','void') THEN
                    RAISE EXCEPTION 'invalid invoice transition';
                END IF;
                IF OLD.status='finalized' AND NEW.status NOT IN ('finalized','void') THEN
                    RAISE EXCEPTION 'invalid invoice transition';
                END IF;
              END IF;
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'invoices cannot be deleted';
              END IF;
              RETURN COALESCE(NEW, OLD);
            END; $$ LANGUAGE plpgsql""")
        cur.execute("DROP TRIGGER IF EXISTS invoice_guard ON reseller_invoices")
        cur.execute("CREATE TRIGGER invoice_guard BEFORE UPDATE OR DELETE ON reseller_invoices FOR EACH ROW EXECUTE FUNCTION oggy_invoice_guard()")
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_invoice_line_guard() RETURNS trigger AS $$
            DECLARE st TEXT;
            BEGIN
              SELECT status INTO st FROM reseller_invoices
              WHERE id = COALESCE(OLD.invoice_id, NEW.invoice_id);
              IF st IN ('finalized','void') THEN
                RAISE EXCEPTION 'lines of finalized invoices are immutable';
              END IF;
              RETURN COALESCE(NEW, OLD);
            END; $$ LANGUAGE plpgsql""")
        cur.execute("DROP TRIGGER IF EXISTS invoice_line_guard ON reseller_invoice_lines")
        cur.execute("CREATE TRIGGER invoice_line_guard BEFORE INSERT OR UPDATE OR DELETE ON reseller_invoice_lines FOR EACH ROW EXECUTE FUNCTION oggy_invoice_line_guard()")
    else:
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_immutable_update
            BEFORE UPDATE ON reseller_invoices
            WHEN OLD.status IN ('finalized','void') AND (
                NEW.subtotal_cents IS NOT OLD.subtotal_cents OR
                NEW.refund_cents IS NOT OLD.refund_cents OR
                NEW.adjustment_credit_cents IS NOT OLD.adjustment_credit_cents OR
                NEW.adjustment_debit_cents IS NOT OLD.adjustment_debit_cents OR
                NEW.topup_cents IS NOT OLD.topup_cents OR
                NEW.total_cents IS NOT OLD.total_cents OR
                NEW.amount_paid_cents IS NOT OLD.amount_paid_cents OR
                NEW.amount_due_cents IS NOT OLD.amount_due_cents OR
                NEW.reseller_id IS NOT OLD.reseller_id OR
                NEW.period_start IS NOT OLD.period_start OR
                NEW.period_end IS NOT OLD.period_end OR
                NEW.invoice_number IS NOT OLD.invoice_number OR
                NEW.public_id IS NOT OLD.public_id)
            BEGIN SELECT RAISE(ABORT, 'finalized invoices are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_void_terminal
            BEFORE UPDATE ON reseller_invoices
            WHEN OLD.status='void' AND NEW.status<>'void'
            BEGIN SELECT RAISE(ABORT, 'void invoices are terminal'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_no_undraft
            BEFORE UPDATE ON reseller_invoices
            WHEN OLD.status='finalized' AND NEW.status='draft'
            BEGIN SELECT RAISE(ABORT, 'finalized invoices cannot return to draft'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_no_delete
            BEFORE DELETE ON reseller_invoices
            BEGIN SELECT RAISE(ABORT, 'invoices cannot be deleted'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_line_frozen_ins
            BEFORE INSERT ON reseller_invoice_lines
            WHEN (SELECT status FROM reseller_invoices WHERE id=NEW.invoice_id) IN ('finalized','void')
            BEGIN SELECT RAISE(ABORT, 'lines of finalized invoices are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_line_frozen_upd
            BEFORE UPDATE ON reseller_invoice_lines
            WHEN (SELECT status FROM reseller_invoices WHERE id=OLD.invoice_id) IN ('finalized','void')
            BEGIN SELECT RAISE(ABORT, 'lines of finalized invoices are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS invoice_line_frozen_del
            BEFORE DELETE ON reseller_invoice_lines
            WHEN (SELECT status FROM reseller_invoices WHERE id=OLD.invoice_id) IN ('finalized','void')
            BEGIN SELECT RAISE(ABORT, 'lines of finalized invoices are immutable'); END""")
    conn.commit()


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

_INV_COLS = ("id", "public_id", "invoice_number", "reseller_id",
             "period_start", "period_end", "status", "currency",
             "subtotal_cents", "refund_cents", "adjustment_credit_cents",
             "adjustment_debit_cents", "topup_cents", "total_cents",
             "amount_paid_cents", "amount_due_cents", "orders_count",
             "transactions_count", "reseller_public_id", "reseller_name",
             "issued_by", "void_reason", "finalized_at", "voided_at",
             "created", "updated")

_LINE_COLS = ("id", "invoice_id", "source_type", "source_reference",
              "order_public_id", "product_public_id", "description",
              "quantity", "unit_amount_cents", "total_cents", "created")


def _d(cols, row):
    return dict(zip(cols, row)) if row else None


def next_invoice_number() -> str:
    """Concurrency-safe sequential number via the settings counter inside a
    single atomic block (write-lock serialized on SQLite; row-locked on
    Postgres).  No MAX()+1, no in-memory counters."""
    conn = get_conn()
    with _pgcompat.atomic(conn):
        cur = get_cursor()
        cur.execute(
            "INSERT INTO settings(key, value) VALUES('invoice_number_seq','0')"
            " ON CONFLICT(key) DO NOTHING"
            if _pgcompat.use_postgres() else
            "INSERT OR IGNORE INTO settings(key, value) VALUES('invoice_number_seq','0')"
        )
        cur.execute(
            "UPDATE settings SET value=CAST(value AS INTEGER)+1"
            " WHERE key='invoice_number_seq'"
        )
        cur.execute(
            "SELECT value FROM settings WHERE key='invoice_number_seq'")
        seq = int(cur.fetchone()[0])
        year = now_text()[:4]
        return f"INV-{year}-{seq:06d}"


def insert_invoice(fields: dict) -> int:
    """Insert one invoice row; returns the new id.  Caller owns the
    transaction boundary (the generation service wraps everything in one
    pgcompat.atomic block)."""
    cur = get_cursor()
    cols = tuple(fields.keys())
    cur.execute(
        f"INSERT INTO reseller_invoices({','.join(cols)})"
        f" VALUES({','.join('?' for _ in cols)})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def get_invoice_by_id(invoice_id: int):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_invoices WHERE id=?", (int(invoice_id),))
    return _d(_INV_COLS, cur.fetchone())


def get_invoice_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_invoices WHERE public_id=?",
                (str(public_id),))
    return _d(_INV_COLS, cur.fetchone())


def get_invoice_for_reseller(reseller_id: int, public_id: str):
    """IDOR-safe fetch: only the owning reseller's row."""
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_invoices WHERE public_id=? AND reseller_id=?",
        (str(public_id), int(reseller_id)),
    )
    return _d(_INV_COLS, cur.fetchone())


def find_open_period_invoice(reseller_id: int, period_start: str,
                             period_end: str):
    """The non-void invoice for this exact period, if one exists."""
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_invoices"
        " WHERE reseller_id=? AND period_start=? AND period_end=?"
        " AND status IN ('draft','finalized')",
        (int(reseller_id), str(period_start), str(period_end)),
    )
    return _d(_INV_COLS, cur.fetchone())


def list_invoices(reseller_id: int = None, status: str = None,
                  limit: int = 50, after_id: int = None,
                  period_start: str = None, public_id_search: str = None,
                  date_from: str = None, date_to: str = None,
                  currency: str = None):
    where, params = " WHERE 1=1", []
    if reseller_id is not None:
        where += " AND reseller_id=?"
        params.append(int(reseller_id))
    if status in INVOICE_STATUSES:
        where += " AND status=?"
        params.append(status)
    if period_start:
        where += " AND period_start >= ?"
        params.append(str(period_start))
    # Phase 15 P7: OVERLAP semantics — an invoice matches when any covered
    # day intersects [date_from, date_to]: period_start <= date_to AND
    # period_end >= date_from.
    if date_from:
        where += " AND period_end >= ?"
        params.append(str(date_from))
    if date_to:
        where += " AND period_start <= ?"
        params.append(str(date_to))
    if currency:
        where += " AND currency=?"
        params.append(str(currency))
    if after_id:
        where += " AND id < ?"
        params.append(int(after_id))
    if public_id_search:
        where += " AND (public_id LIKE ? OR invoice_number LIKE ?)"
        params += [f"%{public_id_search}%"] * 2
    params.append(min(100, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_invoices" + where +
        " ORDER BY id DESC LIMIT ?", tuple(params))
    return [_d(_INV_COLS, r) for r in cur.fetchall()]


def replace_invoice_lines(invoice_id: int, lines: list):
    """Draft-only line rebuild, executed by the service inside its atomic
    block.  Deletes then reinserts; the UNIQUE(invoice_id, source_type,
    source_reference) index is the duplicate backstop."""
    cur = get_cursor()
    cur.execute("DELETE FROM reseller_invoice_lines WHERE invoice_id=?",
                (int(invoice_id),))
    for line in lines:
        line = {**line, "invoice_id": int(invoice_id)}  # parent linkage
        cols = tuple(line.keys())
        cur.execute(
            f"INSERT INTO reseller_invoice_lines({','.join(cols)})"
            f" VALUES({','.join('?' for _ in cols)})",
            tuple(line[c] for c in cols),
        )


def get_invoice_lines(invoice_id: int):
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_invoice_lines WHERE invoice_id=? ORDER BY id",
        (int(invoice_id),))
    return [_d(_LINE_COLS, r) for r in cur.fetchall()]


def set_invoice_status(invoice_id: int, status: str, **extra) -> bool:
    """Transition guard at the SQL level: only the legal moves perform.
    Returns True when the row actually transitioned."""
    if status not in INVOICE_STATUSES:
        raise ValueError("invalid invoice status")
    conn, cur = get_conn(), get_cursor()
    now = now_text()
    sets, params = ["updated=?"], [now]
    if status == "finalized":
        sets.append("finalized_at=?")
        params.append(now)
    if status == "void":
        sets.append("voided_at=?")
        params.append(now)
        if extra.get("void_reason") is not None:
            sets.append("void_reason=?")
            params.append(str(extra["void_reason"])[:300])
    cur.execute(
        f"UPDATE reseller_invoices SET {', '.join(sets)}, status=?"
        " WHERE id=? AND status <> ? AND NOT (status='void') AND"
        " NOT (status='finalized' AND ?= 'draft')",
        tuple(params) + (status, int(invoice_id), status, status),
    )
    conn.commit()
    return cur.rowcount > 0


__all__ = [
    "INVOICE_STATUSES", "LINE_SOURCE_TYPES",
    "generate_invoice_public_id", "ensure_invoice_tables",
    "next_invoice_number", "insert_invoice", "get_invoice_by_id",
    "get_invoice_by_public_id", "get_invoice_for_reseller",
    "find_open_period_invoice", "list_invoices", "replace_invoice_lines",
    "get_invoice_lines", "set_invoice_status",
]
