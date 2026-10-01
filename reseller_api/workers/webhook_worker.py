"""Webhook delivery worker — Phase 9 Prompt 7 (retry / backoff / dead-letter).

Run as a standalone process:
    python -m reseller_api.workers.webhook_worker

Architecture
------------
Business Event → webhook_outbox (persistent) → this worker → HTTP POST → reseller endpoint

Retry policy (Phase 9 Prompt 7)
--------------------------------
Retryable failures   : timeout, DNS, connection reset, TLS, 408, 429, 5xx
Terminal failures    : 3xx, 400, 401, 403, 404, 410; deleted webhook
Max attempts         : MAX_ATTEMPTS (default 10)
Backoff              : bounded exponential with full jitter
                       delay = random(0, min(BASE_DELAY * 2**attempt, CAP_DELAY))
                       e.g.  attempt 1 → 0–60 s
                             attempt 5 → 0–960 s  (capped at 3 600 s)
Dead letter          : outbox row status = 'failed' with failure_reason stored
                       in the delivery log.  No separate DLQ table needed.

Disabled webhook policy
------------------------
A disabled webhook is treated as having no active subscriptions.  If the only
subscription(s) for an event are disabled at delivery time the event is marked
'delivered' immediately (no retry, no dead-letter).  This is intentional: the
reseller opted out; we honour that decision rather than queuing events for a
possibly-never-re-enabled endpoint.

Deleted webhook policy
-----------------------
If a webhook is deleted between poll and delivery (secret returns None) the
delivery is logged with error_category='deleted' and counted as a non-blocking
failure (same as a terminal 4xx for that subscription).  If ALL subscriptions
for the event are deleted/disabled the event is marked 'delivered'.

Concurrency
-----------
Multiple instances (processes, Railway replicas) are safe:
  - Each outbox row is claimed via claim_worker_job() before delivery.
  - A crashed worker's claim expires after CLAIM_TTL_SECONDS.
  - INSERT OR IGNORE in the outbox prevents duplicate logical events.
"""

from __future__ import annotations

import logging
import math
import os
import random
import signal
import time
import uuid

from database.db import get_conn, get_cursor
from reseller_api.services.order_idempotency import claim_worker_job
from reseller_api.services.webhook_delivery import (
    deliver_to_webhook,
    get_active_subscriptions,
    get_attempt_count,
    ensure_delivery_log_table,
    ERR_TIMEOUT, ERR_CONNECTION, ERR_TLS, ERR_DNS,
    ERR_4XX, ERR_5XX, ERR_3XX, ERR_UNKNOWN,
)
from utils.timezone import now_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all overridable via env)
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = int(os.getenv("WEBHOOK_POLL_INTERVAL", "5"))
BATCH_SIZE            = int(os.getenv("WEBHOOK_BATCH_SIZE",    "20"))
CLAIM_TTL_SECONDS     = 120   # stale-claim expiry; > HTTP_TIMEOUT_SECONDS
MAX_ATTEMPTS          = int(os.getenv("WEBHOOK_MAX_ATTEMPTS",  "10"))

# Exponential backoff parameters
BACKOFF_BASE_SECONDS = int(os.getenv("WEBHOOK_BACKOFF_BASE",  "60"))    # first retry window
BACKOFF_CAP_SECONDS  = int(os.getenv("WEBHOOK_BACKOFF_CAP",   "3600"))  # 1-hour ceiling

# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------

# HTTP status codes that are RETRYABLE (temporary server/infra failures)
RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({
    408,  # Request Timeout
    429,  # Too Many Requests
    500,  # Internal Server Error
    502,  # Bad Gateway
    503,  # Service Unavailable
    504,  # Gateway Timeout
})

