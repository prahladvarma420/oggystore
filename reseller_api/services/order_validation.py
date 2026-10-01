"""Order request validation for POST /api/v1/orders.

This module validates the inbound order request and resolves all
server-side values. It does NOT create orders, debit wallets, or
reserve stock. That is the job of a later prompt's order service.

Validation order (all structural / syntactic checks run BEFORE any DB call):
    1. Reject forbidden client-supplied fields.
    2. Syntactically validate product_id (PRD-* format).
    3. Syntactically validate quantity (absolute range, type, sign).
    4. Syntactically validate client_order_id (if provided).
    5. Resolve product from master catalog + re-authorise access (DB).
    6. Apply product max_per_order rule against validated quantity (DB value).
    7. Reject API-unsupported product fulfillment types (M365 / Canva).
    8. Soft stock pre-check (non-authoritative early-out).

Security rules enforced here:
 - product_id is resolved from the public PRD-* identifier only.
 - quantity is validated; product max_per_order rule is enforced.
 - reseller_id, price, total, balance, stock_status, and internal
   IDs are never accepted from the client.
 - client_order_id is sanitised and length-limited but not yet
   committed — uniqueness enforcement happens at order creation time.
 - Special product types that cannot be fulfilled via the API are
   rejected with a clear error (M365, Canva — require email from buyer
   through a Telegram flow that does not exist in the REST API yet).
"""

import re

