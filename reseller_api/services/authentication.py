"""Framework-neutral reseller API-key authentication service.

This module is intentionally Flask-free so it can be unit-tested without
a running application context.  All Flask-specific wiring lives in
``reseller_api.middleware.authentication``.

Security invariants
-------------------
* The raw secret is used only inside ``authenticate_credentials`` and is
  never stored, logged, or returned to callers.
* Timing-safe comparison (``secrets.compare_digest``) is always performed
  even when the key_id lookup fails, preventing timing side-channels.
* Every failure path returns ``None``; callers must not distinguish between
  bad key, bad secret, revoked key, disabled reseller, etc.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

import database.reseller_db as rdb
from reseller_api.context import AuthenticatedPrincipal

# Used for the dummy comparison when a key_id lookup returns nothing,
# so the compare_digest call always takes the same time.
_DUMMY_HASH: str = hashlib.sha256(b"invalid-api-credential-dummy").hexdigest()


def _hash_secret(raw: str) -> str:
    """SHA-256 hex digest of the raw secret string."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def authenticate_credentials(
    key_id: str,
    secret: str,
    expected_environment: str,
) -> Optional[AuthenticatedPrincipal]:
    """Return an ``AuthenticatedPrincipal`` or ``None`` for every failure.

    The deliberately uniform ``None`` result prevents callers from inferring
    whether a key ID, secret, revocation status, environment mismatch, or
    disabled reseller caused authentication to fail.

    Parameters
    ----------
    key_id:
        The public key identifier supplied by the caller (e.g. ``rk_test_…``).
    secret:
        The private secret supplied by the caller.  It is compared in
        constant time and then discarded.
    expected_environment:
        The environment that this API server instance is configured for
        (e.g. ``"test"`` or ``"production"``).
    """
    # Fast-path: empty credentials cannot possibly authenticate.
    if not key_id or not secret:
        return None

    # --- Step 1: fetch the stored auth record --------------------------------
    record = rdb.get_api_auth_record(key_id)
    stored_hash: str = record["secret_hash"] if record else _DUMMY_HASH

    # --- Step 2: constant-time secret comparison (always runs) ---------------
    secret_matches: bool = secrets.compare_digest(stored_hash, _hash_secret(secret))

    # --- Step 3: validate every field; any failure returns None --------------
    if not record or not secret_matches:
        return None
    if record["key_status"] != "active":
        return None
    if not bool(record["key_enabled"]):
        return None
    if record["reseller_status"] != "active":
        return None
    if not bool(record["api_enabled"]):
        return None
    if bool(record["user_blocked"]):
        return None
    if record["environment"] != expected_environment:
        return None

    # --- Step 4: build the principal -----------------------------------------
    principal = AuthenticatedPrincipal(
        environment=record["environment"],
        reseller_id=int(record["reseller_id"]),
        reseller_public_id=str(record["reseller_public_id"]),
        api_key_id=int(record["api_key_id"]),
        api_key_public_id=str(record["key_id"]),
    )

    # --- Step 5: best-effort usage telemetry (non-transactional) -------------
    # Authentication succeeds even if this write fails.
    try:
        rdb.touch_api_key_used_throttled(principal.api_key_id)
    except Exception:  # noqa: BLE001
        pass

    return principal


__all__ = ["authenticate_credentials"]
