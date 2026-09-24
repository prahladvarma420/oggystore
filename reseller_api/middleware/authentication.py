"""Bearer credential parsing and Flask request authentication."""

from flask import current_app, g, request

from reseller_api.context import ApiRequestContext
from reseller_api.errors import ApiError
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.credentials import parse_authorization_header


_GENERIC_AUTH_MESSAGE = "Authentication credentials are missing or invalid."


def authenticate_request():
    parsed = parse_authorization_header(request.headers.get("Authorization", ""))
    if not parsed:
        raise ApiError("authentication_failed", _GENERIC_AUTH_MESSAGE, 401)

    key_id, secret = parsed
    principal = authenticate_credentials(
        key_id,
        secret,
        current_app.config["API_ENVIRONMENT"],
    )
    # Drop the local raw-secret reference before any controller runs.
    secret = None
    parsed = None
    if principal is None:
        raise ApiError("authentication_failed", _GENERIC_AUTH_MESSAGE, 401)

    g.api_context = ApiRequestContext(
        request_id=g.request_id,
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )


__all__ = ["authenticate_request", "parse_authorization_header"]
