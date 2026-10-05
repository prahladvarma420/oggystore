"""Transactional outbox for webhook event delivery — Phase 9 Prompt 3.

Architecture
------------
Every business operation that should trigger a webhook writes a row into the
``webhook_outbox`` table INSIDE the same database transaction that commits the
business fact.  This is the transactional outbox pattern:

    pgcompat.atomic(conn)
        wallet debit          ← financial write
        INSERT INTO orders    ← order write
        emit_event(cur, ...)  ← outbox row, same atomic block
    COMMIT                    ← all three are durable together

A separate delivery worker (Prompt 4) polls rows WHERE status='pending' and
sends outbound HTTP.  Delivery retries update ``attempts`` and
``next_attempt_at``; they never re-insert a new business event row (immutable
logical events, mutable delivery state).

Event types
-----------
Only events for which a concrete state transition exists in the codebase are
implemented.  Unknown event types are rejected at write time.

    order.created       — order committed in create_reseller_order()
    order.failed        — handle_post_debit_failure()
    order.cancelled     — handle_cancellation()
    delivery.completed  — delivery text stored in create_reseller_order()
    refund.created      — _issue_refund() posted successfully

Payload security
----------------
Payloads are built by _build_*_payload() helpers that return ONLY fields safe
for external API consumers.  Internal DB ids, wallet tx ids, encrypted
delivery, raw secrets, supplier credentials, and stack traces are excluded.
"""

import json
import logging
from typing import Optional

from utils.timezone import now_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported event types (whitelist — reject unknown at write time)
# ---------------------------------------------------------------------------

OUTBOX_EVENT_TYPES = frozenset({
    "order.created",
    "order.failed",
    "order.cancelled",
    "delivery.pending",
    "delivery.completed",
    "delivery.failed",
    "refund.created",
    # Phase 11 P6 — master product sync notifications
    "product.updated",   # any owner field change (name/price/description/etc.)
    "product.disabled",  # owner set active=0
    "product.enabled",   # owner set active=1
    "product.deleted",   # owner deleted the product row
    "stock.updated",     # owner added/changed stock levels
})

# Outbox row statuses
STATUS_PENDING   = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED    = "failed"

# Initial retry delay (seconds) — delivery worker uses this for next_attempt_at
INITIAL_RETRY_DELAY_SECONDS = 0   # attempt immediately on first pick-up


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_webhook_outbox_table():
    """Create webhook_outbox and worker_jobs tables if they don't exist.

    Idempotent — safe to call multiple times at startup.
    Called from ensure_reseller_tables() in reseller_db.py.
    """
    from database.db import get_conn, get_cursor
    conn = get_conn()
    cur  = get_cursor()

    # --- webhook_outbox ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS webhook_outbox (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reseller_id     INTEGER NOT NULL,
            event_type      TEXT    NOT NULL,
            aggregate_id    TEXT    NOT NULL,
            payload_json    TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            processed_at    TEXT
        )
    """)

    # Prevents duplicate business events for the same (reseller, event, order).
    # INSERT OR IGNORE is safe: a second call for the same business fact is a
    # no-op rather than an error.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_webhook_outbox_dedup
        ON webhook_outbox(reseller_id, event_type, aggregate_id)
    """)

    # Delivery worker polls: WHERE status='pending' AND next_attempt_at <= now()
    # ORDER BY next_attempt_at ASC LIMIT N
    cur.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_webhook_outbox_pending
        ON webhook_outbox(status, next_attempt_at)
        WHERE status = 'pending'
    """)

    # --- worker_jobs (heartbeat-claim table referenced by order_idempotency) ---
    # This table was designed in order_idempotency.py but never created.
    # Phase 9 creates it here so claim_worker_job() is safe to call.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS worker_jobs (
            job_key      TEXT PRIMARY KEY,
            worker_id    TEXT NOT NULL,
            claimed_at   INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL
        )
    """)

    conn.commit()


