"""Financial adjustment & administrative refund workflow — Phase 15 P5.

The record layer (reseller_adjustments / reseller_refunds) is the BUSINESS
action: reason, actor, approval status.  The wallet ledger remains the only
financial movement record.  Money moves only in `complete_adjustment` and
`issue_admin_refund`, each inside ONE pgcompat.atomic block (record + ledger
row commit or roll back together).

Workflow:  create (pending, no money) → approve (approved, still no money)
           → complete (ledger post + completed, atomically)
           pending/approved → rejected | cancelled (terminal, no money)

Self-approval note: this deployment's WebAdmin has a single owner role;
separation of duties is therefore a no-op today.  The hooks
(created_by/decided_by) record both actors so a future multi-admin role
model can enforce it without a schema change.
"""

import re

import database.pgcompat as _pgcompat
from database.db import get_conn, get_cursor
import database.reseller_db as rdb
import database.adjustment_db as adb
from reseller_api.services.order_failure import (
    _get_order_row, _outstanding_cents, _issue_refund,
)
from utils.timezone import now_text

_ORD_RE = re.compile(r"^ORD-(\d{8})$")


class AdjustmentError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _max_adjustment_cents() -> int:
    """Server-side cap for one manual adjustment (configurable, not
    hard-coded per business role — the owner can raise it in settings)."""
    from database.db import get_setting
    try:
        return int(get_setting("max_manual_adjustment_cents", "10000000"))
    except Exception:
        return 10_000_000   # $100,000.00 default ceiling


def create_adjustment(reseller_id: int, adj_type: str, amount_cents,
                      reason: str, created_by: str = "",
                      note: str = "", reference: str = None) -> dict:
    """Create a PENDING adjustment (no wallet movement)."""
    adb.ensure_adjustment_tables()
    if not rdb.get_reseller(int(reseller_id)):
        raise AdjustmentError("reseller_not_found", "Reseller not found.", 404)
    if adj_type not in adb.ADJUSTMENT_TYPES:
        raise AdjustmentError("invalid_parameter", "type must be credit or debit.", 400)
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise AdjustmentError("invalid_amount", "amount must be integer cents.", 400)
    if amount_cents <= 0:
        raise AdjustmentError("invalid_amount", "amount must be positive.", 400)
    if amount_cents > _max_adjustment_cents():
        raise AdjustmentError("adjustment_limit_exceeded",
                              "Amount exceeds the configured manual-adjustment limit.",
                              422)
    reason = str(reason or "").strip()
    if not reason:
        raise AdjustmentError("invalid_reason", "A reason is required.", 400)
    if len(reason) > 500:
        raise AdjustmentError("invalid_reason", "Reason is too long.", 400)
    if reference is not None:
        reference = str(reference).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}", reference):
            raise AdjustmentError("invalid_reference",
                                  "A safe idempotency reference is required.", 400)
    now = now_text()
    # The planned ledger reference is fixed at creation — the UNIQUE column
    # rejects collisions early, and completion posts exactly this reference
    # (preserving the ledger's idempotency semantics end to end).
    pub_id = adb.generate_adjustment_public_id()
    planned_reference = reference or f"adj.{pub_id}"
    adj_id = adb.insert_adjustment({
        "public_id": pub_id,
        "reseller_id": int(reseller_id),
        "type": adj_type,
        "amount_cents": int(amount_cents),
        "currency": "USDT",
        "reason": reason,
        "source": "manual_admin",
        "status": "pending",
        "ledger_reference": planned_reference,
        "created_by": str(created_by or ""),
        "note": str(note or "")[:300],
        "created": now,
        "updated": now,
    })
    get_conn().commit()
    out = adb.get_adjustment_by_id(adj_id)
    out["is_duplicate"] = False
    return out


def _transition_or_fail(adj_id: int, status: str, allowed_from: tuple,
                        **extra) -> dict:
    adj = adb.get_adjustment_by_id(int(adj_id))
    if not adj:
        raise AdjustmentError("adjustment_not_found", "Adjustment not found.", 404)
    if adj["status"] == status:
        adj["is_duplicate"] = True
        return adj
    if not adb.transition_adjustment(int(adj_id), status, allowed_from, **extra):
        raise AdjustmentError("invalid_transition",
                              f"Cannot move {adj['status']} → {status}.", 409)
    out = adb.get_adjustment_by_id(int(adj_id))
    out["is_duplicate"] = False
    return out


def approve_adjustment(adjustment_id: int, decided_by: str = "") -> dict:
    """pending → approved.  No wallet movement (approval alone moves nothing)."""
    return _transition_or_fail(adjustment_id, "approved", ("pending",),
                               decided_by=decided_by)


def reject_adjustment(adjustment_id: int, decided_by: str = "") -> dict:
    """pending → rejected (terminal; no money ever moved)."""
    return _transition_or_fail(adjustment_id, "rejected", ("pending",),
                               decided_by=decided_by)


def cancel_adjustment(adjustment_id: int, decided_by: str = "") -> dict:
    """pending/approved → cancelled (terminal)."""
    return _transition_or_fail(adjustment_id, "cancelled",
                               ("pending", "approved"), decided_by=decided_by)


