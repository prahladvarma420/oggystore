"""Framework-neutral owner controls for the existing reseller wallet."""
import database.reseller_db as rdb
from reseller_api.services.wallet import WalletError, credit_wallet, get_balance


class AdminWalletError(ValueError):
    def __init__(self, code, message, http_status=400):
        super().__init__(code)
        self.code, self.message, self.http_status = code, message, http_status


def credit_as_admin(actor_role, reseller_id, amount, reference, reason):
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
    try:
        return credit_wallet(
            reseller_id, amount, reference, reason, adjustment=True
        )
    except WalletError as exc:
        status = 409 if exc.code == "idempotency_conflict" else 400
        raise AdminWalletError(exc.code, exc.message, status) from None


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