# HTTP status codes that are TERMINAL (permanent rejection — never retry)
TERMINAL_HTTP_STATUSES: frozenset[int] = frozenset({
    400,  # Bad Request          — our payload is malformed; retrying won't help
    401,  # Unauthorized         — wrong or missing credentials
    403,  # Forbidden            — endpoint rejected us by policy
    404,  # Not Found            — endpoint URL does not exist
    410,  # Gone                 — endpoint explicitly decommissioned
})

# Network / transport error categories that are always retryable
RETRYABLE_ERROR_CATEGORIES: frozenset[str] = frozenset({
    ERR_TIMEOUT,
    ERR_CONNECTION,
    ERR_TLS,
    ERR_DNS,
    ERR_UNKNOWN,  # unknown error → assume transient; retry up to max
})

# Error categories that mean no further attempt should be made
TERMINAL_ERROR_CATEGORIES: frozenset[str] = frozenset({
    "deleted",  # webhook was removed; no endpoint to deliver to
    ERR_3XX,    # redirects disabled by design (SSRF prevention)
})


def is_retryable(result: dict) -> bool:
    """Return True when the delivery result warrants a retry.

    Decision tree:
      1. success → no retry
      2. terminal error_category (deleted / 3xx) → no retry
      3. http_status in TERMINAL_HTTP_STATUSES → no retry
      4. http_status in RETRYABLE_HTTP_STATUSES → retry
      5. error_category in RETRYABLE_ERROR_CATEGORIES → retry
      6. 4xx not in either set → no retry (conservative default)
      7. fallback → retry (assume transient)
    """
    if result.get("success"):
        return False

    cat    = result.get("error_category")
    status = result.get("http_status")

    if cat in TERMINAL_ERROR_CATEGORIES:
        return False

    if status is not None:
        if status in TERMINAL_HTTP_STATUSES:
            return False
        if status in RETRYABLE_HTTP_STATUSES:
            return True
        # Unknown 4xx not in RETRYABLE_HTTP_STATUSES → treat as terminal
        if 400 <= status < 500:
            return False
        # Unknown 5xx not in RETRYABLE_HTTP_STATUSES → retry
        if 500 <= status < 600:
            return True

    if cat in RETRYABLE_ERROR_CATEGORIES:
        return True

    return True   # safest default: retry unknown failures


# ---------------------------------------------------------------------------
# Backoff scheduler
# ---------------------------------------------------------------------------

def _backoff_delay(attempt: int) -> float:
    """Return a jittered delay in seconds using full-jitter exponential backoff.

    Formula:  delay = random(0, min(BASE * 2**attempt, CAP))
    Bounded:  never exceeds BACKOFF_CAP_SECONDS.

    Examples (BASE=60, CAP=3600):
        attempt 0 → 0 – 60 s
        attempt 1 → 0 – 120 s
        attempt 3 → 0 – 480 s
        attempt 5 → 0 – 1920 s  (capped at 3600 s)
        attempt 9 → 0 – 3600 s  (always capped)
    """
    window = min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_CAP_SECONDS)
    return random.uniform(0, window)


def _retry_at(attempt: int) -> str:
    """Return an ISO-8601 UTC timestamp for the next attempt."""
    delay     = _backoff_delay(attempt)
    future_ts = time.time() + delay
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(future_ts))


# ---------------------------------------------------------------------------
# Outbox state transitions
# ---------------------------------------------------------------------------

def _mark_delivered(outbox_id: int) -> None:
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "UPDATE webhook_outbox SET status='delivered', processed_at=? WHERE id=?",
        (now_text(), int(outbox_id)),
    )
    conn.commit()


