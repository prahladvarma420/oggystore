"""Stock validation and reservation for POST /api/v1/orders.

This module is a thin, API-aware wrapper around the MASTER stock system.
It does NOT duplicate stock business logic from database/db.py.

Existing master functions reused directly:
    get_available_stock_count(product_id)  – live unsold code count
    _release_claims(stock_ids)             – undo claimed code rows
    decrypt_text(delivery)                 – decrypt stored delivery text

Why we do NOT call reserve_stock_items() from the master:
    reserve_stock_items() returns only decrypted delivery strings, not the
    stock row IDs needed to call _release_claims() on failure. The order
    service (Prompt 5) must be able to roll back stock if the wallet debit
    fails after reservation. We therefore implement _api_reserve_code_stock()
    which uses the IDENTICAL conditional-UPDATE pattern (FIX43/FIX44) but
    returns (stock_ids, delivery_items) so the rollback path works.

Concurrency model (inherited from the master system, FIX43/FIX44):
    Every stock row is claimed via a single-statement conditional UPDATE:
        UPDATE product_stock SET sold=1 ... WHERE id=? AND sold=0
    rowcount==0 means another thread/process already claimed that row.
    Safe across multiple Railway instances without SELECT FOR UPDATE
    (which is unsafe under autocommit=True on Postgres).
    No in-memory locks are used.

Stock types:
    code / account  — uploaded codes/accounts in product_stock table.
                      Each row is atomically claimed via conditional UPDATE.
                      Delivery strings are returned decrypted.
    manual          — no uploaded codes; stock tracked in products.stock.
                      Decremented atomically via conditional UPDATE:
                        UPDATE products SET stock=stock-? WHERE id=? AND stock>=?
                      This is the same operation the customer bot uses.

Special product types that require interactive email delivery (M365, Canva)
are blocked upstream in the validation layer and never reach this module.
"""

import threading as _threading

from database.db import (
    get_available_stock_count,
    _release_claims,
    cur as db_cur,
    conn as db_conn,
)
from utils.crypto import decrypt_text
from utils.timezone import now_text
from reseller_api.services.order_pricing import OrderPricingResult
from reseller_api.services.order_validation import ValidatedOrderRequest


# ---------------------------------------------------------------------------
# Service error
# ---------------------------------------------------------------------------

