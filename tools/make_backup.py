import os
import time
import zipfile
from pathlib import Path
from cryptography.fernet import Fernet

try:
    from config import DB_PATH
except Exception:
    DB_PATH = os.getenv("DB_PATH", "shopbot.db")

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "secure_backups"))
BACKUP_DIR.mkdir(exist_ok=True)

key = os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("DATA_ENCRYPTION_KEY")
if not key:
    raise SystemExit("Missing BACKUP_ENCRYPTION_KEY or DATA_ENCRYPTION_KEY in .env")

timestamp = time.strftime("%Y%m%d_%H%M%S")
tmp_zip = BACKUP_DIR / f"backup_{timestamp}.zip"
enc_file = BACKUP_DIR / f"backup_{timestamp}.zip.enc"

with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
    db_path = Path(DB_PATH)
    if db_path.exists():
        z.write(db_path, db_path.name)
    for folder in ["logs"]:
        p = Path(folder)
        if p.exists():
            for file in p.rglob("*"):
                if file.is_file():
                    z.write(file, file.as_posix())

data = tmp_zip.read_bytes()
enc = Fernet(key.encode() if isinstance(key, str) else key).encrypt(data)
enc_file.write_bytes(enc)
tmp_zip.unlink(missing_ok=True)

print(f"Encrypted backup created: {enc_file}")
