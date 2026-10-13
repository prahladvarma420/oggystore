"""Atomic request-rate evaluation for authenticated reseller APIs."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Mapping, Optional

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authorization import FOUNDATION_READ, authorize_operation
from utils.timezone import now as local_now


@dataclass(frozen=True)
class RequestRateDecision:
    allowed: bool
    reason: str
    limit: int = 0
    remaining: int = 0
    reset_after: int = 0
    scope: str = "minute"
    counters: Optional[Mapping[str, int]] = None

    def headers(self, include_retry=False):
        headers = {
            "RateLimit-Limit": str(max(0, self.limit)),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(max(0, self.reset_after)),
        }
        if include_retry:
            headers["Retry-After"] = str(max(1, self.reset_after))
        return headers


def _buckets(moment):
    minute = moment.strftime("%Y-%m-%d %H:%M")
    day = moment.strftime("%Y-%m-%d")
    month = moment.strftime("%Y-%m")
    next_minute = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    next_day = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if moment.month == 12:
        next_month = moment.replace(
            year=moment.year + 1, month=1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    else:
        next_month = moment.replace(
            month=moment.month + 1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
    resets = {
        "minute": max(1, int((next_minute - moment).total_seconds())),
        "day": max(1, int((next_day - moment).total_seconds())),
        "month": max(1, int((next_month - moment).total_seconds())),
    }
    return minute, day, month, resets


def _candidate(counter, bucket_name, bucket_value):
    if not counter or counter.get(bucket_name) != bucket_value:
        return 1
    return int(counter.get(bucket_name.replace("bucket", "count"), 0)) + 1


def evaluate_request_rate(context: ApiRequestContext, expected_environment: str,
                          moment=None):
    """Consume one request unit or return a deny decision.

    Client-supplied reseller IDs, query parameters, bodies, IPs, and arbitrary
    headers are not accepted. Only the authenticated immutable context is used.
    """
    authorization = authorize_operation(
        context, FOUNDATION_READ, expected_environment
    )
    if not authorization.allowed and authorization.reason != "request_limit_disabled":
        return RequestRateDecision(False, "identity_not_authorized")
    limits = authorization.limits or {}
    minute_limit = int(limits.get("rate_limit_per_min", 0))
    daily_limit = int(limits.get("daily_request_limit", 0))
    monthly_limit = int(limits.get("monthly_request_limit", 0))

    moment = moment or local_now()
    minute, day, month, resets = _buckets(moment)
    try:
        allowed, counters = rdb.consume_api_request_quota(
            context.reseller_id,
            context.api_key_id,
            minute,
            day,
            month,
            minute_limit,
            daily_limit,
            monthly_limit,
        )
    except Exception:
        return RequestRateDecision(False, "rate_limiter_unavailable")

    if allowed:
        # Per-key usage is visibility data; aggregate reseller enforcement is
        # already complete and must not be undone by telemetry failure.
        rdb.record_api_key_request_usage(
            context.api_key_id, context.reseller_id, minute, day, month
        )
        used = int((counters or {}).get("request_minute_count", 1))
        return RequestRateDecision(
            True, "allowed", minute_limit, max(0, minute_limit - used),
            resets["minute"], "minute", counters,
        )

    checks = (
        ("minute", minute_limit, _candidate(
            counters, "request_minute_bucket", minute
        )),
        ("day", daily_limit, _candidate(
            counters, "request_day_bucket", day
        )),
        ("month", monthly_limit, _candidate(
            counters, "request_month_bucket", month
        )),
    )
    for scope, limit, candidate in checks:
        if candidate > limit:
            return RequestRateDecision(
                False, "rate_limit_exceeded", limit, 0,
                resets[scope], scope, counters,
            )
    return RequestRateDecision(
        False, "rate_limit_exceeded", minute_limit, 0,
        resets["minute"], "minute", counters,
    )


__all__ = ["RequestRateDecision", "evaluate_request_rate"]