# ---------------------------------------------------------------------------
# Core write primitive — called INSIDE pgcompat.atomic() blocks
# ---------------------------------------------------------------------------

def emit_event(
    cur,
    reseller_id: int,
    event_type: str,
    aggregate_id: str,
    payload: dict,
) -> Optional[int]:
    """Insert one outbox row using an existing cursor inside an atomic block.

    MUST be called with a cursor that is already participating in an active
    pgcompat.atomic() block.  The caller owns the commit boundary.

    Parameters
    ----------
    cur           : active database cursor (inside pgcompat.atomic)
    reseller_id   : internal reseller ID (integer)
    event_type    : one of OUTBOX_EVENT_TYPES
    aggregate_id  : public order ID string, e.g. "ORD-00000001"
    payload       : safe dict; will be serialised to JSON

    Returns
    -------
    int   — the new outbox row id, or
    None  — if the event already exists (idempotent via INSERT OR IGNORE)

    Raises
    ------
    ValueError  — if event_type is not in OUTBOX_EVENT_TYPES
    """
    if event_type not in OUTBOX_EVENT_TYPES:
        raise ValueError(
            f"Unknown outbox event type '{event_type}'. "
            f"Supported: {sorted(OUTBOX_EVENT_TYPES)}"
        )

    payload_json = json.dumps(payload, default=str, separators=(",", ":"))
    now = now_text()

    cur.execute(
        """
        INSERT OR IGNORE INTO webhook_outbox
            (reseller_id, event_type, aggregate_id, payload_json,
             status, attempts, next_attempt_at, created_at)
        VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
        """,
        (
            int(reseller_id),
            str(event_type),
            str(aggregate_id),
            payload_json,
            now,   # next_attempt_at = now (deliver immediately on first poll)
            now,   # created_at
        ),
    )
    # Use rowcount to detect INSERT OR IGNORE dedup.
    # rowcount==0 means the unique index fired and no row was inserted.
    if getattr(cur, "rowcount", 1) == 0:
        return None
    row_id = getattr(cur, "lastrowid", None)
    return row_id if row_id else None


# ---------------------------------------------------------------------------
# Standalone emit (opens its own atomic block — for failure/cancel paths)
# ---------------------------------------------------------------------------

def emit_event_atomic(
    reseller_id: int,
    event_type: str,
    aggregate_id: str,
    payload: dict,
) -> Optional[int]:
    """Write one outbox row in its own atomic block.

    Use this when the caller does NOT already hold a pgcompat.atomic() context
    (e.g. order failure/cancellation paths that issue their own commits).

    The transaction contains ONLY the outbox INSERT.  Call this after the
    business write has committed so that a crash between business commit and
    outbox commit leaves the business state correct; the delivery worker can
    tolerate a missing outbox row (at-most-once) whereas a duplicate financial
    write would be far worse.

    For order.created, always use emit_event() inside the business atomic block
    to get the at-least-once guarantee.
    """
    import database.pgcompat as _pgcompat
    from database.db import get_conn, get_cursor

    conn = get_conn()
    cur  = get_cursor()
    row_id = None
    try:
        with _pgcompat.atomic(conn):
            row_id = emit_event(cur, reseller_id, event_type, aggregate_id, payload)
    except Exception:
        log.exception(
            "webhook_outbox: failed to emit %s for %s reseller=%s",
            event_type, aggregate_id, reseller_id,
        )
    return row_id


# ---------------------------------------------------------------------------
# Payload builders — safe external-API data only
# ---------------------------------------------------------------------------

