"""Phase 11 P5 — Reseller-facing pricing service (READ-ONLY).

Resellers can query the server-calculated effective price for any product
they have access to.  They CANNOT set, override, or negotiate prices — that
is exclusively an owner/WebAdmin operation (web_admin/app.py).

All arithmetic is delegated to the existing rdb helpers that use integer
cents throughout. No float arithmetic is used on any money path.

Supported pricing models (from existing architecture):
  - inherited   : no rule → reseller pays the master product price
  - fixed_price : owner sets an explicit per-unit selling price (cents)
  - markup_percent : owner adds a percentage on top of master price (bps)
  - discount_percent: owner applies a percentage reduction (bps)

The reseller API never learns which model is active; it receives only the
authoritative server-calculated effective unit price.
"""
from __future__ import annotations

import database.reseller_db as rdb
from reseller_api.services.authorization import PRODUCTS_READ, authorize_operation

__all__ = [
    "PricingError",
    "get_product_price",
    "list_product_prices",
    "SUPPORTED_RULE_TYPES",
]

# Pricing models the architecture actually supports
SUPPORTED_RULE_TYPES = tuple(rdb.RULE_TYPES)  # fixed_price, discount_percent, markup_percent


class PricingError(Exception):
    """Raised when a price look-up fails for an API-visible reason."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _build_price_record(
    public_id: str,
    name: str | None,
    unit_price_cents: int,
    price_source: str,
) -> dict:
    """Build the client-safe price record.
    Master cost / master price are never separately disclosed.
    """
    return {
        "product_id":   str(public_id),
        "name":         name,
        "price":        rdb.from_cents(unit_price_cents),
        "currency":     "USDT",
        "price_source": price_source,   # audit/debug only; never financial input
    }


def _require_context(context):
    """Return context or raise PricingError 401."""
    if context is None:
        raise PricingError("authentication_required", "Authentication required.", 401)
    return context


def get_product_price(context, public_id: str, environment: str) -> dict:
    """Return the server-authoritative effective price for one product.

    Uses resolve_reseller_price_cents — the SAME function the order flow
    uses — so the price returned here is exactly what will be charged.

    Raises:
        PricingError 401: unauthenticated context.
        PricingError 403: operation not permitted.
        PricingError 404: product not accessible to this reseller.
    """
    ctx = _require_context(context)

    # Authorization check
    decision = authorize_operation(ctx, PRODUCTS_READ, environment)
    if not decision.allowed:
        raise PricingError("authorization_denied", "This operation is not permitted.", 403)

    # Resolve product via the same catalog helper used by the products endpoint
    row = rdb.get_reseller_api_product(ctx.reseller_id, str(public_id or ""))
    if not row:
        raise PricingError("product_not_found", "Product not found.", 404)

    product_id = row["product_id"]   # internal integer id, never returned to client
    unit_cents, price_source = rdb.resolve_reseller_price_cents(
        ctx.reseller_id, product_id, 1
    )
    if unit_cents is None:
        raise PricingError("product_not_found", "Product not found.", 404)

    return _build_price_record(
        row["public_id"],
        row.get("name"),
        unit_cents,
        price_source or "master",
    )


def list_product_prices(context, environment: str) -> list:
    """Return the server-authoritative effective price for every product the
    reseller has access to.  This mirrors the price the server will charge
    when the reseller places an order.

    Master cost and master price are NOT separately disclosed.
    """
    ctx = _require_context(context)

    decision = authorize_operation(ctx, PRODUCTS_READ, environment)
    if not decision.allowed:
        raise PricingError("authorization_denied", "This operation is not permitted.", 403)

    return read_product_prices_for_reseller(ctx.reseller_id)


def read_product_prices_for_reseller(reseller_id: int, *, allocate_missing_ids: bool = True) -> list:
    """Shared authoritative price read; caller must authorize its own identity.

    API retains legacy first-read public-ID allocation. Telegram forbids writes:
    missing IDs require existing owner/API initialization, never a fabricated ID.
    """
    # One SQL query: all active, accessible products for this reseller
    # with their pricing rule (if any) joined in.
    cur = rdb.get_cursor()
    cur.execute(
        "SELECT p.id, p.public_id, p.name, p.price AS master_price, "
        "pr.rule_type, pr.value_cents, pr.value_bps "
        "FROM products p "
        "JOIN reseller_product_access a ON a.product_id=p.id "
        "LEFT JOIN reseller_pricing_rules pr ON pr.product_id=p.id "
        "    AND pr.reseller_id=a.reseller_id AND pr.enabled=1 "
        "WHERE a.reseller_id=? AND a.enabled=1 AND p.active=1 "
        "ORDER BY p.id",
        (int(reseller_id),),
    )
    cols = ("product_id", "public_id", "name", "master_price",
            "rule_type", "value_cents", "value_bps")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    result = []
    for row in rows:
        unit_cents, price_source = rdb.resolve_reseller_price_from_values(
            row["master_price"],
            row.get("rule_type"),
            row.get("value_cents"),
            row.get("value_bps"),
        )
        pub = row["public_id"]
        if not pub:
            if not allocate_missing_ids:
                raise PricingError("product_not_initialized", "Product identifiers need owner/API initialization before this view is available.", 409)
            pub = rdb.ensure_product_public_id(row["product_id"])
        result.append(
            _build_price_record(pub, row.get("name"), unit_cents, price_source or "master")
        )
    return result
