"""Abstract base for external fulfillment clients — Phase 8."""
from abc import ABC, abstractmethod
class ExternalFulfillmentClient(ABC):
    @abstractmethod
    def fulfill(self, order_id: int, product_config: dict, quantity: int) -> dict:
        """Attempt fulfillment. Returns dict with 'status' and optional 'delivery'."""
        ...
