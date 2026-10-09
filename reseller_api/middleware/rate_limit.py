"""Flask request-rate middleware for the v1 API."""

from flask import current_app, g

from reseller_api.errors import ApiError
from reseller_api.services.rate_limit import evaluate_request_rate


def enforce_request_rate_limit():
    context = getattr(g, "api_context", None)
    decision = evaluate_request_rate(
        context,
        current_app.config["API_ENVIRONMENT"],
    )
    g.request_rate_limit = decision
    if decision.allowed:
        return None
    if decision.reason == "identity_not_authorized":
        raise ApiError(
            "authorization_denied", "This operation is not permitted.", 403
        )
    if decision.reason == "rate_limiter_unavailable":
        raise ApiError(
            "service_unavailable",
            "The API is temporarily unavailable.",
            503,
        )
    raise ApiError(
        "rate_limit_exceeded",
        "Too many requests.",
        429,
        headers=decision.headers(include_retry=True),
    )


def apply_rate_limit_headers(response):
    decision = getattr(g, "request_rate_limit", None)
    if decision is not None and (
        decision.allowed or decision.reason == "rate_limit_exceeded"
    ):
        for name, value in decision.headers(
            include_retry=(not decision.allowed and decision.reason == "rate_limit_exceeded")
        ).items():
            response.headers[name] = value
    return response


__all__ = ["apply_rate_limit_headers", "enforce_request_rate_limit"]
