"""Reseller statement endpoints — Phase 15 Prompt 3.

  GET /api/v1/statements                        list (cursor paginated)
  GET /api/v1/statements/<STMT-…>               detail (+ reconciliation)
  GET /api/v1/statements/<STMT-…>/transactions  paginated ledger-referenced lines
  GET /api/v1/statements/<STMT-…>/invoices      related invoice references
  GET /api/v1/statements/<STMT-…>/document      printable HTML document

Everything is scoped to the authenticated reseller; foreign/unknown IDs
return the uniform 404.  Money is decimal USDT strings.  Lines reference
the ledger (reference + type), never copy mutable data, and never expose
internal IDs.
"""

import json
import re
from datetime import datetime, timedelta
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
import database.statement_db as sdb
from database.db import get_cursor

_PUBLIC_RE = re.compile(r"^STMT-[A-HJ-NP-Z2-9]{12}$")

_TYPE_LABELS = {
    "purchase_debit": "Order charge",
    "refund_credit": "Refund",
    "topup_credit": "Wallet top-up",
    "adjustment_credit": "Credit adjustment",
    "adjustment_debit": "Debit adjustment",
}


def _parse_public_id(raw: str) -> str:
    v = str(raw or "").strip()
    if not _PUBLIC_RE.fullmatch(v):
        raise ApiError("statement_not_found", "Statement not found.", 404)
    return v


def _own(context, public_id: str):
    st = sdb.get_statement_for_reseller(context.reseller_id,
                                        _parse_public_id(public_id))
    if not st:
        raise ApiError("statement_not_found", "Statement not found.", 404)
    return st


def _serialize(st: dict) -> dict:
    return {
        "statement_id": st["public_id"],
        "statement_number": st["statement_number"],
        "status": st["status"],
        "currency": st["currency"],
        "period_type": st["period_type"],
        "period": {"start": st["period_start"], "end": st["period_end"],
                   "half_open": True, "timezone": st["timezone"]},
        "opening_balance": rdb.from_cents(st["opening_balance_cents"]),
        "closing_balance": rdb.from_cents(st["closing_balance_cents"]),
        "total_credits": rdb.from_cents(st["total_credits_cents"]),
        "total_debits": rdb.from_cents(st["total_debits_cents"]),
        "purchases": rdb.from_cents(st["purchases_cents"]),
        "refunds": rdb.from_cents(st["refunds_cents"]),
        "adjustment_credits": rdb.from_cents(st["adjustment_credit_cents"]),
        "adjustment_debits": rdb.from_cents(st["adjustment_debit_cents"]),
        "topups": rdb.from_cents(st["topup_cents"]),
        "orders_count": st["orders_count"],
        "transactions_count": st["transactions_count"],
        "invoice_ids": json.loads(st["invoice_ids"] or "[]"),
        "generated_at": st["created"],
        "finalized_at": st["finalized_at"],
    }


def _serialize_line(line: dict) -> dict:
    return {
        "date": line["created"],
        "type": line["tx_type"],
        "label": _TYPE_LABELS.get(line["tx_type"], line["tx_type"]),
        "direction": line["direction"],
        "amount": rdb.from_cents(line["amount_cents"]),
        "balance_after": rdb.from_cents(line["balance_after_cents"]),
        "reference": line["reference"],
        "order_id": line["order_public_id"],
        "description": line["description"],
        "currency": "USDT",
    }


@require_operation(ORDERS_READ)
def list_statements():
    context = get_authenticated_context()
    try:
        limit = int((request.args.get("limit") or "20").strip())
    except ValueError:
        raise ApiError("invalid_pagination", "limit must be a positive integer.", 400)
    if limit < 1 or limit > 100:
        raise ApiError("invalid_pagination", "limit must not exceed 100.", 400)
    after_id = None
    raw_after = (request.args.get("after_id") or "").strip()
    if raw_after:
        row = sdb.get_statement_for_reseller(context.reseller_id, raw_after)
        if not row:
            raise ApiError("invalid_pagination",
                           "after_id must be a valid statement ID.", 400)
        after_id = int(row["id"])
    status = (request.args.get("status") or "").strip()
    if status and status not in sdb.STATEMENT_STATUSES:
        raise ApiError("invalid_parameter", "Unsupported statement status.", 400)
    period_type = (request.args.get("period_type") or "").strip()
    if period_type and period_type not in sdb.PERIOD_TYPES:
        raise ApiError("invalid_parameter", "Unsupported period type.", 400)

    # Phase 15 P7: date filters + currency
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

    rows = sdb.list_statements(reseller_id=context.reseller_id,
                               status=status or None,
                               period_type=period_type or None,
                               limit=limit + 1, after_id=after_id,
                               date_from=date_from, date_to=date_to,
                               currency=currency or None)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return success_response(
        {"statements": [_serialize(r) for r in rows]},
        meta={"pagination": {
            "has_more": has_more,
            "next_after_id": rows[-1]["public_id"] if has_more and rows else None,
        }})