def build_order_created_payload(
    order_id: int,
    reseller_public_id: str,
    product_public_id: str,
    quantity: int,
    unit_price_cents: int,
    total_cents: int,
    client_order_id: Optional[str],
    created_at: str,
) -> dict:
    """Safe payload for order.created events.

    Excluded: internal order id, wallet tx id, encrypted delivery,
    failure_reason, raw SQL values, supplier credentials.
    """
    public_order_id = f"ORD-{int(order_id):08d}"
    unit_price_display = f"{unit_price_cents / 100:.2f}"
    total_display      = f"{total_cents / 100:.2f}"
    out = {
        "event":            "order.created",
        "event_type":       "order.created",
        "order_id":         public_order_id,
        "reseller_id":      str(reseller_public_id),
        "product_id":       str(product_public_id),
        "quantity":         int(quantity),
        "unit_price":       unit_price_display,
        "total":            total_display,
        "currency":         "USDT",
        "order_status":     "created",
        "delivery_status":  "pending",
        "timestamp":        str(created_at),
        "created_at":       str(created_at),
    }
    if client_order_id:
        out["client_order_id"] = str(client_order_id)
    return out


def build_delivery_completed_payload(
    order_id: int,
    reseller_public_id: str,
    product_public_id: str,
    quantity: int,
    created_at: str,
) -> dict:
    """Safe payload for delivery.completed events.

    Excluded: decrypted delivery codes (sensitive product data — consumer
    must call GET /orders/<id>/delivery with their API key to retrieve them).
    """
    return {
        "event":            "delivery.completed",
        "event_type":       "delivery.completed",
        "order_id":         f"ORD-{int(order_id):08d}",
        "reseller_id":      str(reseller_public_id),
        "product_id":       str(product_public_id),
        "quantity":         int(quantity),
        "order_status":     "completed",
        "delivery_status":  "delivered",
        "timestamp":        str(created_at),
        "created_at":       str(created_at),
        "note":             "Retrieve delivery items via GET /api/v1/orders/{order_id}/delivery",
    }


def build_order_failed_payload(
    order_id: int,
    reseller_public_id: str,
    reason: str,
    failed_at: str,
) -> dict:
    """Safe payload for order.failed events.

    Excluded: internal failure_reason if it contains stack traces or
    internal paths — only the sanitised public reason is included.
    """
    # Sanitise: only alphanumeric, underscores, spaces, max 128 chars.
    safe_reason = str(reason)[:128] if reason else "fulfillment_failure"
    return {
        "event":            "order.failed",
        "event_type":       "order.failed",
        "order_id":         f"ORD-{int(order_id):08d}",
        "reseller_id":      str(reseller_public_id),
        "reason":           safe_reason,
        "order_status":     "failed",
        "delivery_status":  "failed",
        "timestamp":        str(failed_at),
        "failed_at":        str(failed_at),
    }


def build_order_cancelled_payload(
    order_id: int,
    reseller_public_id: str,
    cancelled_at: str,
) -> dict:
    """Safe payload for order.cancelled events."""
    return {
        "event":            "order.cancelled",
        "event_type":       "order.cancelled",
        "order_id":         f"ORD-{int(order_id):08d}",
        "reseller_id":      str(reseller_public_id),
        "order_status":     "cancelled",
        "delivery_status":  "cancelled",
        "timestamp":        str(cancelled_at),
        "cancelled_at":     str(cancelled_at),
    }


def build_refund_created_payload(
    order_id: int,
    reseller_public_id: str,
    amount_cents: int,
    refund_type: str,
    created_at: str,
) -> dict:
    """Safe payload for refund.created events.

    Excluded: wallet transaction id, internal ledger reference.
    """
    amount_display = f"{int(amount_cents) / 100:.2f}"
    return {
        "event":        "refund.created",
        "event_type":   "refund.created",
        "order_id":     f"ORD-{int(order_id):08d}",
        "reseller_id":  str(reseller_public_id),
        "amount":       amount_display,
        "currency":     "USDT",
        "refund_type":  str(refund_type),   # "full" | "cancellation" | "partial"
        "timestamp":    str(created_at),
        "created_at":   str(created_at),
    }




