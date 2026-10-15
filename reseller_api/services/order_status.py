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


class OrderStatusUnavailable(ValueError):
    """Ciphertext cannot be authenticated; never guess its delivery presence."""


def persisted_terminal_status(label):
    value = str(label or '').lower()
    if 'failed' in value or value.startswith('⚠'):
        return 'failed'
    if 'cancelled' in value or 'canceled' in value or value.startswith('❌'):
        return 'cancelled'
    return None


def delivery_present(value):
    if value is None or value == '':
        return False
    if isinstance(value, str) and value.startswith('enc:'):
        from utils.crypto import _fernet
        try:
            cipher = _fernet()
            if cipher is None:
                raise ValueError('Encryption unavailable')
            return cipher.decrypt(value[4:].encode('ascii')).decode('utf-8') != ''
        except Exception:
            raise OrderStatusUnavailable('Order delivery state cannot be verified.') from None
    return bool(value)


def load_order_status_row(order_row_or_id):
    """Load one read snapshot; mappings already containing all fields need no SQL."""
    from collections.abc import Mapping
    if isinstance(order_row_or_id, Mapping):
        row = dict(order_row_or_id)
        if all(k in row for k in ('id', 'master_status', 'delivery', 'replacement_status')):
            return row
        oid = int(row['id'])
    else:
        oid = int(order_row_or_id)
    cur = get_cursor()
    cur.execute("SELECT id,master_status,delivery,"
                "(SELECT status FROM replacements WHERE order_id=orders.id "
                "ORDER BY id DESC LIMIT 1) FROM orders WHERE id=?", (oid,))
    row = cur.fetchone()
    if row is None:
        raise ValueError('Order not found.')
    return dict(zip(('id','master_status','delivery','replacement_status'),row))


def api_order_status(order_row_or_id) -> str:
    """Terminal > latest legacy replacement > authenticated delivery presence.

    SQL filters share this precedence after explicit legacy normalization.
    Unknown ciphertext raises, never defaults to completed or processing.
    """
    row = load_order_status_row(order_row_or_id)
    terminal = persisted_terminal_status(row['master_status'])
    if terminal:
        return terminal
    replacement = row['replacement_status']
    if replacement in ('approved', 'auto'):
        return 'replaced'
    if replacement == 'pending':
        return 'warranty_review'
    return 'completed' if delivery_present(row['delivery']) else 'processing'


def derive_order_status(order_id: int) -> OrderStatus:
    row = load_order_status_row(order_id)
    status = api_order_status(row)
    # Terminal precedence must remain available even if old ciphertext is
    # unreadable; terminal orders can never become processing.
    if status == 'failed':
        delivery_status = 'failed'
    else:
        try:
            delivery_status = 'delivered' if delivery_present(row['delivery']) else 'pending'
        except OrderStatusUnavailable:
            if status != 'cancelled':
                raise
            # Preserve terminal precedence without inventing verified delivery.
            delivery_status = 'pending'
    return OrderStatus(int(row['id']), status, delivery_status,
        status == 'completed', status == 'cancelled', status == 'failed',
        status == 'processing', str(row['master_status'] or ''))


def status_filter_sql(status):
    """Canonical predicate, qualified for the orders table (never user SQL).

    No ciphertext-length inference. Caller must check normalization_ready for
    presence-dependent filters. Existing reseller/id index bounds the scan.
    """
    label = "LOWER(COALESCE(orders.master_status,''))"
    failed = f"({label} LIKE '%failed%' OR {label} LIKE '⚠%')"
    cancelled = f"({label} LIKE '%cancelled%' OR {label} LIKE '%canceled%' OR {label} LIKE '❌%')"
    if status == 'failed':
        return failed
    if status == 'cancelled':
        return f"(NOT {failed} AND {cancelled})"
    legacy = "COALESCE((SELECT status FROM replacements WHERE order_id=orders.id ORDER BY id DESC LIMIT 1),'')"
    empty = "(orders.delivery IS NULL OR orders.delivery='')"
    return f"(NOT {failed} AND NOT {cancelled} AND {legacy} NOT IN ('approved','auto','pending') AND " + (empty if status == 'processing' else f"NOT {empty}") + ")"


__all__ = ['OrderStatus', 'derive_order_status', 'api_order_status',
           'delivery_present', 'persisted_terminal_status', 'status_filter_sql', 'load_order_status_row',
           'OrderStatusUnavailable']
