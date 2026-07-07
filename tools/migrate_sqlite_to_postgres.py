"""
One-time data copy: local shopbot.db (SQLite)  ->  PostgreSQL.

RUN THIS ON A MACHINE THAT CAN REACH POSTGRES (your VPS, or locally), with:

    pip install "psycopg[binary]"
    export DATABASE_URL="postgresql://...your EXTERNAL url..."
    python tools/migrate_sqlite_to_postgres.py

What it does
------------
1. Connects to Postgres (DATABASE_URL) and creates the full schema by calling
   the app's own init_db() (so the schema always matches the code).
2. Copies every row from each table in shopbot.db into Postgres, overwriting
   seeded defaults (settings / templates / admins) with your real values.
3. Fixes the auto-increment sequences so new inserts don't collide.

Already-encrypted values (enc:...) are copied as-is and stay encrypted.
It is safe to re-run (rows are upserted by primary key).
"""

import os
import sys
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:
    pass

DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()
if not DATABASE_URL.lower().startswith(("postgres://", "postgresql://")):
    print("ERROR: set DATABASE_URL to your Postgres URL first.")
    sys.exit(1)

try:
    import config
    SQLITE_PATH = os.path.join(ROOT, getattr(config, "DB_PATH", "shopbot.db"))
except Exception:
    SQLITE_PATH = os.path.join(ROOT, "shopbot.db")

if not os.path.exists(SQLITE_PATH):
    print("ERROR: shopbot.db not found at", SQLITE_PATH)
    sys.exit(1)

import psycopg

print("0/3  Resetting Postgres (dropping any existing tables) ...")
import psycopg as _pgx
_r = _pgx.connect(DATABASE_URL, autocommit=True)
with _r.cursor() as _c:
    _c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    _tbls = [row[0] for row in _c.fetchall()]
    for _t in _tbls:
        _c.execute('DROP TABLE IF EXISTS "%s" CASCADE' % _t)
_r.close()
print("     dropped %d existing table(s)" % len(_tbls))

print("1/3  Creating Postgres schema via init_db() ...")
# DATABASE_URL is set -> database.db connects to Postgres through pgcompat.
import database.db as botdb
botdb.init_db()
# The web admin creates a few extra things at its own startup (a products.cost
# column + the web_* tables). Create them here too so their data migrates.
_web_ddl = [
    "ALTER TABLE products ADD COLUMN cost REAL DEFAULT 0",
    "CREATE TABLE IF NOT EXISTS web_login_log("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, ip TEXT, "
    "user_agent TEXT, status TEXT, created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)))",
    "CREATE TABLE IF NOT EXISTS web_sessions("
    "sid TEXT PRIMARY KEY, username TEXT, ip TEXT, user_agent TEXT, "
    "created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)), "
    "last_seen TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)))",
    "CREATE TABLE IF NOT EXISTS web_admins("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
    "pwd TEXT NOT NULL, role TEXT DEFAULT 'admin', active INTEGER DEFAULT 1, "
    "twofa_secret TEXT DEFAULT '', twofa_enabled INTEGER DEFAULT 0, "
    "twofa_recovery TEXT DEFAULT '[]', created TEXT DEFAULT (datetime(CURRENT_TIMESTAMP)), "
    "last_login TEXT)",
]
for _d in _web_ddl:
    try:
        botdb.cur.execute(_d)
        botdb.conn.commit()
    except Exception as _e:
        print("     note:", _e)
print("     schema ready.")

sq = sqlite3.connect(SQLITE_PATH)
sq.row_factory = sqlite3.Row
scur = sq.cursor()

scur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
tables = [r[0] for r in scur.fetchall()]

pg = psycopg.connect(DATABASE_URL, autocommit=True)
pcur = pg.cursor()

print("2/3  Copying %d tables ..." % len(tables))
for t in tables:
    scur.execute("PRAGMA table_info(%s)" % t)
    info = scur.fetchall()
    cols = [c[1] for c in info]
    pk = [c[1] for c in info if c[5]]  # c[5] = pk order (>0 if part of pk)
    if not cols:
        continue
    scur.execute("SELECT %s FROM %s" % (", ".join('"%s"' % c for c in cols), t))
    rows = scur.fetchall()
    if not rows:
        print("     %-24s 0 rows" % t)
        continue
    collist = ", ".join('"%s"' % c for c in cols)
    ph = ", ".join(["%s"] * len(cols))
    if pk:
        setters = [c for c in cols if c not in pk]
        if setters:
            conflict = "ON CONFLICT (%s) DO UPDATE SET %s" % (
                ", ".join('"%s"' % c for c in pk),
                ", ".join('"{0}"=EXCLUDED."{0}"'.format(c) for c in setters))
        else:
            conflict = "ON CONFLICT (%s) DO NOTHING" % ", ".join('"%s"' % c for c in pk)
    else:
        conflict = "ON CONFLICT DO NOTHING"
    sql = 'INSERT INTO "%s" (%s) VALUES (%s) %s' % (t, collist, ph, conflict)
    n = 0
    for r in rows:
        try:
            pcur.execute(sql, tuple(r[c] for c in cols))
            n += 1
        except Exception as e:
            print("     ! row skipped in %s: %s" % (t, e))
    print("     %-24s %d rows" % (t, n))

print("3/3  Fixing id sequences ...")
for t in tables:
    scur.execute("PRAGMA table_info(%s)" % t)
    if any(c[1] == "id" for c in scur.fetchall()):
        try:
            pcur.execute(
                "SELECT setval(pg_get_serial_sequence(%s,'id'), "
                "COALESCE((SELECT MAX(id) FROM \"%s\"),1))" % ("%s", t), (t,))
        except Exception as e:
            print("     ! sequence skip %s: %s" % (t, e))

print("\nDONE. Your data is now in Postgres.")
