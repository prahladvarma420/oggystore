"""API-only content checks and response security headers."""

from flask import request

from reseller_api.errors import ApiError
from reseller_api.services.request_security import validate_request_metadata


_BODY_METHODS = {"POST", "PUT", "PATCH"}


def register_security_middleware(app):
    @app.before_request
    def _validate_http_metadata():
        issue = validate_request_metadata(
            method=request.method,
            content_length=request.environ.get("CONTENT_LENGTH"),
            transfer_encoding=(
                request.environ.get("HTTP_TRANSFER_ENCODING")
                or request.headers.get("Transfer-Encoding")
            ),
            connection=request.headers.get("Connection"),
            content_encoding=request.headers.get("Content-Encoding"),
            method_override=request.headers.get("X-HTTP-Method-Override"),
            host=request.host,
        )
        if issue:
            code, message, status = issue
            raise ApiError(code, message, status)

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
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        if app.config.get("RESELLER_API_HSTS"):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        # CORS is intentionally disabled. No wildcard origin is emitted.
        return response
