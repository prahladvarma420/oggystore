"""Reseller refund read endpoints — Phase 15 P5.

  GET /api/v1/refunds                 own refund records (cursor pagination)
  GET /api/v1/refunds/<REF-…>         one refund record

Read-only: resellers NEVER create credits or refunds through the public API
(administrative financial actions are owner-gated in WebAdmin).  Fields are
reseller-safe; ledger references are included for traceability.
"""

import re

from flask import request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.services.authorization import ORDERS_READ
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
import database.reseller_db as rdb
import database.adjustment_db as adb

_PUBLIC_RE = re.compile(r"^REF-[A-HJ-NP-Z2-9]{10}$")


def _serialize(row) -> dict:
    return {
        "refund_id": row["public_id"],
        "order_id": row["order_public_id"],
        "kind": row["kind"],
        "amount": rdb.from_cents(row["amount_cents"]),
        "currency": row["currency"],
        "reason": row["reason"] or "",
        "source": row["source"],
        "status": row["status"],
        "reference": row["ledger_reference"],
        "created_at": row["created"],
    }


@require_operation(ORDERS_READ)
def list_refunds():
    context = get_authenticated_context()
    try:
        limit = int((request.args.get("limit") or "20").strip())
    except ValueError:
        raise ApiError("invalid_pagination", "limit must be a positive integer.", 400)
    if limit < 1 or limit > 100:
        raise ApiError("invalid_pagination", "limit must not exceed 100.", 400)
    kind = (request.args.get("kind") or "").strip() or None
    if kind and kind not in adb.REFUND_KINDS:
        raise ApiError("invalid_parameter", "Unsupported refund kind.", 400)
    after_id = None
    raw_after = (request.args.get("after_id") or "").strip()
    if raw_after:
        if not _PUBLIC_RE.fullmatch(raw_after):
            raise ApiError("invalid_pagination",
                           "after_id must be a valid refund ID.", 400)
        row = adb.get_refund_by_public_id(raw_after)
        if not row or int(row["reseller_id"]) != int(context.reseller_id):
            raise ApiError("invalid_pagination",
                           "after_id must be a valid refund ID.", 400)
        after_id = int(row["id"])
    rows = adb.list_refunds(reseller_id=context.reseller_id, kind=kind,
                            limit=limit + 1, after_id=after_id)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return success_response(
        {"refunds": [_serialize(r) for r in rows]},
        meta={"pagination": {
            "has_more": has_more,
            "next_after_id": rows[-1]["public_id"] if has_more and rows else None,
        }})


@require_operation(ORDERS_READ)
def refund_detail(refund_id: str):
    context = get_authenticated_context()
    if not _PUBLIC_RE.fullmatch(str(refund_id or "").strip()):
        raise ApiError("refund_not_found", "Refund not found.", 404)
    row = adb.get_refund_by_public_id(str(refund_id).strip())
    if not row or int(row["reseller_id"]) != int(context.reseller_id):
        raise ApiError("refund_not_found", "Refund not found.", 404)
    return success_response({"refund": _serialize(row)})


__all__ = ["list_refunds", "refund_detail"]
