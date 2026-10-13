"""Central API exceptions and sanitized Flask error handlers."""

from flask import g
from werkzeug.exceptions import HTTPException

from reseller_api.responses import error_response


class ApiError(Exception):
    def __init__(self, code, message, status=400, headers=None):
        super().__init__(str(code))
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)
        self.headers = dict(headers or {})


def register_error_handlers(app):
    from reseller_api.services.order_status import OrderStatusUnavailable

    @app.errorhandler(OrderStatusUnavailable)
    def _unverifiable_status(exc):
        return error_response("order_state_unavailable", "Order delivery state cannot be verified.", 503)

    @app.errorhandler(ApiError)
    def _api_error(exc):
        response, status = error_response(exc.code, exc.message, exc.status)
        for name, value in exc.headers.items():
            response.headers[name] = value
        return response, status

    @app.errorhandler(HTTPException)
    def _http_error(exc):
        mapping = {
            400: ("bad_request", "The request is invalid."),
            404: ("not_found", "The requested API resource was not found."),
            405: ("method_not_allowed", "The HTTP method is not allowed for this resource."),
            413: ("payload_too_large", "The request body is too large."),
            415: ("unsupported_media_type", "A JSON request body is required."),
            429: ("rate_limit_exceeded", "Too many requests."),
        }
        code, message = mapping.get(
            exc.code, ("http_error", "The request could not be processed.")
        )
        return error_response(code, message, exc.code or 500)

    @app.errorhandler(Exception)
    def _unexpected_error(exc):
        # Deliberately omit exception text, SQL, paths, and request data.
        app.logger.error(
            "Unhandled reseller API error type=%s request_id=%s",
            type(exc).__name__,
            getattr(g, "request_id", ""),
        )
        return error_response(
            "internal_error",
            "An unexpected error occurred.",
            500,
        )
