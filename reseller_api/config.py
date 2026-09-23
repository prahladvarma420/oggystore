"""Environment-backed configuration limited to the public reseller API."""

import os


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _body_limit() -> int:
    try:
        value = int(os.getenv("RESELLER_API_MAX_BODY_BYTES", "1048576"))
    except (TypeError, ValueError):
        value = 1048576
    return min(max(value, 1024), 8 * 1024 * 1024)


class ApiConfig:
    API_NAME = "OGGY Reseller API"
    API_VERSION = "v1"
    MAX_CONTENT_LENGTH = _body_limit()
    JSON_SORT_KEYS = False
    PROPAGATE_EXCEPTIONS = False
    INIT_DATABASE = True
    RESELLER_API_HSTS = _env_bool("RESELLER_API_HSTS", False)
    API_ENVIRONMENT = (
        os.getenv("RESELLER_API_ENVIRONMENT", "test").strip().lower()
    )
    if API_ENVIRONMENT not in ("test", "production"):
        raise RuntimeError(
            "RESELLER_API_ENVIRONMENT must be 'test' or 'production'"
        )
