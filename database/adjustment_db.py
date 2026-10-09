"""Financial adjustment & refund RECORDS — Phase 15 Prompt 5.

The record layer represents the BUSINESS ACTION (reason, actor, approval,
status).  The wallet ledger remains the only financial movement record.
Every completed adjustment/refund record references exactly one ledger row
(ledger_reference).  Records are written atomically with that ledger row.

Adjustment workflow:  pending → approved → completed
                      pending → rejected | cancelled
                      approved → cancelled
Terminal: completed / rejected / cancelled (trigger-enforced; financial
fields frozen once decided).

Refund records are written exactly once, atomically with the ledger credit
(status is always 'completed' — there is no pending refund state; approval
lives in the admin workflow before posting).
"""

import secrets

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor
from utils.timezone import now_text

ADJUSTMENT_STATUSES = ("pending", "approved", "rejected", "completed",
                       "cancelled")
ADJUSTMENT_TYPES = ("credit", "debit")
REFUND_KINDS = ("full", "partial", "cancellation", "administrative")

_PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_adjustment_public_id() -> str:
    return "ADJ-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET)
                            for _ in range(10))


def generate_refund_public_id() -> str:
    return "REF-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET)
                            for _ in range(10))


def ensure_adjustment_tables():
    conn, cur = get_conn(), get_cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reseller_adjustments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE RESTRICT,
        type TEXT NOT NULL CHECK (type IN ('credit','debit')),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        currency TEXT NOT NULL DEFAULT 'USDT',
        reason TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual_admin',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','approved','rejected','completed','cancelled')),
        ledger_reference TEXT UNIQUE,
        created_by TEXT DEFAULT '',
        decided_by TEXT DEFAULT '',
        decided_at TEXT,
        completed_at TEXT,
        note TEXT DEFAULT '',
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reseller_refunds(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        reseller_id INTEGER NOT NULL REFERENCES resellers(id) ON DELETE RESTRICT,
        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
        order_public_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('full','partial','cancellation','administrative')),
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        currency TEXT NOT NULL DEFAULT 'USDT',
        reason TEXT DEFAULT '',
        source TEXT NOT NULL DEFAULT 'system',
        status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed')),
        ledger_reference TEXT NOT NULL UNIQUE,
        created_by TEXT DEFAULT 'system',
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_adjustments_reseller ON reseller_adjustments(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_adjustments_status ON reseller_adjustments(status, created)",
        "CREATE INDEX IF NOT EXISTS idx_refunds_reseller ON reseller_refunds(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_refunds_order ON reseller_refunds(order_id)",
    ):
        cur.execute(stmt)

    if _pgcompat.use_postgres():
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_adjustment_guard() RETURNS trigger AS $$
            BEGIN
              IF TG_OP = 'UPDATE' THEN
                IF OLD.status IN ('completed','rejected','cancelled') THEN
                    RAISE EXCEPTION 'decided adjustments are terminal';
                END IF;
                IF NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
                   OR NEW.type IS DISTINCT FROM OLD.type
                   OR NEW.reseller_id IS DISTINCT FROM OLD.reseller_id
                   OR NEW.currency IS DISTINCT FROM OLD.currency THEN
                    RAISE EXCEPTION 'adjustment financial fields are immutable';
                END IF;
              END IF;
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'adjustments cannot be deleted';
              END IF;
              RETURN COALESCE(NEW, OLD);
            END; $$ LANGUAGE plpgsql""")
        cur.execute("DROP TRIGGER IF EXISTS adjustment_guard ON reseller_adjustments")
        cur.execute("CREATE TRIGGER adjustment_guard BEFORE UPDATE OR DELETE ON reseller_adjustments FOR EACH ROW EXECUTE FUNCTION oggy_adjustment_guard()")
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_refund_record_guard() RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'refund records are immutable';
            END; $$ LANGUAGE plpgsql""")
        cur.execute("DROP TRIGGER IF EXISTS refund_record_guard ON reseller_refunds")
        cur.execute("CREATE TRIGGER refund_record_guard BEFORE UPDATE OR DELETE ON reseller_refunds FOR EACH ROW EXECUTE FUNCTION oggy_refund_record_guard()")
    else:
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS adjustment_terminal
            BEFORE UPDATE ON reseller_adjustments
            WHEN OLD.status IN ('completed','rejected','cancelled')
            BEGIN SELECT RAISE(ABORT, 'decided adjustments are terminal'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS adjustment_fields_frozen
            BEFORE UPDATE ON reseller_adjustments
            WHEN NEW.amount_cents IS NOT OLD.amount_cents
              OR NEW.type IS NOT OLD.type
              OR NEW.reseller_id IS NOT OLD.reseller_id
              OR NEW.currency IS NOT OLD.currency
            BEGIN SELECT RAISE(ABORT, 'adjustment financial fields are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS adjustment_no_delete
            BEFORE DELETE ON reseller_adjustments
            BEGIN SELECT RAISE(ABORT, 'adjustments cannot be deleted'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS refund_record_immutable_upd
            BEFORE UPDATE ON reseller_refunds
            BEGIN SELECT RAISE(ABORT, 'refund records are immutable'); END""")
        cur.execute("""
            CREATE TRIGGER IF NOT EXISTS refund_record_immutable_del
            BEFORE DELETE ON reseller_refunds
            BEGIN SELECT RAISE(ABORT, 'refund records are immutable'); END""")
    conn.commit()


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

_ADJ_COLS = ("id", "public_id", "reseller_id", "type", "amount_cents",
             "currency", "reason", "source", "status", "ledger_reference",
             "created_by", "decided_by", "decided_at", "completed_at",
             "note", "created", "updated")

_REF_COLS = ("id", "public_id", "reseller_id", "order_id", "order_public_id",
             "kind", "amount_cents", "currency", "reason", "source", "status",
             "ledger_reference", "created_by", "created")


def _d(cols, row):
    return dict(zip(cols, row)) if row else None


def insert_adjustment(fields: dict) -> int:
    """Caller owns the transaction boundary where ledger posting is involved."""
    cur = get_cursor()
    cols = tuple(fields.keys())
    cur.execute(
        f"INSERT INTO reseller_adjustments({','.join(cols)})"
        f" VALUES({','.join('?' for _ in cols)})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def get_adjustment_by_id(adjustment_id: int):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_adjustments WHERE id=?",
                (int(adjustment_id),))
    return _d(_ADJ_COLS, cur.fetchone())


def get_adjustment_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_adjustments WHERE public_id=?",
                (str(public_id),))
    return _d(_ADJ_COLS, cur.fetchone())


def transition_adjustment(adjustment_id: int, new_status: str,
                          allowed_from: tuple, decided_by: str = "",
                          ledger_reference: str = None,
                          completed: bool = False) -> bool:
    """Guarded status transition (rowcount proof).  Caller owns atomicity."""
    if new_status not in ADJUSTMENT_STATUSES:
        raise ValueError("invalid adjustment status")
    cur = get_cursor()
    now = now_text()
    sets = ["status=?", "updated=?"]
    params = [new_status, now]
    if decided_by:
        sets.append("decided_by=?")
        params.append(decided_by)
        sets.append("decided_at=?")
        params.append(now)
    if ledger_reference is not None:
        sets.append("ledger_reference=?")
        params.append(ledger_reference)
    if completed:
        sets.append("completed_at=?")
        params.append(now)
    params.append(int(adjustment_id))
    params.extend(allowed_from)
    cur.execute(
        f"UPDATE reseller_adjustments SET {', '.join(sets)}"
        f" WHERE id=? AND status IN ({','.join('?' for _ in allowed_from)})",
        tuple(params),
    )
    return cur.rowcount > 0


def list_adjustments(reseller_id: int = None, status: str = None,
                     limit: int = 50, after_id: int = None):
    where, params = " WHERE 1=1", []
    if reseller_id is not None:
        where += " AND reseller_id=?"
        params.append(int(reseller_id))
    if status in ADJUSTMENT_STATUSES:
        where += " AND status=?"
        params.append(status)
    if after_id:
        where += " AND id < ?"
        params.append(int(after_id))
    params.append(min(100, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_adjustments" + where +
        " ORDER BY id DESC LIMIT ?", tuple(params))
    return [_d(_ADJ_COLS, r) for r in cur.fetchall()]


def insert_refund_record(fields: dict) -> int:
    """Caller owns the transaction boundary (writes atomically with the
    ledger credit)."""
    cur = get_cursor()
    cols = tuple(fields.keys())
    cur.execute(
        f"INSERT INTO reseller_refunds({','.join(cols)})"
        f" VALUES({','.join('?' for _ in cols)})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def get_refund_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_refunds WHERE public_id=?",
                (str(public_id),))
    return _d(_REF_COLS, cur.fetchone())


def get_refund_by_ledger_reference(reference: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reseller_refunds WHERE ledger_reference=?",
                (str(reference),))
    return _d(_REF_COLS, cur.fetchone())


def list_refunds(reseller_id: int = None, order_id: int = None,
                 kind: str = None, limit: int = 50, after_id: int = None):
    where, params = " WHERE 1=1", []
    if reseller_id is not None:
        where += " AND reseller_id=?"
        params.append(int(reseller_id))
    if order_id is not None:
        where += " AND order_id=?"
        params.append(int(order_id))
    if kind in REFUND_KINDS:
        where += " AND kind=?"
        params.append(kind)
    if after_id:
        where += " AND id < ?"
        params.append(int(after_id))
    params.append(min(100, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reseller_refunds" + where +
        " ORDER BY id DESC LIMIT ?", tuple(params))
    return [_d(_REF_COLS, r) for r in cur.fetchall()]


__all__ = [
    "ADJUSTMENT_STATUSES", "ADJUSTMENT_TYPES", "REFUND_KINDS",
    "generate_adjustment_public_id", "generate_refund_public_id",
    "ensure_adjustment_tables", "insert_adjustment", "get_adjustment_by_id",
    "get_adjustment_by_public_id", "transition_adjustment",
    "list_adjustments", "insert_refund_record", "get_refund_by_public_id",
    "get_refund_by_ledger_reference", "list_refunds",
]
