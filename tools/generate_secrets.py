import secrets
from cryptography.fernet import Fernet

print("Add these to .env:\n")
print("WEB_ADMIN_SECRET=" + secrets.token_urlsafe(64))
print("DATA_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("BACKUP_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
