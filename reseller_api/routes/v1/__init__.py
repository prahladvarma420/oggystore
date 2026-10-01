"""Versioned API v1 router. Future API versions use separate blueprints."""

from flask import Blueprint

from reseller_api.controllers.v1.foundation import api_v1_index
from reseller_api.controllers.v1.products import list_products, product_detail
from reseller_api.controllers.v1.balance import balance_detail
from reseller_api.controllers.v1.orders import create_order, list_orders, order_detail, order_delivery
from reseller_api.controllers.v1.webhooks import (
    create_webhook_endpoint,
    list_webhooks_endpoint,
    get_webhook_endpoint,
    update_webhook_endpoint,
    delete_webhook_endpoint,
    rotate_secret_endpoint,
    retry_event_endpoint,
    list_deliveries_endpoint,
    retry_delivery_endpoint,
)
from reseller_api.middleware.authentication import authenticate_request
from reseller_api.middleware.endpoint_rate_limit import endpoint_category
from reseller_api.services.endpoint_rate_limit import (
    CATEGORY_READ,
    CATEGORY_DELIVERY_READ,
    CATEGORY_ORDER_CREATE,
    CATEGORY_WEBHOOK_WRITE,
    CATEGORY_WEBHOOK_RETRY,
)
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
blueprint.add_url_rule("/products", "products", endpoint_category(CATEGORY_READ)(list_products), methods=["GET"])
blueprint.add_url_rule("/balance", "balance", endpoint_category(CATEGORY_READ)(balance_detail), methods=["GET"])
blueprint.add_url_rule(
    "/products/<string:product_id>",
    "product_detail",
    endpoint_category(CATEGORY_READ)(product_detail),
    methods=["GET"],
)
blueprint.add_url_rule("/orders", "orders_create", endpoint_category(CATEGORY_ORDER_CREATE)(create_order), methods=["POST"])
blueprint.add_url_rule("/orders", "orders_list", endpoint_category(CATEGORY_READ)(list_orders), methods=["GET"])
blueprint.add_url_rule(
    "/orders/<string:order_id>",
    "order_detail",
    endpoint_category(CATEGORY_READ)(order_detail),
    methods=["GET"],
)

blueprint.add_url_rule(
    "/orders/<string:order_id>/delivery",
    "order_delivery",
    endpoint_category(CATEGORY_DELIVERY_READ)(order_delivery),
    methods=["GET"],
)


__all__ = ["blueprint"]

# Phase 9: webhook configuration routes
blueprint.add_url_rule("/webhooks", "webhooks_create", endpoint_category(CATEGORY_WEBHOOK_WRITE)(create_webhook_endpoint), methods=["POST"])
blueprint.add_url_rule("/webhooks", "webhooks_list", endpoint_category(CATEGORY_READ)(list_webhooks_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>",
    "webhook_detail",
    endpoint_category(CATEGORY_READ)(get_webhook_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>",
    "webhook_update",
    endpoint_category(CATEGORY_WEBHOOK_WRITE)(update_webhook_endpoint),
    methods=["PATCH"],
)
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>",
    "webhook_delete",
    endpoint_category(CATEGORY_WEBHOOK_WRITE)(delete_webhook_endpoint),
    methods=["DELETE"],
)
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>/rotate-secret",
    "webhook_rotate_secret",
    endpoint_category(CATEGORY_WEBHOOK_WRITE)(rotate_secret_endpoint),
    methods=["POST"],
)
blueprint.add_url_rule(
    "/webhooks/events/<string:outbox_id>/retry",
    "webhook_event_retry",
    endpoint_category(CATEGORY_WEBHOOK_RETRY)(retry_event_endpoint),
    methods=["POST"],
)
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>/deliveries",
    "webhook_deliveries_list",
    endpoint_category(CATEGORY_READ)(list_deliveries_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>/deliveries/<string:delivery_id>/retry",
    "webhook_delivery_retry",
    endpoint_category(CATEGORY_WEBHOOK_RETRY)(retry_delivery_endpoint),
    methods=["POST"],
)

