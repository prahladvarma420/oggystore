"""Reseller invoice endpoints — Phase 15 Prompt 2.

  GET /api/v1/invoices                        list (cursor paginated)
  GET /api/v1/invoices/<INV-…>                detail
  GET /api/v1/invoices/<INV-…>/lines          line items
  GET /api/v1/invoices/<INV-…>/document       printable HTML document

Every query is scoped to the authenticated reseller — foreign invoice IDs
return the uniform 404 (no existence oracle).  Money is decimal USDT
strings via from_cents.  The HTML document carries no internal database
IDs and no secrets.
"""

import re
from html import escape as _esc

from flask import Response, request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.services.authorization import ORDERS_READ
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
import database.reseller_db as rdb
import database.invoice_db as idb

_PUBLIC_RE = re.compile(r"^INV-[A-HJ-NP-Z2-9]{12}$")

_LINE_LABELS = {
    "purchase": "Order charge",
    "refund_full": "Refund",
    "refund_cancellation": "Cancellation refund",
    "refund_partial": "Partial refund",
    "adjustment_credit": "Credit adjustment",
    "adjustment_debit": "Debit adjustment",
    "topup": "Wallet top-up",
}


def _parse_public_id(raw: str) -> str:
    v = str(raw or "").strip()
    if not _PUBLIC_RE.fullmatch(v):
        raise ApiError("invoice_not_found", "Invoice not found.", 404)
    return v


def _own_invoice(context, public_id: str):
    inv = idb.get_invoice_for_reseller(context.reseller_id,
                                       _parse_public_id(public_id))
    if not inv:
        raise ApiError("invoice_not_found", "Invoice not found.", 404)
    return inv


def _serialize(inv: dict) -> dict:
    return {
        "invoice_id": inv["public_id"],
        "invoice_number": inv["invoice_number"],
        "status": inv["status"],
        "currency": inv["currency"],
        "billing_period": {"start": inv["period_start"],
                           "end": inv["period_end"]},
        "subtotal": rdb.from_cents(inv["subtotal_cents"]),
        "refunds": rdb.from_cents(inv["refund_cents"]),
        "adjustment_credits": rdb.from_cents(inv["adjustment_credit_cents"]),
        "adjustment_debits": rdb.from_cents(inv["adjustment_debit_cents"]),
        "topups": rdb.from_cents(inv["topup_cents"]),
        "total": rdb.from_cents(inv["total_cents"]),
        "amount_paid": rdb.from_cents(inv["amount_paid_cents"]),
        "amount_due": rdb.from_cents(inv["amount_due_cents"]),
        "orders_count": inv["orders_count"],
        "transactions_count": inv["transactions_count"],
        "issued_at": inv["created"],
        "finalized_at": inv["finalized_at"],
        "voided_at": inv["voided_at"],
    }


def _serialize_line(line: dict) -> dict:
    return {
        "type": line["source_type"],
        "label": _LINE_LABELS.get(line["source_type"], line["source_type"]),
        "description": line["description"],
        "reference": line["source_reference"],
        "order_id": line["order_public_id"],
        "product_id": line["product_public_id"],
        "quantity": line["quantity"],
        "unit_amount": rdb.from_cents(line["unit_amount_cents"]),
        "total": rdb.from_cents(line["total_cents"]),
        "currency": "USDT",
        "created_at": line["created"],
    }


