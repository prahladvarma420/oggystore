"""Pure validation for HTTP metadata visible at the WSGI boundary."""

import re


_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_BAD_HOST_CHARS = re.compile(r"[\x00-\x20\x7f,/@\\#?]")


def validate_request_metadata(*, method, content_length=None,
                              transfer_encoding=None, connection=None,
                              content_encoding=None, method_override=None,
                              host=None):
    """Return ``None`` or a safe ``(code, message, status)`` tuple.

    This is defense in depth; the edge proxy and Gunicorn must also reject
    malformed HTTP framing before it reaches Flask.
    """
    generic = ("invalid_request", "The request metadata is invalid.", 400)
    if str(method or "").upper() not in _METHODS:
        return generic

    host = str(host or "")
    if not host or len(host) > 253 or _BAD_HOST_CHARS.search(host):
        return generic

    length = "" if content_length is None else str(content_length).strip()
    transfer = str(transfer_encoding or "").strip().lower()
    connection_tokens = {
        item.strip().lower() for item in str(connection or "").split(",") if item.strip()
    }
    if length and (not length.isdigit() or "," in length):
        return generic
    if length and transfer:
        return generic
    if transfer and transfer != "chunked":
        return generic
    if {"content-length", "transfer-encoding"} & connection_tokens:
        return generic
    if str(method_override or "").strip():
        return generic

    encoding = str(content_encoding or "").strip().lower()
    if encoding and encoding != "identity":
        return (
            "unsupported_content_encoding",
            "Compressed request bodies are not supported.",
            415,
        )
    return None


__all__ = ["validate_request_metadata"]
