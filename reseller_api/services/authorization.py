"""Framework-neutral authorization policy for authenticated reseller APIs."""

from dataclasses import dataclass
from typing import Mapping, Optional

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext


FOUNDATION_READ = "foundation:read"
PRODUCTS_READ = "products:read"
BALANCE_READ = "balance:read"
ORDERS_READ = "orders:read"
DELIVERY_READ = "delivery:read"
ORDERS_CREATE = "orders:create"
WEBHOOKS_READ = "webhooks:read"
WEBHOOKS_WRITE = "webhooks:write"
CATALOG_WRITE = "catalog:write"   # Phase 11 P3: import / remove product mappings

SUPPORTED_OPERATIONS = frozenset({
    FOUNDATION_READ,
    PRODUCTS_READ,
    BALANCE_READ,
    ORDERS_READ,
    DELIVERY_READ,
    ORDERS_CREATE,
    WEBHOOKS_READ,
    WEBHOOKS_WRITE,
    CATALOG_WRITE,
})

_REQUEST_LIMIT_FIELDS = (
    "rate_limit_per_min",
    "daily_request_limit",
    "monthly_request_limit",
)
_ORDER_LIMIT_FIELDS = (
    "orders_per_min",
    "daily_order_limit",
    "monthly_order_limit",
)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    operation: str
    limits: Optional[Mapping[str, int]] = None


def _deny(operation: str, reason: str, limits=None):
    return AuthorizationDecision(False, reason, operation, limits)


def _limits(snapshot):
    names = _REQUEST_LIMIT_FIELDS + _ORDER_LIMIT_FIELDS
    return {name: int(snapshot[name]) for name in names}


def _validated_snapshot(context: ApiRequestContext, expected_environment: str):
    """Re-check mutable state for every authorization decision."""
    if not isinstance(context, ApiRequestContext):
        return None, "missing_authenticated_context"
    if expected_environment not in ("test", "production"):
        return None, "invalid_server_environment"
    if context.environment != expected_environment:
        return None, "context_environment_mismatch"

    snapshot = rdb.get_api_authorization_snapshot(
        context.reseller_id,
        context.api_key_id,
    )
    if not snapshot:
        return None, "key_reseller_scope_mismatch"
    if snapshot["key_id"] != context.api_key_public_id:
        return None, "key_identity_mismatch"
    if snapshot["reseller_public_id"] != context.reseller_public_id:
        return None, "reseller_identity_mismatch"
    if snapshot["key_status"] != "active" or not bool(snapshot["key_enabled"]):
        return None, "key_not_active"
    if snapshot["key_environment"] != expected_environment:
        return None, "key_environment_mismatch"
    if snapshot["reseller_status"] != "active":
        return None, "reseller_not_active"
    if bool(snapshot["user_blocked"]):
        return None, "reseller_blocked"
    if not bool(snapshot["api_enabled"]):
        return None, "api_disabled"
    return snapshot, "authorized"


def authorize_operation(context: ApiRequestContext, operation: str,
                        expected_environment: str):
    """Answer whether this authenticated key may perform a known operation.

    There is currently no stored custom-scope model. Supported operations are
    explicit and deny unknown permissions. Per-reseller configured limits are
    returned for later atomic rate-limit enforcement.
    """
    operation = str(operation or "")
    if operation not in SUPPORTED_OPERATIONS:
        return _deny(operation, "unsupported_operation")

    snapshot, reason = _validated_snapshot(context, expected_environment)
    if not snapshot:
        return _deny(operation, reason)
    limits = _limits(snapshot)

    if any(limits[name] <= 0 for name in _REQUEST_LIMIT_FIELDS):
        return _deny(operation, "request_limit_disabled", limits)
    if operation == ORDERS_CREATE and any(
        limits[name] <= 0 for name in _ORDER_LIMIT_FIELDS
    ):
        return _deny(operation, "order_limit_disabled", limits)
    return AuthorizationDecision(True, "authorized", operation, limits)


def authorize_product_access(context: ApiRequestContext, product_id: int,
                             expected_environment: str,
                             operation: str = PRODUCTS_READ):
    """Authorize one server-resolved master product for this reseller.

    No reseller ID, ownership flag, price, or balance is accepted from client
    input. ``product_id`` must be an internal ID resolved by server code from a
    future public product identifier.
    """
    decision = authorize_operation(context, operation, expected_environment)
    if not decision.allowed:
        return decision
    if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
        return _deny(operation, "invalid_server_product_id", decision.limits)
    state = rdb.get_reseller_product_access_state(context.reseller_id, product_id)
    if not state or not bool(state["product_active"]):
        return _deny(operation, "product_unavailable", decision.limits)
    if not bool(state["access_enabled"]):
        return _deny(operation, "product_access_denied", decision.limits)
    return decision


def can_perform_operation(context, operation, expected_environment):
    return authorize_operation(context, operation, expected_environment).allowed


def can_access_product(context, product_id, expected_environment):
    return authorize_product_access(
        context, product_id, expected_environment, PRODUCTS_READ
    ).allowed


def can_create_order(context, expected_environment):
    return authorize_operation(context, ORDERS_CREATE, expected_environment).allowed


def is_api_enabled(context, expected_environment):
    snapshot, _reason = _validated_snapshot(context, expected_environment)
    return bool(snapshot)


def is_reseller_blocked(context, expected_environment):
    if not isinstance(context, ApiRequestContext):
        return True
    snapshot = rdb.get_api_authorization_snapshot(
        context.reseller_id, context.api_key_id
    )
    if not snapshot:
        return True
    return bool(snapshot["user_blocked"])


__all__ = [
    "AuthorizationDecision",
    "SUPPORTED_OPERATIONS",
    "FOUNDATION_READ", "PRODUCTS_READ", "BALANCE_READ", "ORDERS_READ", "DELIVERY_READ",
    "ORDERS_CREATE", "WEBHOOKS_READ", "WEBHOOKS_WRITE", "CATALOG_WRITE",
    "authorize_operation", "authorize_product_access", "can_perform_operation",
    "can_access_product", "can_create_order", "is_api_enabled",
    "is_reseller_blocked",
]