@require_operation(ORDERS_READ)
def statement_detail(statement_id: str):
    context = get_authenticated_context()
    st = _own(context, statement_id)
    out = _serialize(st)
    out["reconciliation"] = _reconciliation_block(st)
    return success_response({"statement": out})


@require_operation(ORDERS_READ)
def statement_transactions(statement_id: str):
    context = get_authenticated_context()
    st = _own(context, statement_id)
    try:
        limit = int((request.args.get("limit") or "100").strip())
    except ValueError:
        raise ApiError("invalid_pagination", "limit must be a positive integer.", 400)
    if limit < 1 or limit > 500:
        raise ApiError("invalid_pagination", "limit must not exceed 500.", 400)
    after_id = None
    raw_after = (request.args.get("after_id") or "").strip()
    if raw_after:
        if not raw_after.isdigit():
            raise ApiError("invalid_pagination", "invalid cursor.", 400)
        after_id = int(raw_after)
    lines = sdb.get_statement_lines(int(st["id"]), limit=limit + 1,
                                    after_id=after_id)
    has_more = len(lines) > limit
    lines = lines[:limit]
    return success_response(
        {"statement_id": st["public_id"],
         "statement_number": st["statement_number"],
         "currency": "USDT",
         "transactions": [_serialize_line(l) for l in lines]},
        meta={"pagination": {
            "has_more": has_more,
            "next_after_id": str(lines[-1]["id"]) if has_more and lines else None,
        }})


@require_operation(ORDERS_READ)
def statement_invoices(statement_id: str):
    context = get_authenticated_context()
    st = _own(context, statement_id)
    ids = json.loads(st["invoice_ids"] or "[]")
    import database.invoice_db as idb
    out = []
    for pid in ids:
        inv = idb.get_invoice_for_reseller(context.reseller_id, pid)
        if inv:
            out.append({
                "invoice_id": inv["public_id"],
                "invoice_number": inv["invoice_number"],
                "status": inv["status"],
                "total": rdb.from_cents(inv["total_cents"]),
                "billing_period": {"start": inv["period_start"],
                                   "end": inv["period_end"]},
            })
    return success_response({"statement_id": st["public_id"],
                             "invoices": out})


def _reconciliation_block(st: dict) -> dict:
    """Compare the statement window with each overlapping invoice (P15 §18).

    Statements summarize ALL ledger movement (including topups); invoices
    document billed activity only — so they legitimately differ whenever a
    topup occurred in the window.  We compare billable components:
    invoice subtotal vs statement purchases, invoice refunds vs statement
    refunds over the invoice's own days.
    """
    import database.invoice_db as idb
    rid = int(st["reseller_id"])
    blocks = []
    for pid in json.loads(st["invoice_ids"] or "[]"):
        inv = idb.get_invoice_for_reseller(rid, pid)
        if not inv:
            continue
        # statement slice over the invoice's own period (ledger-authoritative)
        cur = get_cursor()
        lo = f"{inv['period_start']} 00:00:00"
        hi = (datetime.strptime(inv["period_end"], "%Y-%m-%d")
              + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN tx_type='purchase_debit' THEN -amount_cents END),0),"
            " COALESCE(SUM(CASE WHEN tx_type='refund_credit' THEN amount_cents END),0)"
            " FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND created >= ? AND created < ?",
            (rid, lo, hi),
        )
        st_purch, st_ref = cur.fetchone()
        match = (int(st_purch) == int(inv["subtotal_cents"])
                 and int(st_ref) == int(inv["refund_cents"]))
        blocks.append({
            "invoice_id": inv["public_id"],
            "invoice_number": inv["invoice_number"],
            "invoice_status": inv["status"],
            "invoice_subtotal": rdb.from_cents(inv["subtotal_cents"]),
            "invoice_refunds": rdb.from_cents(inv["refund_cents"]),
            "statement_purchases_in_period": rdb.from_cents(int(st_purch or 0)),
            "statement_refunds_in_period": rdb.from_cents(int(st_ref or 0)),
            "billable_activity_matches": bool(match),
            "note": ("Statements include wallet funding and balance movement; "
                     "invoices document billed activity only — totals differ "
                     "by design when topups or adjustments occur." if not match
                     else "Billable activity matches."),
        })
    return {"invoices": blocks}


