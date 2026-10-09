"""Reconciliation run + discrepancy persistence — Phase 15 Prompt 6.

Operational records only: a run documents a sweep; a discrepancy documents a
detected inconsistency and its resolution workflow.  Neither stores money or
financial facts — every `expected`/`actual` value is a reporting snapshot
labelled with its authoritative source.

Run status:      running → completed | failed.
Discrepancy:     open → investigating → resolved | ignored
                 resolved → open (regression: the same fingerprint is
                 re-detected on a later run — a money problem RETURNED).
Resolved/ignored are terminal for that finding instance; a re-detection
after resolution opens a NEW discrepancy row (never rewrites history).

Severity rules (documented):
  CRITICAL — money loss/duplication possible (balance_mismatch,
             duplicate purchase debit, refund_exceeds_charge,
             negative_balance, chain_break)
  HIGH     — broken financial linkage (missing ledger row for a completed
             record, refund without debit, adjustment/invoice/statement
             mismatches, orphan transactions, debit without order)
  MEDIUM   — non-debit duplicates, currency mismatch
  INFO     — legacy rows pre-dating the P15 record layer
             (missing refund/adjustment record) and impossible-by-constraint
             confirmations (zero_amount, balance_overflow)
"""

import secrets
from datetime import datetime

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor
from utils.timezone import now_text

RUN_STATUSES = ("running", "completed", "failed")
DISCREPANCY_STATUSES = ("open", "investigating", "resolved", "ignored")
SEVERITIES = ("info", "low", "medium", "high", "critical")

_PUBLIC_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_run_public_id() -> str:
    return "RCR-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET)
                            for _ in range(10))


def generate_discrepancy_public_id() -> str:
    return "DSC-" + "".join(secrets.choice(_PUBLIC_ID_ALPHABET)
                            for _ in range(10))


def ensure_reconciliation_tables():
    conn, cur = get_conn(), get_cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reconciliation_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        scope TEXT NOT NULL DEFAULT 'platform'
            CHECK (scope IN ('platform','reseller')),
        reseller_id INTEGER REFERENCES resellers(id) ON DELETE SET NULL,
        period_start TEXT,
        period_end   TEXT,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running','completed','failed')),
        records_checked INTEGER NOT NULL DEFAULT 0,
        discrepancies_found INTEGER NOT NULL DEFAULT 0,
        severity_summary TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        created_by TEXT DEFAULT '',
        started_at TEXT,
        completed_at TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reconciliation_discrepancies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL,
        reseller_id INTEGER REFERENCES resellers(id) ON DELETE SET NULL,
        type TEXT NOT NULL,
        severity TEXT NOT NULL
            CHECK (severity IN ('info','low','medium','high','critical')),
        source_type TEXT DEFAULT '',
        source_ref TEXT DEFAULT '',
        expected_value TEXT DEFAULT '',
        actual_value TEXT DEFAULT '',
        currency TEXT DEFAULT 'USDT',
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open','investigating','resolved','ignored')),
        description TEXT DEFAULT '',
        resolution_reason TEXT DEFAULT '',
        resolved_by TEXT DEFAULT '',
        resolution_reference TEXT DEFAULT '',
        detected_at TEXT,
        resolved_at TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP,
        updated TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_recon_runs_created ON reconciliation_runs(id)",
        "CREATE INDEX IF NOT EXISTS idx_recon_runs_reseller ON reconciliation_runs(reseller_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_recon_disc_run ON reconciliation_discrepancies(run_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_recon_disc_fp ON reconciliation_discrepancies(fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_recon_disc_status ON reconciliation_discrepancies(status, severity)",
    ):
        cur.execute(stmt)

    # No deletes — reconciliation history is append-only.  Status fields
    # advance through the guarded transitions only.
    if _pgcompat.use_postgres():
        cur.execute("""
            CREATE OR REPLACE FUNCTION oggy_recon_no_delete() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'reconciliation history is append-only'; END;
            $$ LANGUAGE plpgsql""")
        for tbl in ("reconciliation_runs", "reconciliation_discrepancies"):
            cur.execute(f"DROP TRIGGER IF EXISTS recon_no_delete ON {tbl}")
            cur.execute(f"CREATE TRIGGER recon_no_delete BEFORE DELETE ON {tbl} FOR EACH ROW EXECUTE FUNCTION oggy_recon_no_delete()")
    else:
        for tbl in ("reconciliation_runs", "reconciliation_discrepancies"):
            cur.execute(f"""
                CREATE TRIGGER IF NOT EXISTS {tbl}_no_delete
                BEFORE DELETE ON {tbl}
                BEGIN SELECT RAISE(ABORT, 'reconciliation history is append-only'); END""")
    conn.commit()


