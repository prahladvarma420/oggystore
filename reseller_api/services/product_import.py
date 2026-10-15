"""Reseller product import (mapping) service — Phase 11 P3.

A reseller maps an owner-controlled master product into their own catalog.
No inventory, stock codes, fulfillment, or master order data is copied.
The owner remains the source of truth at all times.

Mapping lifecycle
-----------------
  import   -> creates a ``reseller_product_mappings`` row
  get      -> returns the mapping row + master product availability
  remove   -> hard-deletes the mapping row only; master product is untouched

Consistency guarantees
----------------------
  - ON DELETE CASCADE on product_id: when the owner hard-deletes a product
    the mapping is removed automatically (no orphan rows).
  - Disabled/inactive master products (active=0) can't be freshly imported;
    existing mappings survive but the product won't appear in the catalog.
  - reseller_id is NEVER accepted from the client; always taken from the
    authenticated ApiRequestContext.
"""
from __future__ import annotations

import re
from typing import Optional

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authorization import (
    CATALOG_WRITE,
    PRODUCTS_READ,
    authorize_operation,
    authorize_product_access,
)

_PUBLIC_ID_RE = re.compile(r"^PRD-[A-HJ-NP-Z2-9]{12}$")

MAX_CUSTOM_NAME_LEN = 200
MAX_CUSTOM_DESC_LEN = 2000


class ProductImportError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _require_context(context) -> ApiRequestContext:
    if not isinstance(context, ApiRequestContext):
        raise ProductImportError(
            "authentication_failed",
            "Authentication credentials are missing or invalid.",
            401,
        )
    return context


def _resolve_product_id(context: ApiRequestContext,
                        public_id: str,
                        environment: str) -> int:
    """Validate the public_id format, authorize catalog:write + product access,
    and return the internal product_id.

    Security properties:
      - Malformed / nonexistent / inactive / access-denied products all return
        the same 404 code (no existence oracle, no IDOR).
      - reseller_id is never taken from the client.
    """
    if not _PUBLIC_ID_RE.match(str(public_id or "")):
        raise ProductImportError("product_not_found", "Product not found.", 404)

    # Verify the reseller's session is valid and catalog:write is permitted.
    decision = authorize_operation(context, CATALOG_WRITE, environment)
    if not decision.allowed:
        raise ProductImportError(
            "authorization_denied", "This operation is not permitted.", 403
        )

    # Resolve internal id via the same deny-by-default access layer used for
    # PRODUCTS_READ so a reseller cannot import a product they cannot read.
    row = rdb.get_reseller_api_product(context.reseller_id, public_id)
    if not row:
        raise ProductImportError("product_not_found", "Product not found.", 404)
    return int(row["product_id"])


def import_product(
    context,
    public_id: str,
    environment: str,
    custom_name: Optional[str] = None,
    custom_description: Optional[str] = None,
) -> dict:
    """Create a reseller product mapping.

    Returns a mapping summary dict on success.  Raises ProductImportError on
    any validation, authorization, or duplicate-import failure.
    """
    ctx = _require_context(context)

    # Sanitize optional overrides
    if custom_name is not None:
        custom_name = str(custom_name).strip()
        if len(custom_name) > MAX_CUSTOM_NAME_LEN:
            raise ProductImportError(
                "invalid_field",
                f"custom_name must not exceed {MAX_CUSTOM_NAME_LEN} characters.",
            )
        if not custom_name:
            custom_name = None

    if custom_description is not None:
        custom_description = str(custom_description).strip()
        if len(custom_description) > MAX_CUSTOM_DESC_LEN:
            raise ProductImportError(
                "invalid_field",
                f"custom_description must not exceed {MAX_CUSTOM_DESC_LEN} characters.",
            )
        if not custom_description:
            custom_description = None

    product_id = _resolve_product_id(ctx, public_id, environment)

    try:
        mapping = rdb.import_reseller_product(
            ctx.reseller_id, product_id, custom_name, custom_description
        )
    except ValueError as exc:
        code = str(exc)
        if code == "already_imported":
            raise ProductImportError(
                "already_imported",
                "This product is already in your catalog.",
                409,
            ) from None
        raise ProductImportError("import_failed", "Import failed.", 500) from exc

    return _serialize_mapping(mapping, public_id)


