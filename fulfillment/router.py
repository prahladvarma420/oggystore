"""Fulfillment router — Phase 8.

Routes an order to the correct fulfillment client based on
the product's fulfillment_type field.
"""

import json
from database.db import get_cursor

FULFILLMENT_CODE    = "code_stock"
FULFILLMENT_MANUAL  = "manual"
FULFILLMENT_EXTERNAL = "external_api"
FULFILLMENT_SUPPLIER = "supplier_api"


def get_product_fulfillment_type(product_id: int) -> str:
    """Return the fulfillment_type for a product."""
    cur = get_cursor()
    cur.execute("SELECT fulfillment_type FROM products WHERE id=?", (int(product_id),))
    row = cur.fetchone()
    return str(row[0] or FULFILLMENT_CODE) if row else FULFILLMENT_CODE


def route_fulfillment(order_id: int, product_id: int, quantity: int) -> dict:
    """Route an order to the appropriate fulfillment handler.

    Returns dict with 'type' and 'result' keys.
    fulfillment_config is read server-side only; never from client input.
    """
    cur = get_cursor()
    cur.execute(
        "SELECT fulfillment_type, fulfillment_config FROM products WHERE id=?",
        (int(product_id),),
    )
    row = cur.fetchone()
    if not row:
        return {"type": "unknown", "result": {"status": "error", "error": "product not found"}}

    ftype, fconfig_raw = row[0] or FULFILLMENT_CODE, row[1]

    try:
        fconfig = json.loads(fconfig_raw) if fconfig_raw else {}
    except Exception:
        fconfig = {}

    if ftype == FULFILLMENT_MANUAL:
        return {"type": FULFILLMENT_MANUAL, "result": {"status": "pending"}}

    if ftype == FULFILLMENT_EXTERNAL:
        from fulfillment.external_clients.generic_api_client import GenericApiClient
        client = GenericApiClient(fconfig)
        result = client.fulfill(order_id, fconfig, quantity)
        return {"type": FULFILLMENT_EXTERNAL, "result": result}

    if ftype == FULFILLMENT_SUPPLIER:
        from fulfillment.external_clients.supplier_api_client import SupplierApiClient
        client = SupplierApiClient(fconfig)
        result = client.fulfill(order_id, fconfig, quantity)
        return {"type": FULFILLMENT_SUPPLIER, "result": result}

    # Default: code_stock — handled upstream by order_stock.py
    return {"type": FULFILLMENT_CODE, "result": {"status": "handled_upstream"}}


__all__ = [
    "FULFILLMENT_CODE", "FULFILLMENT_MANUAL",
    "FULFILLMENT_EXTERNAL", "FULFILLMENT_SUPPLIER",
    "get_product_fulfillment_type", "route_fulfillment",
]
