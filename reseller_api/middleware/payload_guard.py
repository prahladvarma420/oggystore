
"""Payload guard middleware - Phase 10 Prompt 5."""
from __future__ import annotations
import json, math, logging
log = logging.getLogger(__name__)
_HARD_MAX_BYTES = 512 * 1024
_JSON_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

class _PayloadError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(code); self.code=code; self.message=message; self.status=status

def _scan_nan_inf(obj, path="root"):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            try:
                from reseller_api.errors import ApiError
                raise ApiError("invalid_payload",
                    f"Invalid numeric value at {path}: NaN and Infinity are not allowed.", 400)
            except ImportError:
                raise _PayloadError("invalid_payload",
                    f"Invalid numeric value at {path}: NaN and Infinity are not allowed.", 400)
    elif isinstance(obj, dict):
        for k, v in obj.items(): _scan_nan_inf(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj): _scan_nan_inf(v, f"{path}[{i}]")

def _emit_payload_event(description):
    try:
        from flask import g
        from reseller_api.services import security_logger as sec_log
        ctx = getattr(g, "api_context", None)
        sec_log.emit(sec_log.EVT_PAYLOAD_REJECTED,
            reseller_id=getattr(ctx, "reseller_id", None),
            reseller_public_id=getattr(ctx, "reseller_public_id", None),
            description=description)
    except Exception:
        pass

def register_payload_guard(app):
    from flask import g, request
    from reseller_api.errors import ApiError
    @app.before_request
    def _guard_payload():
        cl = request.content_length
        if cl is not None and cl > _HARD_MAX_BYTES:
            # Phase 13 P9 fix: referenced an undefined name (`content_length`)
            # here — the oversize guard itself crashed with NameError, turning
            # a clean 413 into a 500.  Log the parsed length instead.
            _emit_payload_event(f"payload too large: content_length={cl}")
            raise ApiError("payload_too_large", f"Request body exceeds maximum ({_HARD_MAX_BYTES//1024} KB).", 413)
        if request.method not in _JSON_BODY_METHODS: return
        if "application/json" not in (request.content_type or "").lower(): return
        raw = request.get_data(cache=True)
        if len(raw) > _HARD_MAX_BYTES:
            raise ApiError("payload_too_large", f"Request body exceeds maximum ({_HARD_MAX_BYTES//1024} KB).", 413)
        if not raw: return
        try: parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise ApiError("invalid_json", "Request body is not valid JSON.", 400)
        if not isinstance(parsed, dict):
            raise ApiError("invalid_payload", "Request body must be a JSON object.", 400)
        _scan_nan_inf(parsed)
        g._payload_parsed = parsed

__all__ = ["register_payload_guard", "_HARD_MAX_BYTES", "_scan_nan_inf", "_PayloadError"]
