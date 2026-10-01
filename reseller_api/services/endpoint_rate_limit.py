"""Endpoint-specific rate limiting — Phase 10 Prompt 3.

Different API operations have different resource costs and abuse profiles.
This module enforces per-category, per-reseller minute-window limits that
are ADDITIVE to the global per-reseller request quota implemented in
Phase 10 Prompt 2.  Both must pass for a request to be allowed.

Categories based on the actual API surface
------------------------------------------
READ          — product listing/detail, order list/detail, balance,
                webhook list/detail, webhook deliveries list.
                Higher limit: safe, idempotent reads with no side effects.

DELIVERY_READ — GET /orders/<id>/delivery.  Contains encrypted delivery
                credentials. Separated so credential fetches are counted
                independently from ordinary reads.

ORDER_CREATE  — POST /orders.  Financially significant; protected against
                double-clicks, rapid retries, and automated floods.
                Does NOT replace existing idempotency / wallet / stock logic.

WEBHOOK_WRITE — POST/PATCH/DELETE webhooks, rotate-secret.  Write operations
                with lower limits than reads.

WEBHOOK_RETRY — POST /webhooks/…/retry, POST /webhooks/events/…/retry.
                Manual retry triggers must never be hammerable at scale.

Design
------
* Storage: DB-backed `reseller_endpoint_quotas` table.  Fully distributed
  across Railway/Gunicorn workers — no process-local state.
* Window: fixed one-minute window matching the global rate limiter.
* Fail-open: DB unavailability allows the request through and logs a warning.
  The global rate limiter is still active, so a complete DB outage still
  throttles via the outer layer.
* Cross-category isolation: each category has an independent counter row;
  heavy READ traffic cannot consume the ORDER quota and vice versa.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, Optional

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from utils.timezone import now as local_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category identifiers  (string keys used in the DB row)
# ---------------------------------------------------------------------------

CATEGORY_READ          = "read"
CATEGORY_DELIVERY_READ = "delivery_read"
CATEGORY_ORDER_CREATE  = "order_create"
CATEGORY_WEBHOOK_WRITE = "webhook_write"
CATEGORY_WEBHOOK_RETRY = "webhook_retry"

ALL_CATEGORIES = frozenset({
    CATEGORY_READ,
    CATEGORY_DELIVERY_READ,
    CATEGORY_ORDER_CREATE,
    CATEGORY_WEBHOOK_WRITE,
    CATEGORY_WEBHOOK_RETRY,
})

# ---------------------------------------------------------------------------
# Default limits (requests per minute, per reseller)
# ---------------------------------------------------------------------------

DEFAULT_LIMITS: dict[str, int] = {
    CATEGORY_READ:          300,   # Safe idempotent reads — generous allowance
    CATEGORY_DELIVERY_READ: 60,    # Credential-bearing responses — tighter
    CATEGORY_ORDER_CREATE:  30,    # Financially significant — conservative
    CATEGORY_WEBHOOK_WRITE: 60,    # Configuration writes
    CATEGORY_WEBHOOK_RETRY: 10,    # Manual retry — strictly controlled
}


# ---------------------------------------------------------------------------
# Decision object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EndpointRateDecision:
    allowed: bool
    category: str
    limit: int
    remaining: int
    reset_after: int  # seconds
    reason: str = "allowed"

    def headers(self, include_retry: bool = False) -> dict:
        h = {
            "RateLimit-Category": self.category,
            "RateLimit-Limit":    str(max(0, self.limit)),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset":    str(max(0, self.reset_after)),
        }
        if include_retry:
            h["Retry-After"] = str(max(1, self.reset_after))
        return h


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _minute_bucket(moment) -> tuple[str, int]:
    """Return (bucket_string, seconds_until_next_minute)."""
    bucket = moment.strftime("%Y-%m-%d %H:%M")
    next_min = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    reset_after = max(1, int((next_min - moment).total_seconds()))
    return bucket, reset_after


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_endpoint_rate(
    context: ApiRequestContext,
    category: str,
    limit: Optional[int] = None,
    moment=None,
) -> EndpointRateDecision:
    """Consume one unit from the named category counter for this reseller.

    Args:
        context:  Authenticated request context (reseller_id from auth only).
        category: One of the CATEGORY_* constants.
        limit:    Override the default per-minute limit for this category.
        moment:   Override current time (tests only).

    Returns:
        EndpointRateDecision with allowed=True or False.
    """
    if category not in ALL_CATEGORIES:
        log.warning("[endpoint_rate] unknown category=%r; failing open", category)
        return EndpointRateDecision(True, category, 0, 0, 60)

    effective_limit = int(limit if limit is not None else DEFAULT_LIMITS[category])
    # limit=0 means disabled (fail-open for this category)
    if effective_limit <= 0:
        return EndpointRateDecision(True, category, 0, 0, 60)

    moment = moment or local_now()
    bucket, reset_after = _minute_bucket(moment)

    try:
        allowed, used = rdb.consume_endpoint_quota(
            context.reseller_id, category, bucket, effective_limit
        )
    except Exception as exc:
        log.warning(
            "[endpoint_rate] DB error reseller=%s category=%s: %s; failing open",
            context.reseller_id, category, exc,
        )
        return EndpointRateDecision(True, category, effective_limit,
                                    effective_limit, reset_after, "db_error_open")

    remaining = max(0, effective_limit - used)
    if allowed:
        return EndpointRateDecision(True, category, effective_limit,
                                    remaining, reset_after)
    return EndpointRateDecision(False, category, effective_limit, 0,
                                reset_after, "rate_limit_exceeded")


__all__ = [
    "CATEGORY_READ",
    "CATEGORY_DELIVERY_READ",
    "CATEGORY_ORDER_CREATE",
    "CATEGORY_WEBHOOK_WRITE",
    "CATEGORY_WEBHOOK_RETRY",
    "ALL_CATEGORIES",
    "DEFAULT_LIMITS",
    "EndpointRateDecision",
    "check_endpoint_rate",
]
