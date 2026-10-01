"""Gunicorn entry point for the dedicated reseller API service."""

import os

from reseller_api import create_app


app = create_app()


if __name__ == "__main__":
    # Local/direct fallback. Railway production uses the Gunicorn command in
    # RAILWAY_RESELLER_API.md, which binds to the same host and dynamic port.
    try:
        port = int(os.getenv("PORT", "5001"))
    except (TypeError, ValueError):
        raise RuntimeError("PORT must be an integer") from None
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
