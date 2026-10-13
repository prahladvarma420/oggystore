"""Webhook HMAC-SHA256 signing and verification — Phase 9 Prompt 6.

This module is the single source of truth for:
  * signing outbound webhook requests (used by the delivery worker)
  * verifying inbound webhook requests (SDK reference / test helper)

Signature scheme
----------------
  algorithm : HMAC-SHA256
  signed string : "{unix_timestamp}.{raw_request_body_bytes}"
  header format : "sha256=<lowercase_hex_digest>"

  This matches the well-understood Stripe / GitHub webhook convention.
  The signed string is: the UNIX epoch timestamp (integer, seconds),
  a literal dot, then the exact bytes of the request body as transmitted
  — never a reformatted or re-encoded representation.

Headers sent on every delivery
-------------------------------
  X-Oggystore-Signature-256  : sha256=<hmac_hex>
  X-Oggystore-Timestamp      : <unix epoch seconds, string>
  X-Oggystore-Delivery       : <UUID delivery_id for replay detection>
  X-Oggystore-Event          : <event_type, e.g. "order.created">

Replay protection
-----------------
  Receivers SHOULD:
    1. Reject requests whose X-Oggystore-Timestamp is older than
       FRESHNESS_WINDOW_SECONDS (default 300 s / 5 min).
    2. Cache delivery_id values for FRESHNESS_WINDOW_SECONDS and reject
       duplicates (delivery_id is a UUID per attempt, not per event).
    3. Use event_id (in the JSON body) for idempotent business logic.

Secret security
---------------
  * Secrets are generated with secrets.token_urlsafe(32) (256-bit entropy).
  * Secrets are stored encrypted at rest (AES via utils.crypto).
  * Secrets are decrypted in memory immediately before signing.
  * Secrets are NEVER logged, returned in API responses after creation,
    or included in error messages.
  * Signature comparison uses hmac.compare_digest (constant-time).

Secret rotation
---------------
  Call rotate_webhook_secret() (in webhook_config.py) to replace the
  signing secret.  The old secret is immediately invalidated.  Any
  in-flight deliveries signed with the old secret will fail verification
  on the reseller side; the delivery worker will retry normally.
  The new secret is returned once in plaintext and must be stored by
  the reseller immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

# ---------------------------------------------------------------------------
# Header names  (used by sender and documented for receivers)
# ---------------------------------------------------------------------------

HEADER_SIGNATURE  = "X-Oggystore-Signature-256"  # sha256=<hex>
HEADER_TIMESTAMP  = "X-Oggystore-Timestamp"       # unix epoch (str)
HEADER_DELIVERY   = "X-Oggystore-Delivery"        # UUID delivery_id
HEADER_EVENT      = "X-Oggystore-Event"           # event_type string

# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------

FRESHNESS_WINDOW_SECONDS: int = 300  # 5 minutes; reject older timestamps
ALGORITHM                     = "sha256"
SIGNATURE_PREFIX               = f"{ALGORITHM}="

# ---------------------------------------------------------------------------
# Signing (sender side)
# ---------------------------------------------------------------------------


def build_signed_string(body_bytes: bytes, timestamp: str) -> bytes:
    """Return the exact bytes that are HMAC-signed.

    Format: b"{timestamp}.{body_bytes}"
    Both components are included verbatim so the receiver can reconstruct
    the same string from the headers and raw body.
    """
    return timestamp.encode("ascii") + b"." + body_bytes


def sign_body(secret: str, body_bytes: bytes, timestamp: str) -> str:
    """Sign *body_bytes* with *secret* and return 'sha256=<hex>'.

    Args:
        secret     : plaintext signing secret (high-entropy, from DB after decryption)
        body_bytes : exact bytes that will be transmitted as the request body
        timestamp  : Unix epoch as a decimal string (str(int(time.time())))

    Returns:
        Signature string in the form 'sha256=<lowercase hex digest>'

    Security notes:
        * HMAC key is the UTF-8 encoding of *secret*.
        * The signed payload is b"{timestamp}.{body_bytes}" — including the
          timestamp prevents pre-computation attacks.
        * This function does not log, store, or return the secret.
    """
    signed = build_signed_string(body_bytes, timestamp)
    digest = hmac.new(
        secret.encode("utf-8"),
        signed,
        hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def current_timestamp() -> str:
    """Return the current Unix epoch as a decimal string."""
    return str(int(time.time()))


# ---------------------------------------------------------------------------
# Verification (receiver side — also used in tests)
# ---------------------------------------------------------------------------


def is_fresh(timestamp_str: str, max_age: int = FRESHNESS_WINDOW_SECONDS) -> bool:
    """Return True when *timestamp_str* is within the allowed freshness window.

    Rejects both excessively old timestamps (replay) and timestamps that are
    too far in the future (clock skew / malformed input).

    Args:
        timestamp_str : the value of the X-Oggystore-Timestamp header
        max_age       : maximum allowed age in seconds (default 300)

    Returns:
        True if the timestamp is within (-max_age, +max_age) of now.
    """
    try:
        ts = int(timestamp_str)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    delta = abs(now - ts)
    return delta <= max_age


def verify_signature(
    secret: str,
    body_bytes: bytes,
    timestamp_str: str,
    presented_signature: str,
    *,
    check_freshness: bool = True,
    max_age: int = FRESHNESS_WINDOW_SECONDS,
) -> bool:
    """Verify a webhook signature sent by OggyStore.

    This is the reference implementation for receivers.  It is also used
    by tests to confirm the sender side produces correct output.

    Args:
        secret               : the webhook signing secret (plaintext)
        body_bytes           : the raw request body bytes (must NOT be decoded/re-encoded)
        timestamp_str        : value of the X-Oggystore-Timestamp header
        presented_signature  : value of the X-Oggystore-Signature-256 header
        check_freshness      : if True (default), reject timestamps outside max_age
        max_age              : freshness window in seconds (default 300)

    Returns:
        True if and only if the signature is valid AND the timestamp is fresh.

    Security:
        * Uses hmac.compare_digest — constant-time; immune to timing attacks.
        * Freshness check happens BEFORE the HMAC computation so stale
          requests are cheaply rejected.
        * An absent or malformed presented_signature always returns False.
    """
    if not timestamp_str or not presented_signature:
        return False

    if check_freshness and not is_fresh(timestamp_str, max_age):
        return False

    expected = sign_body(secret, body_bytes, timestamp_str)

    # Constant-time comparison — immune to timing side-channels
    return hmac.compare_digest(
        expected.encode("ascii"),
        presented_signature.encode("ascii"),
    )


def extract_headers(headers: dict) -> dict:
    """Pull the four OggyStore webhook headers from an HTTP request headers dict.

    Header lookup is case-insensitive.

    Returns a dict with keys: signature, timestamp, delivery_id, event_type.
    Values are None when the header is absent.
    """
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    return {
        "signature":   lower.get(HEADER_SIGNATURE.lower()),
        "timestamp":   lower.get(HEADER_TIMESTAMP.lower()),
        "delivery_id": lower.get(HEADER_DELIVERY.lower()),
        "event_type":  lower.get(HEADER_EVENT.lower()),
    }


# ---------------------------------------------------------------------------
# Secret generation (called by webhook_config / reseller_db)
# ---------------------------------------------------------------------------


def generate_webhook_secret() -> str:
    """Return a cryptographically secure random webhook signing secret.

    Uses secrets.token_urlsafe(32) — 256 bits of entropy, URL-safe base64.
    This replaces any ad-hoc token generation in earlier code.
    """
    import secrets as _secrets
    return _secrets.token_urlsafe(32)


__all__ = [
    # Header name constants
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "HEADER_DELIVERY",
    "HEADER_EVENT",
    # Security constants
    "FRESHNESS_WINDOW_SECONDS",
    "ALGORITHM",
    "SIGNATURE_PREFIX",
    # Signing
    "build_signed_string",
    "sign_body",
    "current_timestamp",
    # Verification
    "is_fresh",
    "verify_signature",
    "extract_headers",
    # Secret generation
    "generate_webhook_secret",
]