def build_delivery_pending_payload(
    order_id: int,
    reseller_public_id: str,
    product_public_id: str,
    quantity: int,
    created_at: str,
) -> dict:
    """Safe payload for delivery.pending events (manual / external products).

    Emitted when an order is committed but delivery content is not yet
    available (manual fulfillment or external API fulfillment not yet done).
    """
    return {
        "event":            "delivery.pending",
        "event_type":       "delivery.pending",
        "order_id":         f"ORD-{int(order_id):08d}",
        "reseller_id":      str(reseller_public_id),
        "product_id":       str(product_public_id),
        "quantity":         int(quantity),
        "order_status":     "created",
        "delivery_status":  "pending",
        "timestamp":        str(created_at),
        "created_at":       str(created_at),
        "note":             "Delivery is pending manual or external fulfillment.",
    }


def build_delivery_failed_payload(
    order_id: int,
    reseller_public_id: str,
    reason: str,
    failed_at: str,
) -> dict:
    """Safe payload for delivery.failed events.

    Emitted alongside order.failed when fulfillment could not complete.
    Reason is sanitised to 128 chars max.
    """
    safe_reason = str(reason)[:128] if reason else "fulfillment_failure"
    return {
        "event":            "delivery.failed",
        "event_type":       "delivery.failed",
        "order_id":         f"ORD-{int(order_id):08d}",
        "reseller_id":      str(reseller_public_id),
        "reason":           safe_reason,
        "order_status":     "failed",
        "delivery_status":  "failed",
        "timestamp":        str(failed_at),
        "failed_at":        str(failed_at),
    }

# ---------------------------------------------------------------------------
# Query helpers (used by delivery worker and tests)
# ---------------------------------------------------------------------------

def get_pending_events(limit: int = 50) -> list:
    """Return up to `limit` pending outbox rows ready for delivery.

    Ordered by next_attempt_at ASC so oldest-due rows are delivered first.
    Does NOT filter by reseller_id; the delivery worker handles all resellers.
    """
    from database.db import get_cursor
    cur = get_cursor()
    now = now_text()
    cur.execute(
        """
        SELECT id, reseller_id, event_type, aggregate_id, payload_json,
               attempts, next_attempt_at, created_at
        FROM   webhook_outbox
        WHERE  status = 'pending'
          AND  next_attempt_at <= ?
        ORDER  BY next_attempt_at ASC
        LIMIT  ?
        """,
        (now, int(limit)),
    )
    rows = cur.fetchall()
    return [
        {
            "id":              int(r[0]),
            "reseller_id":     int(r[1]),
            "event_type":      str(r[2]),
            "aggregate_id":    str(r[3]),
            "payload_json":    str(r[4]),
            "attempts":        int(r[5]),
            "next_attempt_at": str(r[6]),
            "created_at":      str(r[7]),
        }
        for r in rows
    ]


def get_outbox_row(row_id: int) -> Optional[dict]:
    """Fetch a single outbox row by internal id (for tests / inspection)."""
    from database.db import get_cursor
    cur = get_cursor()
    cur.execute(
        """
        SELECT id, reseller_id, event_type, aggregate_id, payload_json,
               status, attempts, next_attempt_at, created_at, processed_at
        FROM   webhook_outbox
        WHERE  id = ?
        """,
        (int(row_id),),
    )
    r = cur.fetchone()
    if r is None:
        return None
    return {
        "id":              int(r[0]),
        "reseller_id":     int(r[1]),
        "event_type":      str(r[2]),
        "aggregate_id":    str(r[3]),
        "payload_json":    str(r[4]),
        "status":          str(r[5]),
        "attempts":        int(r[6]),
        "next_attempt_at": str(r[7]),
        "created_at":      str(r[8]),
        "processed_at":    r[9],
    }


def get_outbox_for_order(order_id: int, reseller_id: int) -> list:
    """Return all outbox rows for a given order (by public ID)."""
    from database.db import get_cursor
    cur = get_cursor()
    aggregate_id = f"ORD-{int(order_id):08d}"
    cur.execute(
        """
        SELECT id, event_type, status, attempts, created_at
        FROM   webhook_outbox
        WHERE  aggregate_id = ? AND reseller_id = ?
        ORDER  BY id ASC
        """,
        (aggregate_id, int(reseller_id)),
    )
    rows = cur.fetchall()
    return [
        {
            "id":         int(r[0]),
            "event_type": str(r[1]),
            "status":     str(r[2]),
            "attempts":   int(r[3]),
            "created_at": str(r[4]),
        }
        for r in rows
    ]



