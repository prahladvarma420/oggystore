"""API versioning & deprecation primitives — Phase 13 Prompt 3.

The project's versioning strategy is URL-path prefixing, one Flask blueprint
per version: v1 lives at reseller_api.routes.v1 mounted on "/api/v1".  This
module formalizes that strategy without adding a second mechanism:

  * CURRENT_VERSION / SUPPORTED_VERSIONS — the single source of truth for
    "which versions exist" (drives the X-API-Version response header).
  * apply_version_headers(response) — after_request hook that stamps every
    versioned response with X-API-Version, plus Deprecation / Sunset / Link
    headers when the matched endpoint is registered in DEPRECATED_ENDPOINTS.
  * DEPRECATED_ENDPOINTS — the deprecation registry.  Empty today; entries
    follow RFC 8594 (Sunset) and RFC 9745 (Deprecation) semantics and always
    point at a migration path in RESELLER_API_VERSIONING.md.

Breaking-change policy (what must NEVER change inside /api/v1):
  * removing or renaming response fields (additive-only)
  * changing a field's meaning, type, or unit (e.g. price is a "12.34"
    decimal string in USDT; quantity is an integer)
  * changing public ID formats (ORD-%08d, PRD-*, WHK-%08d, RSR-*)
  * changing the authentication scheme (Bearer key_id.secret)
  * changing existing HTTP status codes on established flows
    (201 create / 200 replay / 402 insufficient balance / 422 stock /
    429 rate+quota / 404 uniform not-found)
  * changing order semantics (idempotent replay, server-side pricing)
Any of the above requires a new blueprint at /api/v2; v1 stays mounted and
unchanged until its announced sunset.
"""

from email.utils import format_datetime
from datetime import datetime, timezone

from flask import request

# ---------------------------------------------------------------------------
# Version registry
# ---------------------------------------------------------------------------

CURRENT_VERSION = "v1"
SUPPORTED_VERSIONS = ("v1",)          # v2 mounts as its own blueprint later
VERSION_HEADER = "X-API-Version"

# ---------------------------------------------------------------------------
# Deprecation registry
# ---------------------------------------------------------------------------
# Key:   "METHOD /api/v1/<path>" (method + absolute path, no query string)
# Value: {"sunset": "YYYY-MM-DD" or None,
#         "replacement": "METHOD /api/v2/<path>" or human note,
#         "reason": short text}
#
# Empty by design: nothing is deprecated today.  The machinery exists so a
# future deprecation is a one-line registry entry, not a code change.
DEPRECATED_ENDPOINTS: dict = {}

_DOCS_LINK = '<https://github.com/prahladvarma420/oggystore/blob/main/RESELLER_API_VERSIONING.md>; rel="deprecation"'


def _sunset_http_date(iso_date: str) -> str:
    """'2027-01-31' -> RFC 1123 HTTP-date at end of day UTC."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )
    return format_datetime(dt, usegmt=True)


def lookup_deprecation(method: str, path: str):
    """Return the registry entry for this request, or None."""
    if not DEPRECATED_ENDPOINTS:
        return None
    key = f"{method.upper()} {path}"
    return DEPRECATED_ENDPOINTS.get(key)


def apply_version_headers(response):
    """Stamp X-API-Version on every v1 response; deprecation headers when
    the endpoint is registered in DEPRECATED_ENDPOINTS.

    Wired as a blueprint after_request hook next to apply_rate_limit_headers.
    Never fails the request.
    """
    try:
        response.headers[VERSION_HEADER] = CURRENT_VERSION
        entry = lookup_deprecation(request.method, request.path)
        if entry:
            response.headers["Deprecation"] = "true"   # RFC 9745
            if entry.get("sunset"):
                response.headers["Sunset"] = _sunset_http_date(entry["sunset"])
            response.headers["Link"] = _DOCS_LINK
            if entry.get("replacement"):
                response.headers["X-API-Replacement"] = str(entry["replacement"])
    except Exception:
        # Header stamping must never break a response.
        pass
    return response


__all__ = [
    "CURRENT_VERSION",
    "SUPPORTED_VERSIONS",
    "VERSION_HEADER",
    "DEPRECATED_ENDPOINTS",
    "apply_version_headers",
    "lookup_deprecation",
]
