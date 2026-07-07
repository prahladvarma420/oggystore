#!/usr/bin/env python3
"""Set / change the web-admin login credentials, stored HASHED in the database.

Run from the project root:

    python tools/set_admin_password.py

It asks for a username and password, then hashes them with a salted, peppered
PBKDF2-SHA256 (600,000 iterations) and stores ONLY the hashes in the database
(settings table). Plaintext is never written anywhere -- the password can never
be decrypted by anyone, including you. After running this you can safely delete
WEB_ADMIN_USERNAME and WEB_ADMIN_PASSWORD from your .env file.

IMPORTANT: keep DATA_ENCRYPTION_KEY (or WEB_ADMIN_SECRET) in your .env unchanged.
It is used as a secret pepper; if it ever changes you must reset the password
again with this tool.
"""
import os
import sys
import hmac
import hashlib
import secrets
import sqlite3
import getpass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Load .env so the pepper (DATA_ENCRYPTION_KEY / WEB_ADMIN_SECRET) is available.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    env_path = os.path.join(ROOT, ".env")
    if os.path.isfile(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Resolve the same database file the bot/web admin use.
try:
    sys.path.insert(0, ROOT)
    import config  # type: ignore
    db_name = getattr(config, "DB_PATH", "shopbot.db")
except Exception:
    db_name = "shopbot.db"

DB_PATH = db_name if os.path.isabs(db_name) else os.path.join(ROOT, db_name)

WEB_CRED_ITERS = 600_000


def pepper():
    return (os.getenv("DATA_ENCRYPTION_KEY") or os.getenv("WEB_ADMIN_SECRET") or "").encode("utf-8")


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8") + pepper(), salt, WEB_CRED_ITERS)
    return "pbkdf2_sha256$%d$%s$%s" % (WEB_CRED_ITERS, salt.hex(), dk.hex())


def hash_username(username):
    return hmac.new(pepper() or b"oggy-admin", username.strip().encode("utf-8"), hashlib.sha256).hexdigest()


def main():
    if not os.path.isfile(DB_PATH):
        print("Database not found at:", DB_PATH)
        print("Run this from your project root after the bot has created the DB.")
        sys.exit(1)
    if not pepper():
        print("WARNING: no DATA_ENCRYPTION_KEY or WEB_ADMIN_SECRET found in .env.")
        print("Credentials will still be hashed, but setting a strong key is strongly recommended.\n")

    print("Set web admin login credentials (stored HASHED in the database).\n")
    username = input("New admin username: ").strip()
    if not username:
        print("Username cannot be empty.")
        sys.exit(1)
    pw1 = getpass.getpass("New admin password: ")
    pw2 = getpass.getpass("Confirm password:  ")
    if pw1 != pw2:
        print("Passwords do not match.")
        sys.exit(1)
    if len(pw1) < 6:
        print("Please use at least 6 characters.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS web_admins("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
        "pwd TEXT NOT NULL, role TEXT DEFAULT 'admin', active INTEGER DEFAULT 1, "
        "twofa_secret TEXT DEFAULT '', twofa_enabled INTEGER DEFAULT 0, "
        "twofa_recovery TEXT DEFAULT '[]', "
        "created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)), last_login TEXT)"
    )

    pwd_hash = hash_password(pw1)

    # web_admins is the table the web admin actually logs in against.
    owner = conn.execute(
        "SELECT id FROM web_admins WHERE role='owner' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    clash = conn.execute(
        "SELECT id FROM web_admins WHERE username=?", (username,)
    ).fetchone()
    if clash and (owner is None or clash["id"] != owner["id"]):
        print("\nAnother admin account already uses the username %r." % username)
        print("Pick a different username, or remove that account from the web panel first.")
        conn.close()
        sys.exit(1)

    if owner:
        conn.execute(
            "UPDATE web_admins SET username=?, pwd=?, active=1 WHERE id=?",
            (username, pwd_hash, owner["id"]),
        )
        action = "updated"
    else:
        conn.execute(
            "INSERT INTO web_admins(username, pwd, role, active, twofa_enabled) "
            "VALUES(?, ?, 'owner', 1, 0)",
            (username, pwd_hash),
        )
        action = "created"

    # Keep a legacy hashed copy in settings too (harmless fallback for fresh DBs).
    for k, v in (("web_admin_username_h", hash_username(username)),
                 ("web_admin_pwd", pwd_hash)):
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, v),
        )
    conn.commit()
    conn.close()

    print("\nDone. Owner login %s (hashed) in: %s" % (action, DB_PATH))
    print("Username:", username)
    print("Restart the web admin and log in with the new credentials.")
    print("On first login you will be asked to set up 2FA (it is mandatory).")


if __name__ == "__main__":
    main()
