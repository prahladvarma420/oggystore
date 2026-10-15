"""Server-side master API kill-switch middleware."""

from reseller_api.errors import ApiError
from reseller_api.services.security_controls import is_global_api_enabled


def enforce_global_api_control():
    if not is_global_api_enabled():
        raise ApiError(
            "api_unavailable",
            "The reseller API is currently unavailable.",
            503,
            headers={"Retry-After": "60"},
        )


__all__ = ["enforce_global_api_control"]
