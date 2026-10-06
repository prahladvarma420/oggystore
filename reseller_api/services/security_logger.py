"""Security audit logger - Phase 10 Prompt 8.

Single integration point for all reseller-API security events.

Design principles
-----------------
* REUSE: writes to the existing reseller_security_events table via
  database.reseller_db.log_security_event. No new table, no new DB.
* SAFE:  emit() swallows every exception -- observability must never
  fail a request.
* CLEAN: redacts secrets via observability.sanitize_log_text before
  any value touches the DB.
* QUIET: noisy low-severity events (e.g. ordinary 429s) are suppressed
  below a configurable threshold so the log stays signal-rich.
* ALERTING: severe events are forwarded to the Telegram owner alert via
  the existing tg_notify channel when configured.
"""
from __future__ import annotations

import logging
from typing import Optional

import database.reseller_db as rdb
from reseller_api.services.observability import sanitize_log_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type constants (stored verbatim in reseller_security_events.event_type)
# ---------------------------------------------------------------------------
EVT_AUTH_FAILURE         = "auth_failure"
EVT_BRUTE_FORCE_BLOCKED  = "brute_force_blocked"
EVT_RATE_LIMIT_EXCEEDED  = "rate_limit_exceeded"
EVT_QUOTA_EXCEEDED       = "quota_exceeded"
EVT_API_DISABLED         = "api_disabled"
EVT_KEY_REVOKED          = "key_revoked_reuse"
EVT_ORDER_BURST_BLOCKED  = "order_burst_blocked"
EVT_WEBHOOK_RETRY_ABUSE  = "webhook_retry_abuse"
EVT_PAYLOAD_REJECTED     = "payload_rejected"
EVT_VALIDATION_REJECTED  = "validation_rejected"
EVT_SUSPICIOUS_PATTERN   = "suspicious_pattern"

# Severe events that may warrant an owner Telegram alert.
_SEVERE = frozenset({
    EVT_BRUTE_FORCE_BLOCKED,
    EVT_QUOTA_EXCEEDED,
    EVT_API_DISABLED,
    EVT_KEY_REVOKED,
    EVT_SUSPICIOUS_PATTERN,
})

# Throttle: rate_limit_exceeded is logged only every Nth hit per reseller
# to avoid flooding the security log with routine 429s.
_RATE_LIMIT_LOG_EVERY = 10  # log on hits 1, 11, 21, ...


def emit(
    event_type: str,
    reseller_id: Optional[int] = None,
    *,
    reseller_public_id: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    description: str = "",
    request_id: Optional[str] = None,
    hit_count: Optional[int] = None,
    alert: bool = False,
) -> None:
    """Write a security event. Always safe -- never raises.

    Parameters
    ----------
    event_type       : One of the EVT_* constants.
    reseller_id      : Internal integer reseller ID (preferred).
    reseller_public_id: Human-readable public ID for the description.
    ip               : Already-resolved client IP (from TRUST_PROXY config).
    user_agent       : Raw User-Agent header value (truncated/sanitised here).
    description      : Free-text context. Secrets must NOT appear here.
    request_id       : Server-generated request ID from g.request_id.
    hit_count        : For throttled events, the current counter value.
    alert            : If True and the event is severe, send a Telegram alert.
    """
    try:
        # ------------------------------------------------------------------
        # Throttle noisy low-value events
        # ------------------------------------------------------------------
        if event_type == EVT_RATE_LIMIT_EXCEEDED and hit_count is not None:
            # Only log the first hit and every 10th thereafter.
            if hit_count > 1 and (hit_count - 1) % _RATE_LIMIT_LOG_EVERY != 0:
                return

        # ------------------------------------------------------------------
        # Sanitise all free-text before it reaches the DB
        # ------------------------------------------------------------------
        safe_ua   = sanitize_log_text(user_agent or "", 300) or None
        safe_desc = sanitize_log_text(description or "", 500)
        if request_id:
            safe_desc = "[req:" + sanitize_log_text(request_id, 64, redact_tokens=False) + "] " + safe_desc
        if reseller_public_id and reseller_id is None:
            safe_desc = "[reseller:" + sanitize_log_text(reseller_public_id, 64) + "] " + safe_desc
        elif reseller_public_id:
            safe_desc = "[" + sanitize_log_text(reseller_public_id, 64) + "] " + safe_desc

        # ------------------------------------------------------------------
        # Persist to reseller_security_events
        # ------------------------------------------------------------------
        rdb.log_security_event(
            reseller_id,
            event_type,
            ip=ip,
            user_agent=safe_ua,
            description=safe_desc.strip(),
        )

        # ------------------------------------------------------------------
        # Optional Telegram alert for severe events
        # ------------------------------------------------------------------
        if (alert or event_type in _SEVERE) and reseller_id is not None:
            _maybe_alert(event_type, reseller_public_id or str(reseller_id),
                         safe_desc, ip)

    except Exception as exc:  # noqa: BLE001
        log.debug("[security_logger] emit failed silently: %s", exc)


def _maybe_alert(event_type: str, reseller_label: str,
                 description: str, ip) -> None:
    """Forward a severe event to Telegram admin channel, best-effort."""
    try:
        import config
        admin_id = getattr(config, "ADMIN_ID", None)
        bot_token = getattr(config, "BOT_TOKEN", None)
        if not admin_id or not bot_token:
            return
        from web_admin.app import tg_notify
        msg = (
            "Security Alert\n"
            "Event: " + event_type + "\n"
            "Reseller: " + reseller_label + "\n"
            "IP: " + str(ip or "unknown") + "\n"
            "Detail: " + description[:200]
        )
        tg_notify(admin_id, msg)
    except Exception:
        pass


__all__ = [
    "emit",
    "EVT_AUTH_FAILURE", "EVT_BRUTE_FORCE_BLOCKED",
    "EVT_RATE_LIMIT_EXCEEDED", "EVT_QUOTA_EXCEEDED",
    "EVT_API_DISABLED", "EVT_KEY_REVOKED",
    "EVT_ORDER_BURST_BLOCKED", "EVT_WEBHOOK_RETRY_ABUSE",
    "EVT_PAYLOAD_REJECTED", "EVT_VALIDATION_REJECTED",
    "EVT_SUSPICIOUS_PATTERN", "_RATE_LIMIT_LOG_EVERY",
]
