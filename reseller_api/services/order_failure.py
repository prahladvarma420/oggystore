"""Failure, cancellation, and refund handling — Phase 8.

Handles post-debit failure recovery, cancellations, and partial refunds.
All refund operations are idempotent: calling twice is safe.

Phase 12 P7 hardening (no new architecture — the existing ledger and its
idempotency references remain the only refund mechanism):

  1. Crash-gap recovery.  Previously a crash between the terminal status
     commit and the refund commit left the order failed/cancelled but
     permanently charged: every retry returned "already_terminal" and never
     completed the refund.  Both terminal handlers are now resumable —
     a retry verifies the refund ledger row and the stock-restore guard and
     completes exactly the missing steps (idempotently).
  2. Per-order refund cap.  Every refund is capped at the order's
     OUTSTANDING charged amount (charged - refunds already posted for this
     order's references).  This makes over-refunding impossible, e.g. a
     partial refund posted after a full failure refund, or a full refund
     posted after a partial one.
  3. Never-debited guard.  Recovery paths confirm the original
     purchase_debit ledger row (reference rapi.{reseller_id}.{request_id})
     before crediting anything, so a pre-debit cancellation retried with
     post_debit=True cannot mint money.
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
#
# The persisted ledger (reseller_wallet_transactions CHECK constraint and the
# TX_TYPES whitelist in database/reseller_db.py) recognises exactly ONE refund
# tx_type: "refund_credit".  Earlier revisions of this module referenced
# "cancellation_refund" / "partial_refund_credit", which were never added to
# the schema whitelist and would have raised ValueError("invalid tx_type")
# at post_wallet_transaction the first time a refund was attempted.
# (Phase 12 P4 repair.)  Refund KINDS remain fully distinguishable through:
#   - the unique idempotency reference
#       rapi.refund.order.N / rapi.cancel_refund.order.N /
#       rapi.partial_refund.order.N
#   - the ledger description
#   - the refund_type field of the refund.created webhook payload
TX_REFUND         = "refund_credit"
TX_CANCEL_REFUND  = "refund_credit"
TX_PARTIAL_REFUND = "refund_credit"


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
               source, client_order_id, request_id
        FROM orders WHERE id=?
        """,
        (int(order_id),),
    )
    row = cur.fetchone()
    if row is None:
        return None
    # Fetch reseller public_id for outbox payloads
    reseller_public_id = ""
    if row[1]:
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
        "request_id": row[10],
        "reseller_public_id": reseller_public_id,
    }


def _is_terminal(status: str) -> bool:
    return (
        _STATUS_FAILED in status or
        _STATUS_CANCELLED in status or
        chr(0x274c) in status or
        chr(0x26a0) in status
    )


def _is_failed(status: str) -> bool:
    return _STATUS_FAILED in status or chr(0x26a0) in status or "Failed" in status


def _is_cancelled(status: str) -> bool:
    return _STATUS_CANCELLED in status or chr(0x274c) in status or "Cancelled" in status


# ---------------------------------------------------------------------------
# Ledger-derived refund accounting (Phase 12 P7)
#
# The wallet ledger is the ONLY source of truth for money moved.  Every
# refund decision below is derived from it, so a crashed handler run can
# always be resumed safely and an order can never be refunded more than it
# was charged.
# ---------------------------------------------------------------------------

def _order_refund_references(order_id: int) -> tuple:
    """All idempotency references under which this order can be refunded."""
    return (
        f"rapi.refund.order.{int(order_id)}",          # full failure refund
        f"rapi.cancel_refund.order.{int(order_id)}",   # cancellation refund
        f"rapi.partial_refund.order.{int(order_id)}",  # partial refund
    )


def _refunded_cents_for_order(reseller_id: int, order_id: int) -> int:
    """Sum of refund_credit cents already posted against this order.

    All four refund families: the exact singleton references
    (full / cancellation / fulfillment-partial) PLUS the sequenced
    administrative family (rapi.admin_refund.order.N.<seq>).
    """
    refs = _order_refund_references(order_id)
    ph = ",".join("?" for _ in refs)
    cur = get_cursor()
    cur.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM reseller_wallet_transactions "
        f"WHERE reseller_id=? AND tx_type='refund_credit' AND (reference IN ({ph})"
        " OR reference LIKE ?)",
        (int(reseller_id), *refs,
         f"rapi.admin_refund.order.{int(order_id)}.%"),
    )
    row = cur.fetchone()
    return int(row[0] or 0)


