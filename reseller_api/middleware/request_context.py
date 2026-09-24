"""Request IDs and safe structured access logging."""

import json
import ipaddress
import logging
import sys
import time

from flask import current_app, g, request

from reseller_api.services.observability import (
    build_audit_record,
    generate_request_id,
    persist_audit_record,
    redact_sensitive,
)


_access_logger = logging.getLogger("reseller_api.access")
_access_logger.setLevel(logging.INFO)
_access_logger.propagate = False
if not _access_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _access_logger.addHandler(handler)


def _client_ip():
    candidate = request.remote_addr or ""
    if current_app.config.get("RESELLER_API_TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return "unknown"


def register_request_context(app):
    @app.before_request
    def _begin_request():
        # Server-generated IDs are authoritative; untrusted incoming IDs are
        # never copied into logs or responses.
        g.request_id = generate_request_id()
        g.request_started_at = time.perf_counter()

    @app.after_request
    def _finish_request(response):
        request_id = getattr(g, "request_id", "")
        response.headers["X-Request-ID"] = request_id
        try:
            elapsed_ms = int(
                (time.perf_counter() - getattr(
                    g, "request_started_at", time.perf_counter()
                )) * 1000
            )
            # Prefer the route template so resource identifiers and arbitrary
            # path data are not copied into logs. Query strings are never used.
            endpoint = (
                request.url_rule.rule if request.url_rule is not None
                else request.path
            )
            record = build_audit_record(
                request_id=request_id,
                method=request.method,
                endpoint=endpoint,
                status_code=response.status_code,
                response_ms=elapsed_ms,
                context=getattr(g, "api_context", None),
                ip=_client_ip(),
                user_agent=request.user_agent.string,
            )
            # Railway polls /health frequently. Keep liveness/readiness cheap
            # and independent of database availability; health requests are
            # still emitted to structured stdout logs, but are not persisted.
            is_health_probe = request.path in ("/health", "/health/db")
            persisted = False if is_health_probe else persist_audit_record(record)
            fields = record.structured_fields()
            fields["audit_persisted"] = persisted
            _access_logger.info(json.dumps(
                redact_sensitive(fields), separators=(",", ":")
            ))
        except Exception:
            # Request observability is strictly best-effort and must never
            # replace or corrupt the actual API response.
            pass
        return response
