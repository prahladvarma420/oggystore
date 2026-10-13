"""Uncached global API security controls backed by Web Admin settings.

The kill-switch value is cached per-process for up to _CACHE_TTL_SECONDS.

Design rationale
----------------
The kill-switch (reseller_api_global_enabled) is a coarse operational toggle
that changes extremely rarely (deliberate admin action).  Without a cache,
every API request causes one extra DB read before the route handler runs,
adding latency and unnecessary load.

The short TTL (30 s) means:
  * All processes reflect a change within 30 seconds of it being written.
  * A DB outage causes fail-closed behaviour after at most one cached window.
  * Process-local memory is ONLY used for this configuration toggle — not for
    any rate-limit counter.  All quota counters remain in the shared DB.
"""

from __future__ import annotations

import threading
import time

import database.db as botdb

GLOBAL_API_SETTING = "reseller_api_global_enabled"

_CACHE_TTL_SECONDS: float = 30.0
_cache_lock = threading.Lock()
_cached_value: bool = False
_cached_at: float = 0.0          # monotonic seconds; 0.0 means "never cached"


def _read_from_db() -> bool:
    """Read kill-switch state from the DB.  Returns False on any error (fail closed)."""
    try:
        value = str(botdb.get_setting(GLOBAL_API_SETTING, "0") or "0").strip().lower()
        return value in ("1", "true", "yes", "on")
    except Exception:
        return False


def is_global_api_enabled() -> bool:
    """Return the current global kill-switch state.

    Reads from process-local cache when fresh (< 30 s old).  Cache miss or
    stale cache triggers a DB re-read and updates the cache.

    Always fails closed: if the DB is unreachable and the cache has expired,
    this returns False (API unavailable) rather than defaulting to open.
    """
    global _cached_value, _cached_at

    now = time.monotonic()
    with _cache_lock:
        if now - _cached_at < _CACHE_TTL_SECONDS:
            return _cached_value

    # Perform the DB read outside the lock so parallel threads can still serve
    # the (stale) cached value during the refresh read.
    fresh = _read_from_db()

    with _cache_lock:
        _cached_value = fresh
        _cached_at = time.monotonic()

    return fresh


def invalidate_kill_switch_cache() -> None:
    """Force the next is_global_api_enabled() call to re-read from the DB.

    Intended for tests and for admin tooling that changes the setting and
    needs the cache to reflect the change immediately.
    """
    global _cached_at
    with _cache_lock:
        _cached_at = 0.0


__all__ = ["GLOBAL_API_SETTING", "invalidate_kill_switch_cache", "is_global_api_enabled"]
