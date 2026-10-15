"""Database backup helpers.

Works with BOTH storage backends:

- SQLite (no DATABASE_URL)  -> consistent online backup including committed WAL.
- PostgreSQL (DATABASE_URL) -> portable JSON snapshot of every table.

SQLite snapshots use the online backup API rather than copying only the main
file. PostgreSQL exports include all public base tables in one consistent view.
This helper does not establish native PostgreSQL restore/PITR coverage; that
requires a separately verified provider or pg_dump/pg_restore procedure.
"""

import gzip
import json
import os
import sqlite3
from datetime import datetime
from contextlib import closing
from pathlib import Path

from config import DB_PATH, BACKUP_DIR, MAX_BACKUPS

try:
    from database.pgcompat import use_postgres
except Exception:
    def use_postgres():
        return False


def _backup_dir() -> Path:
    d = Path(BACKUP_DIR)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    """Complete public-table snapshot from ONE read-only repeatable-read view.

    This is a data export, not a native PostgreSQL schema/PITR backup. Any table
    failure aborts the artifact; callers must not accept an incomplete export.
    """
    from database.pgcompat import connect, atomic
    conn = connect(DB_PATH)
    created = False
    try:
        cur = conn.cursor()
        # Existing atomic helper also disables pgcompat's single-statement
        # reconnect/retry while the complete consistent snapshot is in flight.
        with atomic(conn):
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = {"_meta": {"created": datetime.now().isoformat(),
                                  "backend": "postgres", "version": 1}, "tables": {}}
            for table in _list_tables(cur):
                identifier = '"' + table.replace('"', '""') + '"'
                cur.execute('SELECT * FROM ' + identifier)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
                snapshot["tables"][table] = {"columns": cols, "rows": [list(r) for r in rows]}
        fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, 'wb') as raw, gzip.open(raw, "wt", encoding="utf-8") as fh:
            json.dump(snapshot, fh, default=str)
    except Exception:
        if created:
            try:
                backup_path.unlink()
            except OSError:
                pass
        raise RuntimeError("Database snapshot failed; no complete backup was produced.") from None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return str(backup_path)


def _copy_sqlite_snapshot(source: Path, target: Path) -> None:
    """Online backup API includes committed WAL frames, unlike copying .db."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True,
                                     timeout=30)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError("Database snapshot failed; no complete backup was produced.") from None


def create_backup() -> str:
    """Create a backup and return its path. Raises on genuine failure."""
    backup_dir = _backup_dir()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")

    if use_postgres():
        backup_path = backup_dir / "shopbot_backup_{}.json.gz".format(timestamp)
        result = _dump_postgres(backup_path)
    else:
        db_path = Path(DB_PATH)
        if not db_path.exists():
            raise FileNotFoundError("Database not found: {}".format(DB_PATH))
        backup_path = backup_dir / "shopbot_backup_{}.db".format(timestamp)
        _copy_sqlite_snapshot(db_path, backup_path)
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
