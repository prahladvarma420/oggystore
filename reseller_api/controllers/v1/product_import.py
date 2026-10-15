"""POST|GET|DELETE /api/v1/products/{product_id}/import controller — Phase 11 P3."""

from flask import current_app, request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
from reseller_api.services.authorization import CATALOG_WRITE
from reseller_api.services.product_import import (
    ProductImportError,
    import_product,
    get_mapping,
    remove_mapping,
)


def _env():
    return current_app.config["API_ENVIRONMENT"]


def _ctx():
    return get_authenticated_context()


def _wrap(exc: ProductImportError) -> ApiError:
    return ApiError(exc.code, exc.message, exc.status)


@require_operation(CATALOG_WRITE)
def import_product_endpoint(product_id: str):
    """POST /api/v1/products/{product_id}/import

    Body (JSON, all optional):
      custom_name        – override display name in the reseller's catalog
      custom_description – override description
    """
    body = request.get_json(silent=True) or {}

    # Reject any fields that are not the allowed overrides.
    allowed = {"custom_name", "custom_description"}
    unknown = set(body.keys()) - allowed
    if unknown:
        raise ApiError(
            "invalid_parameter",
            f"Unknown field(s): {', '.join(sorted(unknown))}.",
            400,
        )

    try:
        mapping = import_product(
            _ctx(),
            product_id,
            _env(),
            custom_name=body.get("custom_name"),
            custom_description=body.get("custom_description"),
        )
    except ProductImportError as exc:
        raise _wrap(exc) from None
    return success_response({"mapping": mapping}, status=201)


@require_operation(CATALOG_WRITE)
def get_mapping_endpoint(product_id: str):
    """GET /api/v1/products/{product_id}/import"""
    try:
        mapping = get_mapping(_ctx(), product_id, _env())
    except ProductImportError as exc:
        raise _wrap(exc) from None
    return success_response({"mapping": mapping})


@require_operation(CATALOG_WRITE)
def remove_mapping_endpoint(product_id: str):
    """DELETE /api/v1/products/{product_id}/import"""
    try:
        remove_mapping(_ctx(), product_id, _env())
    except ProductImportError as exc:
        raise _wrap(exc) from None
    return success_response({"removed": True})


__all__ = ["import_product_endpoint", "get_mapping_endpoint", "remove_mapping_endpoint"]
