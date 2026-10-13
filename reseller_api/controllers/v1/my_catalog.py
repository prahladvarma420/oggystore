"""
PHASE 11 P4 — /api/v1/my-products  (reseller's personalised catalog view)

Endpoints:
  GET    /my-products                   list all imported products
  GET    /my-products/<product_id>      get one
  PATCH  /my-products/<product_id>      update custom fields (NOT master product)
  DELETE /my-products/<product_id>      remove mapping (master product unchanged)

Only custom_name, custom_description, reseller_sku, and enabled may be
changed via PATCH.  Any attempt to modify master-product fields is rejected.

Phase 13 P2 — standardised on the API-wide envelope:
  success  -> {"ok": true, "data": {...}, "request_id": ...}
  error    -> {"ok": false, "error": {"code", "message"}, "request_id": ...}
Legacy top-level fields ("products", mapping attributes) are preserved as
deprecated aliases so Phase 11 integrations keep working unchanged.
Errors now use the shared codes: invalid_request, invalid_parameter,
authorization_denied, authentication_failed.
"""
from flask import current_app, request

from reseller_api.middleware.authorization import get_authenticated_context
from reseller_api.services.authorization import CATALOG_WRITE
from reseller_api.errors import ApiError
from reseller_api.responses import success_response_legacy
from reseller_api.services.product_import import (
    ProductImportError,
    get_mapping,
    update_mapping,
    list_mappings,
    remove_mapping,
)
from reseller_api.decorators import require_operation

_ALLOWED_PATCH_FIELDS = frozenset({"custom_name", "custom_description",
                                   "reseller_sku", "enabled"})


def _service_error(exc: ProductImportError) -> ApiError:
    return ApiError(exc.code, exc.message, exc.status)


@require_operation(CATALOG_WRITE)
def list_my_products_endpoint():
    """GET /api/v1/my-products"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        items = list_mappings(context, environment)
    except ProductImportError as exc:
        raise _service_error(exc) from None
    return success_response_legacy(
        {"products": items},
        legacy={"products": items},          # deprecated alias (Phase 11 shape)
    )


@require_operation(CATALOG_WRITE)
def get_my_product_endpoint(product_id: str):
    """GET /api/v1/my-products/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        mapping = get_mapping(context, product_id, environment)
    except ProductImportError as exc:
        raise _service_error(exc) from None
    # Legacy shape was the bare mapping object at top level.
    return success_response_legacy(mapping, legacy=dict(mapping))


@require_operation(CATALOG_WRITE)
def patch_my_product_endpoint(product_id: str):
    """PATCH /api/v1/my-products/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        raise ApiError(
            "invalid_request", "Request body must be a JSON object.", 400)
    if not body:
        raise ApiError("invalid_request", "Nothing to update.", 400)

    unknown = set(body.keys()) - _ALLOWED_PATCH_FIELDS
    if unknown:
        raise ApiError(
            "invalid_parameter",
            f"Unknown or forbidden field(s): {', '.join(sorted(unknown))}.",
            422,
        )

    try:
        updated = update_mapping(context, product_id, environment, body)
    except ProductImportError as exc:
        raise _service_error(exc) from None
    return success_response_legacy(updated, legacy=dict(updated))


@require_operation(CATALOG_WRITE)
def delete_my_product_endpoint(product_id: str):
    """DELETE /api/v1/my-products/<product_id> — 204 No Content on success.

    Matches DELETE /webhooks/<id> semantics (empty body by design).
    """
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        remove_mapping(context, product_id, environment)
    except ProductImportError as exc:
        raise _service_error(exc) from None
    return "", 204
