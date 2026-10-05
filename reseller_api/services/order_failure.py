"""Failure, cancellation, and refund handling — Phase 8.

Handles post-debit failure recovery, cancellations, and partial refunds.
All refund operations are idempotent: calling twice is safe.
"""

from typing import Optional

from database.db import get_conn, get_cursor
from database.reseller_db import post_wallet_transaction
import database.pgcompat as _pgcompat
from utils.timezone import now_text
from reseller_api.services.webhook_outbox import (
    emit_event,
    emit_event_atomic,
    build_delivery_failed_payload,
    build_order_failed_payload,
    build_order_cancelled_payload,
    build_refund_created_payload,
)


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
               master_status, stock_restored, partial_stock_restored,
               source, client_order_id
        FROM orders WHERE id=?
        """,
        (int(order_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    # Fetch reseller public_id for outbox payloads
    cur.execute("SELECT public_id FROM resellers WHERE id=?", (int(row[1]),))
    rr = cur.fetchone()
    reseller_public_id = str(rr[0]) if rr else ""
    return {
        "id": row[0], "reseller_id": row[1], "product_id": row[2],
        "quantity": row[3], "unit_price_cents": row[4],
        "master_status": row[5] or "",
        "stock_restored": bool(row[6]), "partial_stock_restored": bool(row[7]),
        "source": str(row[8] or ""),
        "client_order_id": row[9],
        "reseller_public_id": reseller_public_id,
    }


def _is_terminal(status: str) -> bool:
    return (
        _STATUS_FAILED in status or
        _STATUS_CANCELLED in status or
        chr(0x274c) in status or
        chr(0x26a0) in status
    )


def _mark_order_failed(
    order_id: int, reason: str = "",
    reseller_id: int = 0, reseller_public_id: str = "",
    source: str = "",
):
    conn = get_conn()
    cur  = get_cursor()
    now  = now_text()
    # Phase 9: only reseller_api orders emit webhook events; customer orders skip.
    _is_reseller = (source == "reseller_api") and bool(reseller_id) and bool(reseller_public_id)
    with _pgcompat.atomic(conn):
        cur.execute(
            "UPDATE orders SET master_status=?, failure_reason=? WHERE id=?",
            (_STATUS_FAILED, str(reason), int(order_id)),
        )
        if _is_reseller:
            # Both order.failed and delivery.failed emitted atomically with the status
            # update. INSERT OR IGNORE deduplicates on repeat calls.
            emit_event(
                cur, int(reseller_id), "order.failed",
                f"ORD-{int(order_id):08d}",
                build_order_failed_payload(
                    order_id=order_id,
                    reseller_public_id=str(reseller_public_id),
                    reason=str(reason), failed_at=now,
                ),
            )
            emit_event(
                cur, int(reseller_id), "delivery.failed",
                f"ORD-{int(order_id):08d}",
                build_delivery_failed_payload(
                    order_id=order_id,
                    reseller_public_id=str(reseller_public_id),
                    reason=str(reason), failed_at=now,
                ),
            )


def _mark_order_cancelled(
    order_id: int, reseller_id: int = 0,
    reseller_public_id: str = "", source: str = "",
):
    conn = get_conn()
    cur  = get_cursor()
    now  = now_text()
    _is_reseller = (source == "reseller_api") and bool(reseller_id) and bool(reseller_public_id)
    with _pgcompat.atomic(conn):
        cur.execute(
            "UPDATE orders SET master_status=? WHERE id=?",
            (_STATUS_CANCELLED, int(order_id)),
        )
        if _is_reseller:
            emit_event(
                cur, int(reseller_id), "order.cancelled",
                f"ORD-{int(order_id):08d}",
                build_order_cancelled_payload(
                    order_id=order_id,
                    reseller_public_id=str(reseller_public_id),
                    cancelled_at=now,
                ),
            )


def _issue_refund(
    reseller_id: int,
    amount_cents: int,
    order_id: int,
    tx_type: str,
    reseller_public_id: str = "",
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
    if status in ("posted", "duplicate") and reseller_id and reseller_public_id:
        _refund_type = {
            TX_REFUND:         "full",
            TX_CANCEL_REFUND:  "cancellation",
            TX_PARTIAL_REFUND: "partial",
        }.get(tx_type, tx_type)
        emit_event_atomic(
            int(reseller_id),
            "refund.created",
            f"ORD-{int(order_id):08d}",
            build_refund_created_payload(
                order_id=order_id,
                reseller_public_id=str(reseller_public_id),
                amount_cents=int(amount_cents),
                refund_type=_refund_type,
                created_at=now_text(),
            ),
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

    _mark_order_failed(
        order_id, reason,
        reseller_id=row["reseller_id"],
        reseller_public_id=row.get("reseller_public_id", ""),
        source=row.get("source", ""),
    )

    refunded = False
    if row["unit_price_cents"] and row["quantity"]:
        amount = int(row["unit_price_cents"]) * int(row["quantity"])
        refunded = _issue_refund(row["reseller_id"], amount, order_id, TX_REFUND, reseller_public_id=row.get("reseller_public_id", ""))

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

    _mark_order_cancelled(
        order_id,
        reseller_id=row["reseller_id"],
        reseller_public_id=row.get("reseller_public_id", ""),
        source=row.get("source", ""),
    )

    refunded = False
    if post_debit and row["unit_price_cents"] and row["quantity"]:
        amount = int(row["unit_price_cents"]) * int(row["quantity"])
        refunded = _issue_refund(row["reseller_id"], amount, order_id, TX_CANCEL_REFUND, reseller_public_id=row.get("reseller_public_id", ""))

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
