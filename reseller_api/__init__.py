"""Standalone Flask application factory for the versioned reseller API."""


def create_app(test_config=None):
    """Create the isolated reseller API application.

    This app intentionally has no Web Admin session, CSRF, or Telegram-state
    dependency. Authentication and resource endpoints arrive in later prompts.
    """
    # Lazy imports keep framework-neutral authentication/services testable
    # without importing Flask or creating an HTTP application.
    from flask import Flask
    from reseller_api.config import ApiConfig
    from reseller_api.errors import register_error_handlers
    from reseller_api.middleware.request_context import register_request_context
    from reseller_api.middleware.security import register_security_middleware

    app = Flask("reseller_api")
    app.config.from_object(ApiConfig)
    if test_config:
        app.config.update(test_config)

    if app.config.get("INIT_DATABASE", True):
        # Existing project convention: additive/idempotent schema checks run at
        # service startup. This includes the key-level enabled migration.
        import database.db as botdb
        botdb.init_db()

    register_request_context(app)
    register_security_middleware(app)
    register_error_handlers(app)

    from reseller_api.routes.v1 import blueprint as api_v1_blueprint
    app.register_blueprint(api_v1_blueprint)
    return app


__all__ = ["create_app"]
