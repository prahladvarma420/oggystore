import os
import shutil
from datetime import datetime
from pathlib import Path

from config import DB_PATH, BACKUP_DIR, MAX_BACKUPS


def create_backup() -> str:
    db_path = Path(DB_PATH)

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    backup_dir = Path(BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = backup_dir / f"shopbot_backup_{timestamp}.db"

    shutil.copy2(db_path, backup_path)
    cleanup_old_backups()

    return str(backup_path)


def cleanup_old_backups():
    backup_dir = Path(BACKUP_DIR)

    if not backup_dir.exists():
        return

    backups = sorted(
        backup_dir.glob("shopbot_backup_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    for old_backup in backups[MAX_BACKUPS:]:
        try:
            old_backup.unlink()
        except Exception:
            pass


async def scheduled_backup(context):
    try:
        backup_path = create_backup()
        print(f"Auto backup created: {backup_path}")
    except Exception as exc:
        print(f"Auto backup failed: {exc}")