@require_operation(ORDERS_READ)
def list_invoices():
    context = get_authenticated_context()
    limit_raw = (request.args.get("limit") or "20").strip()
    try:
        limit = int(limit_raw)
    except ValueError:
        raise ApiError("invalid_pagination", "limit must be a positive integer.", 400)
    if limit < 1 or limit > 100:
        raise ApiError("invalid_pagination", "limit must not exceed 100.", 400)
    after_id = None
    raw_after = (request.args.get("after_id") or "").strip()
    if raw_after:
        m = _PUBLIC_RE.fullmatch(raw_after)
        if not m:
            raise ApiError("invalid_pagination",
                           "after_id must be a valid invoice ID.", 400)
        row = idb.get_invoice_for_reseller(context.reseller_id, raw_after)
        if not row:
            raise ApiError("invalid_pagination",
                           "after_id must be a valid invoice ID.", 400)
        after_id = int(row["id"])
    status = (request.args.get("status") or "").strip()
    if status and status not in idb.INVOICE_STATUSES:
        raise ApiError("invalid_parameter", "Unsupported invoice status.", 400)

    # Phase 15 P7: billing-period date filters + currency (USDT only today)
    _DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    date_from = date_to = None
    for name in ("date_from", "date_to"):
        raw = (request.args.get(name) or "").strip()
        if raw:
            if not _DAY.fullmatch(raw):
                raise ApiError("invalid_parameter", f"{name} must be YYYY-MM-DD.",
                               400)
            if name == "date_from":
                date_from = raw
            else:
                date_to = raw
    currency = (request.args.get("currency") or "").strip().upper()
    if currency and currency != "USDT":
        raise ApiError("invalid_parameter", "Unsupported currency.", 400)

    rows = idb.list_invoices(reseller_id=context.reseller_id,
                             status=status or None,
                             limit=limit + 1, after_id=after_id,
                             date_from=date_from, date_to=date_to,
                             currency=currency or None)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return success_response(
        {"invoices": [_serialize(r) for r in rows]},
        meta={"pagination": {
            "has_more": has_more,
            "next_after_id": rows[-1]["public_id"] if has_more and rows else None,
        }})


@require_operation(ORDERS_READ)
def invoice_detail(invoice_id: str):
    context = get_authenticated_context()
    inv = _own_invoice(context, invoice_id)
    return success_response({"invoice": _serialize(inv)})


@require_operation(ORDERS_READ)
def invoice_lines(invoice_id: str):
    context = get_authenticated_context()
    inv = _own_invoice(context, invoice_id)
    lines = idb.get_invoice_lines(int(inv["id"]))
    return success_response({
        "invoice_id": inv["public_id"],
        "invoice_number": inv["invoice_number"],
        "currency": "USDT",
        "lines": [_serialize_line(l) for l in lines],
    })


def _document_html(inv: dict, lines: list) -> str:
    try:
        from config import STORE_NAME
        store = str(STORE_NAME or "OGGY Store")
    except Exception:
        store = "OGGY Store"
    money = rdb.from_cents
    rows = []
    for l in lines:
        rows.append(
            "<tr><td>{}</td><td>{}</td><td class='r'>{}</td>"
            "<td class='r'>{}</td><td class='r'>{} {}</td></tr>".format(
                _esc(_LINE_LABELS.get(l["source_type"], l["source_type"])),
                _esc(str(l["description"] or "")),
                int(l["quantity"]),
                money(l["unit_amount_cents"]),
                money(l["total_cents"]), "USDT",
            ))
    if not rows:
        rows.append("<tr><td colspan='5' class='c dim'>No billable activity in this period.</td></tr>")
    s = _serialize(inv)
    badge = {"draft": "#888", "finalized": "#1a7f37", "void": "#b00"}[inv["status"]]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Invoice {_esc(s['invoice_number'])}</title>