def get_mapping(
    context,
    public_id: str,
    environment: str,
) -> dict:
    """Return the mapping row for one imported product.

    Raises ProductImportError(404) if not imported (or product inaccessible).
    """
    ctx = _require_context(context)

    if not _PUBLIC_ID_RE.match(str(public_id or "")):
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)

    # Authorization check (catalog:write scope covers reads on this sub-resource)
    decision = authorize_operation(ctx, CATALOG_WRITE, environment)
    if not decision.allowed:
        raise ProductImportError(
            "authorization_denied", "This operation is not permitted.", 403
        )

    mapping = rdb.get_reseller_product_mapping_by_public_id(
        ctx.reseller_id, public_id
    )
    if not mapping:
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)
    return _serialize_mapping(mapping, public_id)


def remove_mapping(
    context,
    public_id: str,
    environment: str,
) -> None:
    """Remove (hard-delete) the reseller's mapping for a product.

    The master product and all inventory remain completely untouched.
    Raises ProductImportError(404) if no mapping exists for this reseller.
    """
    ctx = _require_context(context)

    if not _PUBLIC_ID_RE.match(str(public_id or "")):
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)

    decision = authorize_operation(ctx, CATALOG_WRITE, environment)
    if not decision.allowed:
        raise ProductImportError(
            "authorization_denied", "This operation is not permitted.", 403
        )

    # Resolve mapping — resolving via public_id is safer than accepting int IDs
    mapping = rdb.get_reseller_product_mapping_by_public_id(
        ctx.reseller_id, public_id
    )
    if not mapping:
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)

    removed = rdb.remove_reseller_product_mapping(
        ctx.reseller_id, mapping["product_id"]
    )
    if not removed:
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)


MAX_SKU_LEN = 100


def _serialize_mapping(mapping: dict, public_id: str) -> dict:
    """Return only safe, client-facing fields.  Internal product_id is never exposed."""
    return {
        "product_id":          str(public_id),
        "custom_name":         mapping.get("custom_name"),
        "custom_description":  mapping.get("custom_description"),
        "reseller_sku":        mapping.get("reseller_sku"),
        "enabled":             bool(mapping.get("enabled", 1)),
        "imported_at":         mapping.get("created_at"),
        "updated_at":          mapping.get("updated_at"),
    }


_FORBIDDEN_UPDATE_FIELDS = frozenset({
    "product_id", "reseller_id", "price", "stock", "delivery", "fulfillment",
    "cost", "master_price", "master_name", "available_quantity",
    "supplier", "active", "featured", "image_file_id", "redeem_link",
    "validity", "warranty", "rules", "flash_price",
})


