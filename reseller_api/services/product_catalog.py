"""Safe read-only orchestration for the reseller master-product catalog."""

import html
import re

import database.reseller_db as rdb
from reseller_api.context import ApiRequestContext
from reseller_api.services.authorization import authorize_product_access


DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_PAGE = 100_000
MAX_OFFSET = 100_000
MAX_SEARCH_LENGTH = 100
MAX_CATEGORY_LENGTH = 100
MAX_NAME_OUTPUT = 200
MAX_DESCRIPTION_OUTPUT = 1000
MAX_CATEGORY_OUTPUT = 100
MAX_METADATA_OUTPUT = 250
_TAG_RE = re.compile(r"<[^>]*>")
_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_FORBIDDEN_IDENTITY_PARAMS = frozenset({
    "reseller", "reseller_id", "reseller_public_id",
})
_UNSUPPORTED_PARAMS = frozenset({
    "active", "status", "price", "unit_price", "reseller_price",
})
_ALLOWED_PARAMS = frozenset({"page", "limit", "search", "category", "availability"})
_VALID_AVAILABILITY = frozenset({"in_stock", "out_of_stock"})
_PUBLIC_PRODUCT_ID_RE = re.compile(r"^PRD-[A-HJ-NP-Z2-9]{12}$")


class ProductCatalogError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _plain_text(value, maximum: int) -> str:
    value = _BREAK_RE.sub("\n", str(value or ""))
    value = html.unescape(_TAG_RE.sub("", value)).strip()
    if len(value) <= maximum:
        return value
    return value[:maximum - 1].rstrip() + "…"


def _query_text(raw, name: str) -> str:
    if isinstance(raw, (dict, list, tuple, set)):
        raise ProductCatalogError(
            "invalid_filter", f"{name} must be a text value."
        )
    return str(raw or "").strip()


def _reject_parameter_pollution(args):
    getlist = getattr(args, "getlist", None)
    if not callable(getlist):
        return
    if any(len(getlist(key)) != 1 for key in args.keys()):
        raise ProductCatalogError(
            "invalid_parameter", "Duplicate query parameters are not allowed."
        )


def _positive_int(raw, name: str, default: int, maximum: int) -> int:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text.isdigit():
        raise ProductCatalogError(
            "invalid_pagination", f"{name} must be a positive integer."
        )
    value = int(text)
    if value < 1 or value > maximum:
        raise ProductCatalogError(
            "invalid_pagination",
            f"{name} must be between 1 and {maximum}.",
        )
    return value


def parse_product_filters(args):
    """Validate the bounded public query surface without accepting identity."""
    _reject_parameter_pollution(args)
    keys = set(args.keys())
    if keys & _FORBIDDEN_IDENTITY_PARAMS:
        raise ProductCatalogError(
            "invalid_parameter",
            "Reseller identity is determined by the authenticated API key.",
        )
    if keys & _UNSUPPORTED_PARAMS:
        raise ProductCatalogError(
            "invalid_filter",
            "This product filter is not supported.",
        )
    unknown = keys - _ALLOWED_PARAMS
    if unknown:
        raise ProductCatalogError(
            "invalid_parameter", "An unsupported query parameter was provided."
        )

    page = _positive_int(args.get("page"), "page", 1, MAX_PAGE)
    limit = _positive_int(args.get("limit"), "limit", DEFAULT_LIMIT, MAX_LIMIT)
    if (page - 1) * limit > MAX_OFFSET:
        raise ProductCatalogError(
            "invalid_pagination",
            f"Pagination offset must not exceed {MAX_OFFSET} records.",
        )
    search = _query_text(args.get("search", ""), "search")
    category = _query_text(args.get("category", ""), "category")
    availability = _query_text(args.get("availability", ""), "availability")
    if len(search) > MAX_SEARCH_LENGTH:
        raise ProductCatalogError(
            "invalid_filter",
            f"search must not exceed {MAX_SEARCH_LENGTH} characters.",
        )
    if len(category) > MAX_CATEGORY_LENGTH:
        raise ProductCatalogError(
            "invalid_filter",
            f"category must not exceed {MAX_CATEGORY_LENGTH} characters.",
        )
    if availability and availability not in _VALID_AVAILABILITY:
        raise ProductCatalogError(
            "invalid_filter",
            "availability must be 'in_stock' or 'out_of_stock'.",
        )
    return {
        "page": page,
        "limit": limit,
        "search": search,
        "category": category,
        "availability": availability,
    }