<style>
 body{{font-family:Arial,sans-serif;margin:32px;color:#222;max-width:820px}}
 h1{{font-size:22px;margin:0}} .dim{{color:#777}} .r{{text-align:right}}
 .c{{text-align:center}} table{{width:100%;border-collapse:collapse;margin-top:18px}}
 th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;font-size:13px}}
 th{{background:#f6f6f6}} .totals td{{border:none;padding:4px 8px}}
 .badge{{color:#fff;background:{badge};padding:3px 10px;border-radius:10px;font-size:12px}}
 @media print{{.noprint{{display:none}}}}
</style></head><body>
<div class="noprint" style="margin-bottom:12px"><button onclick="window.print()">Print / Save as PDF</button></div>
<h1>{_esc(store)}</h1>
<div class="dim">Reseller invoice</div>
<table style="border:none;margin-top:10px"><tr>
<td style="border:none"><b>Invoice</b><br>{_esc(s['invoice_number'])}<br>
<span class="dim">{_esc(s['invoice_id'])}</span></td>
<td style="border:none"><b>Reseller</b><br>{_esc(str(inv['reseller_name'] or ''))}<br>
<span class="dim">{_esc(str(inv['reseller_public_id'] or ''))}</span></td>
<td style="border:none" class="r"><b>Status</b><br>
<span class="badge">{_esc(s['status'].upper())}</span></td></tr>
<tr><td style="border:none"><b>Billing period</b><br>{_esc(s['billing_period']['start'])} → {_esc(s['billing_period']['end'])}</td>
<td style="border:none"><b>Issued</b><br>{_esc(str(s['issued_at'] or ''))}</td>
<td style="border:none" class="r"><b>Finalized</b><br>{_esc(str(s['finalized_at'] or '—'))}</td></tr></table>
<table><thead><tr><th>Type</th><th>Description</th><th class="r">Qty</th>
<th class="r">Unit</th><th class="r">Total</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<table class="totals" style="margin-left:auto;width:320px">
<tr><td>Subtotal</td><td class="r">{s['subtotal']} USDT</td></tr>
<tr><td>Refunds</td><td class="r">−{s['refunds']} USDT</td></tr>
<tr><td>Adjustments (credits)</td><td class="r">−{s['adjustment_credits']} USDT</td></tr>
<tr><td>Adjustments (debits)</td><td class="r">+{s['adjustment_debits']} USDT</td></tr>
<tr><td><b>Total</b></td><td class="r"><b>{s['total']} USDT</b></td></tr>
<tr><td>Amount paid (settled via wallet)</td><td class="r">{s['amount_paid']} USDT</td></tr>
<tr><td><b>Amount due</b></td><td class="r"><b>{s['amount_due']} USDT</b></td></tr>
</table>
<p class="dim" style="margin-top:18px">Generated { _esc(now_note()) }. This is a
snapshot document of wallet-settled activity; corrections appear as credit
notes or adjustments on later invoices, never by editing this document.</p>
</body></html>"""


def now_note() -> str:
    from utils.timezone import now_text
    return now_text()


@require_operation(ORDERS_READ)
def invoice_transactions(invoice_id: str):
    """GET /api/v1/invoices/<id>/transactions — the ledger rows this invoice
    documents (resolved via the lines' references; own invoice only)."""
    context = get_authenticated_context()
    inv = _own_invoice(context, invoice_id)
    lines = idb.get_invoice_lines(int(inv["id"]))
    refs = [l["source_reference"] for l in lines]
    cur = rdb.get_cursor()
    tx_rows = []
    if refs:
        ph = ",".join("?" for _ in refs)
        cur.execute(
            f"SELECT * FROM reseller_wallet_transactions"
            f" WHERE reseller_id=? AND reference IN ({ph}) ORDER BY id",
            (context.reseller_id, *refs),
        )
        cols = rdb._LEDGER_COLS
        tx_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = []
    for row in tx_rows:
        amount = int(row["amount_cents"])
        out.append({
            "reference": row["reference"],
            "type": row["tx_type"],
            "direction": "credit" if amount > 0 else "debit",
            "amount": rdb.from_cents(abs(amount)),
            "currency": row["currency"],
            "balance_after": rdb.from_cents(int(row["balance_after_cents"])),
            "description": row["description"] or "",
            "created_at": row["created"],
        })
    return success_response({
        "invoice_id": inv["public_id"],
        "invoice_number": inv["invoice_number"],
        "currency": "USDT",
        "transactions": out,
    })


@require_operation(ORDERS_READ)
def invoice_document(invoice_id: str):
    context = get_authenticated_context()
    inv = _own_invoice(context, invoice_id)
    html = _document_html(inv, idb.get_invoice_lines(int(inv["id"])))
    return Response(html, mimetype="text/html")


__all__ = ["list_invoices", "invoice_detail", "invoice_lines",
           "invoice_transactions", "invoice_document"]
