"""Request-context types shared by future authentication and controllers."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ApiRequestContext:
    request_id: str
    environment: Optional[str] = None
    reseller_id: Optional[int] = None
    api_key_id: Optional[int] = None

    def as_public_dict(self):
        """Expose only correlation data; internal IDs are never serialized."""
        return {
            "request_id": self.request_id,
            "environment": self.environment,
        }