_RUN_COLS = ("id", "public_id", "scope", "reseller_id", "period_start",
             "period_end", "status", "records_checked", "discrepancies_found",
             "severity_summary", "error", "created_by", "started_at",
             "completed_at", "created")

_DISC_COLS = ("id", "public_id", "run_id", "fingerprint", "reseller_id",
              "type", "severity", "source_type", "source_ref",
              "expected_value", "actual_value", "currency", "status",
              "description", "resolution_reason", "resolved_by",
              "resolution_reference", "detected_at", "resolved_at",
              "created", "updated")


def _d(cols, row):
    return dict(zip(cols, row)) if row else None


def insert_run(fields: dict) -> int:
    cur = get_cursor()
    cols = tuple(fields.keys())
    cur.execute(
        f"INSERT INTO reconciliation_runs({','.join(cols)})"
        f" VALUES({','.join('?' for _ in cols)})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def finish_run(run_id: int, status: str, records_checked: int,
               discrepancies_found: int, severity_summary: str,
               error: str = "") -> bool:
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reconciliation_runs SET status=?, records_checked=?,"
        " discrepancies_found=?, severity_summary=?, error=?, completed_at=?"
        " WHERE id=? AND status='running'",
        (status, int(records_checked), int(discrepancies_found),
         severity_summary, str(error or "")[:300], now_text(),
         int(run_id)),
    )
    conn.commit()
    return cur.rowcount > 0


def get_run(run_id: int):
    cur = get_cursor()
    cur.execute("SELECT * FROM reconciliation_runs WHERE id=?", (int(run_id),))
    return _d(_RUN_COLS, cur.fetchone())


def get_run_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reconciliation_runs WHERE public_id=?",
                (str(public_id),))
    return _d(_RUN_COLS, cur.fetchone())


def list_runs(limit: int = 50, after_id: int = None, scope: str = None,
              reseller_id: int = None):
    where, params = " WHERE 1=1", []
    if scope in RUN_STATUSES or scope == "platform" or scope == "reseller":
        where += " AND scope=?"
        params.append(scope)
    if reseller_id is not None:
        where += " AND reseller_id=?"
        params.append(int(reseller_id))
    if after_id:
        where += " AND id < ?"
        params.append(int(after_id))
    params.append(min(100, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reconciliation_runs" + where +
        " ORDER BY id DESC LIMIT ?", tuple(params))
    return [_d(_RUN_COLS, r) for r in cur.fetchall()]


def find_open_discrepancy(fingerprint: str):
    """Open/investigating discrepancy with this fingerprint, if any."""
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reconciliation_discrepancies"
        " WHERE fingerprint=? AND status IN ('open','investigating')"
        " ORDER BY id DESC LIMIT 1",
        (str(fingerprint),),
    )
    return _d(_DISC_COLS, cur.fetchone())


def insert_discrepancy(fields: dict) -> int:
    cur = get_cursor()
    cols = tuple(fields.keys())
    cur.execute(
        f"INSERT INTO reconciliation_discrepancies({','.join(cols)})"
        f" VALUES({','.join('?' for _ in cols)})",
        tuple(fields[c] for c in cols),
    )
    return int(cur.lastrowid)


def touch_discrepancy_seen(disc_id: int, run_id: int) -> None:
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE reconciliation_discrepancies SET updated=?, run_id=?"
        " WHERE id=? AND status IN ('open','investigating')",
        (now_text(), int(run_id), int(disc_id)),
    )
    conn.commit()


