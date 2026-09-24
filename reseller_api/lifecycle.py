"""Safe process lifecycle hooks for the dedicated reseller API runtime."""

import atexit
import logging
import sys


_logger = logging.getLogger("reseller_api.lifecycle")
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)


def register_lifecycle(app):
    """Register startup/shutdown logging without exposing configuration values."""
    _logger.info(
        "Reseller API initialized environment=%s database=%s",
        app.config.get("API_ENVIRONMENT", "unknown"),
        app.config.get("DATABASE_BACKEND", "unknown"),
    )

    def _shutdown():
        try:
            from database.db import close_thread_connection

            close_thread_connection()
        except Exception:
            # Shutdown must remain best-effort and must not log driver errors,
            # because those strings can contain connection details.
            pass
        _logger.info("Reseller API worker stopped")

    atexit.register(_shutdown)
