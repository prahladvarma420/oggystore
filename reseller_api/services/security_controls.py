"""Uncached global API security controls backed by Web Admin settings."""

import database.db as botdb


GLOBAL_API_SETTING = "reseller_api_global_enabled"


def is_global_api_enabled() -> bool:
    """Fail closed and read fresh state for every protected request."""
    try:
        value = str(botdb.get_setting(GLOBAL_API_SETTING, "0") or "0").strip().lower()
        return value in ("1", "true", "yes", "on")
    except Exception:
        return False


__all__ = ["GLOBAL_API_SETTING", "is_global_api_enabled"]
