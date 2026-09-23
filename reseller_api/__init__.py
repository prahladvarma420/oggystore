"""Standalone Flask application factory for the versioned reseller API."""

from flask import Flask

from reseller_api.config import ApiConfig
from reseller_api.errors import register_error_handlers
from reseller_api.middleware.request_context import register_request_context
from reseller_api.middleware.security import register_security_middleware


def create_app(test_config=None):
    """Create the isolated reseller API application.

    This app intentionally has no Web Admin session, CSRF, or Telegram-state
    dependency. Authentication and resource endpoints arrive in later prompts.
    """
    app = Flask("reseller_api")
    app.config.from_object(ApiConfig)
    if test_config:
        app.config.update(test_config)

    register_request_context(app)
    register_security_middleware(app)
    register_error_handlers(app)

    from reseller_api.routes.v1 import blueprint as api_v1_blueprint
    app.register_blueprint(api_v1_blueprint)
    return app


__all__ = ["create_app"]
