"""Delivery retrieval service — Phase 8.

Decrypts and returns delivery items for a completed reseller order.
IDOR prevention: always scopes queries to the authenticated reseller_id.
"""

from typing import List

from database.db import get_cursor
from utils.crypto import decrypt_text
from reseller_api.context import ApiRequestContext
from reseller_api.errors import ApiError
from reseller_api.services.order_status import derive_order_status
from reseller_api.services.authorization import DELIVERY_READ, authorize_operation

import re

_PUBLIC_ORDER_ID_RE = re.compile(r'^ORD-(\d{8})$')


def _parse_order_id(raw: str) -> int:
    m = _PUBLIC_ORDER_ID_RE.match(str(raw or '').strip())
    if not m:
        raise ApiError('invalid_order_id', 'Order ID format is invalid. Expected ORD-XXXXXXXX.', 400)
    return int(m.group(1))


def _decrypt_items(delivery_blob: str) -> List[str]:
    """Decrypt and split the delivery blob into individual item strings."""
    if not delivery_blob:
        return []
    try:
        plain = decrypt_text(delivery_blob)
        if not plain:
            return []
        # Split on double newline or separator line
        sep = '\u2500' * 28
        if sep in plain:
            parts = [p.strip() for p in plain.split(sep) if p.strip()]
        elif '\n\n' in plain:
            parts = [p.strip() for p in plain.split('\n\n') if p.strip()]
        else:
            parts = [plain.strip()] if plain.strip() else []
        return parts
    except Exception:
        return []


def get_order_delivery(
    context: ApiRequestContext,
    public_order_id: str,
) -> dict:
    """Return decrypted delivery items for an order owned by this reseller.

    Raises ApiError on permission failure, invalid ID, or missing order.
    Returns a dict with keys: order_id, delivery_status, items, message.
    """
    # Defense-in-depth behind the controller's @require_operation(DELIVERY_READ).
    # ApiRequestContext has no permissions attribute — revalidate mutable
    # key/reseller state through the standard policy layer instead. The
    # context environment was already pinned to this server's environment at
    # authentication time, so it is the authoritative expected value here.
    if not isinstance(context, ApiRequestContext):
        raise ApiError('authentication_failed',
                       'Authentication credentials are missing or invalid.', 401)
    decision = authorize_operation(context, DELIVERY_READ, context.environment)
    if not decision.allowed:
        raise ApiError('authorization_denied', 'This operation is not permitted.', 403)

    internal_id = _parse_order_id(public_order_id)

    cur = get_cursor()
    cur.execute(
        """
        SELECT id, delivery, master_status, product_id
        FROM orders
        WHERE id=? AND reseller_id=? AND source='reseller_api'
        """,
        (internal_id, context.reseller_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ApiError('order_not_found', 'Order not found.', 404)

    order_id_int, delivery_blob, master_status, product_id = row

    status = derive_order_status(order_id_int)

    if status.is_cancelled:
        return {
            'order_id': public_order_id,
            'delivery_status': 'cancelled',
            'items': [],
            'message': 'This order was cancelled.',
        }

    if status.is_failed:
        return {
            'order_id': public_order_id,
            'delivery_status': 'failed',
            'items': [],
            'message': 'This order failed. A refund has been issued if applicable.',
        }

    if not delivery_blob:
        return {
            'order_id': public_order_id,
            'delivery_status': 'pending',
            'items': [],
            'message': 'Delivery is being processed. Please check back shortly.',
        }

    items = _decrypt_items(delivery_blob)
    return {
        'order_id': public_order_id,
        'delivery_status': 'delivered',
        'items': items,
    }


__all__ = ['get_order_delivery']
