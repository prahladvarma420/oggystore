import getpass
import secrets
import hashlib

password = getpass.getpass("Enter new web admin password: ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    raise SystemExit("Passwords do not match.")

salt = secrets.token_hex(16)
hash_value = hashlib.pbkdf2_hmac(
    "sha256",
    password.encode("utf-8"),
    salt.encode("utf-8"),
    300_000
).hex()

print("\nAdd these to .env and remove WEB_ADMIN_PASSWORD:\n")
print(f"WEB_ADMIN_PASSWORD_SALT={salt}")
print(f"WEB_ADMIN_PASSWORD_HASH={hash_value}")
