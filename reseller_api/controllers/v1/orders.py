"""POST /api/v1/orders controller — Phase 7 Prompt 6.

Exposes the full atomic order flow through a clean external API surface.
All trusted values come from server-side services; the client supplies only:
    product_id, quantity, client_order_id (optional)

Public order ID format: ORD-{id:08d}  (safe, non-guessable ordinal)
Order status: shared api_order_status resolver; pending delivery is processing.
"""

from flask import current_app, request

from database.db import get_cursor
from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from database.reseller_db import (
    ensure_product_public_id,
    from_cents,
    get_wallet_balance_cents,
)
from utils.timezone import now_text
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
from reseller_api.services.authorization import ORDERS_CREATE
from reseller_api.services.financial_abuse_guard import (
    FinancialAbuseError,
    assess_order_financial_risk,
    log_financial_event,
)
from reseller_api.services.quota_service import (
    QuotaExceededError,
    check_order_quota,
    record_order_usage,
)
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
    find_client_order,
    find_client_order_after_contention,
)
from reseller_api.services.order_status import api_order_status, derive_order_status, OrderStatusUnavailable, load_order_status_row


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

def _idempotent_replay_response(context, *, client_order_id,
                                after_contention=False):
    """Return a 200 response for an already-committed client_order_id, or None.

    Read-only short-circuit (Phase 12 P7): no stock, wallet, or quota side
    effects.  The service-layer pre-check and the partial unique index stay
    the authoritative write-path guards; this only resolves retries BEFORE
    stock is touched, so a concurrent or late retry resolves the existing
    order instead of failing on transient stock scarcity.

    The lookup is scoped to the authenticated reseller and client_order_id
    is used only as the idempotency key.  Every response field — product,
    quantity, prices, timestamp — comes from the STORED order row (Phase 12
    P9): a replay faithfully returns the original result, and a key reused
    with different parameters can never relabel an existing order.

    after_contention=True serializes the lookup with in-flight writers
    (used after a stock-validation/reservation failure, where the twin
    request may be committing right now).
    """
    if not client_order_id:
        return None
    if after_contention:
        existing = find_client_order_after_contention(
            context.reseller_id, client_order_id)
    else:
        existing = find_client_order(context.reseller_id, client_order_id)
    if existing is None:
        return None
    status_row = load_order_status_row(existing["order_id"])
    order_state = derive_order_status(status_row)
    order_status = api_order_status(status_row)
    unit = existing["unit_price_cents"]
    qty = existing["quantity"]
    product_id = existing.get("product_id")
    payload = {
        "order_id":   f"ORD-{existing['order_id']:08d}",
        "product_id": ensure_product_public_id(int(product_id)) if product_id else None,
        "quantity":   qty,
        "unit_price": from_cents(unit),
        "total":      from_cents(unit * qty),
        "currency":   "USDT",
        "status":     order_status,
        "delivery_status": order_state.delivery_status,
        "created_at": existing["created"],
        "client_order_id": client_order_id,
        "idempotent": True,
    }
    return success_response(payload, status=200)


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
    5b. record_order_usage()    — quota/burst accounting for new orders only.
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
        # Phase 12 P7: the validation layer soft-checks stock and raises
        # out_of_stock / insufficient_stock — which also fires for a
        # legitimate RETRY whose twin already consumed the last unit.
        # Resolve the existing order instead of failing the retry.
        if exc.code in ("out_of_stock", "insufficient_stock"):
            replay = _idempotent_replay_response(
                context,
                client_order_id=str(body.get("client_order_id") or ""),
                after_contention=True,
            )
            if replay is not None:
                return replay
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 1b: Quota check (business-level, admin-controlled) ---
    try:
        check_order_quota(context)
    except QuotaExceededError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 1c: Financial abuse guard (burst / quantity ceiling) ---
    try:
        assess_order_financial_risk(context, validated)
    except FinancialAbuseError as exc:
        log_financial_event(
            context.reseller_id, "order_rejected_abuse",
            detail=exc.code,
            product_id=getattr(validated, 'product_id', None),
            quantity=getattr(validated, 'quantity', None),
        )
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 2: Server-side pricing ---
    try:
        pricing = calculate_order_price(validated)
    except OrderPricingError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 2c: Idempotent-replay resolution (Phase 12 P7) ---
    # A retry bearing an already-committed client_order_id resolves the
    # EXISTING order before stock is touched.  Quota/abuse checks above
    # still apply to every call (unchanged semantics); a replay is simply
    # never allowed to fail on transient stock scarcity.
    replay = _idempotent_replay_response(
        context,
        client_order_id=validated.client_order_id,
    )
    if replay is not None:
        return replay

    # --- Step 3: Stock reservation ---
    try:
        stock = reserve_and_validate_stock(validated, pricing)
    except StockReservationError as exc:
        # A concurrent in-flight duplicate may have committed the order and
        # drained stock since the check above.  Re-check serialized with
        # in-flight writers: a legitimate retry resolves the existing order
        # instead of dying on a spurious 422.
        replay = _idempotent_replay_response(
            context,
            client_order_id=validated.client_order_id,
            after_contention=True,
        )
        if replay is not None:
            return replay
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 4: Atomic wallet debit + order INSERT ---
    # create_reseller_order() owns `stock` and calls release_stock() on failure.
    try:
        result = create_reseller_order(validated, pricing, stock, context)
    except OrderServiceError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # --- Step 4b: Record quota/burst usage (Phase 12 P2) ---
    # The order_placed financial event is what check_order_quota() and the
    # product-burst guard count. Record only genuinely new orders — an
    # idempotent replay of a client_order_id must not consume quota twice.
    # record_order_usage never raises; a logging failure must not fail the
    # already-committed order.
    if not result.is_duplicate:
        record_order_usage(
            context,
            product_id=validated.product_id,
            quantity=validated.quantity,
        )

    # Status verification is not best-effort: never misreport committed orders.
    status_row = load_order_status_row(result.order_id)
    order_state = derive_order_status(status_row)
    order_status = api_order_status(status_row)

    # --- Step 5b: Duplicates answer with the STORED order (Phase 12 P9) ---
    # The service detected the replay after stock was reserved and released;
    # rebuild the response from the original row so a key reused with
    # different parameters still returns the original result verbatim.
    if result.is_duplicate:
        replay = _idempotent_replay_response(
            context, client_order_id=validated.client_order_id,
        )
        if replay is not None:
            return replay

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
        "delivery_status": order_state.delivery_status,
        "status":         order_status,                     # "completed" | "replaced" | ...
        "created_at":     result.created_at,                # ISO-like timestamp from DB
    }
    if http_status == 201:
        payload['fulfillment'] = result.fulfillment

    # Include client_order_id only when the client provided one.
    if validated.client_order_id:
        payload["client_order_id"] = validated.client_order_id

    # Surface idempotency hint when returning an existing order.
    if result.is_duplicate:
        payload["idempotent"] = True

    return success_response(payload, status=http_status)



