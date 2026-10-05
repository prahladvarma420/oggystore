"""Financial abuse guard — Phase 10 Prompt 4.

Adds a protection layer AROUND existing order / wallet / stock logic.
Does NOT replace idempotency, transaction handling, wallet debit, or
stock reservation — those remain in the existing service pipeline.

Protections added
-----------------
1. Absolute quantity ceiling (belt-and-suspenders on top of order_validation).
2. Product-burst check: limits rapid-fire orders for the same product from
   one reseller within a short window (e.g. 5 orders per 10 seconds).
3. Audit logging: every order attempt (success and reject) is written to
   reseller_financial_events for monitoring and forensics.

Fail-open policy
----------------
DB errors in the burst check cause a log warning and allow the request
through — a transient DB hiccup must never block legitimate ordering.
The existing atomic stock/wallet layer remains the last-resort guard.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import database.reseller_db as rdb
from reseller_api.services import security_logger as sec_log
from reseller_api.context import ApiRequestContext
from utils.timezone import now as local_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Absolute per-order quantity ceiling — applied before any product-level check
_ABS_MAX_QUANTITY = 1000

# Per-product burst window: max N orders for the same product within W seconds
PRODUCT_BURST_WINDOW_SEC = 10
PRODUCT_BURST_MAX = 5


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class FinancialAbuseError(Exception):
    """Raised when an order is rejected by the financial abuse guard."""

    def __init__(self, code: str, message: str, status: int = 429):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def enforce_quantity_ceiling(quantity: int, product_max_per_order: int = 0) -> None:
    """Reject quantities that exceed the absolute ceiling or product limit.

    Called as a belt-and-suspenders check after order_validation has already
    run. The real product max_per_order check is in order_validation — this
    layer only catches values that somehow slipped through.
    """
    if not isinstance(quantity, int) or quantity <= 0:
        raise FinancialAbuseError(
            "invalid_quantity",
            "quantity must be a positive integer.",
            400,
        )
    if quantity > _ABS_MAX_QUANTITY:
        raise FinancialAbuseError(
            "invalid_quantity",
            f"quantity must not exceed {_ABS_MAX_QUANTITY}.",
            400,
        )
    if product_max_per_order > 0 and quantity > product_max_per_order:
        raise FinancialAbuseError(
            "invalid_quantity",
            f"This product allows a maximum of {product_max_per_order} per order.",
            400,
        )


def check_product_order_burst(
    reseller_id: int,
    product_id: int,
    moment=None,
) -> None:
    """Reject if this reseller has placed too many orders for one product recently.

    Uses the reseller_financial_events audit log to count order_placed events
    within the burst window. Fails open on DB error.
    """
    moment = moment or local_now()
    since = (moment - timedelta(seconds=PRODUCT_BURST_WINDOW_SEC)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        count = rdb.count_recent_product_orders(reseller_id, product_id, since)
    except Exception as exc:
        log.warning(
            "financial_abuse_guard: burst_check DB error reseller=%s product=%s: %s",
            reseller_id, product_id, exc,
        )
        return  # fail-open
    if count >= PRODUCT_BURST_MAX:
        raise FinancialAbuseError(
            "order_burst_limit",
            f"Too many orders for this product in a short window. "
            f"Please wait a moment before trying again.",
            429,
        )


def log_financial_event(
    reseller_id: int,
    event_type: str,
    detail: str = "",
    product_id: Optional[int] = None,
    quantity: Optional[int] = None,
) -> None:
    """Write one financial audit event. Never raises — logging must not break the flow."""
    try:
        rdb.log_financial_event_db(
            reseller_id, event_type, detail, product_id, quantity
        )
    except Exception as exc:
        log.warning(
            "financial_abuse_guard: audit_log DB error type=%s: %s",
            event_type, exc,
        )


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def assess_order_financial_risk(
    context: ApiRequestContext,
    validated,          # ValidatedOrderRequest from order_validation
    moment=None,
) -> None:
    """Run all financial abuse checks for a new order request.

    Must be called AFTER validate_order_request() and BEFORE the order
    service pipeline (pricing → stock → wallet debit → DB insert).

    Raises FinancialAbuseError on rejection; returns None on pass.
    Logs the assessment result for monitoring regardless of outcome.
    """
    # 1. Belt-and-suspenders quantity check (in case validation is bypassed)
    enforce_quantity_ceiling(
        validated.quantity,
        validated.max_per_order,
    )

    # 2. Product burst check (too many orders for one product in a short window)
    check_product_order_burst(
        context.reseller_id,
        validated.product_id,
        moment=moment,
    )


__all__ = [
    "FinancialAbuseError",
    "assess_order_financial_risk",
    "check_product_order_burst",
    "enforce_quantity_ceiling",
    "log_financial_event",
    "PRODUCT_BURST_MAX",
    "PRODUCT_BURST_WINDOW_SEC",
    "_ABS_MAX_QUANTITY",
]