import database.reseller_db as rdb
from database.db import is_m365_product, is_canva_product, get_available_stock_count
from reseller_api.context import ApiRequestContext
from reseller_api.services.authorization import (
    authorize_product_access,
    ORDERS_CREATE,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Absolute ceiling applied before any DB call — prevents absurdly large
# requests from touching the database at all.
_ABS_MAX_QUANTITY = 1000

# client_order_id rules: printable ASCII only (0x21–0x7E), no whitespace,
# no control characters.
_MAX_CLIENT_ORDER_ID_LEN = 128
_CLIENT_ORDER_ID_RE = re.compile(r'^[\x21-\x7E]{1,128}$')

# Forbidden request body keys that clients must never supply.
_FORBIDDEN_CLIENT_FIELDS = frozenset({
    # Identity / routing — server-resolved only
    "reseller_id",
    "reseller",
    "source",
    # Financial — server-calculated only
    "price",
    "unit_price",
    "unit_price_cents",
    "total",
    "total_cents",
    "balance",
    # Stock / delivery — set by fulfilment pipeline
    "stock_status",
    "delivery",
    "stock_restored",
    "partial_stock_restored",
    # Order metadata — set by server
    "order_id",
    "id",
    "status",
    "master_status",
    "failure_reason",
    "payment_method",
    # Reseller account state — read-only admin fields
    "is_active",
    "is_suspended",
    "is_blocked",
})

# Public product ID pattern — must match the PRD-* format produced by
# reseller_db.ensure_product_public_id().
_PUBLIC_PRODUCT_ID_RE = re.compile(r'^PRD-[A-HJ-NP-Z2-9]{12}$')


# ---------------------------------------------------------------------------
# Service error
# ---------------------------------------------------------------------------

class OrderValidationError(ValueError):
    """Raised when inbound order data fails any validation check."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


# ---------------------------------------------------------------------------
# Validated order request (value object — no side effects)
# ---------------------------------------------------------------------------

class ValidatedOrderRequest:
    """Immutable result of a successful validation pass.

    Contains only server-resolved values. No client-supplied price,
    balance, reseller identity, or internal ID is stored here.
    """

    __slots__ = (
        "reseller_id",        # int  — from authenticated context
        "product_id",         # int  — resolved from public PRD-* id
        "public_product_id",  # str  — PRD-* id for logging / response
        "product_name",       # str  — snapshot from master products table
        "max_per_order",      # int  — product rule
        "quantity",           # int  — validated
        "client_order_id",    # str | None — caller-supplied idempotency key
    )

    def __init__(
        self,
        reseller_id: int,
        product_id: int,
        public_product_id: str,
        product_name: str,
        max_per_order: int,
        quantity: int,
        client_order_id,
    ):
        self.reseller_id = reseller_id
        self.product_id = product_id
        self.public_product_id = public_product_id
        self.product_name = product_name
        self.max_per_order = max_per_order
        self.quantity = quantity
        self.client_order_id = client_order_id


# ---------------------------------------------------------------------------
# Step 1 — Reject forbidden fields
# ---------------------------------------------------------------------------

def _reject_forbidden_fields(body: dict):
    """Reject any key whose value the server must calculate itself."""
    found = set(body.keys()) & _FORBIDDEN_CLIENT_FIELDS
    if found:
        raise OrderValidationError(
            "invalid_parameter",
            "One or more request fields are determined by the server and "
            "must not be supplied by the client.",
        )


# ---------------------------------------------------------------------------
# Step 2 — Syntactic product_id check (no DB)
# ---------------------------------------------------------------------------

def _parse_public_product_id(body: dict) -> str:
    """Extract and syntactically validate the public product ID string.

    Returns the cleaned string on success. Does NOT hit the database.
    The same 'product_not_found' message is used for both bad format and
    actual DB misses to prevent existence oracles for unauthorised callers.
    """
    raw = body.get("product_id")
    if raw is None:
        raise OrderValidationError(
            "missing_field", "product_id is required."
        )
    value = str(raw).strip()
    if not _PUBLIC_PRODUCT_ID_RE.fullmatch(value):
        raise OrderValidationError(
            "product_not_found", "The requested product was not found.", 404
        )
    return value


# ---------------------------------------------------------------------------
# Step 3 — Syntactic quantity check (no DB, no max_per_order yet)
# ---------------------------------------------------------------------------

def _parse_quantity_syntax(body: dict) -> int:
    """Parse and range-check quantity using only absolute limits.

    Product-specific max_per_order is applied later (step 6), after the
    product row has been fetched from the database.
    """
    raw = body.get("quantity")
    if raw is None:
        raise OrderValidationError(
            "missing_field", "quantity is required."
        )
    # Booleans are a subtype of int in Python — reject explicitly.
    if isinstance(raw, bool):
        raise OrderValidationError(
            "invalid_quantity", "quantity must be a positive integer."
        )
    # Reject floats with a fractional part; accept whole-number floats.
    if isinstance(raw, float):
        if not raw.is_integer():
            raise OrderValidationError(
                "invalid_quantity",
                "Fractional quantities are not supported.",
            )
        raw = int(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped.lstrip('-').isdigit():
            raise OrderValidationError(
                "invalid_quantity", "quantity must be a positive integer."
            )
        raw = int(stripped)
    elif not isinstance(raw, int):
        raise OrderValidationError(
            "invalid_quantity", "quantity must be a positive integer."
        )
    else:
        raw = int(raw)

    if raw <= 0:
        raise OrderValidationError(
            "invalid_quantity",
            "quantity must be greater than zero.",
        )
    if raw > _ABS_MAX_QUANTITY:
        raise OrderValidationError(
            "invalid_quantity",
            f"quantity must not exceed {_ABS_MAX_QUANTITY}.",
        )
    return raw


# ---------------------------------------------------------------------------
# Step 4 — Syntactic client_order_id check (no DB)
# ---------------------------------------------------------------------------

def _parse_client_order_id(body: dict):
    """Extract and sanitise the optional idempotency key.

    Allowed characters: printable ASCII (0x21–0x7E, i.e. no spaces or
    control characters). Maximum 128 characters.
    Uniqueness enforcement is NOT done here — it happens at write time.
    """
    raw = body.get("client_order_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise OrderValidationError(
            "invalid_client_order_id",
            "client_order_id must be a string.",
        )
    # Do NOT strip — leading/trailing whitespace is illegal and must be rejected.
    if len(raw) > _MAX_CLIENT_ORDER_ID_LEN:
        raise OrderValidationError(
            "invalid_client_order_id",
            f"client_order_id must not exceed {_MAX_CLIENT_ORDER_ID_LEN} characters.",
        )
    if not raw:
        raise OrderValidationError(
            "invalid_client_order_id",
            "client_order_id must not be empty when provided.",
        )
    if not _CLIENT_ORDER_ID_RE.fullmatch(raw):
        raise OrderValidationError(
            "invalid_client_order_id",
            "client_order_id may only contain printable ASCII characters "
            "(no spaces or control characters).",
        )
    return raw


# ---------------------------------------------------------------------------
# Step 5 — Product resolution + authorization (DB)
# ---------------------------------------------------------------------------

def _resolve_product(context: ApiRequestContext, public_product_id: str,
                     expected_environment: str) -> dict:
    """Load and authorise the product from the master catalog.

    Uses the existing reseller_db read path — no duplicate product logic.
    Returns the raw row dict on success; raises OrderValidationError otherwise.
    """
    row = rdb.get_reseller_api_product(context.reseller_id, public_product_id)
    if not row:
        raise OrderValidationError(
            "product_not_found", "The requested product was not found.", 404
        )

    # Re-validate mutable reseller/key/product/access state. A concurrent
    # revocation therefore fails closed before any stock or wallet is touched.
    decision = authorize_product_access(
        context,
        int(row["product_id"]),
        expected_environment,
        ORDERS_CREATE,
    )
    if not decision.allowed:
        raise OrderValidationError(
            "product_not_found", "The requested product was not found.", 404
        )

    # NOTE: product_active is already enforced by get_reseller_api_product
    # (WHERE p.active=1). No redundant check needed here.
    return row


# ---------------------------------------------------------------------------
# Step 6 — Apply product max_per_order rule (uses DB value)
# ---------------------------------------------------------------------------

def _apply_max_per_order(quantity: int, max_per_order: int) -> int:
    """Enforce the product's own per-order quantity limit.

    Called after the product row has been fetched so max_per_order is a
    server value, never a client-supplied one.
    """
    effective_max = max(1, int(max_per_order or 1))
    if quantity > effective_max:
        raise OrderValidationError(
            "invalid_quantity",
            f"This product allows a maximum of {effective_max} per order.",
        )
    return quantity


# ---------------------------------------------------------------------------
# Step 7 — Reject API-unsupported product types
# ---------------------------------------------------------------------------

def _check_api_unsupported_product_type(product_id: int):
    """Block product types that require an interactive Telegram delivery flow.

    M365 and Canva invite products ask the customer for an email address via
    a Telegram message after payment. That interactive flow does not exist in
    the REST API. They are blocked here until a later phase adds an
    explicit email field to the order request and the delivery path.
    """
    if is_m365_product(product_id):
        raise OrderValidationError(
            "product_not_available_via_api",
            "This product requires interactive email delivery and cannot be "
            "ordered through the API at this time.",
            422,
        )
    if is_canva_product(product_id):
        raise OrderValidationError(
            "product_not_available_via_api",
            "This product requires interactive email delivery and cannot be "
            "ordered through the API at this time.",
            422,
        )


# ---------------------------------------------------------------------------
# Step 8 — Soft stock pre-check (non-authoritative early-out)
# ---------------------------------------------------------------------------

def _check_stock_available(product_id: int, quantity: int):
    """Soft stock pre-check using the existing master stock function.

    This is an early-out guard, not the authoritative reservation. The
    real atomic reservation happens inside the order service.
    """
    available = get_available_stock_count(product_id)
    if available <= 0:
        raise OrderValidationError(
            "out_of_stock",
            "This product is currently out of stock.",
            422,
        )
    if available < quantity:
        raise OrderValidationError(
            "insufficient_stock",
            f"Only {available} unit(s) available for this product.",
            422,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_order_request(
    context: ApiRequestContext,
    body: dict,
    expected_environment: str,
) -> ValidatedOrderRequest:
    """Validate an inbound POST /api/v1/orders request body.

    Returns a ValidatedOrderRequest containing only server-resolved
    values. Raises OrderValidationError on any failure.

    Structural / syntactic checks (steps 1–4) run before any DB call so
    that the correct error code is always returned regardless of whether
    the product exists.

    This function does NOT create orders, debit wallets, or reserve
    stock. Callers must use the order service for those operations.
    """
    if not isinstance(body, dict):
        raise OrderValidationError(
            "invalid_request", "A JSON object request body is required."
        )

    # --- Steps 1–4: Pure structural checks — no DB calls ---
    _reject_forbidden_fields(body)          # Step 1
    public_product_id = _parse_public_product_id(body)   # Step 2
    quantity_pre = _parse_quantity_syntax(body)           # Step 3
    client_order_id = _parse_client_order_id(body)        # Step 4

    # --- Step 5: Resolve product from master catalog + authorize (DB) ---
    product_row = _resolve_product(context, public_product_id, expected_environment)
    product_id = int(product_row["product_id"])
    max_per_order = max(1, int(product_row.get("max_per_order") or 1))

    # --- Step 6: Apply product max_per_order rule (server value) ---
    quantity = _apply_max_per_order(quantity_pre, max_per_order)

    # --- Step 7: Reject API-unsupported product fulfillment types ---
    _check_api_unsupported_product_type(product_id)

    # --- Step 8: Soft stock pre-check (non-authoritative early-out) ---
    _check_stock_available(product_id, quantity)

    return ValidatedOrderRequest(
        reseller_id=context.reseller_id,
        product_id=product_id,
        public_product_id=public_product_id,
        product_name=str(product_row.get("name") or ""),
        max_per_order=max_per_order,
        quantity=quantity,
        client_order_id=client_order_id,
    )


__all__ = [
    "OrderValidationError",
    "ValidatedOrderRequest",
    "validate_order_request",
]
