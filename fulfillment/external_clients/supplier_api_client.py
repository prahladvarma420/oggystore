"""Supplier API fulfillment client stub — Phase 8."""
from fulfillment.external_clients.base import ExternalFulfillmentClient
class SupplierApiClient(ExternalFulfillmentClient):
    def __init__(self, config: dict):
        self.supplier_id = config.get("supplier_id", "")
        self.api_key     = config.get("api_key", "")
    def fulfill(self, order_id: int, product_config: dict, quantity: int) -> dict:
        return {"status": "pending", "message": "Supplier fulfillment pending."}