def _mark_failed_terminal(outbox_id: int, reason: str = "max_attempts") -> None:
    """Dead-letter: mark an outbox row permanently failed.

    The 'failure_reason' column is added lazily (via ALTER TABLE IF NOT EXISTS)
    so older deployments without the column are not broken.
    reason is stored for diagnostics; it never contains secret values.
    """
    conn, cur = get_conn(), get_cursor()
    # Best-effort: add failure_reason column if missing (idempotent)
    try:
        cur.execute("ALTER TABLE webhook_outbox ADD COLUMN failure_reason TEXT")
        conn.commit()
    except Exception:
        pass  # column already exists or DB doesn't support ALTER — safe to ignore
    cur.execute(
        "UPDATE webhook_outbox SET status='failed', processed_at=?, failure_reason=? WHERE id=?",
        (now_text(), str(reason)[:255], int(outbox_id)),
    )
    conn.commit()
    log.error(
        "[webhook-worker] outbox_id=%s dead-lettered reason=%s",
        outbox_id, reason,
    )


def _schedule_retry(outbox_id: int, attempts_so_far: int) -> None:
    """Increment attempt counter and schedule the next delivery attempt."""
    conn, cur = get_conn(), get_cursor()
    next_at = _retry_at(attempts_so_far)
    cur.execute(
        """
        UPDATE webhook_outbox
        SET    attempts = attempts + 1,
               next_attempt_at = ?
        WHERE  id = ?
        """,
        (next_at, int(outbox_id)),
    )
    conn.commit()


def reset_for_manual_retry(outbox_id: int) -> bool:
    """Reset a dead-lettered ('failed') outbox row for manual re-delivery.

    IMPORTANT: this resets delivery state only.  It does NOT create a new
    business event, insert a new outbox row, or touch any order/wallet table.
    The original event payload is preserved and will be re-delivered as-is.

    Returns True on success, False if the row is not in 'failed' status
    (prevents accidental reset of pending/delivered rows).
    """
    conn, cur = get_conn(), get_cursor()
    cur.execute(
        "SELECT status FROM webhook_outbox WHERE id=?", (int(outbox_id),)
    )
    row = cur.fetchone()
    if not row or row[0] != "failed":
        return False
    cur.execute(
        """
        UPDATE webhook_outbox
        SET    status          = 'pending',
               attempts        = 0,
               next_attempt_at = ?,
               processed_at    = NULL,
               failure_reason  = NULL
        WHERE  id = ?
          AND  status = 'failed'
        """,
        (now_text(), int(outbox_id)),
    )
    conn.commit()
    log.info("[webhook-worker] outbox_id=%s reset for manual retry", outbox_id)
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Outbox polling
# ---------------------------------------------------------------------------