# ---------------------------------------------------------------------------
# Quote endpoint — Phase 12 Prompt 3
# ---------------------------------------------------------------------------

@require_operation(ORDERS_CREATE)
def quote_order():
    """POST /api/v1/orders/quote — authoritative, NON-BINDING price quote.

    Runs the exact same validation and pricing pipeline as order creation
    (including the Phase 12 P2 mapping invariant), so a quote always reflects
    what the order endpoint would charge at this moment.

    A quote touches nothing:
      - no stock reservation (reports the soft availability read only)
      - no wallet movement   (balance is read, never debited)
      - no quota consumption (record_order_usage is order-only)
      - no persistence       (no quote rows, no quote IDs, nothing to replay)

    Price consistency: order creation ALWAYS recalculates authoritatively via
    calculate_order_price(). There is deliberately no way to reference a quote
    from an order request — a stale or manipulated quote cannot influence the
    charged amount by construction. Price changes or stock-outs between quote
    and order surface as the new price / a 422 at order time.
    """
    context = get_authenticated_context()

    body = request.get_json(silent=True, force=False)
    if body is None:
        raise ApiError(
            "invalid_request",
            "A valid JSON request body with Content-Type: application/json is required.",
            400,
        )

    # Same validation pipeline as order creation (mapping, access, quantity,
    # availability soft-check). Client-supplied price/cost/markup/discount
    # fields are rejected inside the validator.
    try:
        validated = validate_order_request(
            context,
            body,
            current_app.config["API_ENVIRONMENT"],
        )
    except OrderValidationError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # Same single pricing source as order creation — integer cents only.
    try:
        pricing = calculate_order_price(validated)
    except OrderPricingError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

    # Read-only wallet position so the reseller sees affordability BEFORE
    # ordering. The wallet is never touched by a quote.
    balance_cents = get_wallet_balance_cents(context.reseller_id)
    available = int(validated.available_quantity or 0)

    payload = {
        "product_id":         validated.public_product_id,
        "quantity":           pricing.quantity,
        "unit_price":         pricing.unit_price_display,
        "total":              pricing.total_display,
        "currency":           pricing.currency,
        "price_source":       pricing.price_source,   # master | master_tier | rule:*
        "max_per_order":      validated.max_per_order,
        "available_quantity": available,
        "in_stock":           available >= pricing.quantity,
        "wallet_balance":     from_cents(balance_cents),
        "sufficient_funds":   balance_cents >= pricing.total_cents,
        "quoted_at":          now_text(),
    }
    if validated.email_product:
        payload['requires_buyer_email'] = True
    return success_response(payload)


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
    status         str  processing | completed | failed | cancelled  (Phase 12 P6: persisted
                          lifecycle states only; replaced / warranty_review
                          remain display sub-states and are not filterable)

    NOT accepted
    ------------
    reseller_id, reseller, user_id, source  (rejected with 400)
    """
    context = get_authenticated_context()
    try:
        params = parse_order_list_params(request.args)
        orders, pagination = get_order_list(context, params)
    except OrderQueryError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None

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
    except (ApiError, OrderStatusUnavailable):
        raise
    except Exception as exc:
        raise ApiError("delivery_error", "Delivery could not be retrieved.", 500) from exc
    return success_response({"delivery": result})

__all__ = ["create_order", "quote_order", "list_orders", "order_detail", "order_delivery"]
