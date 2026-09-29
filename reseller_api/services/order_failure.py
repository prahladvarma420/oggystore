"""Failure, cancellation, and refund handling — Phase 8.

Handles post-debit failure recovery, cancellations, and partial refunds.
All refund operations are idempotent: calling twice is safe.
"""

from typing import Optional

from database.db import get_conn, get_cursor
from database.reseller_db import post_wallet_transaction


# ---------------------------------------------------------------------------
# Refund tx types
# ---------------------------------------------------------------------------

TX_REFUND         = "refund_credit"
TX_CANCEL_REFUND  = "cancellation_refund"
TX_PARTIAL_REFUND = "partial_refund_credit"


# ---------------------------------------------------------------------------
# Terminal status labels
# ---------------------------------------------------------------------------

_STATUS_FAILED    = chr(0x26a0) + " Failed"
_STATUS_CANCELLED = chr(0x274c) + " Cancelled"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_order_row(order_id: int) -> Optional[dict]:
    cur = get_cursor()
    cur.execute(
        """
        SELECT id, reseller_id, product_id, quantity, unit_price_cents,
               master_status, stock_restored, partial_stock_restored
        FROM orders WHERE id=?
        """,
        (int(order_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "reseller_id": row[1], "product_id": row[2],
        "quantity": row[3], "unit_price_cents": row[4],
        "master_status": row[5] or "",
        "stock_restored": bool(row[6]), "partial_stock_restored": bool(row[7]),
    }


def _is_terminal(status: str) -> bool:
    return (
        _STATUS_FAILED in status or
        _STATUS_CANCELLED in status or
        chr(0x274c) in status or
        chr(0x26a0) in status
    )


def _mark_order_failed(order_id: int, reason: str = ""):
    cur = get_cursor()
    conn = get_conn()
    cur.execute(
        "UPDATE orders SET master_status=?, failure_reason=? WHERE id=?",
        (_STATUS_FAILED, str(reason), int(order_id)),
    )
    conn.commit()


def _mark_order_cancelled(order_id: int):
    cur = get_cursor()
    conn = get_conn()
    cur.execute(
        "UPDATE orders SET master_status=? WHERE id=?",
        (_STATUS_CANCELLED, int(order_id)),
    )
    conn.commit()


def _issue_refund(
    reseller_id: int,
    amount_cents: int,
    order_id: int,
    tx_type: str,
) -> bool:
    """Issue a refund credit. Returns True if newly posted, False if duplicate."""
    ref = f"rapi.refund.order.{order_id}"
    status, _ = post_wallet_transaction(
        reseller_id=int(reseller_id),
        tx_type=tx_type,
        amount_cents=int(amount_cents),   # positive = credit
        reference=ref,
        description=f"Refund for order ORD-{int(order_id):08d}",
        related_order_reference=ref,
    )
    return status in ("posted", "duplicate")


def _restore_stock(product_id: int, quantity: int, order_id: int, guard_col: str) -> bool:
    """Restore stock with idempotency guard. Returns True if newly restored."""
    from reseller_api.services.order_idempotency import atomic_restore_stock
    return atomic_restore_stock(product_id, quantity, guard_col, order_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def handle_post_debit_failure(
    order_id: int,
    restore_stock: bool = True,
    reason: str = "fulfillment_failure",
) -> dict:
    """Handle a post-debit fulfillment failure.

    Marks the order as Failed, refunds the full amount, and optionally
    restores stock. Idempotent: safe to call multiple times.

    Returns a result dict with keys: result, refunded, stock_restored.
    """
    row = _get_order_row(order_id)
    if row is None:
        return {"result": "order_not_found", "refunded": False, "stock_restored": False}

    if _is_terminal(row["master_status"]):
        return {"result": "already_terminal", "refunded": False, "stock_restored": False}

    _mark_order_failed(order_id, reason)

    refunded = False
    if row["unit_price_cents"] and row["quantity"]:
        amount = int(row["unit_price_cents"]) * int(row["quantity"])
        refunded = _issue_refund(row["reseller_id"], amount, order_id, TX_REFUND)

    stocked = False
    if restore_stock and row["product_id"] and row["quantity"] and not row["stock_restored"]:
        stocked = _restore_stock(
            row["product_id"], row["quantity"], order_id, "stock_restored"
        )

    return {"result": "handled", "refunded": refunded, "stock_restored": stocked}


def handle_cancellation(
    order_id: int,
    post_debit: bool = False,
) -> dict:
    """Cancel an order, optionally issuing a refund if already debited.

    Args:
        order_id:   The internal order ID.
        post_debit: True if the wallet was already debited (triggers refund).

    Returns a result dict with keys: result, refunded.
    """
    row = _get_order_row(order_id)
    if row is None:
        return {"result": "order_not_found", "refunded": False}

    if _is_terminal(row["master_status"]):
        return {"result": "already_terminal", "refunded": False}

    _mark_order_cancelled(order_id)

    refunded = False
    if post_debit and row["unit_price_cents"] and row["quantity"]:
        amount = int(row["unit_price_cents"]) * int(row["quantity"])
        refunded = _issue_refund(row["reseller_id"], amount, order_id, TX_CANCEL_REFUND)

    return {"result": "cancelled", "refunded": refunded}


def handle_partial_failure(
    order_id: int,
    fulfilled_qty: int,
    failed_qty: int,
) -> dict:
    """Issue a partial refund for units that failed to fulfil.

    The order is NOT marked as failed — it completed partially.
    Idempotent via the refund reference uniqueness constraint.

    Returns a result dict with keys: result, refund_cents.
    """
    row = _get_order_row(order_id)
    if row is None:
        return {"result": "order_not_found", "refund_cents": 0}

    if not row["unit_price_cents"] or failed_qty <= 0:
        return {"result": "no_refund_needed", "refund_cents": 0}

    refund_cents = int(row["unit_price_cents"]) * int(failed_qty)

    # Use a distinct reference so it doesn’t clash with a full-refund
    ref = f"rapi.partial_refund.order.{order_id}"
    status, _ = post_wallet_transaction(
        reseller_id=int(row["reseller_id"]),
        tx_type=TX_PARTIAL_REFUND,
        amount_cents=refund_cents,
        reference=ref,
        description=(
            f"Partial refund for order ORD-{int(order_id):08d} "
            f"({failed_qty} of {row['quantity']} units failed)"
        ),
        related_order_reference=ref,
    )

    if row["product_id"] and failed_qty > 0 and not row["partial_stock_restored"]:
        _restore_stock(
            row["product_id"], failed_qty, order_id, "partial_stock_restored"
        )

    return {"result": "partial_refund_issued", "refund_cents": refund_cents}


__all__ = [
    "TX_REFUND", "TX_CANCEL_REFUND", "TX_PARTIAL_REFUND",
    "handle_post_debit_failure",
    "handle_cancellation",
    "handle_partial_failure",
]