def _poll_pending_events(limit: int = BATCH_SIZE) -> list:
    cur  = get_cursor()
    now  = now_text()
    cur.execute(
        """
        SELECT id, reseller_id, event_type, aggregate_id, payload_json, attempts
        FROM   webhook_outbox
        WHERE  status = 'pending'
          AND  next_attempt_at <= ?
        ORDER  BY next_attempt_at ASC
        LIMIT  ?
        """,
        (now, int(limit)),
    )
    cols = ["id", "reseller_id", "event_type", "aggregate_id", "payload_json", "attempts"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Per-event delivery
# ---------------------------------------------------------------------------

def _process_event(row: dict) -> None:
    """Deliver one outbox event to all applicable webhook subscriptions.

    Steps
    -----
    1. Claim a distributed job lock (prevents duplicate delivery across workers).
    2. Find enabled subscriptions for this reseller / event_type.
    3. Deliver to each subscription; classify each result.
    4. Update outbox row: delivered / schedule-retry / dead-letter.

    Dead-letter trigger
    -------------------
    An event is dead-lettered when EITHER:
      a) ALL subscriptions returned terminal failures (4xx / deleted / 3xx).
      b) attempts + 1 >= MAX_ATTEMPTS (backoff budget exhausted).

    Disabled-webhook policy
    -----------------------
    If there are no *enabled* subscriptions the event is marked 'delivered'
    immediately.  Disabled = opted out; we do not queue for re-enablement.
    """
    outbox_id   = int(row["id"])
    reseller_id = int(row["reseller_id"])
    event_type  = row["event_type"]
    attempts    = int(row["attempts"])

    job_key = f"whk_deliver_{outbox_id}"
    if not claim_worker_job(job_key, _WORKER_ID, ttl_seconds=CLAIM_TTL_SECONDS):
        log.debug(
            "[webhook-worker] outbox_id=%s already claimed — skipping", outbox_id
        )
        return

    subscriptions = get_active_subscriptions(reseller_id, event_type)

    if not subscriptions:
        log.info(
            "[webhook-worker] outbox_id=%s no active subscriptions — marking delivered",
            outbox_id,
        )
        _mark_delivered(outbox_id)
        return

    any_retryable     = False
    all_terminal_fail = True   # assume worst; set False when any sub succeeds

    for sub in subscriptions:
        webhook_id  = sub["id"]
        webhook_url = sub["url"]
        attempt_num = get_attempt_count(outbox_id, webhook_id) + 1

        result = deliver_to_webhook(
            outbox_event_id=outbox_id,
            outbox_row=row,
            webhook_id=webhook_id,
            webhook_url=webhook_url,
            reseller_id=reseller_id,
            attempt=attempt_num,
        )

        if result.get("success"):
            log.info(
                "[webhook-worker] outbox_id=%s webhook_id=%s attempt=%s OK",
                outbox_id, webhook_id, attempt_num,
            )
            all_terminal_fail = False
        elif is_retryable(result):
            any_retryable     = True
            all_terminal_fail = False
            log.warning(
                "[webhook-worker] outbox_id=%s webhook_id=%s retryable "
                "category=%s status=%s attempt=%s",
                outbox_id, webhook_id,
                result.get("error_category"), result.get("http_status"), attempt_num,
            )
        else:
            # Terminal failure for this subscription
            log.warning(
                "[webhook-worker] outbox_id=%s webhook_id=%s terminal "
                "category=%s status=%s — no retry for this sub",
                outbox_id, webhook_id,
                result.get("error_category"), result.get("http_status"),
            )

    # --- Outbox state transition ---
    if not any_retryable and all_terminal_fail:
        # Every subscription either terminal-failed or was deleted/3xx
        _mark_failed_terminal(outbox_id, reason="all_terminal")

    elif not any_retryable and not all_terminal_fail:
        # At least one subscription succeeded; none need retry
        _mark_delivered(outbox_id)

    elif attempts + 1 >= MAX_ATTEMPTS:
        # Retry budget exhausted
        _mark_failed_terminal(outbox_id, reason="max_attempts")

    else:
        # At least one subscription needs a retry
        _schedule_retry(outbox_id, attempts)


# ---------------------------------------------------------------------------
# Shutdown flag and worker identity
# ---------------------------------------------------------------------------

_WORKER_ID = str(uuid.uuid4())
_RUNNING   = True


def _handle_sigterm(signum, frame):  # noqa: ARG001
    global _RUNNING
    log.info("[webhook-worker] SIGTERM — stopping after current batch")
    _RUNNING = False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> int:
    """Poll and process one batch.  Returns count of events processed."""
    rows = _poll_pending_events()
    for row in rows:
        try:
            _process_event(row)
        except Exception:
            log.exception(
                "[webhook-worker] unhandled error processing outbox_id=%s", row.get("id")
            )
    return len(rows)


def run_forever() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    ensure_delivery_log_table()
    log.info(
        "[webhook-worker] started worker_id=%s poll_interval=%ss max_attempts=%s "
        "backoff_base=%ss backoff_cap=%ss",
        _WORKER_ID, POLL_INTERVAL_SECONDS, MAX_ATTEMPTS,
        BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS,
    )
    while _RUNNING:
        try:
            n = run_once()
            if n:
                log.debug("[webhook-worker] processed %s event(s)", n)
        except Exception:
            log.exception("[webhook-worker] fatal error in poll loop")
        time.sleep(POLL_INTERVAL_SECONDS)
    log.info("[webhook-worker] stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()
