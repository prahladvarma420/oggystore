"""Endpoint-category rate-limit middleware — Phase 10 Prompt 3.

Provides a decorator that wraps individual view functions and enforces
the per-category per-reseller minute-window limit defined in
reseller_api.services.endpoint_rate_limit.

Usage in route registrations (routes/v1/__init__.py)::

    from reseller_api.middleware.endpoint_rate_limit import endpoint_category
    from reseller_api.services.endpoint_rate_limit import CATEGORY_READ

    # wrap the view function before add_url_rule
    list_products = endpoint_category(CATEGORY_READ)(list_products)

The decorator must be applied AFTER the @require_operation decorator so that
auth context (g.api_context) is guaranteed to exist at the time of the limit
check.  Because the before_request hooks already run authenticate_request()
before any view, the decorator wrapper running inside the view function body
always has a valid api_context available.
"""

from functools import wraps

from flask import g

from reseller_api.errors import ApiError
from reseller_api.services.endpoint_rate_limit import check_endpoint_rate
from reseller_api.services import security_logger as sec_log


def endpoint_category(category: str, limit: int = None):
    """Decorator factory that applies an endpoint-category rate limit.

    Args:
        category: One of the CATEGORY_* constants.
        limit:    Override per-minute limit (None = use DEFAULT_LIMITS).

    Returns:
        A decorator that wraps a view function.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            context = getattr(g, "api_context", None)
            if context is not None:  # paranoia — auth always runs first
                decision = check_endpoint_rate(context, category, limit)
                if not decision.allowed:
                    sec_log.emit(
                        sec_log.EVT_RATE_LIMIT_EXCEEDED,
                        reseller_id=context.reseller_id,
                        reseller_public_id=context.reseller_public_id,
                        description=f"category={decision.category} limit={decision.limit}/min",
                        hit_count=decision.count,
                    )
                    raise ApiError(
                        "rate_limit_exceeded",
                        f"Too many requests for this operation type. "
                        f"Category: {decision.category}.",
                        429,
                        headers=decision.headers(include_retry=True),
                    )
            return fn(*args, **kwargs)
        return wrapper
    return decorator


__all__ = ["endpoint_category"]
