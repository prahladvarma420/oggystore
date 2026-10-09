
"""Phase 11 P6 -- Master product -> reseller sync service.

Architecture
-----------
The reseller API has NO per-reseller copy of product data.  All catalog
reads go live to the master `products` table.  "Sync" therefore means:

  1. ORDER-TIME ENFORCEMENT  (already in place, no new code needed)
     * get_reseller_api_product filters WHERE p.active=1 -- a disabled
       product is immediately invisible to resellers at the DB level.
     * get_available_stock_count reads live master stock.
     * resolve_reseller_price_cents reads master price live on every call;
       rule-based resellers keep their rule applied on top automatically.

  2. WEBHOOK NOTIFICATIONS  (this module)
     When the owner changes a master product, every reseller that has an
     active access grant receives a webhook event via the existing
     transactional outbox -> delivery-worker pipeline.

Supported sync events (based on actual web_admin code paths):
  product.updated   -- any owner field change (name, price, description...)
  product.disabled  -- owner toggled active=0
  product.enabled   -- owner toggled active=1
  product.deleted   -- owner deleted the product row
  stock.updated     -- owner added/changed stock levels

Events NOT implemented:
  product.created   -- resellers do not auto-receive access to new products;
                       the owner must explicitly grant access per-reseller.
  product.archived  -- no archive concept in the codebase; products are
                       either active, inactive (disabled), or deleted.
  price.updated     -- covered by product.updated with changed_fields=["price"];
                       a separate event type is unnecessary.

Cache:
  Product data is NOT cached at the reseller API layer.  The only
  in-process cache is the global API kill-switch in security_controls.py
  (30 s TTL, unrelated to product data).  There is therefore nothing to
  invalidate for product changes -- "no stale data" is guaranteed by
  reading master tables live on every request.

Concurrency guarantee:
  emit_event_atomic() uses the same INSERT OR IGNORE dedup index as all
  other outbox writes.  A duplicate call for the same
  (reseller_id, event_type, aggregate_id) is a safe no-op.
"""
from __future__ import annotations

import logging
from typing import Optional

from utils.timezone import now_text

log = logging.getLogger(__name__)

