"""Generic HTTP API fulfillment client stub — Phase 8."""
import requests
from fulfillment.external_clients.base import ExternalFulfillmentClient
class GenericApiClient(ExternalFulfillmentClient):
    def __init__(self, config: dict):
        self.endpoint = config.get("endpoint", "")
        self.api_key  = config.get("api_key", "")
        self.timeout  = int(config.get("timeout", 30))
    def fulfill(self, order_id: int, product_config: dict, quantity: int) -> dict:
        payload = {"order_id": order_id, "quantity": quantity}
        try:
            resp = requests.post(
                self.endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            data = resp.json()
            return {"status": "fulfilled", "delivery": data.get("delivery", "")}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
