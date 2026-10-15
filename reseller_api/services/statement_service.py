"""Reseller financial statement generation — Phase 15 Prompt 3.

A statement is a chronological account-activity snapshot over a billing
period: opening balance (ledger-derived), every ledger movement in the
window (references, never copies), totals by direction/type, closing
balance.  Financially READ-ONLY — generation writes no wallet rows.

Boundaries are half-open [start, end) in the shop timezone — the same clock
the ledger stamps with — so boundary transactions belong to exactly one
period.  Line ordering is (created, id): deterministic even when many rows
share a second.

Lifecycle: open (regeneration refreshes the same row) → finalized (frozen;
the trigger layer enforces it even if the service is bypassed).
"""

import re
from datetime import datetime, timedelta

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor
import database.reseller_db as rdb
import database.statement_db as sdb
from utils.timezone import now as tz_now, now_text
try:
    from utils.timezone import TZ_LABEL as _TZ_LABEL
except Exception:
    _TZ_LABEL = ""

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_CREDIT_TYPES = ("topup_credit", "refund_credit", "adjustment_credit")
_DEBIT_TYPES = ("purchase_debit", "adjustment_debit")


class StatementError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _day(value: str, name: str) -> str:
    v = str(value or "").strip()
    if not _DAY.fullmatch(v):
        raise StatementError("invalid_period", f"{name} must be YYYY-MM-DD.", 400)
    return v


