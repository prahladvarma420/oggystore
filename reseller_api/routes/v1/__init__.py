"""Versioned API v1 router. Future API versions use separate blueprints."""

from flask import Blueprint

from reseller_api.controllers.v1.foundation import api_v1_index
from reseller_api.middleware.authentication import authenticate_request
from reseller_api.middleware.rate_limit import (
    apply_rate_limit_headers,
    enforce_request_rate_limit,
)


blueprint = Blueprint(
    "reseller_api_v1",
    __name__,
    url_prefix="/api/v1",
)

# Authentication is deny-by-default for every current and future v1 route.
blueprint.before_request(authenticate_request)
blueprint.before_request(enforce_request_rate_limit)
blueprint.after_request(apply_rate_limit_headers)

# Support the canonical trailing-slash namespace and a JSON no-slash alias.
blueprint.add_url_rule("", "index_no_slash", api_v1_index, methods=["GET"])
blueprint.add_url_rule("/", "index", api_v1_index, methods=["GET"])


__all__ = ["blueprint"]
