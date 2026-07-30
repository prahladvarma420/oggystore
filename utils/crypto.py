import os
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:"


def _fernet():
    key = os.getenv("DATA_ENCRYPTION_KEY")
    if not key:
        return None
    raw = key.encode() if isinstance(key, str) else key
    try:
        return Fernet(raw)
    except Exception:
        # Accept ANY passphrase: derive a valid 32-byte urlsafe-base64 Fernet
        # key from it so encryption never crashes on a non-Fernet key.
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(derived)


def encrypt_text(value: str) -> str:
    if value is None:
        return value
    value = str(value)

    if value.startswith(PREFIX):
        return value

    f = _fernet()
    if not f:
        return value

    return PREFIX + f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_text(value: str) -> str:
    if value is None:
        return value
    value = str(value)

    if not value.startswith(PREFIX):
        return value

    f = _fernet()
    if not f:
        return "[encrypted: DATA_ENCRYPTION_KEY missing]"

    try:
        return f.decrypt(value[len(PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return "[encrypted: invalid key]"


# Loud startup warning: without a key, encrypt_text() stores PLAINTEXT product
# codes / credentials and nobody notices.
try:
    if not (os.getenv("DATA_ENCRYPTION_KEY") or "").strip():
        print(
            "[crypto] WARNING: DATA_ENCRYPTION_KEY is not set - "
            "delivery codes and credentials are being stored UNENCRYPTED.",
            flush=True,
        )
except Exception:
    pass
