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
    """
    if guard_column not in _VALID_GUARD_COLS:
        raise ValueError(
            f"Invalid guard column '{guard_column}'. "
            f"Must be one of: {sorted(_VALID_GUARD_COLS)}"
        )
    cur = get_cursor()
    conn = get_conn()
    # Check idempotency guard
    cur.execute(
        f"SELECT {guard_column} FROM orders WHERE id=?",
        (int(order_id),),
    )
    row = cur.fetchone()
    if row and row[0]:
        return False  # already restored
    cur.execute(
        "UPDATE products SET stock = stock + ? WHERE id = ?",
        (int(quantity), int(product_id)),
    )
    cur.execute(
        f"UPDATE orders SET {guard_column}=1 WHERE id=?",
        (int(order_id),),
    )
    conn.commit()
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
    """Write encrypted delivery to an order row, only if not already set.

    Returns:
        'written'      — delivery stored
        'already_set'  — delivery already present (idempotent)
    """
    cur = get_cursor()
    conn = get_conn()
    cur.execute("SELECT delivery FROM orders WHERE id=?", (int(order_id),))
    row = cur.fetchone()
    if row and row[0]:
        return "already_set"
    cur.execute(
        "UPDATE orders SET delivery=? WHERE id=? AND (delivery IS NULL OR delivery='')",
        (str(encrypted_delivery), int(order_id)),
    )
    conn.commit()
    return "written"


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
