"""Versioned API v1 router. Future API versions use separate blueprints."""

from flask import Blueprint

from reseller_api.controllers.v1.foundation import api_v1_index
from reseller_api.controllers.v1.products import list_products, product_detail
from reseller_api.controllers.v1.product_import import (
    import_product_endpoint,
    get_mapping_endpoint,
    remove_mapping_endpoint,
)
from reseller_api.controllers.v1.my_catalog import (
    list_my_products_endpoint,
    get_my_product_endpoint,
    patch_my_product_endpoint,
    delete_my_product_endpoint,
)
from reseller_api.controllers.v1.pricing import (
    list_prices_endpoint,
    get_price_endpoint,
)
from reseller_api.controllers.v1.balance import balance_detail, wallet_summary
from reseller_api.controllers.v1.usage import usage_summary
from reseller_api.controllers.v1.analytics import sales_analytics
from reseller_api.controllers.v1.exports import export_orders_csv
from reseller_api.controllers.v1.transactions import list_transactions as list_transactions_endpoint
from reseller_api.controllers.v1.refunds import (
    list_refunds as list_refunds_endpoint,
    refund_detail as refund_detail_endpoint,
)
from reseller_api.controllers.v1.statements import (
    list_statements as list_statements_endpoint,
    statement_detail as statement_detail_endpoint,
    statement_transactions as statement_transactions_endpoint,
    statement_invoices as statement_invoices_endpoint,
    statement_document as statement_document_endpoint,
)
from reseller_api.controllers.v1.invoices import (
    list_invoices as list_invoices_endpoint,
    invoice_detail as invoice_detail_endpoint,
    invoice_lines as invoice_lines_endpoint,
    invoice_transactions as invoice_transactions_endpoint,
    invoice_document as invoice_document_endpoint,
)
from reseller_api.controllers.v1.orders import create_order, quote_order, list_orders, order_detail, order_delivery
from reseller_api.controllers.v1.webhooks import (
    create_webhook_endpoint,
    test_webhook_endpoint,
    webhook_analytics_endpoint,
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
    CATEGORY_CATALOG_WRITE,
)
from reseller_api.middleware.security_controls import enforce_global_api_control
from reseller_api.middleware.rate_limit import (
    apply_rate_limit_headers,
    enforce_request_rate_limit,
)
from reseller_api.versioning import apply_version_headers


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
# Phase 13 P3: every versioned response carries X-API-Version (and
# Deprecation/Sunset/Link when an endpoint is registered as deprecated).
blueprint.after_request(apply_version_headers)