def _document_html(st: dict, lines: list) -> str:
    try:
        from config import STORE_NAME
        store = str(STORE_NAME or "OGGY Store")
    except Exception:
        store = "OGGY Store"
    money = rdb.from_cents
    s = _serialize(st)
    rows = []
    for l in lines:
        sign = "+" if l["direction"] == "credit" else "−"
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td>"
            "<td class='r'>{}{}</td><td class='r'>{}</td></tr>".format(
                _esc(str(l["created"])),
                _esc(_TYPE_LABELS.get(l["tx_type"], l["tx_type"])),
                _esc(str(l["description"] or "")),
                sign, money(l["amount_cents"]),
                money(l["balance_after_cents"]),
            ))
    if not rows:
        rows.append("<tr><td colspan='5' class='c dim'>No activity in this period.</td></tr>")
    badge = {"open": "#888", "finalized": "#1a7f37"}[st["status"]]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Statement {_esc(s['statement_number'])}</title>
<style>
 body{{font-family:Arial,sans-serif;margin:32px;color:#222;max-width:860px}}
 h1{{font-size:22px;margin:0}} .dim{{color:#777}} .r{{text-align:right}}
 .c{{text-align:center}} table{{width:100%;border-collapse:collapse;margin-top:18px}}
 th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;font-size:12px}}
 th{{background:#f6f6f6}} .totals td{{border:none;padding:4px 8px}}
 .badge{{color:#fff;background:{badge};padding:3px 10px;border-radius:10px;font-size:12px}}
 @media print{{.noprint{{display:none}}}}
</style></head><body>
<div class="noprint" style="margin-bottom:12px"><button onclick="window.print()">Print / Save as PDF</button></div>
<h1>{_esc(store)}</h1>
<div class="dim">Account statement</div>
<table style="border:none;margin-top:10px"><tr>
<td style="border:none"><b>Statement</b><br>{_esc(s['statement_number'])}<br>
<span class="dim">{_esc(s['statement_id'])}</span></td>
<td style="border:none"><b>Reseller</b><br>{_esc(str(st['reseller_name'] or ''))}<br>
<span class="dim">{_esc(str(st['reseller_public_id'] or ''))}</span></td>
<td style="border:none" class="r"><b>Status</b><br>
<span class="badge">{_esc(s['status'].upper())}</span></td></tr>
<tr><td style="border:none"><b>Period</b><br>{_esc(s['period']['start'][:10])} → {_esc(s['period']['end'][:10])}
<span class="dim">(half-open, {_esc(s['period']['timezone'] or 'shop time')})</span></td>
<td style="border:none"><b>Generated</b><br>{_esc(str(s['generated_at'] or ''))}</td>
<td style="border:none" class="r"><b>Finalized</b><br>{_esc(str(s['finalized_at'] or '—'))}</td></tr></table>
<table class="totals" style="margin-left:auto;width:340px">
<tr><td>Opening balance</td><td class="r">{s['opening_balance']} USDT</td></tr>
<tr><td>Credits (topups {s['topups']} · refunds {s['refunds']} · adjustments {s['adjustment_credits']})</td><td class="r">+{s['total_credits']} USDT</td></tr>
<tr><td>Debits (purchases {s['purchases']} · adjustments {s['adjustment_debits']})</td><td class="r">−{s['total_debits']} USDT</td></tr>
<tr><td><b>Closing balance</b></td><td class="r"><b>{s['closing_balance']} USDT</b></td></tr>
</table>
<table><thead><tr><th>Date</th><th>Type</th><th>Description</th>
<th class="r">Amount</th><th class="r">Balance</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="dim" style="margin-top:18px">Generated from the authoritative wallet
ledger. {'This statement is finalized and frozen.' if s['status']=='finalized' else 'This open snapshot refreshes on regeneration.'}</p>
</body></html>"""


@require_operation(ORDERS_READ)
def statement_document(statement_id: str):
    context = get_authenticated_context()
    st = _own(context, statement_id)
    # documents cap at 2000 lines — huge histories export via CSV endpoint
    lines = sdb.get_statement_lines(int(st["id"]), limit=2000)
    return Response(_document_html(st, lines), mimetype="text/html")


__all__ = ["list_statements", "statement_detail", "statement_transactions",
           "statement_invoices", "statement_document"]
