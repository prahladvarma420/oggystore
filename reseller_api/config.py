"""Environment-backed configuration limited to the public reseller API."""

import os

from dotenv import load_dotenv


# Match the root project's configuration behavior for local development.
# Railway variables already exist in the process environment and take priority.
load_dotenv()


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


def _api_environment() -> str:
    value = os.getenv("RESELLER_API_ENVIRONMENT", "test").strip().lower()
    if value not in ("test", "production"):
        raise RuntimeError(
            "RESELLER_API_ENVIRONMENT must be 'test' or 'production'"
        )
    return value


def _database_backend() -> str:
    value = (os.getenv("DATABASE_URL", "") or "").strip().lower()
    if value.startswith(("postgres://", "postgresql://")):
        return "postgresql"
    return "sqlite"


def _is_railway_runtime() -> bool:
    return any(
        os.getenv(name)
        for name in (
            "RAILWAY_PROJECT_ID",
            "RAILWAY_ENVIRONMENT_ID",
            "RAILWAY_ENVIRONMENT_NAME",
            "RAILWAY_SERVICE_ID",
        )
    )


_API_ENVIRONMENT = _api_environment()
_DEFAULT_REQUIRE_POSTGRES = _API_ENVIRONMENT == "production" or _is_railway_runtime()


class ApiConfig:
    API_NAME = "OGGY Reseller API"
    API_VERSION = "v1"
    MAX_CONTENT_LENGTH = _body_limit()
    JSON_SORT_KEYS = False
    PROPAGATE_EXCEPTIONS = False
    INIT_DATABASE = True
    RESELLER_API_HSTS = _env_bool("RESELLER_API_HSTS", False)
    RESELLER_API_TRUST_PROXY = _env_bool("RESELLER_API_TRUST_PROXY", False)
    API_ENVIRONMENT = _API_ENVIRONMENT
    REQUIRE_POSTGRES = _env_bool(
        "RESELLER_API_REQUIRE_POSTGRES", _DEFAULT_REQUIRE_POSTGRES
    )
    DATABASE_BACKEND = _database_backend()


def validate_database_config(config) -> None:
    """Fail closed instead of silently creating a local Railway SQLite DB."""
    if config.get("REQUIRE_POSTGRES") and config.get("DATABASE_BACKEND") != "postgresql":
        raise RuntimeError(
            "The reseller API requires a PostgreSQL DATABASE_URL in this environment"
        )