# Support the canonical trailing-slash namespace and a JSON no-slash alias.
blueprint.add_url_rule("", "index_no_slash", api_v1_index, methods=["GET"])
blueprint.add_url_rule("/", "index", api_v1_index, methods=["GET"])
blueprint.add_url_rule("/products", "products", endpoint_category(CATEGORY_READ)(list_products), methods=["GET"])
blueprint.add_url_rule("/balance", "balance", endpoint_category(CATEGORY_READ)(balance_detail), methods=["GET"])
# Phase 13 P6 — reseller-safe own-usage visibility
blueprint.add_url_rule("/usage", "usage_summary", endpoint_category(CATEGORY_READ)(usage_summary), methods=["GET"])
# Phase 14 P4 — reseller-safe wallet/financial summary
blueprint.add_url_rule(
    "/wallet/summary",
    "wallet_summary",
    endpoint_category(CATEGORY_READ)(wallet_summary),
    methods=["GET"],
)
# Phase 15 P5 — reseller refund records (read-only)
blueprint.add_url_rule(
    "/refunds", "refunds_list",
    endpoint_category(CATEGORY_READ)(list_refunds_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/refunds/<string:refund_id>", "refund_detail",
    endpoint_category(CATEGORY_READ)(refund_detail_endpoint), methods=["GET"])

# Phase 15 P4 — reseller ledger history (own transactions only)
blueprint.add_url_rule(
    "/transactions", "transactions_list",
    endpoint_category(CATEGORY_READ)(list_transactions_endpoint), methods=["GET"])

# Phase 15 P3 — reseller statements (static rules rank above the dynamic id)
blueprint.add_url_rule(
    "/statements", "statements_list",
    endpoint_category(CATEGORY_READ)(list_statements_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/statements/<string:statement_id>", "statement_detail",
    endpoint_category(CATEGORY_READ)(statement_detail_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/statements/<string:statement_id>/transactions", "statement_transactions",
    endpoint_category(CATEGORY_READ)(statement_transactions_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/statements/<string:statement_id>/invoices", "statement_invoices",
    endpoint_category(CATEGORY_READ)(statement_invoices_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/statements/<string:statement_id>/document", "statement_document",
    endpoint_category(CATEGORY_READ)(statement_document_endpoint), methods=["GET"])

# Phase 15 P2 — reseller invoices (static rules rank above the dynamic id)
blueprint.add_url_rule(
    "/invoices", "invoices_list",
    endpoint_category(CATEGORY_READ)(list_invoices_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/invoices/<string:invoice_id>", "invoice_detail",
    endpoint_category(CATEGORY_READ)(invoice_detail_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/invoices/<string:invoice_id>/lines", "invoice_lines",
    endpoint_category(CATEGORY_READ)(invoice_lines_endpoint), methods=["GET"])
blueprint.add_url_rule(
    "/invoices/<string:invoice_id>/transactions", "invoice_transactions",
    endpoint_category(CATEGORY_READ)(invoice_transactions_endpoint),
    methods=["GET"])
blueprint.add_url_rule(
    "/invoices/<string:invoice_id>/document", "invoice_document",
    endpoint_category(CATEGORY_READ)(invoice_document_endpoint), methods=["GET"])

# Phase 14 P8 — reseller-safe CSV export of own orders
blueprint.add_url_rule(
    "/exports/orders.csv",
    "export_orders_csv",
    endpoint_category(CATEGORY_READ)(export_orders_csv),
    methods=["GET"],
)
# Phase 14 P3 — reseller-safe sales analytics
blueprint.add_url_rule(
    "/analytics/sales",
    "sales_analytics",
    endpoint_category(CATEGORY_READ)(sales_analytics),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/products/<string:product_id>",
    "product_detail",
    endpoint_category(CATEGORY_READ)(product_detail),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/products/<string:product_id>/import",
    "product_import",
    endpoint_category(CATEGORY_CATALOG_WRITE)(import_product_endpoint),
    methods=["POST"],
)
blueprint.add_url_rule(
    "/products/<string:product_id>/import",
    "product_import_get",
    endpoint_category(CATEGORY_CATALOG_WRITE)(get_mapping_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/products/<string:product_id>/import",
    "product_import_delete",
    endpoint_category(CATEGORY_CATALOG_WRITE)(remove_mapping_endpoint),
    methods=["DELETE"],
)
# Phase 11 P4 — reseller's personalised catalog (my-products)
# Phase 13 P2: the two GETs are reads (they previously consumed the WRITE
# budget); writes keep CATEGORY_CATALOG_WRITE.  endpoint_category is applied
# here only — the controllers no longer self-wrap (double-count fix).
blueprint.add_url_rule(
    "/my-products",
    "my_products_list",
    endpoint_category(CATEGORY_READ)(list_my_products_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/my-products/<string:product_id>",
    "my_product_get",
    endpoint_category(CATEGORY_READ)(get_my_product_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/my-products/<string:product_id>",
    "my_product_patch",
    endpoint_category(CATEGORY_CATALOG_WRITE)(patch_my_product_endpoint),
    methods=["PATCH"],
)
blueprint.add_url_rule(
    "/my-products/<string:product_id>",
    "my_product_delete",
    endpoint_category(CATEGORY_CATALOG_WRITE)(delete_my_product_endpoint),
    methods=["DELETE"],
)
# Phase 11 P5 - reseller pricing read endpoints
blueprint.add_url_rule(
    "/pricing",
    "pricing_list",
    endpoint_category(CATEGORY_READ)(list_prices_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule(
    "/pricing/<string:product_id>",
    "pricing_detail",
    endpoint_category(CATEGORY_READ)(get_price_endpoint),
    methods=["GET"],
)
blueprint.add_url_rule("/orders", "orders_create", endpoint_category(CATEGORY_ORDER_CREATE)(create_order), methods=["POST"])
# Phase 12 P3: non-binding price quote. Read-only (no stock/wallet/quota
# writes), so it shares the READ category. Werkzeug ranks this static rule
# above the dynamic /orders/<order_id> rule below.
blueprint.add_url_rule("/orders/quote", "orders_quote", endpoint_category(CATEGORY_READ)(quote_order), methods=["POST"])
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
# Phase 14 P6 — own webhook analytics (static path, ranked above <webhook_id>)
blueprint.add_url_rule(
    "/webhooks/analytics",
    "webhook_analytics",
    endpoint_category(CATEGORY_READ)(webhook_analytics_endpoint),
    methods=["GET"],
)
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
# Phase 13 P7 — synchronous signed connectivity test (manual-retry budget)
blueprint.add_url_rule(
    "/webhooks/<string:webhook_id>/test",
    "webhook_test_ping",
    endpoint_category(CATEGORY_WEBHOOK_RETRY)(test_webhook_endpoint),
    methods=["POST"],
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

