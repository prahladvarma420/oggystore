"""Non-sensitive health controllers for the dedicated API runtime."""

from flask import current_app, jsonify


def health():
    """Process liveness only; intentionally performs no database query."""
    return jsonify({"status": "ok", "service": "reseller-api"}), 200


def database_health():
    """Explicit readiness probe using the project's shared DB connection."""
    cursor = None
    try:
        import database.db as botdb

        cursor = botdb.get_conn().cursor()
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("database probe returned no result")
        return jsonify({"status": "ok", "service": "reseller-api"}), 200
    except Exception:
        # Never log the exception: database drivers can include connection
        # details in error strings. The HTTP response is deliberately generic.
        current_app.logger.warning("Reseller API database health check failed")
        return jsonify({"status": "unavailable", "service": "reseller-api"}), 503
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
