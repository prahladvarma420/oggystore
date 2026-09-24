"""Framework-neutral reseller API-key authentication service."""

import hashlib
import secrets

import database.reseller_db as rdb
from reseller_api.context import AuthenticatedPrincipal


_DUMMY_HASH = hashlib.sha256(b"invalid-api-credential").hexdigest()


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def authenticate_credentials(key_id: str, secret: str, expected_environment: str):
    """Return an authenticated principal or ``None`` for every failure.

    The deliberately uniform result prevents callers from revealing whether a
    key, reseller, status, environment, or secret caused authentication to
    fail. The raw secret is used only for this constant-time comparison.
    """
    record = rdb.get_api_auth_record(key_id)
    stored_hash = record["secret_hash"] if record else _DUMMY_HASH
    secret_matches = secrets.compare_digest(stored_hash, _hash_secret(secret))

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

    principal = AuthenticatedPrincipal(
        environment=record["environment"],
        reseller_id=int(record["reseller_id"]),
        reseller_public_id=str(record["reseller_public_id"]),
        api_key_id=int(record["api_key_id"]),
        api_key_public_id=str(record["key_id"]),
    )

    # One atomic conditional update per minute at most. Authentication remains
    # successful if usage telemetry cannot be written; request services still
    # perform their own transactional writes later.
    try:
        rdb.touch_api_key_used_throttled(principal.api_key_id)
    except Exception:
        pass
    return principal


__all__ = ["authenticate_credentials"]
