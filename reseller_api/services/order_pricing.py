"""Server-side order price calculation for POST /api/v1/orders.

This module is the ONLY permitted price source for reseller orders.
No client-supplied price, total, discount, or unit_price is ever used.

All arithmetic is integer-cents only. Floats are forbidden on money paths.
The existing resolve_reseller_price_cents() function from reseller_db is
the sole pricing engine — no duplicate pricing logic is created here.

Pricing result fields:
    reseller_id         int   — from authenticated context (never from client)
    product_id          int   — server-resolved internal ID
    quantity            int   — validated quantity
    unit_price_cents    int   — server-calculated unit price in integer cents
    total_cents         int   — unit_price_cents * quantity  (integer, no float)
    price_source        str   — audit trail: master / master_tier / rule:*
    unit_price_display  str   — '19.99'  (from_cents, for API response only)
    total_display       str   — '59.97'  (from_cents, for API response only)
    currency            str   — always 'USDT'
"""

from database.reseller_db import resolve_reseller_price_cents, from_cents
from reseller_api.services.order_validation import ValidatedOrderRequest


# ---------------------------------------------------------------------------
# Service error
# ---------------------------------------------------------------------------

class OrderPricingError(ValueError):
    """Raised when server-side price cannot be determined."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


# ---------------------------------------------------------------------------
# Pricing result (value object — no side effects)
# ---------------------------------------------------------------------------

class OrderPricingResult:
    """Immutable pricing result produced entirely by the server.

    This object is the only permitted source of financial values for
    the downstream stock-check, wallet-debit, and order-creation steps.
    Never derive monetary values from client input after this point.
    """

    __slots__ = (
        "reseller_id",
        "product_id",
        "quantity",
        "unit_price_cents",
        "total_cents",
        "price_source",
        "unit_price_display",
        "total_display",
        "currency",
    )

    def __init__(
        self,
        reseller_id: int,
        product_id: int,
        quantity: int,
        unit_price_cents: int,
        total_cents: int,
        price_source: str,
    ):
        if isinstance(unit_price_cents, float) or isinstance(total_cents, float):
            raise TypeError("float money values are not allowed in OrderPricingResult")
        self.reseller_id = int(reseller_id)
        self.product_id = int(product_id)
        self.quantity = int(quantity)
        self.unit_price_cents = int(unit_price_cents)
        self.total_cents = int(total_cents)
        self.price_source = str(price_source)
        self.unit_price_display = from_cents(self.unit_price_cents)
        self.total_display = from_cents(self.total_cents)
        self.currency = "USDT"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def calculate_order_price(
    validated: ValidatedOrderRequest,
) -> OrderPricingResult:
    """Calculate the server-side price for a validated order request.

    Uses the existing resolve_reseller_price_cents() from reseller_db —
    the same function used by the Phase 5 product catalog. No duplicate
    pricing logic is introduced here.

    Args:
        validated: A ValidatedOrderRequest produced by validate_order_request().
                   All values in this object are server-resolved; no client
                   financial inputs are accepted or used.

    Returns:
        OrderPricingResult with integer-cent unit price and total.

    Raises:
        OrderPricingError: If a server-side price cannot be determined for
                           this reseller / product / quantity combination.
    """
    unit_price_cents, price_source = resolve_reseller_price_cents(
        validated.reseller_id,
        validated.product_id,
        validated.quantity,
    )

    if unit_price_cents is None:
        # Product disappeared or reseller has no applicable price rule and
        # the master product no longer exists.  Fail closed.
        raise OrderPricingError(
            "product_not_found",
            "The requested product was not found.",
            404,
        )

    if unit_price_cents < 0:
        # Defensive guard — a misconfigured markup rule could in theory
        # produce a negative unit price. Refuse the order rather than
        # crediting the reseller for buying.
        raise OrderPricingError(
            "pricing_error",
            "A server-side pricing error occurred. Please contact support.",
            500,
        )

    # Integer multiplication only — no floats on the money path.
    total_cents = int(unit_price_cents) * int(validated.quantity)

    return OrderPricingResult(
        reseller_id=validated.reseller_id,
        product_id=validated.product_id,
        quantity=validated.quantity,
        unit_price_cents=unit_price_cents,
        total_cents=total_cents,
        price_source=price_source or "master",
    )


__all__ = [
    "OrderPricingError",
    "OrderPricingResult",
    "calculate_order_price",
]
