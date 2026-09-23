"""API-only content checks and response security headers."""

from flask import request

from reseller_api.errors import ApiError


_BODY_METHODS = {"POST", "PUT", "PATCH"}


def register_security_middleware(app):
    @app.before_request
    def _require_json_for_bodies():
        if request.method not in _BODY_METHODS:
            return
        if not request.content_length:
            return
        if not request.is_json:
            raise ApiError(
                "unsupported_media_type",
                "A JSON request body is required.",
                415,
            )

    @app.after_request
    def _api_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if app.config.get("RESELLER_API_HSTS"):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        # CORS is intentionally disabled. No wildcard origin is emitted.
        return response
