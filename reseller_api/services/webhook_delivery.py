"""Webhook HTTP delivery service — Phase 9 Prompt 5.

Responsibilities
----------------
* Build HMAC-SHA256–signed payloads.
* Send a single POST to one webhook endpoint.
* Classify the HTTP / network response.
* Persist a row in webhook_delivery_log.
* Update the parent webhook_outbox row.

This module has NO Telegram or bot dependencies — it is safe to import
in the delivery worker process without starting the bot.

Security rules
--------------
* The signing secret is decrypted immediately before use and never logged.
* Only event_id / delivery_id / request_id appear in log lines.
* No Authorization header values, no API keys, no webhook secrets are logged.
* No DB transactions are open during HTTP I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import requests
from requests.exceptions import (
    ConnectionError as _ConnError,
    SSLError as _SSLError,
    Timeout as _Timeout,
)

from database.db import get_conn, get_cursor
import database.pgcompat as _pgcompat
from utils.timezone import now_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HTTP_TIMEOUT_SECONDS    = 10        # hard wall-clock timeout per attempt
MAX_RESPONSE_BYTES      = 8_192     # discard oversized response bodies
DELIVERY_USER_AGENT     = "OggyStore-Webhook/1.0 (+https://oggystore.com)"
# Header name constants imported from webhook_signature below (avoids duplication).
# Aliases retained for backward-compat in this module:
#   SIGNATURE_HEADER, DELIVERY_ID_HEADER, EVENT_TYPE_HEADER, TIMESTAMP_HEADER

# Error categories stored in delivery log (never contain secret data)
ERR_TIMEOUT     = "timeout"
ERR_CONNECTION  = "connection"
ERR_TLS         = "tls"
ERR_DNS         = "dns"
ERR_4XX         = "4xx"
ERR_5XX         = "5xx"
ERR_3XX         = "3xx"  # redirects treated as failure (no follow by design)
ERR_UNKNOWN     = "unknown"


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Signing — delegated to the canonical security module
# ---------------------------------------------------------------------------
from reseller_api.services.webhook_signature import (
    sign_body          as _sign_payload,   # backward-compat alias used below
    FRESHNESS_WINDOW_SECONDS,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_DELIVERY,
    HEADER_EVENT,
)


# ---------------------------------------------------------------------------
# Response classification
# ---------------------------------------------------------------------------

def _classify(status: Optional[int]) -> tuple[bool, Optional[str]]:
    """Return (success, error_category) for an HTTP status code.

    success=True only for 2xx.  3xx, 4xx, 5xx all count as failures.
    error_category is None on success.
    """
    if status is None:
        return False, ERR_UNKNOWN
    if 200 <= status < 300:
        return True, None
    if 300 <= status < 400:
        return False, ERR_3XX
    if 400 <= status < 500:
        return False, ERR_4XX
    if 500 <= status < 600:
        return False, ERR_5XX
    return False, ERR_UNKNOWN


# ---------------------------------------------------------------------------
# Core HTTP send — NO open DB transaction during this call
# ---------------------------------------------------------------------------

def _send_http(
    url: str,
    secret: str,
    payload_dict: dict,
    event_type: str,
    delivery_id: str,
) -> dict:
    """POST the signed payload to `url`.

    Returns a result dict with keys:
      http_status, success, error_category, elapsed_ms, responded_at
    Never raises — all exceptions are caught and classified.
    """
    body_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    timestamp  = str(int(time.time()))
    signature  = _sign_payload(secret, body_bytes, timestamp)

    headers = {
        "Content-Type":     "application/json",
        "User-Agent":       DELIVERY_USER_AGENT,
        HEADER_SIGNATURE:   signature,
        HEADER_DELIVERY:    delivery_id,
        HEADER_EVENT:       event_type,
        HEADER_TIMESTAMP:   timestamp,
    }

    t0 = time.monotonic()
    try:
        resp = requests.post(
            url,
            data=body_bytes,
            headers=headers,
            timeout=HTTP_TIMEOUT_SECONDS,
            verify=True,           # always enforce TLS
            allow_redirects=False, # redirects = failure (prevents SSRF)
            stream=True,           # don't buffer full response body
        )
        # Read at most MAX_RESPONSE_BYTES — external servers must not block us
        _ = resp.raw.read(MAX_RESPONSE_BYTES)
        elapsed_ms   = int((time.monotonic() - t0) * 1000)
        success, cat = _classify(resp.status_code)
        return {
            "http_status":   resp.status_code,
            "success":       success,
            "error_category": cat,
            "elapsed_ms":    elapsed_ms,
            "responded_at":  now_text(),
        }
    except _Timeout:
        return _err_result(t0, ERR_TIMEOUT, None)
    except _SSLError:
        return _err_result(t0, ERR_TLS, None)
    except _ConnError as exc:
        cat = ERR_DNS if "NameResolutionError" in type(exc).__name__ or "getaddrinfo" in str(exc) else ERR_CONNECTION
        return _err_result(t0, cat, None)
    except Exception:
        log.exception("Unexpected error during webhook HTTP send (delivery_id=%s)", delivery_id)
        return _err_result(t0, ERR_UNKNOWN, None)


def _err_result(t0: float, category: str, status: Optional[int]) -> dict:
    return {
        "http_status":    status,
        "success":        False,
        "error_category": category,
        "elapsed_ms":     int((time.monotonic() - t0) * 1000),
        "responded_at":   now_text(),
    }


# ---------------------------------------------------------------------------
# Delivery log persistence
# ---------------------------------------------------------------------------

def ensure_delivery_log_table() -> None:
    """Create webhook_delivery_log if it does not exist (idempotent)."""
    conn = get_conn()
    cur  = get_cursor()
    # executescript is SQLite-only — use one execute() per statement so this
    # works on both SQLite and the Postgres-compatibility cursor.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS webhook_delivery_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            outbox_event_id INTEGER NOT NULL,
            webhook_id      INTEGER NOT NULL,
            reseller_id     INTEGER NOT NULL,
            attempt         INTEGER NOT NULL DEFAULT 1,
            request_id      TEXT    NOT NULL,
            requested_at    TEXT    NOT NULL,
            responded_at    TEXT,
            http_status     INTEGER,
            success         INTEGER NOT NULL DEFAULT 0,
            error_category  TEXT,
            next_retry_at   TEXT,
            created_at      TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_dlg_outbox"
        " ON webhook_delivery_log(outbox_event_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_dlg_webhook"
        " ON webhook_delivery_log(webhook_id, created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS ix_dlg_reseller"
        " ON webhook_delivery_log(reseller_id, created_at)"
    )
    # Prevent two workers logging the same attempt for the same event+webhook.
    # ON CONFLICT IGNORE makes INSERT OR IGNORE a safe upsert guard.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_dlg_attempt"
        " ON webhook_delivery_log(outbox_event_id, webhook_id, attempt)"
    )
    # Enforce UUID uniqueness at the DB level.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_dlg_request_id"
        " ON webhook_delivery_log(request_id)"
    )
    conn.commit()


def record_delivery_attempt(
    outbox_event_id: int,
    webhook_id: int,
    reseller_id: int,
    attempt: int,
    request_id: str,
    requested_at: str,
    result: dict,
    next_retry_at: Optional[str] = None,
) -> int:
    """Insert one row into webhook_delivery_log; return the new row id."""
    conn = get_conn()
    cur  = get_cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO webhook_delivery_log
            (outbox_event_id, webhook_id, reseller_id, attempt, request_id,
             requested_at, responded_at, http_status, success,
             error_category, next_retry_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        -- INSERT OR IGNORE: if a row for this (outbox_event_id, webhook_id, attempt)
        -- already exists (two workers raced), the second insert is silently dropped.
        -- The first writer wins; no duplicate log rows.
        """,
        (
            int(outbox_event_id),
            int(webhook_id),
            int(reseller_id),
            int(attempt),
            str(request_id),
            str(requested_at),
            result.get("responded_at"),
            result.get("http_status"),
            1 if result.get("success") else 0,
            result.get("error_category"),
            next_retry_at,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_delivery_log_for_event(outbox_event_id: int) -> list:
    """Return all delivery log rows for an outbox event (newest first)."""
    cur = get_cursor()
    cur.execute(
        "SELECT * FROM webhook_delivery_log WHERE outbox_event_id=? ORDER BY id DESC",
        (int(outbox_event_id),),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_attempt_count(outbox_event_id: int, webhook_id: int) -> int:
    """Return the number of delivery attempts recorded for an event × webhook pair."""
    cur = get_cursor()
    cur.execute(
        "SELECT COUNT(*) FROM webhook_delivery_log WHERE outbox_event_id=? AND webhook_id=?",
        (int(outbox_event_id), int(webhook_id)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Find active subscriptions for an event
# ---------------------------------------------------------------------------

def get_active_subscriptions(reseller_id: int, event_type: str) -> list:
    """Return enabled webhook rows whose events list includes event_type.

    Returns list of dicts: {id, url, events}.
    The secret is NOT returned here — fetch it separately just before signing.
    """
    import json as _json
    cur = get_cursor()
    cur.execute(
        "SELECT id, url, events FROM reseller_webhooks "
        "WHERE reseller_id=? AND enabled=1",
        (int(reseller_id),),
    )
    rows = cur.fetchall()
    result = []
    for row_id, url, events_raw in rows:
        try:
            subscribed = _json.loads(events_raw) if events_raw else None
        except Exception:
            subscribed = None
        # None or missing events → subscribed to all events (default)
        if subscribed is None or event_type in subscribed:
            result.append({"id": row_id, "url": url})
    return result


def get_webhook_secret_by_id(webhook_id: int, reseller_id: int) -> Optional[str]:
    """Fetch and decrypt the signing secret for one webhook. Returns None if gone."""
    from utils.crypto import decrypt_text
    cur = get_cursor()
    cur.execute(
        "SELECT secret_encrypted FROM reseller_webhooks WHERE id=? AND reseller_id=?",
        (int(webhook_id), int(reseller_id)),
    )
    row = cur.fetchone()
    if not row:
        return None
    return decrypt_text(row[0])


# ---------------------------------------------------------------------------
# Main delivery entry point (called by the worker per outbox-event × webhook)
# ---------------------------------------------------------------------------

def deliver_to_webhook(
    outbox_event_id: int,
    outbox_row: dict,
    webhook_id: int,
    webhook_url: str,
    reseller_id: int,
    attempt: int = 1,
) -> dict:
    """Deliver one outbox event to one webhook endpoint.

    1. Decrypt secret (in-memory only, never logged).
    2. Build signed POST outside any DB transaction.
    3. Send HTTP (no DB transaction open).
    4. Record delivery log row.
    5. Return result dict.

    Logs only: event_id, delivery_id, webhook_id — never the secret.
    """
    import json as _json

    request_id   = str(uuid.uuid4())
    requested_at = now_text()
    event_type   = str(outbox_row.get("event_type", ""))

    # Build payload — inject event_id so reseller can deduplicate
    try:
        payload = _json.loads(outbox_row["payload_json"])
    except Exception:
        payload = {}
    payload["event_id"]        = outbox_event_id
    payload["idempotency_key"] = str(outbox_event_id)  # stable across ALL retries
    payload["delivery_id"]    = request_id              # unique per attempt

    log.info(
        "[webhook] delivering event_id=%s event_type=%s webhook_id=%s attempt=%s request_id=%s",
        outbox_event_id, event_type, webhook_id, attempt, request_id,
    )

    # Fetch secret OUTSIDE any transaction — function opens its own cursor
    secret = get_webhook_secret_by_id(webhook_id, reseller_id)
    if secret is None:
        log.warning(
            "[webhook] webhook_id=%s no longer exists; skipping (event_id=%s)",
            webhook_id, outbox_event_id,
        )
        return {"success": False, "error_category": "deleted", "http_status": None,
                "responded_at": now_text(), "elapsed_ms": 0, "request_id": request_id}

    # --- HTTP: no DB transaction is open during this call ---
    result = _send_http(webhook_url, secret, payload, event_type, request_id)

    log.info(
        "[webhook] event_id=%s webhook_id=%s attempt=%s status=%s success=%s category=%s elapsed_ms=%s",
        outbox_event_id, webhook_id, attempt,
        result.get("http_status"), result.get("success"),
        result.get("error_category"), result.get("elapsed_ms"),
    )

    # Record delivery log (uses its own connection, no long-lived transaction)
    record_delivery_attempt(
        outbox_event_id=outbox_event_id,
        webhook_id=webhook_id,
        reseller_id=reseller_id,
        attempt=attempt,
        request_id=request_id,
        requested_at=requested_at,
        result=result,
    )

    result["request_id"] = request_id
    return result


__all__ = [
    "ensure_delivery_log_table",
    "deliver_to_webhook",
    "get_active_subscriptions",
    "get_webhook_secret_by_id",
    "record_delivery_attempt",
    "get_delivery_log_for_event",
    "get_attempt_count",
    "HTTP_TIMEOUT_SECONDS",
    "HEADER_SIGNATURE",
    "HEADER_DELIVERY",
    "HEADER_EVENT",
    "ERR_TIMEOUT", "ERR_CONNECTION", "ERR_TLS", "ERR_DNS",
    "ERR_4XX", "ERR_5XX", "ERR_3XX", "ERR_UNKNOWN",
]


# ---------------------------------------------------------------------------
# Delivery history queries — Prompt 9 (Webhook Management API)
# ---------------------------------------------------------------------------

def list_deliveries_for_webhook(
    reseller_id: int,
    webhook_id: int,
    limit: int = 50,
) -> list:
    """Return delivery log rows for one webhook, joined with outbox for event context.

    IDOR-safe: scoped by BOTH reseller_id and webhook_id.
    Sensitive fields (secret, auth headers, stack traces, payload_json) are
    never stored in webhook_delivery_log and are never returned here.

    Returns newest-first, limited to `limit` rows (max 100).
    """
    from database.db import get_cursor as _gc
    cur = _gc()
    cur.execute(
        """
        SELECT
            d.id              AS delivery_id,
            d.outbox_event_id AS event_id,
            o.event_type,
            o.status          AS event_status,
            d.attempt,
            d.http_status,
            d.success,
            d.error_category,
            d.requested_at    AS last_attempt_at,
            d.next_retry_at,
            d.created_at,
            o.attempts        AS total_attempts,
            o.created_at      AS event_created_at
        FROM  webhook_delivery_log d
        JOIN  webhook_outbox o ON o.id = d.outbox_event_id
        WHERE d.webhook_id  = ?
          AND d.reseller_id = ?
        ORDER BY d.id DESC
        LIMIT ?
        """,
        (int(webhook_id), int(reseller_id), max(1, min(int(limit), 100))),
    )
    cols = [desc[0] for desc in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    result = []
    for r in rows:
        result.append({
            "delivery_id":    r["delivery_id"],
            "event_id":       r["event_id"],
            "event_type":     r["event_type"],
            "event_status":   r["event_status"],
            "attempt":        r["attempt"],
            "http_status":    r["http_status"],
            "success":        bool(r["success"]),
            "error_category": r["error_category"],
            "last_attempt_at": r["last_attempt_at"],
            "next_retry_at":  r["next_retry_at"],
            "created_at":     r["created_at"],
        })
    return result


def get_delivery_for_webhook(
    reseller_id: int,
    webhook_id: int,
    delivery_id: int,
) -> Optional[dict]:
    """Fetch a single delivery log row scoped by reseller_id + webhook_id.

    IDOR-safe: a delivery_id that belongs to a different reseller or a different
    webhook owned by the same reseller returns None — indistinguishable from
    a non-existent row.

    Returns None when not found.
    """
    from database.db import get_cursor as _gc
    cur = _gc()
    cur.execute(
        """
        SELECT
            d.id              AS delivery_id,
            d.outbox_event_id,
            d.attempt,
            d.http_status,
            d.success,
            d.error_category,
            d.requested_at,
            d.next_retry_at,
            d.created_at,
            o.event_type,
            o.status          AS event_status,
            o.attempts        AS total_attempts
        FROM  webhook_delivery_log d
        JOIN  webhook_outbox o ON o.id = d.outbox_event_id
        WHERE d.id          = ?
          AND d.webhook_id  = ?
          AND d.reseller_id = ?
        """,
        (int(delivery_id), int(webhook_id), int(reseller_id)),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [desc[0] for desc in cur.description]
    r = dict(zip(cols, row))
    return {
        "delivery_id":     r["delivery_id"],
        "outbox_event_id": r["outbox_event_id"],   # internal — used for retry
        "event_type":      r["event_type"],
        "event_status":    r["event_status"],
        "attempt":         r["attempt"],
        "http_status":     r["http_status"],
        "success":         bool(r["success"]),
        "error_category":  r["error_category"],
        "last_attempt_at": r["requested_at"],
        "next_retry_at":   r["next_retry_at"],
        "created_at":      r["created_at"],
    }
