"""Versioned API v1 router. Future API versions use separate blueprints."""

from flask import Blueprint

from reseller_api.controllers.v1.foundation import api_v1_index


blueprint = Blueprint(
    "reseller_api_v1",
    __name__,
    url_prefix="/api/v1",
)

# Support the canonical trailing-slash namespace and a JSON no-slash alias.
blueprint.add_url_rule("", "index_no_slash", api_v1_index, methods=["GET"])
blueprint.add_url_rule("/", "index", api_v1_index, methods=["GET"])


__all__ = ["blueprint"]