# ---------------------------------------------------------------------------
# Phase 11 P6 — Product sync payload builders
# ---------------------------------------------------------------------------

def build_product_updated_payload(
    public_id: str,
    name: str,
    changed_fields: list,
    updated_at: str,
) -> dict:
    """Safe payload for product.updated events.

    Only API-safe fields are included; internal DB ids, cost, supplier
    credentials, and delivery encryption keys are excluded.
    """
    return {
        "event":          "product.updated",
        "event_type":     "product.updated",
        "product_id":     str(public_id),
        "name":           str(name) if name else None,
        "changed_fields": [str(f) for f in (changed_fields or [])],
        "timestamp":      str(updated_at),
        "note":           "Re-fetch product details via GET /api/v1/products/{product_id}",
    }


def build_product_disabled_payload(
    public_id: str,
    name: str,
    disabled_at: str,
) -> dict:
    """Safe payload for product.disabled events.

    Reseller cannot create new orders for this product while disabled.
    Existing mappings remain; the reseller's custom fields are preserved.
    """
    return {
        "event":       "product.disabled",
        "event_type":  "product.disabled",
        "product_id":  str(public_id),
        "name":        str(name) if name else None,
        "active":      False,
        "timestamp":   str(disabled_at),
        "note":        "Orders for this product are blocked until the owner re-enables it.",
    }


def build_product_enabled_payload(
    public_id: str,
    name: str,
    enabled_at: str,
) -> dict:
    """Safe payload for product.enabled events."""
    return {
        "event":       "product.enabled",
        "event_type":  "product.enabled",
        "product_id":  str(public_id),
        "name":        str(name) if name else None,
        "active":      True,
        "timestamp":   str(enabled_at),
    }


def build_product_deleted_payload(
    public_id: str,
    name: str,
    deleted_at: str,
) -> dict:
    """Safe payload for product.deleted events.

    The product row has been permanently deleted. Reseller mappings that
    reference this product_id will have been cascade-deleted by ON DELETE
    CASCADE on reseller_product_mappings. The reseller should remove any
    local reference to this product_id.
    """
    return {
        "event":       "product.deleted",
        "event_type":  "product.deleted",
        "product_id":  str(public_id),
        "name":        str(name) if name else None,
        "timestamp":   str(deleted_at),
        "note":        "This product no longer exists. Remove any local references to it.",
    }


def build_stock_updated_payload(
    public_id: str,
    name: str,
    available_quantity,
    updated_at: str,
) -> dict:
    """Safe payload for stock.updated events.

    Availability count is included so resellers can decide whether to
    continue promoting the product. Internal stock item IDs and delivery
    codes are excluded.
    """
    return {
        "event":              "stock.updated",
        "event_type":         "stock.updated",
        "product_id":         str(public_id),
        "name":               str(name) if name else None,
        "available_quantity": int(available_quantity) if available_quantity is not None else 0,
        "timestamp":          str(updated_at),
    }

__all__ = [
    "OUTBOX_EVENT_TYPES",
    "STATUS_PENDING",
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "ensure_webhook_outbox_table",
    "emit_event",
    "emit_event_atomic",
    "build_order_created_payload",
    "build_delivery_completed_payload",
    "build_order_failed_payload",
    "build_order_cancelled_payload",
    "build_delivery_pending_payload",
    "build_delivery_failed_payload",
    "build_refund_created_payload",
    "get_pending_events",
    "get_outbox_row",
    "get_outbox_for_order",
    # Phase 11 P6 product sync builders
    "build_product_updated_payload",
    "build_product_disabled_payload",
    "build_product_enabled_payload",
    "build_product_deleted_payload",
    "build_stock_updated_payload",
]