def complete_adjustment(adjustment_id: int, decided_by: str = "") -> dict:
    """approved → completed: post the ledger row and finish atomically.

    The adjustment record, the ledger transaction, and the wallet balance
    commit in ONE atomic block — there is no record-without-money or
    money-without-record state.
    """
    adj = adb.get_adjustment_by_id(int(adjustment_id))
    if not adj:
        raise AdjustmentError("adjustment_not_found", "Adjustment not found.", 404)
    if adj["status"] == "completed":
        adj["is_duplicate"] = True
        return adj

    conn = get_conn()
    with _pgcompat.atomic(conn):
        # Re-read + transition inside the block: the status guard makes a
        # double-complete impossible (the loser's rowcount is 0).
        cur = get_cursor()
        cur.execute("SELECT status FROM reseller_adjustments WHERE id=?",
                    (int(adjustment_id),))
        row = cur.fetchone()
        if row is None:
            raise AdjustmentError("adjustment_not_found", "Adjustment not found.", 404)
        current = row[0]
        if current == "completed":
            out = adb.get_adjustment_by_id(int(adjustment_id))
            out["is_duplicate"] = True
            return out
        if current != "approved":
            raise AdjustmentError(
                "invalid_transition",
                "Adjustment must be approved before completion.", 409)

        reference = str(adj["ledger_reference"] or f"adj.{adj['public_id']}")
        tx_type = "adjustment_credit" if adj["type"] == "credit" else "adjustment_debit"
        amount = int(adj["amount_cents"]) if adj["type"] == "credit" else -int(adj["amount_cents"])
        status, data = rdb.post_wallet_transaction(
            reseller_id=int(adj["reseller_id"]),
            tx_type=tx_type,
            amount_cents=amount,
            reference=reference,
            description=str(adj["reason"]),
        )
        if status == "insufficient_funds":
            raise AdjustmentError("insufficient_funds",
                                  "The reseller wallet has insufficient funds.",
                                  402)
        if status == "idempotency_conflict":
            raise AdjustmentError("idempotency_conflict",
                                  "Ledger reference conflict.", 409)
        if status not in ("posted", "duplicate"):
            raise AdjustmentError("adjustment_post_failed",
                                  f"Ledger posting failed: {status}", 500)
        ok = adb.transition_adjustment(
            int(adjustment_id), "completed", ("approved",),
            decided_by=decided_by, ledger_reference=reference, completed=True)
        if not ok:
            raise AdjustmentError("invalid_transition",
                                  "Adjustment was already completed.", 409)
    out = adb.get_adjustment_by_id(int(adjustment_id))
    out["is_duplicate"] = False
    return out


def issue_admin_refund(order_id: int, amount_cents, reason: str,
                       actor: str = "admin") -> dict:
    """Owner-initiated refund for a reseller order (any lifecycle state with
    outstanding charge).  Atomic: ledger credit + refund record + outbox
    event in one block; capped at the order's outstanding amount.
    """
    adb.ensure_adjustment_tables()
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise AdjustmentError("invalid_amount", "amount must be integer cents.", 400)
    if amount_cents <= 0:
        raise AdjustmentError("invalid_amount", "amount must be positive.", 400)
    reason = str(reason or "").strip()
    if not reason:
        raise AdjustmentError("invalid_reason", "A reason is required.", 400)

    row = _get_order_row(int(order_id))
    if not row or row.get("source") != "reseller_api":
        raise AdjustmentError("order_not_found", "Order not found.", 404)

    conn = get_conn()
    with _pgcompat.atomic(conn):
        # Outstanding is computed INSIDE the block so concurrent refunds
        # serialize; the second one sees the first's committed credit.
        outstanding = _outstanding_cents(row)
        if outstanding <= 0:
            raise AdjustmentError("nothing_to_refund",
                                  "This order is already fully refunded.", 409)
        if amount_cents > outstanding:
            raise AdjustmentError(
                "refund_exceeds_refundable",
                f"Refundable amount is {rdb.from_cents(outstanding)} USDT.",
                422)

        # sequence per order for the administrative family (multi-refund safe)
        cur = get_cursor()
        cur.execute(
            "SELECT COUNT(*) FROM reseller_wallet_transactions"
            " WHERE reseller_id=? AND tx_type='refund_credit'"
            " AND reference LIKE ?",
            (int(row["reseller_id"]),
             f"rapi.admin_refund.order.{int(order_id)}.%"),
        )
        seq = int(cur.fetchone()[0]) + 1
        reference = f"rapi.admin_refund.order.{int(order_id)}.{seq}"

        # The shared refund poster writes the business record atomically with
        # the ledger credit (record_source/actor travel with it).
        posted = _issue_refund(
            row["reseller_id"], amount_cents, int(order_id),
            reseller_public_id=row.get("reseller_public_id", ""),
            reference=reference,
            refund_type="administrative",
            description=f"Administrative refund for ORD-{int(order_id):08d}: {reason[:200]}",
            record_source="admin",
            record_reason=reason,
            record_actor=str(actor or "admin"),
        )
        if not posted:
            raise AdjustmentError("refund_post_failed",
                                  "The refund could not be posted.", 500)

    rec = adb.get_refund_by_ledger_reference(reference)
    rec["is_duplicate"] = False
    return rec


__all__ = [
    "AdjustmentError", "create_adjustment", "approve_adjustment",
    "reject_adjustment", "cancel_adjustment", "complete_adjustment",
    "issue_admin_refund",
]
