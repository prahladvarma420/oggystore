import os
import sys
from pathlib import Path
from cryptography.fernet import Fernet

# The .env lives in the PROJECT ROOT (the folder that contains bot.py), which is
# the parent of this tools/ folder. Load it explicitly so this script works no
# matter which directory you run it from (e.g. inside tools/ or the root).
ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

if len(sys.argv) < 2:
    raise SystemExit("Usage: python tools/decrypt_backup.py backups/shopbot_backup_xxxxxxxx.zip.enc")

key = os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("DATA_ENCRYPTION_KEY")
if not key:
    raise SystemExit(
        "Missing BACKUP_ENCRYPTION_KEY or DATA_ENCRYPTION_KEY in .env\n"
        "Checked: " + str(ROOT / ".env") + "\n"
        "Make sure that .env file exists and contains one of those keys."
    )

# Resolve the backup path: accept it as given, or relative to the project root
# (so 'backups/..' works even when run from inside tools/).
enc_file = Path(sys.argv[1])
if not enc_file.exists():
    alt = ROOT / sys.argv[1]
    if alt.exists():
        enc_file = alt
if not enc_file.exists():
    raise SystemExit("Backup file not found: " + str(sys.argv[1]))

out_file = enc_file.with_suffix("")

try:
    data = Fernet(key.encode() if isinstance(key, str) else key).decrypt(enc_file.read_bytes())
except Exception as e:
    raise SystemExit(
        "Could not decrypt this backup with the current key.\n"
        "It was likely encrypted with a different BACKUP_ENCRYPTION_KEY/"
        "DATA_ENCRYPTION_KEY than the one now in .env.\n"
        "Details: " + str(e)
    )

out_file.write_bytes(data)
print("Decrypted backup: " + str(out_file))
