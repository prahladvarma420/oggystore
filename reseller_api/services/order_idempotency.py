"""Atomic idempotency helpers for reseller order operations — Phase 8.

All functions in this module are atomic at the SQLite level.
They are reusable across the order creation and failure recovery paths.
"""

import time
from typing import Optional, Tuple

from database.db import get_conn, get_cursor
from database.reseller_db import post_wallet_transaction


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKER_JOB_TTL_SECONDS = 300   # stale-job TTL for worker heartbeat
_VALID_GUARD_COLS = frozenset({"stock_restored", "partial_stock_restored"})


# ---------------------------------------------------------------------------
# Custom errors
# ---------------------------------------------------------------------------

class StockExhaustedError(ValueError):
    """Raised when atomic stock deduction fails due to insufficient quantity."""
    pass


# ---------------------------------------------------------------------------
# Request-slot idempotency
# ---------------------------------------------------------------------------

def claim_request_slot(reseller_id: int, request_id: str) -> Optional[int]:
    """Return existing order_id if this (reseller_id, request_id) was already processed.

    Returns None if this is a new request slot.
    """
    if not request_id:
        return None
    cur = get_cursor()
    cur.execute(
        "SELECT id FROM orders WHERE reseller_id=? AND request_id=? AND source='reseller_api'",
        (int(reseller_id), str(request_id)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# Stock operations
# ---------------------------------------------------------------------------

def atomic_deduct_stock(product_id: int, quantity: int) -> bool:
    """Atomically deduct stock from products.stock.

    Uses a conditional UPDATE: only succeeds if stock >= quantity.
    Raises StockExhaustedError on insufficient stock.
    Returns True on success.
    """
    cur = get_cursor()
    conn = get_conn()
    cur.execute(
        "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
        (int(quantity), int(product_id), int(quantity)),
    )
    if cur.rowcount != 1:
        raise StockExhaustedError(
            f"Insufficient stock for product {product_id} (requested {quantity})."
        )
    conn.commit()
    return True


def atomic_restore_stock(
    product_id: int,
    quantity: int,
    guard_column: str,
    order_id: int,
) -> bool:
    """Atomically restore stock and mark the guard column on the order row.

    The guard_column must be one of the allowed set to prevent SQL injection.
    Returns True if stock was restored; False if already restored (idempotent).

    Phase 12 P4: code-stock products restore by precise order attribution —
    order creation links claimed product_stock rows via order_id, so the
    restore releases exactly this order's units (never another order's).
    When no attributed rows exist (manual products, or legacy orders created
    before attribution), the manual products.stock counter is bumped instead,
    preserving the original behavior.

    Phase 12 P7: the guard claim and the stock restore now commit in ONE
    pgcompat.atomic block, and the guard is claimed with a conditional
    UPDATE (… WHERE guard IS NULL OR guard=0).  Two concurrent restore
    attempts can no longer both pass a check-then-act guard read and
    double-restore the manual stock counter — exactly one wins the claim;
    the loser gets rowcount=0 and returns False.  A crash anywhere inside
    the block rolls the guard claim back with the restore, so a retry can
    complete it later.
    """
    if guard_column not in _VALID_GUARD_COLS:
        raise ValueError(
            f"Invalid guard column '{guard_column}'. "
            f"Must be one of: {sorted(_VALID_GUARD_COLS)}"
        )
    import database.pgcompat as _pgcompat
    cur = get_cursor()
    conn = get_conn()
    with _pgcompat.atomic(conn):
        # Claim the guard first, atomically. rowcount==0 means a previous
        # (or concurrent) run already restored — idempotent no-op.
        cur.execute(
            f"UPDATE orders SET {guard_column}=1 "
            f"WHERE id=? AND ({guard_column} IS NULL OR {guard_column}=0)",
            (int(order_id),),
        )
        if cur.rowcount != 1:
            return False  # already restored (or order missing)
        # Precise release: up to `quantity` code rows attributed to this order.
        cur.execute(
            "SELECT id FROM product_stock WHERE order_id=? AND sold=1 LIMIT ?",
            (int(order_id), int(quantity)),
        )
        ids = [int(r[0]) for r in cur.fetchall()]
        if ids:
            ph = ",".join("?" for _ in ids)
            cur.execute(
                f"UPDATE product_stock "
                f"SET sold=0, sold_to=NULL, sold_at=NULL, order_id=NULL "
                f"WHERE id IN ({ph})",
                tuple(ids),
            )
        else:
            cur.execute(
                "UPDATE products SET stock = stock + ? WHERE id = ?",
                (int(quantity), int(product_id)),
            )
    return True


# ---------------------------------------------------------------------------
# Wallet operations
# ---------------------------------------------------------------------------

def idempotent_wallet_debit(
    reseller_id: int,
    amount_cents: int,
    reference: str,
    description: str,
) -> Tuple[str, object]:
    """Debit the reseller wallet idempotently.

    Returns (status, data) where status is:
        'debited'           — new debit posted
        'already_debited'   — reference already exists (idempotent replay)
        'insufficient_funds'— balance too low
    """
    status, data = post_wallet_transaction(
        reseller_id=int(reseller_id),
        tx_type="purchase_debit",
        amount_cents=-int(amount_cents),
        reference=str(reference),
        description=str(description),
        related_order_reference=str(reference),
    )
    if status == "posted":
        return "debited", data
    if status == "duplicate":
        return "already_debited", data
    if status == "insufficient_funds":
        return "insufficient_funds", None
    return status, data


# ---------------------------------------------------------------------------
# Delivery write idempotency
# ---------------------------------------------------------------------------

def safe_write_delivery(order_id: int, encrypted_delivery: str) -> str:
    """Write once, joining the caller transaction without a mid-block commit.

    Standalone calls own an atomic block; SQLite/PG callers already inside
    pgcompat.atomic retain sole commit ownership. Legacy encrypted-empty
    values are writable. Conditional UPDATE + rowcount handles concurrent
    writes; terminal/missing rows are never reported as successful writes.

    Returns written | already_set | terminal | order_not_found.
    """
    from contextlib import nullcontext
    import database.pgcompat as pgcompat
    from reseller_api.services.order_delivery import has_stored_delivery
    conn = get_conn()
    joined = bool(getattr(conn, "in_transaction", False)
                  or getattr(conn, "_transaction_depth", 0))
    with nullcontext() if joined else pgcompat.atomic(conn):
        cur = get_cursor()
        cur.execute("SELECT delivery, master_status, source FROM orders WHERE id=?", (int(order_id),))
        row = cur.fetchone()
        if row is None:
            return "order_not_found"
        label = str(row[1] or "").lower()
        if any(s in label for s in ("failed", "cancelled", "canceled", "⚠", "❌")):
            return "terminal"
        if has_stored_delivery(row[0]):
            return "already_set"
        value = encrypted_delivery
        if row[2] == 'reseller_api':
            from reseller_api.services.order_status import delivery_present
            if not delivery_present(value):
                value = None
        observed = str(row[0] or "")
        cur.execute(
            "UPDATE orders SET delivery=? WHERE id=? AND COALESCE(delivery, '')=? "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%failed%' "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%cancelled%' "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%canceled%' "
            "AND COALESCE(master_status, '') NOT LIKE '%⚠%' "
            "AND COALESCE(master_status, '') NOT LIKE '%❌%'",
            (value if value is None else str(value), int(order_id), observed),
        )
        if cur.rowcount == 1:
            return "written"
        cur.execute("SELECT master_status FROM orders WHERE id=?", (int(order_id),))
        current = cur.fetchone()
        if current is None:
            return "order_not_found"
        label = str(current[0] or "").lower()
        return "terminal" if any(s in label for s in ("failed", "cancelled", "canceled", "⚠", "❌")) else "already_set"


# ---------------------------------------------------------------------------
# Worker-job claim (heartbeat-based)
# ---------------------------------------------------------------------------

def claim_worker_job(
    job_key: str,
    worker_id: str,
    ttl_seconds: int = WORKER_JOB_TTL_SECONDS,
) -> bool:
    """Claim a job slot idempotently.

    Inserts a new row or reclaims a stale row (heartbeat_at too old).
    Returns True on success, False if already claimed by another worker.
    """
    cur = get_cursor()
    conn = get_conn()
    now = int(time.time())
    stale_threshold = now - int(ttl_seconds)

    # Try to claim a stale or non-existent slot
    cur.execute(
        """
        INSERT INTO worker_jobs(job_key, worker_id, claimed_at, heartbeat_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(job_key) DO UPDATE
          SET worker_id=excluded.worker_id,
              claimed_at=excluded.claimed_at,
              heartbeat_at=excluded.heartbeat_at
          WHERE heartbeat_at < ?
        """,
        (str(job_key), str(worker_id), now, now, stale_threshold),
    )
    conn.commit()
    # Verify we own the slot
    cur.execute(
        "SELECT worker_id FROM worker_jobs WHERE job_key=?",
        (str(job_key),),
    )
    row = cur.fetchone()
    return bool(row and row[0] == str(worker_id))


__all__ = [
    "StockExhaustedError",
    "WORKER_JOB_TTL_SECONDS",
    "claim_request_slot",
    "atomic_deduct_stock",
    "atomic_restore_stock",
    "idempotent_wallet_debit",
    "safe_write_delivery",
    "claim_worker_job",
]
