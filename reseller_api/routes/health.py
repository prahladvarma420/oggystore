"""Public health routes for Railway liveness and readiness checks."""

from flask import Blueprint

from reseller_api.controllers.health import database_health, health


blueprint = Blueprint("reseller_health", __name__)
blueprint.add_url_rule("/health", "health", health, methods=["GET"])
blueprint.add_url_rule("/health/db", "database_health", database_health, methods=["GET"])


__all__ = ["blueprint"]
