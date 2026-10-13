"""Consistent, non-sensitive JSON response builders for all API versions."""

from flask import g, jsonify


def _request_id():
    return getattr(g, "request_id", "")


def success_response(data=None, status=200, meta=None):
    payload = {
        "ok": True,
        "data": {} if data is None else data,
        "request_id": _request_id(),
    }
    if meta is not None:
        payload["meta"] = meta
    return jsonify(payload), status


def success_response_legacy(data=None, *, legacy=None, status=200, meta=None):
    """Standard envelope plus legacy top-level aliases (Phase 13 P2).

    Used by the Phase 11 catalog/pricing endpoints, which originally returned
    bare payloads (e.g. {"products": [...]}) before the ok/data/request_id
    envelope became the API-wide standard.  `legacy` is merged at the top
    level UNCHANGED so existing integrations keep working; new consumers
    should read `data`.  Legacy keys must never collide with the envelope
    keys (ok, data, request_id, meta) — callers are responsible.
    """
    payload = {
        "ok": True,
        "data": {} if data is None else data,
        "request_id": _request_id(),
    }
    if meta is not None:
        payload["meta"] = meta
    if legacy:
        for key, value in legacy.items():
            if key in ("ok", "data", "request_id", "meta"):
                continue
            payload[key] = value
    return jsonify(payload), status


def error_response(code, message, status=400):
    return jsonify({
        "ok": False,
        "error": {
            "code": str(code),
            "message": str(message),
        },
        "request_id": _request_id(),
    }), status
