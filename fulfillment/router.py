"""Fulfillment router — Phase 8.

Routes an order to the correct fulfillment client based on
the product's fulfillment_type field.
"""

import json
from database.db import _safe_add_column, get_conn, get_cursor

FULFILLMENT_CODE    = "code_stock"
FULFILLMENT_MANUAL  = "manual"
FULFILLMENT_EXTERNAL = "external_api"
FULFILLMENT_SUPPLIER = "supplier_api"


def ensure_fulfillment_columns():
    """Add the router's product columns if missing (idempotent).

    Phase 12 P10 finding: fulfillment_type / fulfillment_config were never
    added to any schema path, so route_fulfillment() would have crashed with
    "no such column" the first time it was invoked.  Registered with the
    Phase 12 startup schema hook (ensure_order_metadata_columns) so every
    boot applies it.
    """
    conn = get_conn()
    _safe_add_column("products", "fulfillment_type", "TEXT")
    _safe_add_column("products", "fulfillment_config", "TEXT")
    conn.commit()


def get_product_fulfillment_type(product_id: int) -> str:
    """Return the fulfillment_type for a product."""
    cur = get_cursor()
    cur.execute("SELECT fulfillment_type FROM products WHERE id=?", (int(product_id),))
    row = cur.fetchone()
    return str(row[0] or FULFILLMENT_CODE) if row else FULFILLMENT_CODE


# Bound outstanding DNS/HTTP calls even when an OS resolver stalls. Threads
# run network-only work, never delivery/refund writes. No 16.7 worker here.
import threading
import queue
import time
_DISPATCH_SLOTS = threading.BoundedSemaphore(16)


def route_fulfillment(order_id: int, product_id: int, quantity: int) -> dict:
    cur = get_cursor()
    cur.execute("SELECT fulfillment_type, fulfillment_config FROM products WHERE id=?",(int(product_id),))
    row = cur.fetchone()
    if not row:
        return {"type":"unknown", "result":{"status":"terminal_error","category":"unknown"}}
    ftype, raw = row[0] or FULFILLMENT_CODE, row[1]
    if ftype == FULFILLMENT_MANUAL:
        return {"type":ftype,"result":{"status":"pending"}}
    if ftype == FULFILLMENT_CODE:
        return {"type":ftype,"result":{"status":"handled_upstream"}}
    if ftype not in (FULFILLMENT_EXTERNAL,FULFILLMENT_SUPPLIER):
        return {"type":ftype,"result":{"status":"terminal_error","category":"unknown"}}
    try:
        from fulfillment.external_clients.generic_api_client import load_config
        cfg = load_config(raw)
        from fulfillment.external_clients.generic_api_client import GenericApiClient, validate_config
        if ftype == FULFILLMENT_SUPPLIER:
            from fulfillment.external_clients.supplier_api_client import SupplierApiClient, validate_supplier_config
            cfg = validate_supplier_config(cfg,resolve=False)
            client = SupplierApiClient(cfg)
        else:
            cfg = validate_config(cfg,resolve=False)
            client = GenericApiClient(cfg)
    except Exception:
        return {"type":ftype,"result":{"status":"terminal_error","category":"unknown"}}
    return {"type":ftype,"result":dispatch_client(client,cfg,order_id,quantity)}


def dispatch_client(client, cfg, order_id, quantity):
    """Reuse the 16.6 bounded, network-only transport for placement and polls."""
    if not _DISPATCH_SLOTS.acquire(blocking=False):
        return {"status":"pending","category":"dispatch_busy"}
    deadline = time.monotonic()+cfg['dispatch_budget']
    client.deadline = deadline
    result_queue = queue.Queue(maxsize=1)
    def send():
        try:
            result_queue.put(client.fulfill(order_id,cfg,quantity))
        except Exception:
            result_queue.put({"status":"terminal_error","category":"unknown"})
        finally:
            _DISPATCH_SLOTS.release()
    try:
        threading.Thread(target=send,daemon=True,name='bounded-fulfillment-dispatch').start()
    except Exception:
        _DISPATCH_SLOTS.release()
        return {"status":"pending","category":"dispatch_busy"}
    try:
        result = result_queue.get(timeout=max(0,deadline-time.monotonic()))
    except queue.Empty:
        client.stop.set()
        result = {"status":"pending","category":"dispatch_budget"}
    return result


__all__ = ["FULFILLMENT_CODE", "FULFILLMENT_MANUAL", "FULFILLMENT_EXTERNAL",
           "FULFILLMENT_SUPPLIER", "get_product_fulfillment_type", "route_fulfillment", "dispatch_client"]
