"""
PHASE 11 P4 — /api/v1/my-products  (reseller's personalised catalog view)

Endpoints:
  GET    /my-products                   list all imported products
  GET    /my-products/<product_id>      get one
  PATCH  /my-products/<product_id>      update custom fields (NOT master product)
  DELETE /my-products/<product_id>      remove mapping (master product unchanged)

Only custom_name, custom_description, reseller_sku, and enabled may be
changed via PATCH.  Any attempt to modify master-product fields is rejected.
"""
from flask import current_app, request, jsonify

from reseller_api.middleware.authorization import get_authenticated_context
from reseller_api.services.authorization import CATALOG_WRITE
from reseller_api.services.endpoint_rate_limit import CATEGORY_CATALOG_WRITE
from reseller_api.services.product_import import (
    ProductImportError,
    get_mapping,
    update_mapping,
    list_mappings,
    remove_mapping,
)
from reseller_api.decorators import require_operation, endpoint_category

_ALLOWED_PATCH_FIELDS = frozenset({"custom_name", "custom_description",
                                   "reseller_sku", "enabled"})


@require_operation(CATALOG_WRITE)
@endpoint_category(CATEGORY_CATALOG_WRITE)
def list_my_products_endpoint():
    """GET /api/v1/my-products"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        items = list_mappings(context, environment)
    except ProductImportError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return jsonify({"products": items}), 200


@require_operation(CATALOG_WRITE)
@endpoint_category(CATEGORY_CATALOG_WRITE)
def get_my_product_endpoint(product_id: str):
    """GET /api/v1/my-products/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        mapping = get_mapping(context, product_id, environment)
    except ProductImportError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return jsonify(mapping), 200


@require_operation(CATALOG_WRITE)
@endpoint_category(CATEGORY_CATALOG_WRITE)
def patch_my_product_endpoint(product_id: str):
    """PATCH /api/v1/my-products/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "invalid_body", "message": "Request body must be a JSON object."}), 400

    if not body:
        return jsonify({"error": "empty_body", "message": "Nothing to update."}), 400

    unknown = set(body.keys()) - _ALLOWED_PATCH_FIELDS
    if unknown:
        return jsonify({
            "error": "invalid_field",
            "message": f"Unknown or forbidden field(s): {', '.join(sorted(unknown))}.",
        }), 422

    try:
        updated = update_mapping(context, product_id, environment, body)
    except ProductImportError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return jsonify(updated), 200


@require_operation(CATALOG_WRITE)
@endpoint_category(CATEGORY_CATALOG_WRITE)
def delete_my_product_endpoint(product_id: str):
    """DELETE /api/v1/my-products/<product_id>"""
    context = get_authenticated_context()
    environment = current_app.config["API_ENVIRONMENT"]
    try:
        remove_mapping(context, product_id, environment)
    except ProductImportError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return "", 204
