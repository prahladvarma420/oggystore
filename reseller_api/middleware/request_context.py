"""Request IDs and safe structured access logging."""

import json
import logging
import sys
import time
import uuid

from flask import g, request


_access_logger = logging.getLogger("reseller_api.access")
_access_logger.setLevel(logging.INFO)
_access_logger.propagate = False
if not _access_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _access_logger.addHandler(handler)


def register_request_context(app):
    @app.before_request
    def _begin_request():
        # Server-generated IDs are authoritative; untrusted incoming IDs are
        # never copied into logs or responses.
        g.request_id = uuid.uuid4().hex
        g.request_started_at = time.perf_counter()

    @app.after_request
    def _finish_request(response):
        request_id = getattr(g, "request_id", "")
        elapsed_ms = int(
            (time.perf_counter() - getattr(g, "request_started_at", time.perf_counter()))
            * 1000
        )
        response.headers["X-Request-ID"] = request_id
        _access_logger.info(json.dumps({
            "event": "api_request",
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "response_ms": elapsed_ms,
        }, separators=(",", ":")))
        return response
