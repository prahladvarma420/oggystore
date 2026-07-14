"""Dependency-free TOTP (RFC 6238) generator.

Used to auto-generate the 6-digit Microsoft 2FA code from a stored
authenticator SETUP KEY (base32 secret), so the bot can sign in on its own
when a saved session expires — no manual session re-upload needed.
"""

import base64
import hashlib
import hmac
import struct
import time


def _b32_decode(secret: str) -> bytes:
    s = (secret or "").replace(" ", "").replace("-", "").upper()
    if not s:
        raise ValueError("empty 2FA secret")
    pad = (-len(s)) % 8
    s += "=" * pad
    return base64.b32decode(s, casefold=True)


def totp_now(secret: str, digits: int = 6, period: int = 30, algorithm: str = "sha1", at=None) -> str:
    """Return the current TOTP code for a base32 secret."""
    key = _b32_decode(secret)
    now = int(at if at is not None else time.time())
    counter = now // period
    msg = struct.pack(">Q", counter)
    digestmod = getattr(hashlib, algorithm)
    h = hmac.new(key, msg, digestmod).digest()
    offset = h[-1] & 0x0F
    binary = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def is_valid_secret(secret: str) -> bool:
    try:
        totp_now(secret)
        return True
    except Exception:
        return False
