"""Read-only authenticated reseller balance controller."""

from flask import current_app, request

from reseller_api.errors import ApiError
from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.responses import success_response
from reseller_api.services.authorization import BALANCE_READ
from reseller_api.services.wallet import WalletError, get_authenticated_balance
import database.reseller_db as rdb


def balance_detail():
    # This resource has no client-selectable identity or filters. Reject every
    # query parameter, including reseller_id, rather than silently ignoring it.
    if request.args:
        raise ApiError(
            "invalid_request",
            "The balance endpoint does not accept query parameters.",
            400,
        )
    try:
        wallet = get_authenticated_balance(
            get_authenticated_context(),
            current_app.config["API_ENVIRONMENT"],
        )
    except WalletError as exc:
        status = 403 if exc.code == "authorization_denied" else 400
        raise ApiError(exc.code, exc.message, status) from None
    return success_response({
        "balance": wallet["balance"],
        "currency": wallet["currency"],
    })


@require_operation(BALANCE_READ)
def wallet_summary():
    """GET /api/v1/wallet/summary — financial summary from the immutable
    ledger + wallet balance (Phase 14 P4).  Read-only; own data only."""
    context = get_authenticated_context()
    # Re-validate mutable account state, same as the balance endpoint.
    get_authenticated_balance(context, current_app.config["API_ENVIRONMENT"])
    return success_response({
        "wallet": rdb.get_wallet_financial_summary(context.reseller_id),
    })


__all__ = ["balance_detail", "wallet_summary"]
