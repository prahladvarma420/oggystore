"""Phase 11 P5 - Reseller pricing read endpoints.

GET /api/v1/pricing            -> list effective prices for all accessible products
GET /api/v1/pricing/<prod_id>  -> effective price for one product

Resellers can READ their server-calculated effective price but CANNOT set,
update, or negotiate prices.  All pricing rules are owner-only via WebAdmin.
"""
from flask import current_app, jsonify

from reseller_api.middleware.authorization import get_authenticated_context
from reseller_api.services.authorization import PRODUCTS_READ
from reseller_api.services.endpoint_rate_limit import CATEGORY_READ
from reseller_api.services.pricing import PricingError, get_product_price, list_product_prices
from reseller_api.decorators import require_operation, endpoint_category


@require_operation(PRODUCTS_READ)
@endpoint_category(CATEGORY_READ)
def list_prices_endpoint():
    """GET /api/v1/pricing"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        prices = list_product_prices(context, environment)
    except PricingError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return jsonify({"prices": prices}), 200


@require_operation(PRODUCTS_READ)
@endpoint_category(CATEGORY_READ)
def get_price_endpoint(product_id: str):
    """GET /api/v1/pricing/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        price = get_product_price(context, product_id, environment)
    except PricingError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return jsonify(price), 200
