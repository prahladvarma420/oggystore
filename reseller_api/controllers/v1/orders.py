"""POST /api/v1/orders controller — Phase 7 Prompt 6.

Exposes the full atomic order flow through a clean external API surface.
All trusted values come from server-side services; the client supplies only:
    product_id, quantity, client_order_id (optional)

Public order ID format: ORD-{id:08d}  (safe, non-guessable ordinal)
Order status: sourced from the master order system (get_order_status_label),
              mapped to clean machine-readable strings.
"""

from flask import current_app, request

from database.db import get_cursor, get_order_status_label
from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
from reseller_api.services.authorization import ORDERS_CREATE
from reseller_api.services.order_validation import (
    OrderValidationError,
    validate_order_request,
)
from reseller_api.services.order_pricing import (
    OrderPricingError,
    calculate_order_price,
)
from reseller_api.services.order_stock import (
    StockReservationError,
    reserve_and_validate_stock,
)
from reseller_api.services.order_service import (
    OrderServiceError,
    create_reseller_order,
)


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

def _api_status(order_id: int) -> str:
    """Map the master order status label to a clean API string.

    The master system returns emoji-prefixed strings like '\u2705 Completed'.
    We strip those for a stable, machine-readable API surface.
    """
    label = get_order_status_label(order_id)
    if "Replaced" in label:
        return "replaced"
    if "Warranty" in label or "warranty" in label:
        return "warranty_review"
    return "completed"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@require_operation(ORDERS_CREATE)
def create_order():
    """POST /api/v1/orders — atomic reseller order creation.

    Accepted client fields
    ----------------------
    product_id      (string, required)  — public product ID
    quantity        (int, required)     — number of units
    client_order_id (string, optional)  — client idempotency key

    NOT accepted / NOT trusted from the client
    ------------------------------------------
    reseller_id, price, total, wallet_balance, stock_status

    Flow
    ----
    1. Parse JSON body.
    2. validate_order_request() — product existence, access, quantity rules.
    3. calculate_order_price()  — server-side pricing, never client total.
    4. reserve_and_validate_stock() — lock stock units.
    5. create_reseller_order()  — atomic wallet debit + order INSERT.
    6. Fetch master order status and return clean public response.
    """
    context = get_authenticated_context()

    body = request.get_json(silent=True, force=False)
    if body is None:
        raise ApiError(
            "invalid_request",
            "A valid JSON request body with Content-Type: application/json is required.",
            400,
        )

    # --- Step 1: Validate request (product, access, quantity) ---
    try:
        validated = validate_order_request(
            context,
            body,
            current_app.config["API_ENVIRONMENT"],
        )
    except OrderValidationError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 2: Server-side pricing ---
    try:
        pricing = calculate_order_price(validated)
    except OrderPricingError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 3: Stock reservation ---
    try:
        stock = reserve_and_validate_stock(validated, pricing)
    except StockReservationError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 4: Atomic wallet debit + order INSERT ---
    # create_reseller_order() owns `stock` and calls release_stock() on failure.
    try:
        result = create_reseller_order(validated, pricing, stock, context)
    except OrderServiceError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 5: Fetch master status (post-commit, non-blocking) ---
    try:
        order_status = _api_status(result.order_id)
    except Exception:
        # Status fetch is best-effort; the order is already committed.
        order_status = "completed"

    # --- Step 6: Return clean public response ---
    # Only fields appropriate for external API consumers are included.
    # Raw DB IDs, wallet tx IDs, and internal fields are excluded.
    http_status = 200 if result.is_duplicate else 201
    payload = {
        "order_id":       result.public_order_id,          # e.g. "ORD-00000012"
        "product_id":     validated.public_product_id,      # e.g. "PRD-ABCDE12345"
        "quantity":       pricing.quantity,
        "unit_price":     pricing.unit_price_display,       # e.g. "20.00"
        "total":          pricing.total_display,            # e.g. "40.00"
        "currency":       pricing.currency,                 # "USDT"
        "status":         order_status,                     # "completed" | "replaced" | ...
        "created_at":     result.created_at,                # ISO-like timestamp from DB
    }
    # Include client_order_id only when the client provided one.
    if validated.client_order_id:
        payload["client_order_id"] = validated.client_order_id

    # Surface idempotency hint when returning an existing order.
    if result.is_duplicate:
        payload["idempotent"] = True

    return success_response(payload, status=http_status)



# ---------------------------------------------------------------------------
# Order retrieval endpoints — Phase 7 Prompt 7
# ---------------------------------------------------------------------------

from reseller_api.services.authorization import ORDERS_READ
from reseller_api.services.order_query import (
    OrderQueryError,
    parse_order_list_params,
    get_order_list,
    get_order_detail,
)


@require_operation(ORDERS_READ)
def list_orders():
    """GET /api/v1/orders — paginated order list for the authenticated reseller.

    Supported query parameters
    --------------------------
    limit          int  1..100, default 20
    after_id       str  cursor (ORD-XXXXXXXX) for next-page pagination
    product_id     str  public product ID filter (PRD-...)
    client_order_id str exact client_order_id filter
    date_from      str  YYYY-MM-DD inclusive lower bound on created
    date_to        str  YYYY-MM-DD inclusive upper bound on created

    NOT accepted
    ------------
    reseller_id, reseller, user_id, source  (rejected with 400)
    status  (not index-backed; not implemented to prevent full-table scans)
    """
    context = get_authenticated_context()
    try:
        params = parse_order_list_params(request.args)
    except OrderQueryError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    orders, pagination = get_order_list(context, params)
    return success_response({"orders": orders}, meta={"pagination": pagination})


@require_operation(ORDERS_READ)
def order_detail(order_id: str):
    """GET /api/v1/orders/<order_id> — single order detail.

    IDOR prevention: the query always includes AND reseller_id=<auth_context>
    so that a valid ORD-... ID belonging to another reseller returns 404,
    indistinguishable from an invalid ID.

    The raw URL segment is treated as untrusted user input and validated
    against the ORD-XXXXXXXX regex before any DB query.
    """
    context = get_authenticated_context()
    try:
        order = get_order_detail(context, order_id)
    except OrderQueryError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None
    return success_response({"order": order})




# ---------------------------------------------------------------------------
# Delivery endpoint — Phase 8
# ---------------------------------------------------------------------------

from reseller_api.services.authorization import DELIVERY_READ
from reseller_api.services.order_delivery import get_order_delivery


@require_operation(DELIVERY_READ)
def order_delivery(order_id: str):
    """GET /api/v1/orders/<order_id>/delivery — return decrypted delivery items.

    IDOR prevention: query always scopes to authenticated reseller_id.
    Returns decrypted delivery items only for completed orders owned by
    the authenticated reseller. Pending/failed/cancelled orders return
    an appropriate status message with an empty items list.
    """
    context = get_authenticated_context()
    try:
        result = get_order_delivery(context, order_id)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError("delivery_error", "Delivery could not be retrieved.", 500) from exc
    return success_response({"delivery": result})

__all__ = ["create_order", "list_orders", "order_detail", "order_delivery"]
