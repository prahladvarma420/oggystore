"""Pure credential parser shared by HTTP middleware and security tests."""

import re


_KEY_ID_RE = re.compile(r"^rk_(?:test|live)_[A-HJ-NP-Z2-9]{8}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def parse_authorization_header(value: str):
    """Parse ``Authorization: Bearer <key_id>.<secret>`` or return ``None``."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    parts = value.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1]
    if token.count(".") != 1:
        return None
    key_id, secret = token.split(".", 1)
    if not _KEY_ID_RE.fullmatch(key_id) or not _SECRET_RE.fullmatch(secret):
        return None
    return key_id, secret


__all__ = ["parse_authorization_header"]
