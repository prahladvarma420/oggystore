"""Order status derivation — Phase 8.

Derives the canonical status of a reseller order from the master orders
table. All status logic is read-only; this module never writes to the DB.
"""

from dataclasses import dataclass
from database.db import get_cursor


@dataclass(frozen=True)
class OrderStatus:
    order_id: int
    order_status: str          # 'processing' | 'completed' | 'failed' | 'cancelled'
    delivery_status: str       # 'pending' | 'delivered' | 'failed'
    is_completed: bool
    is_cancelled: bool
    is_failed: bool
    is_processing: bool
    master_status_label: str   # raw label from DB, for internal use only


status_map = {
    chr(0x2705): 'completed',
    chr(0x1f504): 'processing',
    chr(0x26a0): 'failed',
    chr(0x274c): 'cancelled',
}


def derive_order_status(order_id: int) -> OrderStatus:
    """Derive canonical order status from master_status label."""
    cur = get_cursor()
    cur.execute(
        "SELECT master_status, delivery FROM orders WHERE id=?",
        (int(order_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Order {order_id} not found.")

    label = str(row[0] or '')
    delivery = row[1]

    # Map emoji prefix to canonical string
    order_status = 'processing'  # default
    for emoji, status in status_map.items():
        if label.startswith(emoji):
            order_status = status
            break
    # Fallback for old labels that don't have emoji prefix
    if 'Completed' in label or 'Complete' in label:
        order_status = 'completed'
    elif 'Failed' in label or 'failed' in label:
        order_status = 'failed'
    elif 'Cancelled' in label or 'Canceled' in label:
        order_status = 'cancelled'

    delivery_status = 'delivered' if delivery else 'pending'
    if order_status == 'failed':
        delivery_status = 'failed'

    return OrderStatus(
        order_id=order_id,
        order_status=order_status,
        delivery_status=delivery_status,
        is_completed=(order_status == 'completed'),
        is_cancelled=(order_status == 'cancelled'),
        is_failed=(order_status == 'failed'),
        is_processing=(order_status == 'processing'),
        master_status_label=label,
    )


__all__ = ['OrderStatus', 'derive_order_status']
