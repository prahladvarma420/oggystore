"""Standalone Flask application factory for the versioned reseller API."""


def create_app(test_config=None):
    """Create the isolated reseller API application.

    This app intentionally has no Web Admin session, CSRF, or Telegram-state
    dependency. It reuses only the project's shared database and reseller
    service layers.
    """
    # Lazy imports keep framework-neutral authentication/services testable
    # without importing Flask or creating an HTTP application.
    from flask import Flask
    from reseller_api.config import ApiConfig, validate_database_config
    from reseller_api.errors import register_error_handlers
    from reseller_api.lifecycle import register_lifecycle
    from reseller_api.middleware.request_context import register_request_context
    from reseller_api.middleware.security import register_security_middleware

    app = Flask("reseller_api")
    app.config.from_object(ApiConfig)
    if test_config:
        app.config.update(test_config)

    # Railway/production must never silently fall back to a local SQLite file.
    # Only the backend type is retained in app config; the URL is never copied
    # into Flask config, responses, or logs.
    validate_database_config(app.config)

    if app.config.get("INIT_DATABASE", True):
        # Reuse the existing project's additive/idempotent schema checks. No
        # API-specific database, table copy, or migration system is created.
        import database.db as botdb
        botdb.init_db()

    # Phase 13 P7: sandbox/production isolation guard — the API refuses to
    # boot against a database stamped with a different environment.
    from reseller_api.config import validate_environment_marker
    validate_environment_marker(app.config)

    register_request_context(app)
    register_security_middleware(app)

    # Phase 10 P5: payload size & JSON validation guard
    app.config.setdefault("MAX_CONTENT_LENGTH", 256 * 1024)  # Flask hard limit (256 KB)
    from reseller_api.middleware.payload_guard import register_payload_guard
    register_payload_guard(app)
    register_error_handlers(app)

    from reseller_api.routes.health import blueprint as health_blueprint
    from reseller_api.routes.v1 import blueprint as api_v1_blueprint

    app.register_blueprint(health_blueprint)
    app.register_blueprint(api_v1_blueprint)
    register_lifecycle(app)
    return app


__all__ = ["create_app"]
