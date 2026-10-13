"""Reseller invoice generation service — Phase 15 Prompt 2.

Builds invoice DOCUMENTS from the authoritative wallet ledger + master
orders.  Invoices never move money: generation performs zero wallet
transactions (prepaid model — every charge settled at order time), so
amount_paid == total and amount_due == 0 by construction.

Pipeline per the Phase 15 design:
  validate reseller → validate period → read authoritative records →
  build line items (ledger-referenced) → totals → unique number →
  one atomic commit (header + lines) → finalize.

Idempotency: one non-void invoice per (reseller, period) enforced by the
partial UNIQUE index; a repeated generation returns the existing invoice.
Concurrency: number allocation runs inside the same atomic block (settings
counter is write-lock serialized); unique-violation losers refetch.

Refunds are represented as credit lines in the period they OCCURRED — the
historical purchase line in the earlier invoice is never rewritten.
"""

import re
from datetime import datetime, timedelta

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor
import database.reseller_db as rdb
import database.invoice_db as idb
from utils.timezone import now_text, now as tz_now

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvoiceError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _day(value: str, name: str) -> str:
    v = str(value or "").strip()
    if not _DAY.fullmatch(v):
        raise InvoiceError("invalid_period", f"{name} must be YYYY-MM-DD.", 400)
    return v


def _eligible_ledger_rows(reseller_id: int, lo: str, hi: str):
    """Authoritative wallet movements inside the billing window."""
    cur = get_cursor()
    cur.execute(
        "SELECT tx_type, amount_cents, reference, description, created,"
        " related_order_reference"
        " FROM reseller_wallet_transactions"
        " WHERE reseller_id=? AND created >= ? AND created <= ?"
        " ORDER BY id",
        (int(reseller_id), lo, hi),
    )
    return cur.fetchall()


def _order_public_map(reseller_id: int, references):
    """Map ledger references (rapi.{rid}.{req} and rapi.*.order.N) to public
    order/product ids for readable, internal-id-free lines."""
    cur = get_cursor()
    out = {}
    refs = [r for r in references if r]
    if not refs:
        return out
    # wallet purchase references -> orders via request_id
    cur.execute(
        "SELECT id, request_id, product_id, product_name, quantity,"
        " unit_price_cents FROM orders"
        " WHERE reseller_id=? AND source='reseller_api'",
        (int(reseller_id),),
    )
    by_req = {}
    by_id = {}
    for oid, req, pid, pname, qty, unit in cur.fetchall():
        by_req[f"rapi.{reseller_id}.{req}"] = oid
        by_id[int(oid)] = (pid, pname, qty, unit)
    for ref in refs:
        oid = by_req.get(ref)
        m = re.search(r"\.order\.(\d+)$", str(ref))
        if oid is None and m:
            oid = int(m.group(1))
        if oid is None or oid not in by_id:
            continue
        pid, pname, qty, unit = by_id[oid]
        out[ref] = {
            "order_public_id": f"ORD-{int(oid):08d}",
            "product_public_id": (rdb.ensure_product_public_id(int(pid))
                                  if pid else None),
            "product_name": str(pname or ""),
            "quantity": int(qty or 1),
            "unit_cents": int(unit or 0),
        }
    return out


def _build_lines(reseller_id: int, ledger_rows):
    """Turn authoritative ledger rows into invoice line dicts."""
    lines = []
    meta = _order_public_map(reseller_id, [r[2] for r in ledger_rows])
    for tx_type, amount, reference, description, created, _related in ledger_rows:
        amount = int(amount)
        m = meta.get(reference, {})
        base = {
            "source_reference": str(reference),
            "order_public_id": m.get("order_public_id"),
            "product_public_id": m.get("product_public_id"),
            "created": str(created or ""),
        }
        if tx_type == "purchase_debit":
            unit = abs(int(m.get("unit_cents") or 0))
            qty = int(m.get("quantity") or 1)
            total = abs(amount)
            lines.append({**base,
                          "source_type": "purchase",
                          "description": (m.get("product_name") or "Order charge")
                                         + (f" x{qty}" if qty > 1 else ""),
                          "quantity": qty,
                          "unit_amount_cents": unit or total,
                          "total_cents": total})
        elif tx_type == "refund_credit":
            kind = ("refund_cancellation" if ".cancel_refund." in reference
                    else "refund_partial" if ".partial_refund." in reference
                    else "refund_full")
            lines.append({**base,
                          "source_type": kind,
                          "description": description or "Refund",
                          "quantity": 1,
                          "unit_amount_cents": -abs(amount),
                          "total_cents": -abs(amount)})
        elif tx_type == "adjustment_credit":
            lines.append({**base, "source_type": "adjustment_credit",
                          "description": description or "Credit adjustment",
                          "quantity": 1, "unit_amount_cents": abs(amount),
                          "total_cents": abs(amount)})
        elif tx_type == "adjustment_debit":
            lines.append({**base, "source_type": "adjustment_debit",
                          "description": description or "Debit adjustment",
                          "quantity": 1, "unit_amount_cents": abs(amount),
                          "total_cents": abs(amount)})
        elif tx_type == "topup_credit":
            lines.append({**base, "source_type": "topup",
                          "description": description or "Wallet top-up",
                          "quantity": 1, "unit_amount_cents": abs(amount),
                          "total_cents": abs(amount)})
    return lines


