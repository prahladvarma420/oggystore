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


def error_response(code, message, status=400):
    return jsonify({
        "ok": False,
        "error": {
            "code": str(code),
            "message": str(message),
        },
        "request_id": _request_id(),
    }), status
