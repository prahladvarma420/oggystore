"""Pure request-ID, redaction, and Phase-1 audit-log helpers."""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]+")
_FULL_API_CREDENTIAL = re.compile(
    r"\brk_(?:test|live)_[A-HJ-NP-Z2-9]{8}\.[A-Za-z0-9_-]{32,128}\b"
)
_LONG_SECRET_LIKE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,128}(?![A-Za-z0-9_-])")
_SENSITIVE_PARTS = (
    "buyer_email", "email", "authorization", "password", "passwd", "api_secret", "secret",
    "private_key", "wallet_credential", "payment_credential", "token",
    "fulfillment_config", "fulfillment_ref", "supplier_reference", "api_key", "hmac", "supplier_credential",
)
_EMAIL_VALUE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REDACTED = "[REDACTED]"


def generate_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_log_text(value, limit=300, redact_tokens=True) -> str:
    text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
    text = _EMAIL_VALUE.sub(REDACTED, text)
    text = _BEARER_VALUE.sub("Bearer " + REDACTED, text)
    if redact_tokens:
        text = _FULL_API_CREDENTIAL.sub(REDACTED, text)
        text = _LONG_SECRET_LIKE.sub(REDACTED, text)
    return text[:max(1, int(limit))]


def redact_sensitive(value):
    """Recursively redact sensitive named fields for any future safe logs.

    Request bodies and headers are not logged by the current middleware at all;
    this remains a defense-in-depth helper for structured metadata.
    """
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_PARTS):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact_sensitive(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return sanitize_log_text(value, 1000)
    return value


@dataclass(frozen=True)
class ApiAuditRecord:
    request_id: str
    method: str
    endpoint: str
    status_code: int
    response_ms: int
    timestamp: str
    reseller_id: Optional[int] = None
    api_key_id: Optional[int] = None
    reseller_public_id: Optional[str] = None
    key_id: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None

    def structured_fields(self):
        return {
            "event": "api_request",
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "reseller": self.reseller_public_id,
            "api_key": self.key_id,
            "method": self.method,
            "endpoint": self.endpoint,
            "status": self.status_code,
            "response_ms": self.response_ms,
            "ip": self.ip,
            "user_agent": self.user_agent,
        }


def build_audit_record(request_id: str, method: str, endpoint: str,
                       status_code: int, response_ms: int,
                       context: ApiRequestContext = None, ip: str = None,
                       user_agent: str = None):
    return ApiAuditRecord(
        request_id=sanitize_log_text(request_id, 64, redact_tokens=False),
        method=sanitize_log_text(method, 12).upper(),
        endpoint=sanitize_log_text(endpoint, 300),
        status_code=int(status_code),
        response_ms=max(0, int(response_ms)),
        timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        reseller_id=(context.reseller_id if context else None),
        api_key_id=(context.api_key_id if context else None),
        reseller_public_id=(context.reseller_public_id if context else None),
        key_id=(context.api_key_public_id if context else None),
        ip=sanitize_log_text(ip, 64) or None,
        user_agent=sanitize_log_text(user_agent, 300) or None,
    )


def persist_audit_record(record: ApiAuditRecord) -> bool:
    """Best-effort persistence; observability can never fail the request."""
    try:
        rdb.log_api_request(
            record.reseller_id,
            record.api_key_id,
            record.endpoint,
            record.method,
            record.status_code,
            request_id=record.request_id,
            response_ms=record.response_ms,
            reseller_public_id=record.reseller_public_id,
            key_id=record.key_id,
            ip=record.ip,
            user_agent=record.user_agent,
        )
        return True
    except Exception:
        return False


__all__ = [
    "ApiAuditRecord", "REDACTED", "build_audit_record",
    "generate_request_id", "persist_audit_record", "redact_sensitive",
    "sanitize_log_text",
]
