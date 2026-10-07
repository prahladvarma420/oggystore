
"""Reseller quota service — Phase 10 Prompt 7.

Quotas are long-period usage allowances set by the platform owner/admin.
They are conceptually SEPARATE from rate limits (short-term throttling).

  RATE LIMIT : max N requests per minute  → protects infrastructure
  QUOTA      : max N orders per day/month → enforces business agreements
"""
from __future__ import annotations
import logging
from utils.timezone import now as local_now
import database.reseller_db as rdb
from reseller_api.services import security_logger as sec_log
from reseller_api.context import ApiRequestContext

log = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    def __init__(self, code, message, status=429):
        super().__init__(code)
        self.code = code; self.message = message; self.status = status


def check_order_quota(context: ApiRequestContext) -> None:
    """Raise QuotaExceededError if quota is exceeded. Fails open on DB error."""
    try:
        settings = rdb.get_quota_settings(context.reseller_id)
    except Exception as exc:
        log.warning("[quota] settings lookup error reseller=%s: %s; failing open", context.reseller_id, exc)
        return

    if not settings.get("api_enabled", True):
        sec_log.emit(sec_log.EVT_API_DISABLED, reseller_id=context.reseller_id,
            reseller_public_id=context.reseller_public_id,
            description="API access disabled by admin")
        raise QuotaExceededError("api_disabled",
            "API access is currently disabled for your account. Please contact support.", 403)

    moment = local_now()
    day = moment.strftime("%Y-%m-%d")
    month = moment.strftime("%Y-%m")

    try:
        counts = rdb.count_orders_for_quota(context.reseller_id, day, month)
    except Exception as exc:
        log.warning("[quota] count error reseller=%s: %s; failing open", context.reseller_id, exc)
        return

    daily_quota = int(settings.get("daily_order_quota", 0))
    monthly_quota = int(settings.get("monthly_order_quota", 0))

    if daily_quota > 0 and counts["daily"] >= daily_quota:
        sec_log.emit(sec_log.EVT_QUOTA_EXCEEDED, reseller_id=context.reseller_id,
            reseller_public_id=context.reseller_public_id,
            description=f"daily_order_quota={daily_quota} used={counts['daily']}")
        raise QuotaExceededError("daily_order_quota_exceeded",
            f"Daily order quota ({daily_quota}) reached. Your quota resets at midnight UTC.", 429)
    if monthly_quota > 0 and counts["monthly"] >= monthly_quota:
        sec_log.emit(sec_log.EVT_QUOTA_EXCEEDED, reseller_id=context.reseller_id,
            reseller_public_id=context.reseller_public_id,
            description=f"monthly_order_quota={monthly_quota} used={counts['monthly']}")
        raise QuotaExceededError("monthly_order_quota_exceeded",
            f"Monthly order quota ({monthly_quota}) reached. Resets at the start of next month.", 429)


def record_order_usage(context: ApiRequestContext, product_id=None, quantity=None) -> None:
    """Write an order_placed financial event for quota tracking. Never raises."""
    try:
        rdb.log_financial_event_db(
            context.reseller_id, "order_placed",
            detail="quota_usage", product_id=product_id, quantity=quantity,
        )
    except Exception as exc:
        log.warning("[quota] record_order_usage error reseller=%s: %s", context.reseller_id, exc)


__all__ = ["QuotaExceededError", "check_order_quota", "record_order_usage"]
