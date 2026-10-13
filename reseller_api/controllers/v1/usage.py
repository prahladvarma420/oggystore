"""GET /api/v1/usage — reseller-safe self-service usage visibility (Phase 13 P6).

Everything is scoped to the authenticated reseller and computed by the shared
rdb.get_reseller_api_usage() aggregation (the same numbers the owner sees in
WebAdmin).  Deliberately EXCLUDED: IPs, user agents, key material, per-day
breakdowns older than today, and any platform-wide or cross-reseller data.
"""

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.services.authorization import PRODUCTS_READ
from reseller_api.responses import success_response
import database.reseller_db as rdb


@require_operation(PRODUCTS_READ)
def usage_summary():
    """Return the authenticated reseller's own usage summary."""
    context = get_authenticated_context()
    u = rdb.get_reseller_api_usage(context.reseller_id)
    # Phase 14 P2 (additive): own per-endpoint breakdown (30d) and per-key
    # request counts.  Public key ids only; scoped to the caller's rows.
    endpoints = rdb.get_reseller_endpoint_usage(context.reseller_id)
    keys = rdb.get_reseller_key_usage(context.reseller_id)
    return success_response({
        "usage": {
            "endpoints": endpoints,
            "keys": keys,
            "requests": {
                "total":      u["requests_total"],
                "successful": u["requests_ok"],
                "failed":     u["requests_failed"],
                "today":      u["requests_today"],
            },
            "orders": {
                "total":       u["orders_total"],
                "today":       u["orders_today"],
                "this_month":  u["orders_this_month"],
                "volume":      u["order_volume"],
                "currency":    "USDT",
            },
            "quota": {
                "daily_limit":        u["quota_daily"],
                "daily_used":         u["orders_today"],
                "daily_remaining":    u["quota_daily_remaining"],
                "monthly_limit":      u["quota_monthly"],
                "monthly_used":       u["orders_this_month"],
                "monthly_remaining":  u["quota_monthly_remaining"],
            },
            "rate_limits": {
                "limited_requests": u["rate_limited_429"],
            },
            "webhooks": {
                "configured":       u["webhooks_configured"],
                "enabled":          u["webhooks_enabled"],
                "failed_deliveries": u["webhook_deliveries_failed"],
                "dead_lettered_events": u["webhook_events_dead_lettered"],
            },
            "activity": {
                "last_request_at":   u["last_request_at"],
                "last_key_used_at":  u["last_key_used_at"],
            },
        }
    })


__all__ = ["usage_summary"]
