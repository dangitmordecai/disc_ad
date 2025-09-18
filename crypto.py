# crypto.py
import base64, os, hashlib
from cryptography.fernet import Fernet
from hashlib import pbkdf2_hmac

MASTER_KEY = os.environ.get("SECRET_MASTER_KEY", "")
if not MASTER_KEY:
    raise RuntimeError("SECRET_MASTER_KEY not set in environment.")

def _derive_key(user_id: str) -> bytes:
    """Derive a per-user Fernet key from the master key and user_id."""
    salt = user_id.encode()
    key = pbkdf2_hmac("sha256", MASTER_KEY.encode(), salt, 100_000, dklen=32)
    return base64.urlsafe_b64encode(key)

def encrypt_for_user(user_id: str, plaintext: str) -> str:
    f = Fernet(_derive_key(user_id))
    return f.encrypt(plaintext.encode()).decode()

def decrypt_for_user(user_id: str, ciphertext: str) -> str:
    f = Fernet(_derive_key(user_id))
    return f.decrypt(ciphertext.encode()).decode()
