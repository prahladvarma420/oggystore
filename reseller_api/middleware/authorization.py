"""Reusable Flask authorization decorators for future v1 controllers."""

from functools import wraps

from flask import current_app, g

from reseller_api.errors import ApiError
from reseller_api.services.authorization import authorize_operation


_GENERIC_DENIED_MESSAGE = "This operation is not permitted."


def get_authenticated_context():
    context = getattr(g, "api_context", None)
    if context is None:
        raise ApiError(
            "authentication_failed",
            "Authentication credentials are missing or invalid.",
            401,
        )
    return context


def authorize_current_operation(operation: str):
    context = get_authenticated_context()
    decision = authorize_operation(
        context,
        operation,
        current_app.config["API_ENVIRONMENT"],
    )
    if not decision.allowed:
        # Internal policy reasons are not exposed, preventing status, limit,
        # ownership, and resource-state oracles.
        raise ApiError("authorization_denied", _GENERIC_DENIED_MESSAGE, 403)
    g.authorization = decision
    return decision


def require_operation(operation: str):
    """Decorator for future endpoints; no route is added in this prompt."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            authorize_current_operation(operation)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


__all__ = [
    "authorize_current_operation", "get_authenticated_context",
    "require_operation",
]
