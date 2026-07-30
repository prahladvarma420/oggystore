"""Database backup helpers.

Works with BOTH storage backends:

- SQLite (no DATABASE_URL)  -> file copy of the .db, exactly like before.
- PostgreSQL (DATABASE_URL) -> portable JSON snapshot of every table.

The previous version only ever did shutil.copy2(DB_PATH). On Railway the bot
runs on Postgres, so shopbot.db does not exist and create_backup() raised
FileNotFoundError on every single run -- silently, because every caller wrapped
it in a bare except. Backups had never worked in production.
"""

import gzip
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from config import DB_PATH, BACKUP_DIR, MAX_BACKUPS

try:
    from database.pgcompat import use_postgres
except Exception:
    def use_postgres():
        return False


def _backup_dir() -> Path:
    d = Path(BACKUP_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_tables(cur):
    """Return user table names for the active backend."""
    if use_postgres():
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )
    else:
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    return [r[0] for r in cur.fetchall()]


def _dump_postgres(backup_path: Path) -> str:
    """Write a gzipped JSON snapshot of every table.

    Uses its own short-lived connection so it never shares the global cursor
    with live bot traffic (the global cursor is not concurrency safe).
    """
    from database.pgcompat import connect

    conn = connect(DB_PATH)
    try:
        cur = conn.cursor()
        snapshot = {
            "_meta": {
                "created": datetime.now().isoformat(),
                "backend": "postgres",
                "version": 1,
            },
            "tables": {},
        }
        for table in _list_tables(cur):
            try:
                cur.execute('SELECT * FROM "{}"'.format(table))
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                snapshot["tables"][table] = {
                    "columns": cols,
                    "rows": [list(r) for r in rows],
                }
            except Exception as exc:
                snapshot["tables"][table] = {"error": str(exc)}

        with gzip.open(backup_path, "wt", encoding="utf-8") as fh:
            json.dump(snapshot, fh, default=str)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return str(backup_path)


def create_backup() -> str:
    """Create a backup and return its path. Raises on genuine failure."""
    backup_dir = _backup_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if use_postgres():
        backup_path = backup_dir / "shopbot_backup_{}.json.gz".format(timestamp)
        result = _dump_postgres(backup_path)
    else:
        db_path = Path(DB_PATH)
        if not db_path.exists():
            raise FileNotFoundError("Database not found: {}".format(DB_PATH))
        backup_path = backup_dir / "shopbot_backup_{}.db".format(timestamp)
        shutil.copy2(db_path, backup_path)
        result = str(backup_path)

    cleanup_old_backups()
    return result


def cleanup_old_backups():
    backup_dir = Path(BACKUP_DIR)
    if not backup_dir.exists():
        return

    backups = []
    for pattern in ("shopbot_backup_*.db", "shopbot_backup_*.json.gz"):
        backups.extend(backup_dir.glob(pattern))

    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for old_backup in backups[MAX_BACKUPS:]:
        try:
            old_backup.unlink()
        except Exception:
            pass


async def scheduled_backup(context):
    try:
        backup_path = create_backup()
        print("[backup] Auto backup created: {}".format(backup_path), flush=True)
    except Exception as exc:
        # Surface the failure instead of swallowing it -- this is exactly how
        # the broken-on-Postgres bug stayed invisible for so long.
        print("[backup] Auto backup FAILED: {}".format(exc), flush=True)
        try:
            from config import ADMIN_ID
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="\u26a0\ufe0f <b>Automatic backup failed</b>\n\n<code>{}</code>".format(exc),
                parse_mode="HTML",
            )
        except Exception:
            pass
