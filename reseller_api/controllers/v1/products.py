"""GET /api/v1/products controller."""

from flask import current_app, request

from reseller_api.middleware.authorization import (
    get_authenticated_context,
    require_operation,
)
from reseller_api.responses import success_response
from reseller_api.errors import ApiError
from reseller_api.services.authorization import PRODUCTS_READ
from reseller_api.services.product_catalog import (
    ProductCatalogError,
    get_product,
    get_products,
    parse_product_detail_request,
    parse_product_filters,
)


@require_operation(PRODUCTS_READ)
def list_products():
    try:
        filters = parse_product_filters(request.args)
    except ProductCatalogError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None
    products, pagination = get_products(get_authenticated_context(), filters)
    return success_response({"products": products}, meta={"pagination": pagination})


def product_detail(product_id):
    try:
        public_id = parse_product_detail_request(product_id, request.args)
        product = get_product(
            get_authenticated_context(),
            public_id,
            current_app.config["API_ENVIRONMENT"],
        )
    except ProductCatalogError as exc:
        raise ApiError(exc.code, exc.message, exc.status) from None
    return success_response({"product": product})


__all__ = ["list_products", "product_detail"]
