"""Controller for the API v1 namespace discovery response."""

from flask import current_app

from reseller_api.responses import success_response


def api_v1_index():
    return success_response({
        "name": current_app.config["API_NAME"],
        "version": current_app.config["API_VERSION"],
        "status": "available",
        # Keep in sync with reseller_api/routes/v1/__init__.py
        "available_resources": [
            "products", "my-products", "pricing", "balance", "orders",
            "usage", "analytics", "webhooks", "invoices", "statements",
            "transactions", "refunds",
        ],
        "message": "The products, catalog, pricing, balance, order, and "
                   "webhook resources are available.",
    })