def get_products(context: ApiRequestContext, filters: dict):
    if not isinstance(context, ApiRequestContext):
        raise ProductCatalogError(
            "authentication_failed",
            "Authentication credentials are missing or invalid.",
        )

    rows, total = rdb.list_reseller_api_products(
        context.reseller_id,
        page=filters["page"],
        limit=filters["limit"],
        search=filters["search"],
        category=filters["category"],
        availability=filters.get("availability", ""),
    )
    products = []
    for row in rows:
        price_cents, _source = rdb.resolve_reseller_price_from_values(
            row["master_price"], row["rule_type"], row["value_cents"],
            row["value_bps"],
        )
        if price_cents is None:
            continue
        available = max(0, int(row["available_quantity"] or 0))
        products.append(_serialize_product(row, price_cents, available))

    page = filters["page"]
    limit = filters["limit"]
    pages = (total + limit - 1) // limit if total else 0
    return products, {
        "page": page,
        "limit": limit,
        "total": total,
        "pages": pages,
        "has_next": page < pages,
        "has_previous": page > 1 and total > 0,
    }


def _serialize_product(row: dict, price_cents: int, available=None):
    if available is None:
        available = max(0, int(row["available_quantity"] or 0))
    return {
        "id": str(row["public_id"]),
        "name": _plain_text(row["name"], MAX_NAME_OUTPUT),
        "description": _plain_text(row["description"], MAX_DESCRIPTION_OUTPUT),
        "category": (
            _plain_text(row["category_name"], MAX_CATEGORY_OUTPUT)
            if row["category_name"] is not None else None
        ),
        "price": rdb.from_cents(price_cents),
        "currency": "USDT",
        # status is always "active" here: only active+access-enabled products
        # reach serialization (inactive/deleted filtered at query time).
        "status": "active",
        "availability": "in_stock" if available > 0 else "out_of_stock",
        "available_quantity": available,
        "max_per_order": max(1, int(row["max_per_order"] or 1)),
        "validity": _plain_text(row["validity"], MAX_METADATA_OUTPUT),
        "warranty": _plain_text(row["warranty"], MAX_METADATA_OUTPUT),
        "featured": bool(row["featured"]),
    }


def parse_product_detail_request(product_id, args=None) -> str:
    """Validate the opaque path id and reject all client-owned fields."""
    if args is not None:
        _reject_parameter_pollution(args)
    if args is not None and set(args.keys()):
        keys = set(args.keys())
        if keys & (_FORBIDDEN_IDENTITY_PARAMS | _UNSUPPORTED_PARAMS | {"total", "cost"}):
            raise ProductCatalogError(
                "invalid_parameter",
                "Identity and pricing fields are determined by the server.",
            )
        raise ProductCatalogError(
            "invalid_parameter", "This endpoint does not accept query parameters."
        )
    value = str(product_id or "")
    if not _PUBLIC_PRODUCT_ID_RE.fullmatch(value):
        raise ProductCatalogError(
            "product_not_found", "The requested product was not found.", 404
        )
    return value


def get_product(context: ApiRequestContext, public_id: str,
                expected_environment: str):
    """Return one reseller-authorized product without existence oracles."""
    if not isinstance(context, ApiRequestContext):
        raise ProductCatalogError(
            "authentication_failed",
            "Authentication credentials are missing or invalid.",
            401,
        )
    row = rdb.get_reseller_api_product(context.reseller_id, public_id)
    if not row:
        raise ProductCatalogError(
            "product_not_found", "The requested product was not found.", 404
        )

    # Revalidate mutable reseller/key/product/access state after lookup. A
    # concurrent revocation therefore fails closed before serialization.
    decision = authorize_product_access(
        context, int(row["product_id"]), expected_environment
    )
    if not decision.allowed:
        raise ProductCatalogError(
            "product_not_found", "The requested product was not found.", 404
        )
    price_cents, _source = rdb.resolve_reseller_price_from_values(
        row["master_price"], row["rule_type"], row["value_cents"],
        row["value_bps"],
    )
    if price_cents is None:
        raise ProductCatalogError(
            "product_not_found", "The requested product was not found.", 404
        )
    return _serialize_product(row, price_cents)


__all__ = [
    "DEFAULT_LIMIT", "MAX_LIMIT", "MAX_OFFSET", "ProductCatalogError", "get_product",
    "get_products", "parse_product_detail_request", "parse_product_filters",
]
