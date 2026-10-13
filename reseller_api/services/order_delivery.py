"""Delivery retrieval service — Phase 8.

Decrypts and returns delivery items for a completed reseller order.
IDOR prevention: always scopes queries to the authenticated reseller_id.
"""

from typing import List

from database.db import get_cursor
from utils.crypto import decrypt_text
from reseller_api.context import ApiRequestContext
from reseller_api.errors import ApiError
from reseller_api.services.order_status import derive_order_status, persisted_terminal_status, delivery_present
from reseller_api.services.authorization import DELIVERY_READ, authorize_operation

import re

_PUBLIC_ORDER_ID_RE = re.compile(r'^ORD-(\d{8})$')


def _parse_order_id(raw: str) -> int:
    m = _PUBLIC_ORDER_ID_RE.match(str(raw or '').strip())
    if not m:
        raise ApiError('invalid_order_id', 'Order ID format is invalid. Expected ORD-XXXXXXXX.', 400)
    return int(m.group(1))


def has_stored_delivery(delivery_blob) -> bool:
    """Logical delivery presence, including legacy encrypted-empty values.

    Unreadable encrypted data is treated as delivered (fail closed). Only a
    successfully decrypted empty string can be considered undelivered.
    """
    if not delivery_blob:
        return False
    try:
        return bool(decrypt_text(delivery_blob))
    except Exception:
        return True


def _decrypt_items(delivery_blob: str) -> List[str]:
    """Decrypt and split the delivery blob into individual item strings."""
    if not delivery_blob:
        return []
    # Authenticate before decrypt_text can substitute an error placeholder.
    delivery_present(delivery_blob)
    try:
        plain = decrypt_text(delivery_blob)
        if not plain:
            return []
        # Nonempty whitespace is valid stored content under the existing contract.
        if not plain.strip():
            return [plain]
        # Split on double newline or separator line
        sep = '\u2500' * 28
        if sep in plain:
            parts = [p.strip() for p in plain.split(sep) if p.strip()]
        elif '\n\n' in plain:
            parts = [p.strip() for p in plain.split('\n\n') if p.strip()]
        else:
            parts = [plain.strip()] if plain.strip() else []
        return parts or [plain]
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

    if not has_stored_delivery(delivery_blob):
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


class DeliveryCompletionError(ValueError):
    """Safe owner-action error; never includes submitted delivery content."""
    def __init__(self, message, status=400):
        self.message = message
        self.status = status
        super().__init__(message)


def complete_manual_delivery(order_id: int, delivery: str) -> dict:
    """Complete one reseller order in a single outer business transaction.

    The owner route provides authZ. Eligibility is checked again under the
    row lock; safe_write_delivery joins this transaction and strict outbox
    emission propagates failures. No wallet, stock or external-network work.
    """
    import os
    import database.pgcompat as pgcompat
    from database.db import get_conn
    from utils.crypto import encrypt_text
    from utils.timezone import now_text
    from reseller_api.services.order_idempotency import safe_write_delivery
    from reseller_api.services.order_status import status_map
    from reseller_api.services.webhook_outbox import (
        emit_event_atomic, build_delivery_completed_payload,
    )
    if not isinstance(delivery, str) or not delivery:
        raise DeliveryCompletionError("Delivery must be a non-empty string.", 400)
    if len(delivery) > 10000:
        raise DeliveryCompletionError("Delivery exceeds 10,000 characters.", 413)
    public_id = f"ORD-{int(order_id):08d}"
    base = {"order_id": public_id, "delivery_status": "delivered"}
    conn = get_conn()
    with pgcompat.atomic(conn):
        cur = get_cursor()
        cur.execute(
            "SELECT source, delivery, master_status, reseller_id, product_id, quantity "
            "FROM orders WHERE id=?" + (" FOR UPDATE" if pgcompat.use_postgres() else ""),
            (int(order_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise DeliveryCompletionError("Order not found.", 404)
        source, stored, label, rid, pid, quantity = row
        if source != "reseller_api":
            raise DeliveryCompletionError("Only reseller API orders can be delivered here.", 400)
        if persisted_terminal_status(label) is not None:
            raise DeliveryCompletionError("Order is already terminal.", 409)
        if not (os.getenv("DATA_ENCRYPTION_KEY") or "").strip():
            raise DeliveryCompletionError("Delivery encryption is not configured.", 503)
        if has_stored_delivery(stored):
            return {**base, "note": "delivery already present", "written": False}
        # Metadata reads only: no self-healing helper can commit this block.
        cur.execute("SELECT public_id FROM resellers WHERE id=?", (rid,))
        reseller = cur.fetchone()
        cur.execute("SELECT public_id FROM products WHERE id=?", (pid,))
        product = cur.fetchone()
        if not reseller or not reseller[0] or not product or not product[0] or not quantity:
            raise DeliveryCompletionError("Required order metadata is unavailable.", 422)
        # force=True prevents user text starting with enc: bypassing encryption.
        encrypted = encrypt_text(delivery, force=True)
        if not encrypted.startswith("enc:") or encrypted == delivery:
            raise DeliveryCompletionError("Delivery encryption is unavailable.", 503)
        outcome = safe_write_delivery(int(order_id), encrypted)
        if outcome == "already_set":
            return {**base, "note": "delivery already present", "written": False}
        if outcome != "written":
            raise DeliveryCompletionError("Order is no longer eligible for delivery.",
                                          404 if outcome == "order_not_found" else 409)
        now = now_text()
        prefix = next(icon for icon, value in status_map.items() if value == "completed")
        cur.execute("UPDATE orders SET master_status=?, updated=? WHERE id=?",
                    (prefix + " Completed", now, int(order_id)))
        emit_event_atomic(
            int(rid), "delivery.completed", public_id,
            build_delivery_completed_payload(
                order_id=int(order_id), reseller_public_id=str(reseller[0]),
                product_public_id=str(product[0]), quantity=int(quantity), created_at=now,
            ), raise_on_error=True,
        )
    return {**base, "written": True}


__all__ = ['get_order_delivery', 'has_stored_delivery',
           'complete_manual_delivery', 'DeliveryCompletionError']
