"""IP-based authentication-failure rate limiting.

Protects the pre-auth surface from brute-force and credential-stuffing.

Algorithm: fixed-window per minute per IP (SHA-256 hashed — never plain).
Storage:    DB-backed `reseller_ip_auth_counters` table.
            One row per (ip_hash, minute_bucket). Fully distributed: any
            number of Railway/Gunicorn processes share the same DB and see
            the same counter values. No process-local state is used.

Limits (conservative defaults)
-------------------------------
AUTH_FAILURE_LIMIT_PER_MINUTE = 20
    20 failed authentication attempts per IP per minute before the IP is
    blocked for the remainder of that minute window.

    Rationale:
      * A legitimate client hits this 0-1 times/min (key rotation, typo).
      * An attacker scanning rk_test_XXXXXXXX needs >10^13 guesses;
        at 20/min they are throttled at the first window.
      * Multiple users behind NAT collectively fail <5 times/min in practice.

Burst behaviour
----------------
Fixed-window: the counter resets atomically when the minute rolls over.
No carry-over from the previous window. A burst of exactly 20 failures in
the last second of a window, then 20 more in the first second of the next
window, is possible but harmless — 40 failed attempts in 2 seconds is still
far below any realistic brute-force threshold.

Identity used: hashed source IP only (pre-auth; no reseller identity yet).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

import database.reseller_db as rdb
from utils.timezone import now as local_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUTH_FAILURE_LIMIT_PER_MINUTE: int = 20
"""Maximum failed auth attempts per source IP per 1-minute fixed window."""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_ip(ip: str) -> str:
    """One-way SHA-256 of the IP; only the first 32 hex chars are stored.

    Raw IPs are never written to reseller_ip_auth_counters.
    """
    return hashlib.sha256((ip or "unknown").encode("utf-8")).hexdigest()[:32]


def _minute_bucket(moment=None) -> tuple:
    """Return (bucket_str, seconds_until_next_minute)."""
    m = moment or local_now()
    bucket = m.strftime("%Y-%m-%d %H:%M")
    next_min = m.replace(second=0, microsecond=0) + timedelta(minutes=1)
    reset_after = max(1, int((next_min - m).total_seconds()))
    return bucket, reset_after


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_ip_auth_failure_rate(ip: str, moment=None) -> tuple:
    """Return (allowed, current_failure_count) for this IP in the current window.

    allowed=True  => the caller may proceed with the auth attempt.
    allowed=False => IP is blocked for this minute; caller should raise 429.

    This function does NOT increment the counter.  Call record_ip_auth_failure()
    only after a confirmed authentication failure.

    Failure mode: DB unavailability causes fail-open (allowed=True) so that
    a transient DB error does not block all legitimate traffic.  Logging covers
    this condition.
    """
    bucket, _ = _minute_bucket(moment)
    ip_hash = _hash_ip(ip)
    try:
        count = rdb.get_ip_auth_failure_count(ip_hash, bucket)
    except Exception:
        log.warning(
            "[auth_rate_limit] DB read error for ip_hash=%.8s; failing open",
            ip_hash,
        )
        return True, 0
    allowed = count < AUTH_FAILURE_LIMIT_PER_MINUTE
    return allowed, count


def record_ip_auth_failure(ip: str, moment=None) -> int:
    """Atomically increment the failure counter for this IP. Return new count.

    Called after every confirmed authentication failure (bad credentials,
    malformed token, etc.).  DB errors are swallowed and logged; a failed
    write must never surface as an error to the caller.
    """
    bucket, _ = _minute_bucket(moment)
    ip_hash = _hash_ip(ip)
    try:
        new_count = rdb.increment_ip_auth_failure(ip_hash, bucket)
        log.debug(
            "[auth_rate_limit] failure recorded ip_hash=%.8s bucket=%s count=%s",
            ip_hash, bucket, new_count,
        )
        return new_count
    except Exception:
        log.warning(
            "[auth_rate_limit] DB write error for ip_hash=%.8s; counter not incremented",
            ip_hash,
        )
        return 0


def seconds_until_auth_reset(moment=None) -> int:
    """Return seconds remaining in the current minute window."""
    _, reset_after = _minute_bucket(moment)
    return reset_after


__all__ = [
    "AUTH_FAILURE_LIMIT_PER_MINUTE",
    "check_ip_auth_failure_rate",
    "record_ip_auth_failure",
    "seconds_until_auth_reset",
]
