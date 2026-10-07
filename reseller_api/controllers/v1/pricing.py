"""Phase 11 P5 - Reseller pricing read endpoints.

GET /api/v1/pricing            -> list effective prices for all accessible products
GET /api/v1/pricing/<prod_id>  -> effective price for one product

Resellers can READ their server-calculated effective price but CANNOT set,
update, or negotiate prices.  All pricing rules are owner-only via WebAdmin.

Phase 13 P2 — standardised on the API-wide envelope
({"ok", "data", "request_id"} / structured error object).  The Phase 11
top-level fields ("prices", price attributes) remain as deprecated aliases
so existing integrations keep working unchanged.
"""
from flask import current_app

from reseller_api.middleware.authorization import get_authenticated_context
from reseller_api.services.authorization import PRODUCTS_READ
from reseller_api.errors import ApiError
from reseller_api.responses import success_response_legacy
from reseller_api.services.pricing import PricingError, get_product_price, list_product_prices
from reseller_api.decorators import require_operation


@require_operation(PRODUCTS_READ)
def list_prices_endpoint():
    """GET /api/v1/pricing"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        prices = list_product_prices(context, environment)
    except PricingError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None
    return success_response_legacy(
        {"prices": prices},
        legacy={"prices": prices},           # deprecated alias (Phase 11 shape)
    )


@require_operation(PRODUCTS_READ)
def get_price_endpoint(product_id: str):
    """GET /api/v1/pricing/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        price = get_product_price(context, product_id, environment)
    except PricingError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None
    # Legacy shape was the bare price object at top level.
    return success_response_legacy(price, legacy=dict(price))
