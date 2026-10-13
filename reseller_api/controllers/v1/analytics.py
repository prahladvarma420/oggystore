"""GET /api/v1/analytics/sales — reseller-safe sales analytics (Phase 14 P3).

Computed from the authoritative order + ledger records by
rdb.get_reseller_sales_analytics(), scoped hard to the authenticated
reseller.  Optional date filters: ?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
(inclusive, shop timezone).  Money is decimal strings in USDT, same as the
rest of the API.
"""

from flask import request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.services.authorization import ORDERS_READ
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
import database.reseller_db as rdb

_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


_PRODUCT_RE = __import__("re").compile(r"^PRD-[A-HJ-NP-Z2-9]{12}$")


@require_operation(ORDERS_READ)
def sales_analytics():
    context = get_authenticated_context()
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None and not _DATE_RE.fullmatch(value):
            raise ApiError(
                "invalid_parameter",
                f"{name} must be YYYY-MM-DD.",
                400,
            )
    # Optional per-product drilldown (Phase 14 P5): the reseller only ever
    # sees their OWN numbers — a product they cannot access simply yields
    # zeros (no existence oracle).
    product_id = None
    raw_pid = (request.args.get("product_id") or "").strip()
    if raw_pid:
        if not _PRODUCT_RE.fullmatch(raw_pid):
            raise ApiError("invalid_parameter",
                           "product_id must be a valid PRD-* identifier.", 400)
        row = rdb.get_reseller_api_product(context.reseller_id, raw_pid)
        if row:
            product_id = int(row["product_id"])
        else:
            product_id = -1   # not accessible: force an empty scope
    data = rdb.get_reseller_sales_analytics(
        context.reseller_id, date_from=date_from, date_to=date_to,
        product_id=product_id,
    )
    return success_response({"sales": data})


__all__ = ["sales_analytics"]
