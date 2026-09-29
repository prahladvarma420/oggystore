"""Versioned API v1 router. Future API versions use separate blueprints."""

from flask import Blueprint

from reseller_api.controllers.v1.foundation import api_v1_index
from reseller_api.controllers.v1.products import list_products, product_detail
from reseller_api.controllers.v1.balance import balance_detail
from reseller_api.controllers.v1.orders import create_order, list_orders, order_detail, order_delivery
from reseller_api.middleware.authentication import authenticate_request
from reseller_api.middleware.security_controls import enforce_global_api_control
from reseller_api.middleware.rate_limit import (
    apply_rate_limit_headers,
    enforce_request_rate_limit,
)


blueprint = Blueprint(
    "reseller_api_v1",
    __name__,
    url_prefix="/api/v1",
)

# The fresh database-backed master switch runs before authentication, then
# authentication/rate limiting remain deny-by-default for every v1 route.
blueprint.before_request(enforce_global_api_control)
blueprint.before_request(authenticate_request)
blueprint.before_request(enforce_request_rate_limit)
blueprint.after_request(apply_rate_limit_headers)

# Support the canonical trailing-slash namespace and a JSON no-slash alias.
blueprint.add_url_rule("", "index_no_slash", api_v1_index, methods=["GET"])
blueprint.add_url_rule("/", "index", api_v1_index, methods=["GET"])
blueprint.add_url_rule("/products", "products", list_products, methods=["GET"])
blueprint.add_url_rule("/balance", "balance", balance_detail, methods=["GET"])
blueprint.add_url_rule(
    "/products/<string:product_id>",
    "product_detail",
    product_detail,
    methods=["GET"],
)
blueprint.add_url_rule("/orders", "orders_create", create_order, methods=["POST"])
blueprint.add_url_rule("/orders", "orders_list", list_orders, methods=["GET"])
blueprint.add_url_rule(
    "/orders/<string:order_id>",
    "order_detail",
    order_detail,
    methods=["GET"],
)

blueprint.add_url_rule(
    "/orders/<string:order_id>/delivery",
    "order_delivery",
    order_delivery,
    methods=["GET"],
)


__all__ = ["blueprint"]