def _refund_row_exists(reseller_id: int, reference: str) -> bool:
    cur = get_cursor()
    cur.execute(
        "SELECT 1 FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type='refund_credit' AND reference=?",
        (int(reseller_id), str(reference)),
    )
    return cur.fetchone() is not None


def _refund_amount_for(reseller_id: int, reference: str):
    """Amount of the existing refund row for this reference, or None."""
    cur = get_cursor()
    cur.execute(
        "SELECT amount_cents FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type='refund_credit' AND reference=?",
        (int(reseller_id), str(reference)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _debit_posted_for_order(row: dict) -> bool:
    """True when the order's original purchase_debit exists in the ledger.

    The debit reference is the deterministic wallet reference written at
    order creation: rapi.{reseller_id}.{request_id}.  Reseller-API orders
    always carry both fields (the debit and the order row commit in the same
    atomic block), so a missing row proves the order was never charged.
    """
    if not row.get("reseller_id") or not row.get("request_id"):
        return False
    cur = get_cursor()
    cur.execute(
        "SELECT 1 FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type='purchase_debit' AND reference=?",
        (int(row["reseller_id"]), f"rapi.{int(row['reseller_id'])}.{row['request_id']}"),
    )
    return cur.fetchone() is not None


def _charged_cents(row: dict) -> int:
    if not row.get("unit_price_cents") or not row.get("quantity"):
        return 0
    return int(row["unit_price_cents"]) * int(row["quantity"])


def _outstanding_cents(row: dict) -> int:
    """Charged minus already refunded; never negative."""
    if not row.get("reseller_id"):
        return 0
    return max(0, _charged_cents(row) - _refunded_cents_for_order(
        int(row["reseller_id"]), int(row["id"])
    ))


def _partial_restored_units(row: dict) -> int:
    """Units already returned to stock by a partial refund of this order.

    A partial refund restores exactly the units it refunds, so the ledger
    row's amount (unit_price x failed_qty) tells us how many units no longer
    need restoring when the whole order fails later.  Without this, a
    partial-then-full failure on a manual-stock product would return MORE
    units to stock than were ever consumed.
    """
    if not row.get("partial_stock_restored"):
        return 0
    if not row.get("reseller_id") or not row.get("unit_price_cents"):
        return 0
    cur = get_cursor()
    cur.execute(
        "SELECT amount_cents FROM reseller_wallet_transactions "
        "WHERE reseller_id=? AND tx_type='refund_credit' AND reference=?",
        (int(row["reseller_id"]), f"rapi.partial_refund.order.{int(row['id'])}"),
    )
    r = cur.fetchone()
    if not r or not r[0]:
        return 0
    return max(0, int(r[0]) // int(row["unit_price_cents"]))


def _remaining_restore_qty(row: dict) -> int:
    """Units a full-failure restore still needs to return."""
    if not row.get("quantity"):
        return 0
    return max(0, int(row["quantity"]) - _partial_restored_units(row))


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
            "UPDATE orders SET master_status=?, failure_reason=?, updated=? WHERE id=? "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%failed%' "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%cancelled%' "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%canceled%'",
            (_STATUS_FAILED, str(reason), now, int(order_id)),
        )
        if cur.rowcount != 1:
            return False
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

    return True


def _mark_order_cancelled(
    order_id: int, reseller_id: int = 0,
    reseller_public_id: str = "", source: str = "",
    reason: str = "", require_pre_delivery: bool = False,
    expected_delivery: str = "",
):
    conn = get_conn()
    cur  = get_cursor()
    now  = now_text()
    _is_reseller = (source == "reseller_api") and bool(reseller_id) and bool(reseller_public_id)
    with _pgcompat.atomic(conn):
        cur.execute(
            "UPDATE orders SET master_status=?, failure_reason=?, updated=? WHERE id=? "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%failed%' "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%cancelled%' "
            "AND LOWER(COALESCE(master_status, '')) NOT LIKE '%canceled%' "
            + ("AND COALESCE(delivery, '')=?" if require_pre_delivery else ""),
            (_STATUS_CANCELLED, str(reason), now, int(order_id))
            + ((expected_delivery,) if require_pre_delivery else ()),
        )
        if cur.rowcount != 1:
            return False
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

    return True


def _issue_refund(
    reseller_id: int,
    amount_cents: int,
    order_id: int,
    tx_type: str = TX_REFUND,
    reseller_public_id: str = "",
    reference: str = None,
    refund_type: str = "full",
    description: str = None,
    record_source: str = "system",
    record_reason: str = "",
    record_actor: str = "system",
) -> bool:
    """Issue a refund credit. Returns True if newly posted, False if duplicate.

    tx_type is always the whitelisted ledger type (TX_REFUND); the refund
    KIND travels in `reference` (idempotency key) and `refund_type`
    (webhook payload label: full | cancellation | partial).
    """
    ref = reference or f"rapi.refund.order.{order_id}"
    status, _ = post_wallet_transaction(
        reseller_id=int(reseller_id),
        tx_type=tx_type,
        amount_cents=int(amount_cents),   # positive = credit
        reference=ref,
        description=description or f"Refund for order ORD-{int(order_id):08d}",
        related_order_reference=ref,
    )
    if status == "posted":
        # Phase 15 P5: every refund writes its business record atomically
        # with the ledger credit (the caller's atomic block makes them one
        # commit).  The refund RECORD represents the action; the ledger row
        # remains the only money movement.
        #
        # NEVER call ensure_adjustment_tables() (or any committing helper)
        # here: its trailing conn.commit() would commit the caller's atomic
        # block mid-flight — exactly the divergence this test gate exists to
        # prevent (P15 P9 finding: crash after a mid-block commit left a
        # ledger credit with no record).  Tables are ensured at startup via
        # ensure_reseller_tables() and at service entry points.
        try:
            import database.adjustment_db as adb
            kind_map = {"full": "full", "cancellation": "cancellation",
                        "partial": "partial", "administrative": "administrative"}
            adb.insert_refund_record({
                "public_id": adb.generate_refund_public_id(),
                "reseller_id": int(reseller_id),
                "order_id": int(order_id),
                "order_public_id": f"ORD-{int(order_id):08d}",
                "kind": kind_map.get(refund_type, "full"),
                "amount_cents": int(amount_cents),
                "currency": "USDT",
                "reason": str(record_reason or "")[:500],
                "source": str(record_source or "system"),
                "status": "completed",
                "ledger_reference": ref,
                "created_by": str(record_actor or "system"),
                "created": now_text(),
            })
        except Exception:
            # A duplicate record (same ledger_reference) means this refund was
            # already recorded — safe to continue; anything else propagates so
            # the caller's atomic block rolls back with the ledger credit.
            import database.pgcompat as _pg
            import sys as _sys
            exc = _sys.exc_info()[1]
            if not _pg.is_unique_violation(exc):
                raise

    if status in ("posted", "duplicate") and reseller_id and reseller_public_id:
        emit_event_atomic(
            int(reseller_id),
            "refund.created",
            f"ORD-{int(order_id):08d}",
            build_refund_created_payload(
                order_id=order_id,
                reseller_public_id=str(reseller_public_id),
                amount_cents=int(amount_cents),
                refund_type=refund_type,
                created_at=now_text(),
            ),
        )
    return status in ("posted", "duplicate")


def _post_refund_serialized(
    row: dict,
    reference: str,
    refund_type: str,
    wanted_cents=None,
    description: str = None,
    record_source: str = "system",
    record_actor: str = "system",
    record_reason: str = "",
):
    """Atomically cap-and-post this order's refund (Phase 12 P8).

    The "already refunded" read and the ledger write run in ONE
    pgcompat.atomic block (BEGIN IMMEDIATE on SQLite), so concurrent refund
    decisions for the same order SERIALIZE: the loser reads the winner's
    committed refund and caps — or skips — correctly.  Without this, a
    simultaneous full-failure refund and partial refund both read
    outstanding=charged and both post, over-refunding the wallet (proven:
    14/20 racing trials refunded 3000 on a 2000 charge).
    UNIQUE(reseller_id, tx_type, reference) remains the final backstop.

    Returns (posted, cents):
      posted=False, cents=existing_amount  -> idempotent replay (no new write)
      posted=False, cents=0                -> nothing left to refund
      posted=True,  cents=amount           -> refund newly posted
    """
    reseller_id = int(row["reseller_id"])
    order_id    = int(row["id"])
    conn = get_conn()
    with _pgcompat.atomic(conn):
        existing = _refund_amount_for(reseller_id, reference)
        if existing is not None:
            return False, existing
        outstanding = _outstanding_cents(row)
        amount = outstanding if wanted_cents is None else min(int(wanted_cents), outstanding)
        if amount <= 0:
            return False, 0
        posted = _issue_refund(
            reseller_id, amount, order_id, TX_REFUND,
            reseller_public_id=row.get("reseller_public_id", ""),
            reference=reference,
            refund_type=refund_type,
            description=description,
            record_source=record_source,
            record_actor=record_actor,
            record_reason=record_reason,
        )
        return posted, (amount if posted else 0)


def _restore_stock(product_id: int, quantity: int, order_id: int, guard_col: str) -> bool:
    """Restore stock with idempotency guard. Returns True if newly restored."""
    from reseller_api.services.order_idempotency import atomic_restore_stock
    return atomic_restore_stock(product_id, quantity, guard_col, order_id)


def _ensure_record_tables():
    """Ensure the P5 record tables at SERVICE ENTRY points — outside every
    atomic block (a committing ensure mid-transaction would break atomicity;
    see PHASE15_SECURITY_AUDIT.md)."""
    try:
        import database.adjustment_db as adb
        adb.ensure_adjustment_tables()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _notify_terminal_owner(order_id, event):
    # No transport inside financial work, and no notification failure may alter it.
    try:
        from reseller_api.services.order_notifications import queue_owner_order_event, _transaction_open
        if not _transaction_open():
            queue_owner_order_event(int(order_id), event)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("notification category=terminal_dispatch_failed")


def handle_post_debit_failure(
    order_id: int,
    restore_stock: bool = True,
    reason: str = "fulfillment_failure",
    record_source: str = "system",
    record_actor: str = "system",
) -> dict:
    """Handle a post-debit fulfillment failure.

    Marks the order as Failed, refunds the outstanding charged amount, and
    optionally restores stock. Idempotent and crash-resumable: if a previous
    run died after the status commit but before the refund or the stock
    restore committed, the next call completes exactly the missing steps
    (result "recovered").  A fully-recovered order replays as a pure no-op
    (result "already_terminal").

    Returns a result dict with keys: result, refunded, stock_restored.
    """
    _ensure_record_tables()
    row = _get_order_row(order_id)
    if row is None:
        return {"result": "order_not_found", "refunded": False, "stock_restored": False}

    if _is_terminal(row["master_status"]):
        if not _is_failed(row["master_status"]):
            # Cancelled orders belong to the cancellation flow.
            return {"result": "already_terminal", "refunded": False,
                    "stock_restored": False}
        # Recovery resume: complete any steps a crashed run missed.  The
        # refund decision is serialized (P8): concurrent recovery runs or a
        # racing partial refund cannot push refunds past the charge.
        recovered = False
        refunded = False
        if row["reseller_id"] and _debit_posted_for_order(row):
            posted, _c = _post_refund_serialized(
                row, f"rapi.refund.order.{int(order_id)}", "full",
                record_source=record_source, record_actor=record_actor,
                record_reason=reason,
            )
            refunded = posted
            recovered = recovered or posted
        stocked = False
        restore_qty = _remaining_restore_qty(row)
        if restore_stock and row["product_id"] and restore_qty > 0 \
                and not row["stock_restored"]:
            stocked = _restore_stock(
                row["product_id"], restore_qty, order_id, "stock_restored"
            )
            recovered = recovered or stocked
        if recovered:
            _notify_terminal_owner(order_id, "order.failed")
        return {
            "result": "recovered" if recovered else "already_terminal",
            "refunded": refunded,
            "stock_restored": stocked,
        }

    claimed = _mark_order_failed(
        order_id, reason,
        reseller_id=row["reseller_id"],
        reseller_public_id=row.get("reseller_public_id", ""),
        source=row.get("source", ""),
    )

    if not claimed:
        return {"result": "already_terminal", "refunded": False,
                "stock_restored": False}

    # Refund only what is still outstanding: partial refunds may already have
    # returned part of the charge.  The cap is computed and posted inside one
    # serialized block (P8), so racing refunds can never exceed the charge.
    refunded = False
    if row["reseller_id"]:
        posted, _c = _post_refund_serialized(
            row, f"rapi.refund.order.{int(order_id)}", "full",
            record_source=record_source, record_actor=record_actor,
            record_reason=reason,
        )
        refunded = posted

    stocked = False
    restore_qty = _remaining_restore_qty(row)
    if restore_stock and row["product_id"] and restore_qty > 0 and not row["stock_restored"]:
        stocked = _restore_stock(
            row["product_id"], restore_qty, order_id, "stock_restored"
        )

    _notify_terminal_owner(order_id, "order.failed")
    return {"result": "handled", "refunded": refunded, "stock_restored": stocked}


def handle_cancellation(
    order_id: int,
    post_debit: bool = False,
    reason: str = "",
    refund_if_debited: Optional[bool] = None,
    record_source: str = "system",
    record_actor: str = "system",
    restore_stock: bool = False,
    require_pre_delivery: bool = False,
) -> dict:
    """Cancel using the existing ledger/stock/outbox engine.

    Preserve the legacy post_debit calling convention. Owner callers enable
    require_pre_delivery and restore_stock; historic internal callers retain
    their existing behavior. The status claim is conditional and shares its
    transaction with order.cancelled. Financial/stock steps are resumable.
    """
    _ensure_record_tables()  # committing schema helper, BEFORE atomic work
    if refund_if_debited is not None:
        post_debit = bool(refund_if_debited)
    row = _get_order_row(order_id)
    empty = {"refunded": False, "stock_restored": False}
    if row is None:
        return {"result": "order_not_found", **empty}
    terminal = _is_terminal(row["master_status"])
    if terminal and not _is_cancelled(row["master_status"]):
        return {"result": "already_terminal", **empty}
    expected_delivery = ""
    if require_pre_delivery:
        from reseller_api.services.order_delivery import has_stored_delivery
        cur = get_cursor()
        cur.execute("SELECT delivery FROM orders WHERE id=?", (int(order_id),))
        delivery_row = cur.fetchone()
        expected_delivery = str(delivery_row[0] or "") if delivery_row else ""
        if has_stored_delivery(expected_delivery):
            return {"result": "already_delivered", **empty}
    if not terminal:
        claimed = _mark_order_cancelled(
            order_id, reseller_id=row["reseller_id"],
            reseller_public_id=row.get("reseller_public_id", ""),
            source=row.get("source", ""), reason=reason,
            require_pre_delivery=require_pre_delivery,
            expected_delivery=expected_delivery,
        )
        if not claimed:
            cur = get_cursor()
            cur.execute("SELECT delivery FROM orders WHERE id=?", (int(order_id),))
            current = cur.fetchone()
            result = ("already_delivered" if require_pre_delivery and current
                      and has_stored_delivery(current[0]) else "already_terminal")
            return {"result": result, **empty}
    refunded = False
    if post_debit and row["reseller_id"] and _debit_posted_for_order(row):
        refunded, _ = _post_refund_serialized(
            row, f"rapi.cancel_refund.order.{int(order_id)}", "cancellation",
            record_source=record_source, record_actor=record_actor,
            record_reason=reason,
        )
    stocked = False
    qty = _remaining_restore_qty(row)
    if restore_stock and row["product_id"] and qty > 0 and not row["stock_restored"]:
        stocked = _restore_stock(row["product_id"], qty, order_id, "stock_restored")
    if not terminal or refunded or stocked:
        _notify_terminal_owner(order_id, "order.cancelled")
    return {"result": ("recovered" if refunded or stocked else "already_terminal")
            if terminal else "cancelled", "refunded": refunded,
            "stock_restored": stocked}


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
    _ensure_record_tables()
    row = _get_order_row(order_id)
    if row is None:
        return {"result": "order_not_found", "refund_cents": 0}

    if _is_terminal(row["master_status"]):
        # Terminal orders are owned by the failure/cancellation flows, whose
        # full-outstanding refunds already cover every unrefunded unit.
        # Posting a partial here would over-refund the reseller's wallet.
        return {"result": "order_terminal", "refund_cents": 0}

    if not row["unit_price_cents"] or failed_qty <= 0 or not row["reseller_id"]:
        return {"result": "no_refund_needed", "refund_cents": 0}

    # Cap at the order's outstanding charge — computed and posted inside one
    # serialized block (P8), so a racing full-failure refund cannot push the
    # total past the charge.  A replay echoes the original refund amount
    # without posting again.
    posted, refund_cents = _post_refund_serialized(
        row,
        f"rapi.partial_refund.order.{int(order_id)}",
        "partial",
        wanted_cents=int(row["unit_price_cents"]) * int(failed_qty),
        description=(
            f"Partial refund for order ORD-{int(order_id):08d} "
            f"({failed_qty} of {row['quantity']} units failed)"
        ),
    )
    if refund_cents <= 0:
        return {"result": "no_refund_needed", "refund_cents": 0}

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
