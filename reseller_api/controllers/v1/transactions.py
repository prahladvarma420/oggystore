"""GET /api/v1/transactions — reseller's own ledger history (Phase 15 P4).

Reads the authoritative immutable ledger, scoped hard to the authenticated
reseller.  Cursor pagination (id DESC, opaque `after` cursor); filters:
type, direction, date_from/date_to (inclusive days, shop clock), order_id
(ORD-*; matches the order's debit and all its refund rows).

Response fields are reseller-safe: reference (the ledger's idempotency
identity), type, direction, amounts as decimal strings, balance_after, and
balance_before DERIVED as (balance_after − amount) — the ledger stores
balance_after authoritatively; balance_before is exact chain arithmetic,
never client-supplied.  Internal row IDs appear only as the opaque
pagination cursor in meta, never as a data field.
"""

import re

from flask import request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.services.authorization import BALANCE_READ
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
import database.reseller_db as rdb
from database.db import get_cursor

_TX_DIRECTION = {
    "topup_credit": "credit",
    "refund_credit": "credit",
    "adjustment_credit": "credit",
    "purchase_debit": "debit",
    "adjustment_debit": "debit",
}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ORD_RE = re.compile(r"^ORD-(\d{8})$")


def _serialize(row) -> dict:
    amount = int(row["amount_cents"])
    bal_after = int(row["balance_after_cents"])
    direction = _TX_DIRECTION[row["tx_type"]]
    return {
        "reference": row["reference"],
        "type": row["tx_type"],
        "direction": direction,
        "amount": rdb.from_cents(abs(amount)),
        "currency": row["currency"],
        # derived chain value — exact, never client-supplied
        "balance_before": rdb.from_cents(bal_after - amount),
        "balance_after": rdb.from_cents(bal_after),
        "order_id": row.get("order_public_id"),
        "description": row["description"] or "",
        "created_at": row["created"],
    }


@require_operation(BALANCE_READ)
def list_transactions():
    context = get_authenticated_context()
    rid = int(context.reseller_id)

    try:
        limit = int((request.args.get("limit") or "50").strip())
    except ValueError:
        raise ApiError("invalid_pagination", "limit must be a positive integer.", 400)
    if limit < 1 or limit > 100:
        raise ApiError("invalid_pagination", "limit must not exceed 100.", 400)

    where, params = "reseller_id=?", [rid]

    tx_type = (request.args.get("type") or "").strip()
    if tx_type:
        if tx_type not in rdb.TX_TYPES:
            raise ApiError("invalid_parameter", "Unsupported transaction type.", 400)
        where += " AND tx_type=?"
        params.append(tx_type)

    direction = (request.args.get("direction") or "").strip()
    if direction:
        if direction not in ("debit", "credit"):
            raise ApiError("invalid_parameter", "direction must be debit or credit.", 400)
        types = [t for t, d in _TX_DIRECTION.items() if d == direction]
        where += f" AND tx_type IN ({','.join('?' for _ in types)})"
        params.extend(types)

    for name, bound, op in (("date_from", " 00:00:00", ">="),
                            ("date_to", " 23:59:59", "<=")):
        raw = (request.args.get(name) or "").strip()
        if raw:
            if not _DAY.fullmatch(raw):
                raise ApiError("invalid_parameter", f"{name} must be YYYY-MM-DD.", 400)
            where += f" AND created {op} ?"
            params.append(raw + bound)

    order_id = (request.args.get("order_id") or "").strip()
    if order_id:
        m = _ORD_RE.fullmatch(order_id)
        if not m:
            raise ApiError("invalid_parameter",
                           "order_id must be a valid ORD-* identifier.", 400)
        oid = int(m.group(1))
        cur = get_cursor()
        cur.execute(
            "SELECT request_id FROM orders WHERE id=? AND reseller_id=?"
            " AND source='reseller_api'",
            (oid, rid))
        row = cur.fetchone()
        if not row:
            # foreign/unknown order -> empty page (no oracle)
            return success_response(
                {"transactions": []},
                meta={"pagination": {"has_more": False, "next_after": None}})
        refs = [f"rapi.{rid}.{row[0]}",
                f"rapi.refund.order.{oid}",
                f"rapi.cancel_refund.order.{oid}",
                f"rapi.partial_refund.order.{oid}"]
        where += f" AND reference IN ({','.join('?' for _ in refs)})"
        params.extend(refs)

    raw_after = (request.args.get("after") or "").strip()
    if raw_after:
        if not raw_after.isdigit():
            raise ApiError("invalid_pagination", "invalid cursor.", 400)
        where += " AND id < ?"
        params.append(int(raw_after))

    params.append(limit + 1)
    cur = get_cursor()
    cur.execute(
        f"SELECT * FROM reseller_wallet_transactions WHERE {where}"
        " ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    rows = [dict(zip(rdb._LEDGER_COLS, r)) for r in cur.fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]

    # attach public order ids (own orders only)
    order_refs = {}
    refs = [r["reference"] for r in rows] + [r["related_order_reference"] or ""
                                             for r in rows]
    if refs:
        cur.execute(
            "SELECT id, request_id FROM orders WHERE reseller_id=? AND source='reseller_api'",
            (rid,))
        by_req = {f"rapi.{rid}.{r[1]}": int(r[0]) for r in cur.fetchall() if r[1]}
        for row in rows:
            pub = None
            for cand in (row["related_order_reference"], row["reference"]):
                if not cand:
                    continue
                if cand in by_req:
                    pub = f"ORD-{by_req[cand]:08d}"
                    break
                m = re.search(r"\.order\.(\d+)$", str(cand))
                if m:
                    pub = f"ORD-{int(m.group(1)):08d}"
                    break
            order_refs[row["id"]] = pub
    for row in rows:
        row["order_public_id"] = order_refs.get(row["id"])

    return success_response(
        {"transactions": [_serialize(r) for r in rows]},
        meta={"pagination": {
            "has_more": has_more,
            "next_after": str(rows[-1]["id"]) if has_more and rows else None,
        }})


__all__ = ["list_transactions"]
