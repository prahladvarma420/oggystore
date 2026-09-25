"""Controller for the API v1 namespace discovery response."""

from flask import current_app

from reseller_api.responses import success_response


def api_v1_index():
    return success_response({
        "name": current_app.config["API_NAME"],
        "version": current_app.config["API_VERSION"],
        "status": "available",
        "available_resources": ["products"],
        "message": "The products resource is available.",
    })
