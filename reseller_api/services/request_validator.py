
"""Request validation helpers — Phase 10 Prompt 5.

Centralised constants and validators for all string-length, array-size,
pagination, and field-allowlist checks across the reseller API.

This module intentionally does NOT import Flask so it remains testable
without an HTTP application context.
"""
from __future__ import annotations
import re
from typing import Any, Optional


class ValidationError(Exception):
    """Raised by validators when a field fails validation."""
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = code; self.message = message; self.status = status
    def to_api_error(self):
        """Convert to ApiError inside a Flask request context."""
        from reseller_api.errors import ApiError
        return ApiError(self.code, self.message, self.status)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_CLIENT_ORDER_ID_LEN  = 128
MAX_PRODUCT_ID_LEN       = 64
MAX_WEBHOOK_URL_LEN      = 2048
MAX_SEARCH_LEN           = 256
MAX_PAGE_SIZE            = 100
DEFAULT_PAGE_SIZE        = 20
MAX_EVENTS_ARRAY_LEN     = 50
MAX_METADATA_FIELDS      = 20
MAX_METADATA_VALUE_LEN   = 512
MAX_NOTE_LEN             = 4096

_ALLOWED_SORT_FIELDS = frozenset({
    "created_at", "updated_at", "id", "status", "quantity", "total",
    "created", "updated",
})
_ALLOWED_FILTER_FIELDS = frozenset({
    "status", "product_id", "environment", "created_after", "created_before",
    "enabled",
})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Validators (raise ValidationError, not ApiError)
# ---------------------------------------------------------------------------

def validate_string_length(value, field, max_len, required=False):
    if value is None:
        if required: raise ValidationError("missing_field", f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise ValidationError("invalid_field", f"{field} must be a string.")
    stripped = value.strip()
    if len(stripped) > max_len:
        raise ValidationError("field_too_long", f"{field} must not exceed {max_len} characters.")
    if required and not stripped:
        raise ValidationError("missing_field", f"{field} must not be empty.")
    return stripped or None


def validate_page_size(raw):
    if raw is None: return DEFAULT_PAGE_SIZE
    try: size = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("invalid_parameter", "per_page must be an integer.")
    if size < 1: raise ValidationError("invalid_parameter", "per_page must be at least 1.")
    if size > MAX_PAGE_SIZE: raise ValidationError("invalid_parameter", f"per_page must not exceed {MAX_PAGE_SIZE}.")
    return size


def validate_sort_field(raw):
    if raw is None: return None
    if not isinstance(raw, str): raise ValidationError("invalid_parameter", "sort must be a string.")
    clean = raw.strip().lower().lstrip("-")
    if clean not in _ALLOWED_SORT_FIELDS:
        raise ValidationError("invalid_parameter", f"Invalid sort field: {raw!r}.")
    return raw.strip()


def validate_filter_field(key):
    if key.lower() not in _ALLOWED_FILTER_FIELDS:
        raise ValidationError("invalid_parameter", f"Unsupported filter field: {key!r}.")


def validate_date_string(value, field):
    if value is None: return None
    if not isinstance(value, str):
        raise ValidationError("invalid_field", f"{field} must be a date string (YYYY-MM-DD).")
    if not _DATE_RE.match(value.strip()):
        raise ValidationError("invalid_field", f"{field} must be in YYYY-MM-DD format.")
    return value.strip()


def validate_events_array(events):
    if events is None: return []
    if not isinstance(events, list):
        raise ValidationError("invalid_field", "events must be an array.")
    if len(events) > MAX_EVENTS_ARRAY_LEN:
        raise ValidationError("field_too_long", f"events array must not exceed {MAX_EVENTS_ARRAY_LEN} items.")
    return events


def reject_unexpected_fields(body, allowed):
    if not isinstance(body, dict): return
    unexpected = set(body.keys()) - allowed
    if unexpected:
        raise ValidationError("unexpected_fields",
                              f"Unexpected fields in request: {', '.join(sorted(unexpected))}.")


__all__ = [
    "ValidationError",
    "MAX_CLIENT_ORDER_ID_LEN", "MAX_PRODUCT_ID_LEN", "MAX_WEBHOOK_URL_LEN",
    "MAX_SEARCH_LEN", "MAX_PAGE_SIZE", "DEFAULT_PAGE_SIZE", "MAX_EVENTS_ARRAY_LEN",
    "validate_string_length", "validate_page_size", "validate_sort_field",
    "validate_filter_field", "validate_date_string", "validate_events_array",
    "reject_unexpected_fields",
]