def _period_bounds(period_type: str, start: str = None, end: str = None,
                   month: str = None, invoice_id: str = None):
    """Resolve (start_ts, end_ts) half-open shop-time bounds per type."""
    if period_type == "custom":
        ds = _day(start, "start_date")
        de = _day(end, "end_date")
        if de < ds:
            raise StatementError("invalid_period",
                                 "end_date must not precede start_date.", 400)
        lo = f"{ds} 00:00:00"
        hi = (datetime.strptime(de, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S")
        return lo, hi

    if period_type == "monthly":
        m = str(month or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}", m):
            raise StatementError("invalid_period", "month must be YYYY-MM.", 400)
        try:
            first = datetime.strptime(m + "-01", "%Y-%m-%d")
        except ValueError:
            raise StatementError("invalid_period", "month must be YYYY-MM.", 400)
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (first.strftime("%Y-%m-%d 00:00:00"),
                nxt.strftime("%Y-%m-%d 00:00:00"))

    if period_type == "weekly":
        # ISO week (Monday 00:00 shop-time → next Monday 00:00)
        ds = _day(start, "start_date")
        d0 = datetime.strptime(ds, "%Y-%m-%d")
        monday = d0 - timedelta(days=d0.weekday())
        return (monday.strftime("%Y-%m-%d 00:00:00"),
                (monday + timedelta(days=7)).strftime("%Y-%m-%d 00:00:00"))

    if period_type == "invoice":
        import database.invoice_db as idb
        invoice = idb.get_invoice_by_public_id(str(invoice_id or ""))
        if not invoice:
            raise StatementError("invoice_not_found", "Invoice not found.", 404)
        lo = f"{invoice['period_start']} 00:00:00"
        hi = (datetime.strptime(invoice["period_end"], "%Y-%m-%d")
              + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        return lo, hi

    raise StatementError("invalid_period",
                         "period_type must be monthly, weekly, custom, or invoice.",
                         400)


def generate_statement(reseller_id: int, period_type: str = "monthly",
                       start_date: str = None, end_date: str = None,
                       month: str = None, invoice_id: str = None,
                       generated_by: str = "", finalize: bool = False) -> dict:
    """Generate (or refresh/return) the statement for a reseller+period.

    Zero wallet writes.  Header + lines + number commit atomically.
    """
    sdb.ensure_statement_tables()

    reseller = rdb.get_reseller(int(reseller_id))
    if not reseller:
        raise StatementError("reseller_not_found", "Reseller not found.", 404)

    if period_type not in sdb.PERIOD_TYPES:
        raise StatementError("invalid_period", "Unsupported period type.", 400)
    lo, hi = _period_bounds(period_type, start_date, end_date, month,
                            invoice_id)
    # Note: future-dated statements are allowed — they are read-only
    # documents that simply contain an opening balance and no activity.

    existing = sdb.find_period_statement(int(reseller_id), period_type, lo, hi)
    if existing and existing["status"] == "finalized":
        existing["is_duplicate"] = True
        return existing

    cur = get_cursor()
    rid = int(reseller_id)

    # Opening balance: net ledger movement strictly before the window.
    cur.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
        " WHERE reseller_id=? AND created < ?",
        (rid, lo),
    )
    opening = int(cur.fetchone()[0] or 0)

    # Window rows, stable order (created, id) — deterministic even at
    # second-resolution collisions.
    cur.execute(
        "SELECT id, tx_type, amount_cents, balance_after_cents, reference,"
        " description, created, related_order_reference"
        " FROM reseller_wallet_transactions"
        " WHERE reseller_id=? AND created >= ? AND created < ?"
        " ORDER BY created, id",
        (rid, lo, hi),
    )
    rows = cur.fetchall()

    credits = sum(int(r[2]) for r in rows if r[1] in _CREDIT_TYPES)
    debits = sum(-int(r[2]) for r in rows if r[1] in _DEBIT_TYPES)
    purchases = sum(-int(r[2]) for r in rows if r[1] == "purchase_debit")
    refunds = sum(int(r[2]) for r in rows if r[1] == "refund_credit")
    adj_c = sum(int(r[2]) for r in rows if r[1] == "adjustment_credit")
    adj_d = sum(-int(r[2]) for r in rows if r[1] == "adjustment_debit")
    topups = sum(int(r[2]) for r in rows if r[1] == "topup_credit")
    orders_count = sum(1 for r in rows if r[1] == "purchase_debit")
    closing = opening + credits - debits

    # Integrity self-check: closing must equal the ledger's own position
    # at the window end.  A mismatch means the ledger is inconsistent —
    # refuse to publish the document rather than certify bad data.
    cur.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions"
        " WHERE reseller_id=? AND created < ?",
        (rid, hi),
    )
    ledger_at_end = int(cur.fetchone()[0] or 0)
    if ledger_at_end != closing:
        raise StatementError(
            "reconciliation_failed",
            "Ledger position at period end does not match the computed "
            "closing balance; the statement was not created.",
            500,
        )

    # order public ids for traceable purchase/refund lines
    order_pub = {}
    cur.execute(
        "SELECT id, request_id FROM orders WHERE reseller_id=? AND source='reseller_api'",
        (rid,))
    by_req = {f"rapi.{rid}.{req}": int(oid) for oid, req in cur.fetchall() if req}
    def _order_pub(ref, related):
        for cand in (related, ref):
            if not cand:
                continue
            if cand in by_req:
                return f"ORD-{by_req[cand]:08d}"
            m = re.search(r"\.order\.(\d+)$", str(cand))
            if m:
                return f"ORD-{int(m.group(1)):08d}"
        return None

    lines = []
    for lid, tx_type, amount, bal_after, ref, desc, created, related in rows:
        direction = "credit" if tx_type in _CREDIT_TYPES else "debit"
        lines.append({
            "ledger_id": int(lid),
            "reference": str(ref or ""),
            "tx_type": str(tx_type),
            "direction": direction,
            "amount_cents": abs(int(amount)),
            "balance_after_cents": int(bal_after),
            "description": str(desc or ""),
            "order_public_id": _order_pub(ref, related),
            "created": str(created),
        })

    # related invoices (overlap by covered days)
    first_day = lo[:10]
    last_day = (datetime.strptime(hi, "%Y-%m-%d %H:%M:%S")
                - timedelta(seconds=1)).strftime("%Y-%m-%d")
    inv_refs = sdb.invoices_overlapping(rid, first_day, last_day)
    invoice_ids = [i["public_id"] for i in inv_refs]

    conn = get_conn()
    with _pgcompat.atomic(conn):
        if existing:  # open statement: refresh the snapshot in place
            st_id = int(existing["id"])
            sdb.replace_statement_lines(st_id, lines)
            cur.execute(
                "UPDATE reseller_statements SET opening_balance_cents=?,"
                " closing_balance_cents=?, total_credits_cents=?,"
                " total_debits_cents=?, purchases_cents=?, refunds_cents=?,"
                " adjustment_credit_cents=?, adjustment_debit_cents=?,"
                " topup_cents=?, transactions_count=?, orders_count=?,"
                " invoice_ids=?, updated=?"
                " WHERE id=? AND status='open'",
                (opening, closing, credits, debits, purchases, refunds,
                 adj_c, adj_d, topups, len(lines), orders_count,
                 __import__("json").dumps(invoice_ids), now_text(), st_id),
            )
            if cur.rowcount != 1:
                out = sdb.get_statement_by_id(st_id)   # lost a finalize race
                out["is_duplicate"] = True
                return out
        else:
            st_id = sdb.insert_statement({
                "public_id": sdb.generate_statement_public_id(),
                "statement_number": sdb.next_statement_number(),
                "reseller_id": rid,
                "period_type": period_type,
                "period_start": lo,
                "period_end": hi,
                "timezone": str(_TZ_LABEL or ""),
                "status": "open",
                "currency": "USDT",
                "opening_balance_cents": opening,
                "closing_balance_cents": closing,
                "total_credits_cents": credits,
                "total_debits_cents": debits,
                "purchases_cents": purchases,
                "refunds_cents": refunds,
                "adjustment_credit_cents": adj_c,
                "adjustment_debit_cents": adj_d,
                "topup_cents": topups,
                "transactions_count": len(lines),
                "orders_count": orders_count,
                "invoice_ids": __import__("json").dumps(invoice_ids),
                "reseller_public_id": str(reseller["public_id"]),
                "reseller_name": str(reseller.get("name") or ""),
                "generated_by": str(generated_by or ""),
                "updated": now_text(),
            })
            sdb.replace_statement_lines(st_id, lines)
        if finalize:
            sdb.finalize_statement(st_id)

    out = sdb.get_statement_by_id(st_id)
    out["is_duplicate"] = False
    return out


def finalize_statement(statement_id: int) -> dict:
    st = sdb.get_statement_by_id(int(statement_id))
    if not st:
        raise StatementError("statement_not_found", "Statement not found.", 404)
    if st["status"] == "finalized":
        st["is_duplicate"] = True
        return st
    if not sdb.finalize_statement(int(statement_id)):
        raise StatementError("invalid_transition",
                             "Statement cannot be finalized from its current state.",
                             409)
    out = sdb.get_statement_by_id(int(statement_id))
    out["is_duplicate"] = False
    return out


__all__ = ["StatementError", "generate_statement", "finalize_statement"]