class StockReservationError(ValueError):
    """Raised when stock cannot be reserved for the order."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


# ---------------------------------------------------------------------------
# Reservation result (value object)
# ---------------------------------------------------------------------------

class StockReservationResult:
    """Carries the reserved delivery content and release handle.

    delivery_items  — list of decrypted delivery strings (one per unit).
                       For manual-delivery products this is an empty list;
                       the delivery text lives on the product row itself.
    stock_ids       — list of product_stock.id integers that were claimed.
                       Empty for manual products. Used by release_stock().
    stock_type      — 'code' | 'manual'
    product_id      — int
    quantity        — int  (number of units reserved)
    released        — bool  (True after release_stock() is called)

    Call release_stock() inside any exception handler that fires after
    reserve_and_validate_stock() succeeds but before the order is committed.
    The wallet refund must also be issued in that handler (Prompt 5).
    """

    __slots__ = (
        "delivery_items",
        "stock_ids",
        "stock_type",
        "product_id",
        "quantity",
        "released",
    )

    def __init__(
        self,
        delivery_items: list,
        stock_ids: list,
        stock_type: str,
        product_id: int,
        quantity: int,
    ):
        self.delivery_items = list(delivery_items)
        self.stock_ids      = list(stock_ids)       # int IDs for _release_claims
        self.stock_type     = str(stock_type)       # 'code' | 'manual'
        self.product_id     = int(product_id)
        self.quantity       = int(quantity)
        self.released       = False

    def release_stock(self):
        """Undo the reservation. Safe to call more than once (idempotent).

        For code-based products: calls _release_claims() on the tracked IDs.
        For manual products: restores products.stock by the reserved quantity.
        """
        if self.released:
            return
        self.released = True
        if self.stock_type == "code" and self.stock_ids:
            _release_claims(self.stock_ids)
        elif self.stock_type == "manual":
            _restore_manual_stock(self.product_id, self.quantity)


# ---------------------------------------------------------------------------
# Code-stock reservation (API-specific, returns IDs for rollback)
# ---------------------------------------------------------------------------

def _api_reserve_code_stock(product_id: int, buyer_id: int, quantity: int):
    """Atomically claim `quantity` unsold code rows; return (stock_ids, deliveries).

    Uses the SAME conditional-UPDATE pattern as reserve_stock_items() in
    database/db.py (FIX43/FIX44). The only difference: we return the
    stock row IDs alongside decrypted delivery strings so that
    StockReservationResult.release_stock() can call _release_claims().

    Returns (stock_ids, delivery_items) on success.
    Returns (None, None) if the full quantity cannot be claimed.
    """
    quantity = int(quantity)
    if quantity <= 0:
        return None, None

    claimed = []   # list of (stock_id, encrypted_delivery)
    stamp = now_text()
    stalls = 0

    for _attempt in range(12):
        if len(claimed) >= quantity:
            break
        need = quantity - len(claimed)
        # Over-fetch generously: under contention most candidates will already
        # have been taken by another process by the time we try to claim them.
        db_cur.execute(
            "SELECT id, delivery FROM product_stock "
            "WHERE product_id=? AND sold=0 ORDER BY id ASC LIMIT ?",
            (product_id, need * 5 + 25),
        )
        rows = db_cur.fetchall()
        if not rows:
            break

        already = {sid for sid, _d in claimed}
        progressed = False
        for stock_id, delivery in rows:
            if len(claimed) >= quantity:
                break
            if stock_id in already:
                continue
            db_cur.execute(
                "UPDATE product_stock SET sold=1, sold_to=?, sold_at=? "
                "WHERE id=? AND sold=0",
                (buyer_id, stamp, stock_id),
            )
            if db_cur.rowcount == 1:
                claimed.append((stock_id, delivery))
                progressed = True

        if progressed:
            stalls = 0
            continue
        stalls += 1
        if stalls >= 4:
            break
        _threading.Event().wait(0.05 * stalls)

    if len(claimed) < quantity:
        _release_claims([sid for sid, _d in claimed])
        return None, None

    # Phase 12 P7: a failure AFTER the rows are claimed must not leak them.
    # If the commit fails, the claims were never durable — release them
    # anyway (the release UPDATEs are then harmless no-ops) and report the
    # reservation as failed so the caller never builds an order on rows that
    # are still unsold.  If decryption fails (corrupted ciphertext), release
    # the committed claims and let the caller surface a clean 422 instead of
    # an unhandled 500 with orphaned sold=1 rows.
    try:
        db_conn.commit()
    except Exception:
        _release_claims([sid for sid, _d in claimed])
        return None, None

    stock_ids = [sid for sid, _d in claimed]
    try:
        delivery_items = [decrypt_text(d) for _sid, d in claimed]
    except Exception:
        _release_claims(stock_ids)
        return None, None
    return stock_ids, delivery_items


# ---------------------------------------------------------------------------
# Manual stock helpers (no uploaded codes)
# ---------------------------------------------------------------------------

def _get_manual_stock(product_id: int) -> int:
    """Read products.stock for products that have no uploaded code rows."""
    db_cur.execute(
        "SELECT stock FROM products WHERE id=? AND active=1",
        (product_id,),
    )
    row = db_cur.fetchone()
    return int(row[0] or 0) if row else 0


def _claim_manual_stock(product_id: int, quantity: int) -> bool:
    """Atomically decrement products.stock by `quantity`.

    Uses a conditional single-statement UPDATE — the same operation as
    the customer buy_product() flow — so it is safe across multiple
    Railway instances without a row-level lock.

    Returns True if the decrement succeeded, False if stock was insufficient.
    """
    db_cur.execute(
        "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
        (quantity, product_id, quantity),
    )
    if db_cur.rowcount == 1:
        try:
            db_conn.commit()
        except Exception:
            pass
        return True
    # The no-match UPDATE still opened an implicit write transaction on this
    # thread's (long-lived) connection; close it so the connection does not
    # keep holding the database write lock and block other writers.
    try:
        db_conn.rollback()
    except Exception:
        pass
    return False


def _restore_manual_stock(product_id: int, quantity: int):
    """Undo a _claim_manual_stock on order failure."""
    try:
        db_cur.execute(
            "UPDATE products SET stock = stock + ? WHERE id = ?",
            (quantity, product_id),
        )
        db_conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reserve_and_validate_stock(
    validated: ValidatedOrderRequest,
    pricing: OrderPricingResult,
) -> StockReservationResult:
    """Validate and atomically reserve stock for a reseller order.

    Args:
        validated:  Server-resolved order request from validate_order_request().
        pricing:    Server-calculated pricing from calculate_order_price().

    Returns:
        StockReservationResult with reserved delivery content and release handle.

    Raises:
        StockReservationError: If stock is unavailable or cannot be atomically
                               claimed. No state has been modified in this case.

    IMPORTANT: If this function succeeds and a subsequent step fails, the
    caller MUST call result.release_stock() to return the stock, and must
    also refund the wallet if already debited (Prompt 5).
    """
    product_id = validated.product_id
    quantity   = validated.quantity
    # reseller_id is used as the sold_to marker. This is a server value;
    # the client cannot supply or influence it.
    buyer_id   = validated.reseller_id

    # --- Step 1: Check code-based stock (uploaded codes / accounts) ---
    live_count = 0 if getattr(validated,"email_product",None) else get_available_stock_count(product_id)

    if live_count > 0:
        # Code-based product. Validate quantity before attempting claim.
        if live_count < quantity:
            raise StockReservationError(
                "insufficient_stock",
                f"Only {live_count} unit(s) available for this product.",
                422,
            )
        # Atomic multi-unit reservation — returns IDs + delivery strings.
        stock_ids, delivery_items = _api_reserve_code_stock(
            product_id, buyer_id, quantity
        )
        if stock_ids is None:
            # Reservation failed (concurrent purchases drained stock).
            actual = get_available_stock_count(product_id)
            if actual < quantity:
                raise StockReservationError(
                    "insufficient_stock",
                    f"Only {actual} unit(s) available for this product.",
                    422,
                )
            raise StockReservationError(
                "stock_reservation_failed",
                "Stock could not be reserved. Please try again.",
                422,
            )
        return StockReservationResult(
            delivery_items=delivery_items,
            stock_ids=stock_ids,
            stock_type="code",
            product_id=product_id,
            quantity=quantity,
        )

    # --- Step 2: No uploaded codes — check manual stock counter ---
    manual_stock = _get_manual_stock(product_id)
    if manual_stock <= 0:
        raise StockReservationError(
            "out_of_stock",
            "This product is currently out of stock.",
            422,
        )
    if manual_stock < quantity:
        raise StockReservationError(
            "insufficient_stock",
            f"Only {manual_stock} unit(s) available for this product.",
            422,
        )

    # Atomically decrement the manual stock counter.
    if not _claim_manual_stock(product_id, quantity):
        actual = _get_manual_stock(product_id)
        if actual < quantity:
            raise StockReservationError(
                "insufficient_stock",
                f"Only {actual} unit(s) available for this product.",
                422,
            )
        raise StockReservationError(
            "stock_reservation_failed",
            "Stock could not be reserved. Please try again.",
            422,
        )

    return StockReservationResult(
        delivery_items=[],   # delivery text is on the product row itself
        stock_ids=[],
        stock_type="manual",
        product_id=product_id,
        quantity=quantity,
    )


__all__ = [
    "StockReservationError",
    "StockReservationResult",
    "reserve_and_validate_stock",
]
