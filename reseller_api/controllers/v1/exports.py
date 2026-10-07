"""GET /api/v1/exports/orders.csv — reseller's own orders as CSV (Phase 14 P8).

Streams from the master orders table scoped to the authenticated reseller.
Contains ONLY reseller-safe fields: no delivery content, no internal ids,
no other reseller's data.  Every export is recorded as a security event and
in the standard API request log.  Optional inclusive date filters:
?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD.  Capped at 100k rows per export.
"""

import csv
import io
import re

from flask import Response, request, stream_with_context

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.services.authorization import ORDERS_READ
from reseller_api.errors import ApiError
from reseller_api.services.order_status import derive_order_status
from database.db import get_cursor
import database.reseller_db as rdb

_MAX_ROWS = 100_000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FIELDS = [
    "order_id", "product_id", "product_name", "quantity",
    "unit_price", "total", "currency", "status", "delivery_status",
    "client_order_id", "created_at", "updated_at",
    "refunded", "refund_total",
]


@require_operation(ORDERS_READ)
def export_orders_csv():
    context = get_authenticated_context()
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    where = "reseller_id=? AND source='reseller_api'"
    params = [context.reseller_id]
    for name, value, bound in (("date_from", date_from, " 00:00:00"),
                               ("date_to", date_to, " 23:59:59")):
        if value is not None:
            if not _DATE_RE.fullmatch(value):
                raise ApiError("invalid_parameter",
                               f"{name} must be YYYY-MM-DD.", 400)
            where += " AND created " + (">=" if name == "date_from" else "<=") + " ?"
            params.append(value + bound)
    params.append(_MAX_ROWS)

    cur = get_cursor()
    cur.execute(
        f"SELECT id, product_id, product_name, quantity, unit_price_cents,"
        f" master_status, delivery, client_order_id, created, updated"
        f" FROM orders WHERE {where} ORDER BY id DESC LIMIT ?",
        tuple(params),
    )
    rows = cur.fetchall()

    # refund totals per order from the immutable ledger (own account only)
    refund_map = {}
    if rows:
        cur.execute(
            "SELECT reference, amount_cents FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'",
            (context.reseller_id,),
        )
        for ref, amount in cur.fetchall():
            m = re.search(r"\.order\.(\d+)$", str(ref or ""))
            if m:
                refund_map[int(m.group(1))] = refund_map.get(int(m.group(1)), 0) + int(amount)

    def _rows():
        for (oid, product_id, pname, qty, unit_cents, mstatus, delivery,
             coid, created, updated) in rows:
            status = derive_order_status(int(oid))
            refund_cents = refund_map.get(int(oid), 0)
            yield [
                f"ORD-{int(oid):08d}",
                rdb.ensure_product_public_id(int(product_id)) if product_id else "",
                pname or "", int(qty or 1),
                rdb.from_cents(int(unit_cents or 0)),
                rdb.from_cents(int(unit_cents or 0) * int(qty or 1)),
                "USDT",
                status.order_status,
                status.delivery_status,
                coid or "", created or "", updated or "",
                "true" if refund_cents else "false",
                rdb.from_cents(refund_cents),
            ]

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_FIELDS)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for row in _rows():
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    try:
        from reseller_api.services import security_logger as sec_log
        sec_log.emit("data_export", reseller_id=context.reseller_id,
                     reseller_public_id=context.reseller_public_id,
                     description=f"orders.csv rows={len(rows)}")
    except Exception:
        pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="oggystore-orders.csv"'},
    )


__all__ = ["export_orders_csv"]