def generate_invoice(reseller_id: int, period_start: str, period_end: str,
                     issued_by: str = "", finalize: bool = True) -> dict:
    """Generate (or idempotently return) the invoice for a reseller+period.

    Zero wallet writes.  One atomic commit for header + lines + number.
    """
    idb.ensure_invoice_tables()

    reseller = rdb.get_reseller(int(reseller_id))
    if not reseller:
        raise InvoiceError("reseller_not_found", "Reseller not found.", 404)

    d_start = _day(period_start, "period_start")
    d_end = _day(period_end, "period_end")
    if d_end < d_start:
        raise InvoiceError("invalid_period",
                           "period_end must not precede period_start.", 400)
    if d_start > tz_now().strftime("%Y-%m-%d"):
        raise InvoiceError("invalid_period",
                           "period_start cannot be in the future.", 400)

    # Idempotency: an existing non-void invoice for this period is the answer
    existing = idb.find_open_period_invoice(int(reseller_id), d_start, d_end)
    if existing and existing["status"] == "finalized":
        existing["is_duplicate"] = True
        return existing

    lo = f"{d_start} 00:00:00"
    hi = f"{d_end} 23:59:59"
    rows = _eligible_ledger_rows(int(reseller_id), lo, hi)
    lines = _build_lines(int(reseller_id), rows)

    subtotal = sum(l["total_cents"] for l in lines if l["source_type"] == "purchase")
    refunds = sum(-l["total_cents"] for l in lines
                  if l["source_type"].startswith("refund_"))
    adj_c = sum(l["total_cents"] for l in lines
                if l["source_type"] == "adjustment_credit")
    adj_d = sum(l["total_cents"] for l in lines
                if l["source_type"] == "adjustment_debit")
    topups = sum(l["total_cents"] for l in lines if l["source_type"] == "topup")
    total = subtotal - refunds + adj_d - adj_c
    total = max(0, total)     # a document never bills a negative amount
    orders_count = sum(1 for l in lines if l["source_type"] == "purchase")

    conn = get_conn()
    with _pgcompat.atomic(conn):
        if existing:  # draft regeneration: rebuild lines + totals
            inv_id = int(existing["id"])
            idb.replace_invoice_lines(inv_id, lines)
            cur = get_cursor()
            cur.execute(
                "UPDATE reseller_invoices SET subtotal_cents=?, refund_cents=?,"
                " adjustment_credit_cents=?, adjustment_debit_cents=?,"
                " topup_cents=?, total_cents=?, amount_paid_cents=?,"
                " amount_due_cents=?, orders_count=?, transactions_count=?,"
                " updated=? WHERE id=? AND status='draft'",
                (subtotal, refunds, adj_c, adj_d, topups, total, total, 0,
                 orders_count, len(lines), now_text(), inv_id),
            )
            if cur.rowcount != 1:
                # lost a race with a finalizer: return the committed state
                cur.execute(
                    "SELECT id FROM reseller_invoices WHERE id=?", (inv_id,))
                out = idb.get_invoice_by_id(inv_id)
                out["is_duplicate"] = True
                return out
        else:
            public_id = idb.generate_invoice_public_id()
            number = idb.next_invoice_number()
            inv_id = idb.insert_invoice({
                "public_id": public_id,
                "invoice_number": number,
                "reseller_id": int(reseller_id),
                "period_start": d_start,
                "period_end": d_end,
                "status": "draft",
                "currency": "USDT",
                "subtotal_cents": subtotal,
                "refund_cents": refunds,
                "adjustment_credit_cents": adj_c,
                "adjustment_debit_cents": adj_d,
                "topup_cents": topups,
                "total_cents": total,
                "amount_paid_cents": total,   # prepaid: settled at order time
                "amount_due_cents": 0,
                "orders_count": orders_count,
                "transactions_count": len(lines),
                "reseller_public_id": str(reseller["public_id"]),
                "reseller_name": str(reseller.get("name") or ""),
                "issued_by": str(issued_by or ""),
                "updated": now_text(),
            })
            idb.replace_invoice_lines(inv_id, lines)

        if finalize:
            idb.set_invoice_status(inv_id, "finalized")

    out = idb.get_invoice_by_id(inv_id)
    out["is_duplicate"] = False
    return out


def void_invoice(invoice_id: int, reason: str) -> dict:
    """Void an invoice (correction).  Finalized or draft both voidable;
    void is terminal (trigger-enforced).  Never deletes anything."""
    inv = idb.get_invoice_by_id(int(invoice_id))
    if not inv:
        raise InvoiceError("invoice_not_found", "Invoice not found.", 404)
    if inv["status"] == "void":
        inv["is_duplicate"] = True
        return inv
    if not idb.set_invoice_status(int(invoice_id), "void",
                                  void_reason=str(reason or "")[:300]):
        raise InvoiceError("invalid_transition",
                           "Invoice cannot be voided from its current state.",
                           409)
    out = idb.get_invoice_by_id(int(invoice_id))
    out["is_duplicate"] = False
    return out


__all__ = ["InvoiceError", "generate_invoice", "void_invoice"]
