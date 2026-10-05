"""Bearer credential parsing and Flask request authentication.

Phase 10 Prompt 2: Adds IP-based brute-force protection via a DB-backed
fixed-window failure counter before credentials are checked.

Flow
----
1. Resolve client IP (honours RESELLER_API_TRUST_PROXY).
2. Check IP auth-failure rate (DB counter).  If limit reached → 429.
3. Parse the Authorization header.  Malformed → increment failure counter, 401.
4. Authenticate credentials against DB.  Bad match → increment failure counter, 401.
5. On success, build ApiRequestContext and store in g.api_context.

The failure counter is incremented on every unsuccessful auth attempt
(parse failure, bad credentials, revoked key, blocked reseller).
A successful authentication does NOT reset the counter — the fixed-window
expires naturally at the end of the current minute.

Security invariants
-------------------
* The raw secret is cleared immediately after use (secret = None).
* Auth error messages are generic — no reason for the failure is revealed.
* The 429 brute-force block is keyed on the (hashed) source IP only,
  matching the unauthenticated nature of the surface being protected.
"""

from flask import current_app, g, request

from reseller_api.context import ApiRequestContext
from reseller_api.errors import ApiError
from reseller_api.services.auth_rate_limit import (
    AUTH_FAILURE_LIMIT_PER_MINUTE,
    check_ip_auth_failure_rate,
    record_ip_auth_failure,
    seconds_until_auth_reset,
)
from reseller_api.services.authentication import authenticate_credentials
from reseller_api.services.credentials import parse_authorization_header
from reseller_api.services import security_logger as sec_log


_GENERIC_AUTH_MESSAGE = "Authentication credentials are missing or invalid."


def _request_ip() -> str:
    """Return the client IP, honouring RESELLER_API_TRUST_PROXY.

    When the config flag is True, the leftmost value of X-Forwarded-For is
    used.  This must only be enabled when Railway/a trusted proxy is the
    sole path to the API; otherwise clients can spoof the header.
    """
    candidate = request.remote_addr or "unknown"
    if current_app.config.get("RESELLER_API_TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Railway/trusted proxy appends the real client IP on the RIGHT.
            # Using the rightmost value prevents a client from spoofing the
            # header by injecting a fabricated leftmost IP address.
            candidate = forwarded.rsplit(",", 1)[-1].strip()
    return candidate or "unknown"


def authenticate_request():
    """Authenticate one API request.  Raises ApiError on any failure."""
    ip = _request_ip()

    # ------------------------------------------------------------------
    # Step 1: IP brute-force check (DB-backed, distributed across instances)
    # ------------------------------------------------------------------
    allowed, failure_count = check_ip_auth_failure_rate(ip)
    if not allowed:
        sec_log.emit(
            sec_log.EVT_BRUTE_FORCE_BLOCKED,
            ip=ip,
            user_agent=(request.headers.get("User-Agent") or "")[:200],
            description=f"IP blocked after {failure_count} auth failures",
        )
        raise ApiError(
            "rate_limit_exceeded",
            "Too many requests.",
            429,
            headers={"Retry-After": str(seconds_until_auth_reset())},
        )

    # ------------------------------------------------------------------
    # Step 2: Parse Authorization header
    # ------------------------------------------------------------------
    parsed = parse_authorization_header(request.headers.get("Authorization", ""))
    if not parsed:
        record_ip_auth_failure(ip)
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, ip=ip,
            user_agent=(request.headers.get("User-Agent") or "")[:200],
            description="malformed or missing Authorization header")
        raise ApiError("authentication_failed", _GENERIC_AUTH_MESSAGE, 401)

    key_id, secret = parsed

    # ------------------------------------------------------------------
    # Step 3: Credential verification (constant-time secret comparison)
    # ------------------------------------------------------------------
    principal = authenticate_credentials(
        key_id,
        secret,
        current_app.config["API_ENVIRONMENT"],
    )
    # Drop raw secret from local scope immediately — never let it leak into
    # tracebacks, logs, or responses.
    secret = None
    parsed = None

    if principal is None:
        record_ip_auth_failure(ip)
        sec_log.emit(sec_log.EVT_AUTH_FAILURE, ip=ip,
            user_agent=(request.headers.get("User-Agent") or "")[:200],
            description="credential verification failed")
        raise ApiError("authentication_failed", _GENERIC_AUTH_MESSAGE, 401)

    # ------------------------------------------------------------------
    # Step 4: Build request context (success path — no failure recorded)
    # ------------------------------------------------------------------
    g.api_context = ApiRequestContext(
        request_id=g.request_id,
        environment=principal.environment,
        reseller_id=principal.reseller_id,
        reseller_public_id=principal.reseller_public_id,
        api_key_id=principal.api_key_id,
        api_key_public_id=principal.api_key_public_id,
    )


__all__ = ["authenticate_request", "parse_authorization_header"]