def transition_discrepancy(disc_id: int, new_status: str,
                           allowed_from: tuple, reason: str = "",
                           resolver: str = "", reference: str = "") -> bool:
    if new_status not in DISCREPANCY_STATUSES:
        raise ValueError("invalid discrepancy status")
    conn, cur = get_conn(), get_cursor()
    now = now_text()
    sets = ["status=?", "updated=?"]
    params = [new_status, now]
    if new_status in ("resolved", "ignored"):
        sets += ["resolution_reason=?", "resolved_by=?",
                 "resolution_reference=?", "resolved_at=?"]
        params += [str(reason or "")[:500], str(resolver or ""),
                   str(reference or "")[:200], now]
    params.append(int(disc_id))
    params.extend(allowed_from)
    cur.execute(
        f"UPDATE reconciliation_discrepancies SET {', '.join(sets)}"
        f" WHERE id=? AND status IN ({','.join('?' for _ in allowed_from)})",
        tuple(params),
    )
    conn.commit()
    return cur.rowcount > 0


def get_discrepancy_by_public_id(public_id: str):
    cur = get_cursor()
    cur.execute("SELECT * FROM reconciliation_discrepancies WHERE public_id=?",
                (str(public_id),))
    return _d(_DISC_COLS, cur.fetchone())


def list_discrepancies(run_id: int = None, status: str = None,
                       severity: str = None, disc_type: str = None,
                       reseller_id: int = None, limit: int = 100,
                       after_id: int = None):
    where, params = " WHERE 1=1", []
    if run_id is not None:
        where += " AND run_id=?"
        params.append(int(run_id))
    if status in DISCREPANCY_STATUSES:
        where += " AND status=?"
        params.append(status)
    if severity in SEVERITIES:
        where += " AND severity=?"
        params.append(severity)
    if disc_type:
        where += " AND type=?"
        params.append(str(disc_type)[:64])
    if reseller_id is not None:
        where += " AND reseller_id=?"
        params.append(int(reseller_id))
    if after_id:
        where += " AND id < ?"
        params.append(int(after_id))
    params.append(min(500, max(1, int(limit))))
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM reconciliation_discrepancies" + where +
        " ORDER BY id DESC LIMIT ?", tuple(params))
    return [_d(_DISC_COLS, r) for r in cur.fetchall()]


def discrepancy_dashboard() -> dict:
    cur = get_cursor()
    cur.execute("SELECT COUNT(*) FROM reconciliation_runs")
    runs = int(cur.fetchone()[0])
    cur.execute(
        "SELECT status, COUNT(*) FROM reconciliation_discrepancies"
        " GROUP BY status")
    by_status = {s: int(c) for s, c in cur.fetchall()}
    cur.execute(
        "SELECT severity, COUNT(*) FROM reconciliation_discrepancies"
        " WHERE status IN ('open','investigating') GROUP BY severity")
    open_by_sev = {s: int(c) for s, c in cur.fetchall()}
    cur.execute(
        "SELECT type, COUNT(*) FROM reconciliation_discrepancies"
        " WHERE status IN ('open','investigating') GROUP BY type")
    open_by_type = {t: int(c) for t, c in cur.fetchall()}
    return {
        "total_runs": runs,
        "open": by_status.get("open", 0),
        "investigating": by_status.get("investigating", 0),
        "resolved": by_status.get("resolved", 0),
        "ignored": by_status.get("ignored", 0),
        "open_by_severity": open_by_sev,
        "open_by_type": open_by_type,
    }


__all__ = [
    "RUN_STATUSES", "DISCREPANCY_STATUSES", "SEVERITIES",
    "generate_run_public_id", "generate_discrepancy_public_id",
    "ensure_reconciliation_tables", "insert_run", "finish_run",
    "get_run", "get_run_by_public_id", "list_runs",
    "find_open_discrepancy", "insert_discrepancy",
    "touch_discrepancy_seen", "transition_discrepancy",
    "get_discrepancy_by_public_id", "list_discrepancies",
    "discrepancy_dashboard",
]