def update_mapping(
    context,
    public_id: str,
    environment: str,
    fields: dict,
) -> dict:
    """PATCH a reseller mapping.  Only custom_name, custom_description,
    reseller_sku, and enabled may be changed.  Master product is never touched.
    """
    ctx = _require_context(context)

    if not _PUBLIC_ID_RE.match(str(public_id or "")):
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)

    # Reject any attempt to modify master-product fields.
    forbidden = _FORBIDDEN_UPDATE_FIELDS & set(fields.keys())
    if forbidden:
        raise ProductImportError(
            "invalid_field",
            f"Field(s) are read-only or do not exist: {', '.join(sorted(forbidden))}.",
        )

    unknown = set(fields.keys()) - {"custom_name", "custom_description",
                                    "reseller_sku", "enabled"}
    if unknown:
        raise ProductImportError(
            "invalid_field",
            f"Unknown field(s): {', '.join(sorted(unknown))}.",
        )

    decision = authorize_operation(ctx, CATALOG_WRITE, environment)
    if not decision.allowed:
        raise ProductImportError(
            "authorization_denied", "This operation is not permitted.", 403
        )

    mapping = rdb.get_reseller_product_mapping_by_public_id(ctx.reseller_id, public_id)
    if not mapping:
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)

    # Validate field values
    kwargs: dict = {}
    if "custom_name" in fields:
        v = fields["custom_name"]
        if v is not None:
            v = str(v).strip()
            if len(v) > MAX_CUSTOM_NAME_LEN:
                raise ProductImportError(
                    "invalid_field",
                    f"custom_name must not exceed {MAX_CUSTOM_NAME_LEN} characters.",
                )
        kwargs["custom_name"] = v or None

    if "custom_description" in fields:
        v = fields["custom_description"]
        if v is not None:
            v = str(v).strip()
            if len(v) > MAX_CUSTOM_DESC_LEN:
                raise ProductImportError(
                    "invalid_field",
                    f"custom_description must not exceed {MAX_CUSTOM_DESC_LEN} characters.",
                )
        kwargs["custom_description"] = v or None

    if "reseller_sku" in fields:
        v = fields["reseller_sku"]
        if v is not None:
            v = str(v).strip()
            if len(v) > MAX_SKU_LEN:
                raise ProductImportError(
                    "invalid_field",
                    f"reseller_sku must not exceed {MAX_SKU_LEN} characters.",
                )
        kwargs["reseller_sku"] = v or None

    if "enabled" in fields:
        v = fields["enabled"]
        if not isinstance(v, bool):
            raise ProductImportError(
                "invalid_field", "enabled must be a boolean."
            )
        kwargs["enabled"] = v

    updated = rdb.update_reseller_product_mapping(
        ctx.reseller_id, mapping["product_id"], **kwargs
    )
    if not updated:
        raise ProductImportError("mapping_not_found", "Mapping not found.", 404)
    return _serialize_mapping(updated, public_id)


def list_mappings(context, environment: str) -> list:
    """Return all imported product mappings for the authenticated reseller."""
    ctx = _require_context(context)
    decision = authorize_operation(ctx, CATALOG_WRITE, environment)
    if not decision.allowed:
        raise ProductImportError(
            "authorization_denied", "This operation is not permitted.", 403
        )
    return read_product_mappings_for_reseller(ctx.reseller_id)


def read_product_mappings_for_reseller(reseller_id: int) -> list:
    """Shared read projection. Caller MUST authorize its server-resolved identity.

    API calls retain their ApiRequestContext gate; Telegram uses its independent
    get_active_reseller gate. No key impersonation or new authentication surface.
    """
    rows = rdb.list_reseller_product_mappings(reseller_id)
    result = []
    for row in rows:
        pub = row.get("public_id", "")
        # Compute price via existing pricing resolution
        price_cents, _src = rdb.resolve_reseller_price_from_values(
            row["master_price"], row["rule_type"],
            row["value_cents"], row["value_bps"]
        )
        available = max(0, int(row["available_quantity"] or 0))
        entry = _serialize_mapping(row, pub)
        entry["master_name"] = row.get("master_name")
        entry["master_active"] = bool(row.get("master_active", 0))
        access_enabled = bool(row.get("access_enabled", 0))
        entry["access_enabled"] = access_enabled
        if access_enabled:
            entry["price"] = rdb.from_cents(price_cents)
            entry["currency"] = "USDT"
            entry["availability"] = "in_stock" if available > 0 else "out_of_stock"
            entry["category"] = row.get("category_name")
        else:
            # Owner revoked this reseller's product access: the mapping row
            # stays manageable (custom fields, remove), but live master
            # commercial data (price / stock / category) is no longer
            # disclosed — consistent with the catalog and pricing endpoints.
            entry["price"] = None
            entry["currency"] = "USDT"
            entry["availability"] = None
            entry["category"] = None
        result.append(entry)
    return result


__all__ = [
    "ProductImportError",
    "import_product",
    "get_mapping",
    "update_mapping",
    "list_mappings",
    "remove_mapping",
    "MAX_CUSTOM_NAME_LEN",
    "MAX_CUSTOM_DESC_LEN",
    "MAX_SKU_LEN",
]
