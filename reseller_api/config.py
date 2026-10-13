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


def validate_environment_marker(config) -> None:
    """Fail closed when this API instance is attached to the wrong
    environment's database (Phase 13 P7 sandbox isolation).

    The first boot against a database stamps it with the API's environment
    ('test' = sandbox, 'production') in the shared settings table.  Every
    later boot must match: a test-mode API pointed at a production-stamped
    database (or vice versa) refuses to start, so sandbox keys/orders/
    stock/wallet can never touch production data by misconfiguration.
    """
    env = config.get("API_ENVIRONMENT")
    if env not in ("test", "production"):
        return  # validate on boot elsewhere
    from database.db import get_setting, set_setting
    try:
        marker = get_setting("deployment_environment")
    except Exception:
        # The settings table does not exist yet.  Real deployments always
        # run init_db() before this check (INIT_DATABASE defaults True), so a
        # missing schema means a DB-less unit-test harness — skip the guard
        # there, enforce everywhere else.
        if config.get("INIT_DATABASE", True):
            raise RuntimeError(
                "Cannot verify the deployment environment marker: the "
                "database schema is not initialized."
            )
        return
    if marker is None:
        set_setting("deployment_environment", env)
        return
    if marker != env:
        raise RuntimeError(
            "Environment mismatch: this database is stamped "
            f"'{marker}' but the API is configured for '{env}'. "
            "Point the sandbox API at its own database."
        )


def validate_database_config(config) -> None:
    """Fail closed instead of silently creating a local Railway SQLite DB."""
    if config.get("REQUIRE_POSTGRES") and config.get("DATABASE_BACKEND") != "postgresql":
        raise RuntimeError(
            "The reseller API requires a PostgreSQL DATABASE_URL in this environment"
        )