__all__ = [
    "emit_product_event_for_resellers",
    "notify_product_updated",
    "notify_product_disabled",
    "notify_product_enabled",
    "notify_product_deleted",
    "notify_stock_updated",
    "get_resellers_with_product_access",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def get_resellers_with_product_access(product_id: int) -> list:
    """Return all resellers with an active access grant for product_id.

    Returns [{reseller_id: int, public_id: str}, ...]
    """
    from database.db import get_cursor
    cur = get_cursor()
    cur.execute(
        "SELECT a.reseller_id, r.public_id "
        "FROM reseller_product_access a "
        "JOIN resellers r ON r.id = a.reseller_id "
        "WHERE a.product_id = ? AND a.enabled = 1",
        (int(product_id),),
    )
    return [
        {"reseller_id": int(r[0]), "public_id": str(r[1] or "")}
        for r in cur.fetchall()
    ]


def _get_product_row(product_id: int):
    """Return (public_id, name) for a product, or (None, None) if deleted."""
    from database.db import get_cursor
    cur = get_cursor()
    cur.execute("SELECT public_id, name FROM products WHERE id=?", (int(product_id),))
    row = cur.fetchone()
    if not row:
        return None, None
    pub = str(row[0]) if row[0] else None
    if not pub:
        from database.reseller_db import ensure_product_public_id
        pub = ensure_product_public_id(product_id)
    return pub, (str(row[1]) if row[1] else None)


# ---------------------------------------------------------------------------
# Core fan-out primitive
# ---------------------------------------------------------------------------

def emit_product_event_for_resellers(
    product_id: int,
    product_public_id: str,
    event_type: str,
    payload: dict,
    resellers=None,
) -> int:
    """Emit one outbox event per reseller that has access to this product.

    aggregate_id = <product_public_id>:<minute-bucket> so the dedup index
    prevents duplicate events for the same (reseller_id, event_type,
    product, minute) without permanently suppressing later changes.

    Returns the count of events successfully enqueued.
    """
    from reseller_api.services.webhook_outbox import emit_event_atomic

    if resellers is None:
        resellers = get_resellers_with_product_access(product_id)

    if not resellers:
        log.debug(
            "[product_sync] %s product_id=%s: no resellers with access",
            event_type, product_id,
        )
        return 0

    # aggregate_id carries a coarse per-minute bucket alongside the product's
    # public id. The outbox dedup index (reseller_id, event_type, aggregate_id)
    # therefore still collapses accidental double-emission of the SAME change
    # (retries / double-clicks within the same minute), while a LATER
    # legitimate change of the same event type for the same product is
    # delivered normally. Without the bucket, the permanent unique index
    # silently swallowed every repeat event forever (one-shot semantics).
    bucket = now_text()[:16]  # "YYYY-MM-DD HH:MM"
    aggregate_id = "{}:{}".format(product_public_id, bucket)
    emitted = 0
    for rs in resellers:
        rid = int(rs["reseller_id"])
        try:
            row_id = emit_event_atomic(rid, event_type, aggregate_id, payload)
            if row_id:
                emitted += 1
                log.info(
                    "[product_sync] queued %s outbox_id=%s reseller_id=%s product=%s",
                    event_type, row_id, rid, aggregate_id,
                )
        except Exception:
            log.exception(
                "[product_sync] failed to emit %s reseller_id=%s product=%s",
                event_type, rid, aggregate_id,
            )
    return emitted


# ---------------------------------------------------------------------------
# Public notification entry points (called from web_admin after commits)
# ---------------------------------------------------------------------------

def notify_product_updated(product_id: int, changed_fields=None) -> int:
    """Emit product.updated to all resellers with access.

    Call AFTER the UPDATE products SQL has committed.
    changed_fields is the list of field names modified (e.g. ["name","price"]).
    Internal/sensitive field names (cost, redeem_link, rules, delivery) are
    stripped from the payload before dispatch.
    """
    from reseller_api.services.webhook_outbox import build_product_updated_payload
    pub, name = _get_product_row(product_id)
    if not pub:
        return 0
    _INTERNAL = frozenset({"cost", "redeem_link", "rules", "delivery"})
    safe_fields = [f for f in (changed_fields or []) if f not in _INTERNAL]
    payload = build_product_updated_payload(
        public_id=pub, name=name,
        changed_fields=safe_fields, updated_at=now_text(),
    )
    return emit_product_event_for_resellers(product_id, pub, "product.updated", payload)


def notify_product_disabled(product_id: int) -> int:
    """Emit product.disabled to all resellers with access.

    Call AFTER UPDATE products SET active=0 has committed.
    Order-time enforcement is already in place via the p.active=1 filter;
    this notification lets resellers react promptly in their own systems.
    """
    from reseller_api.services.webhook_outbox import build_product_disabled_payload
    pub, name = _get_product_row(product_id)
    if not pub:
        return 0
    payload = build_product_disabled_payload(
        public_id=pub, name=name, disabled_at=now_text()
    )
    return emit_product_event_for_resellers(product_id, pub, "product.disabled", payload)


def notify_product_enabled(product_id: int) -> int:
    """Emit product.enabled to all resellers with access.

    Call AFTER UPDATE products SET active=1 has committed.
    """
    from reseller_api.services.webhook_outbox import build_product_enabled_payload
    pub, name = _get_product_row(product_id)
    if not pub:
        return 0
    payload = build_product_enabled_payload(
        public_id=pub, name=name, enabled_at=now_text()
    )
    return emit_product_event_for_resellers(product_id, pub, "product.enabled", payload)


def notify_product_deleted(
    product_id: int,
    product_public_id: str,
    product_name,
    resellers=None,
) -> int:
    """Emit product.deleted to all resellers with access.

    IMPORTANT: call this BEFORE the DELETE FROM products SQL executes.
    After deletion the public_id and reseller_product_access rows are gone
    (cascade-deleted), so the access lookup and public_id resolution must
    happen first.

    Parameters
    ----------
    product_id         : internal integer id (for the access look-up).
    product_public_id  : PRD-* string captured BEFORE the delete.
    product_name       : product name captured BEFORE the delete.
    resellers          : pre-fetched list captured BEFORE the delete,
                         or None to fetch inside this call.
    """
    from reseller_api.services.webhook_outbox import build_product_deleted_payload
    payload = build_product_deleted_payload(
        public_id=product_public_id, name=product_name, deleted_at=now_text()
    )
    return emit_product_event_for_resellers(
        product_id, product_public_id, "product.deleted", payload,
        resellers=resellers,
    )


def notify_stock_updated(product_id: int, available_quantity) -> int:
    """Emit stock.updated to all resellers with access.

    Call AFTER stock rows have been committed.
    available_quantity is the current count of unsold/available stock items.
    """
    from reseller_api.services.webhook_outbox import build_stock_updated_payload
    pub, name = _get_product_row(product_id)
    if not pub:
        return 0
    payload = build_stock_updated_payload(
        public_id=pub, name=name,
        available_quantity=available_quantity, updated_at=now_text(),
    )
    return emit_product_event_for_resellers(product_id, pub, "stock.updated", payload)
