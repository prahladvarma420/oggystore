"""Framework-neutral owner controls for the existing reseller wallet."""
import database.reseller_db as rdb
from reseller_api.services.wallet import WalletError, get_balance


class AdminWalletError(ValueError):
    def __init__(self, code, message, http_status=400):
        super().__init__(code)
        self.code, self.message, self.http_status = code, message, http_status


def credit_as_admin(actor_role, reseller_id, amount, reference, reason):
    """Owner wallet credit — routes through the Phase 15 adjustment record
    layer (create → approve → complete atomically) so every manual credit
    leaves an auditable business record linked to its ledger row.

    The caller's reference stays the ledger idempotency key, preserving the
    exact duplicate/conflict semantics this endpoint always had.
    """
    if actor_role != "owner":
        raise AdminWalletError("authorization_denied", "Owner permission required", 403)
    reason = str(reason or "").strip()
    reference = str(reference or "").strip()
    if not reference:
        raise AdminWalletError("invalid_reference", "Reference is required")
    if not reason:
        raise AdminWalletError("invalid_reason", "Reason is required")
    if len(reason) > 500:
        raise AdminWalletError("invalid_reason", "Reason must not exceed 500 characters")

    from reseller_api.services.adjustment_service import (
        AdjustmentError, create_adjustment, approve_adjustment,
        complete_adjustment,
    )
    from reseller_api.services.wallet import WalletResult

    try:
        cents = int(rdb.to_cents(amount))
        if cents <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise AdminWalletError("invalid_amount", "A positive amount is required") from None

    import database.pgcompat as _pgcompat
    try:
        adj = create_adjustment(reseller_id, "credit", cents, reason,
                                created_by=str(actor_role), reference=reference)
        approve_adjustment(adj["id"], decided_by=str(actor_role))
        done = complete_adjustment(adj["id"], decided_by=str(actor_role))
    except AdjustmentError as exc:
        status = {"idempotency_conflict": 409, "adjustment_limit_exceeded": 422,
                  "insufficient_funds": 402, "reseller_not_found": 404,
                  "invalid_transition": 409}.get(exc.code, 400)
        raise AdminWalletError(exc.code, exc.message, status) from None
    except Exception as exc:
        # A record row with this planned reference already exists -> the same
        # request already ran: return the original result (idempotent replay)
        if _pgcompat.is_unique_violation(exc):
            row = rdb.get_wallet_transaction_by_reference(
                reseller_id, "adjustment_credit", reference)
            if row:
                return WalletResult("duplicate", int(row["id"]),
                                    int(row["balance_after_cents"]))
            raise AdminWalletError("idempotency_conflict",
                                   "This reference was already used.", 409) from None
        raise

    row = rdb.get_wallet_transaction_by_reference(reseller_id,
                                                  "adjustment_credit",
                                                  reference)
    return WalletResult("posted", int(row["id"]),
                        int(row["balance_after_cents"]))


def wallet_history(reseller_id, search="", tx_type=None, page=1, per_page=25):
    page = max(1, int(page))
    per_page = min(100, max(10, int(per_page)))
    rows, total = rdb.search_wallet_transactions(
        reseller_id, search, tx_type or None, per_page, (page - 1) * per_page
    )
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
        rows, total = rdb.search_wallet_transactions(
            reseller_id, search, tx_type or None, per_page, (page - 1) * per_page
        )
    return {
        "balance": get_balance(reseller_id),
        "transactions": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }
