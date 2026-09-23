"""Request-context types shared by future authentication and controllers."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Validated identity returned by the authentication service.

    It intentionally contains neither the raw secret nor its stored hash.
    """
    environment: str
    reseller_id: int
    reseller_public_id: str
    api_key_id: int
    api_key_public_id: str


@dataclass(frozen=True)
class ApiRequestContext:
    request_id: str
    environment: str
    reseller_id: int
    reseller_public_id: str
    api_key_id: int
    api_key_public_id: str

    def as_public_dict(self):
        """Expose only correlation data; internal IDs are never serialized."""
        return {
            "request_id": self.request_id,
            "environment": self.environment,
            "reseller": self.reseller_public_id,
            "api_key": self.api_key_public_id,
        }
