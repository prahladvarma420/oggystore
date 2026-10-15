#!/usr/bin/env python3
"""Reset the web-admin 2FA so you can set it up fresh.

Run this once from the project root:

    python tools/reset_2fa.py

It clears the stored 2FA secret, disables 2FA, and wipes recovery codes.
After running it, restart the web admin, log in with just your username and
password, then open the Security page and set up 2FA again from the beginning.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Resolve the same database file the bot/web admin use.
try:
    sys.path.insert(0, ROOT)
    import config  # type: ignore
    db_name = getattr(config, "DB_PATH", "shopbot.db")
except Exception:
    db_name = "shopbot.db"

DB_PATH = db_name if os.path.isabs(db_name) else os.path.join(ROOT, db_name)

if not os.path.isfile(DB_PATH):
    print("Database not found at:", DB_PATH)
    print("Run this script from your project root.")
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

rows = conn.execute(
    "SELECT id, username, twofa_enabled FROM web_admins ORDER BY id ASC"
).fetchall()
if rows:
    print("Web admin accounts:")
    for row in rows:
        state = "2FA on" if row["twofa_enabled"] else "2FA off"
        print("  - %s  (%s)" % (row["username"], state))
    target = input("\nUsername to reset 2FA for (leave blank to reset ALL): ").strip()
    if target:
        cur = conn.execute(
            "UPDATE web_admins SET twofa_secret='', twofa_enabled=0, twofa_recovery='[]' "
            "WHERE username=?",
            (target,),
        )
        if cur.rowcount == 0:
            print("No account found with username:", target)
        else:
            print("2FA reset for:", target)
    else:
        conn.execute(
            "UPDATE web_admins SET twofa_secret='', twofa_enabled=0, twofa_recovery='[]'"
        )
        print("2FA reset for ALL web admin accounts.")
else:
    print("No web_admins accounts found yet -- nothing to reset.")

# also clear the legacy global 2FA settings (older single-admin setup)
for key, value in (
    ("web_2fa_enabled", "0"),
    ("web_2fa_secret", ""),
    ("web_2fa_recovery", "[]"),
):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
conn.commit()
conn.close()

print("\nDone on:", DB_PATH)
print("Restart the web admin, log in with username + password,")
print("then set up 2FA again from the start at first login.")
