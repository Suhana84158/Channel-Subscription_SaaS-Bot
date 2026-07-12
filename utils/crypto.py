import base64
import hashlib
from cryptography.fernet import Fernet
from config import SECRET_KEY


def _fernet() -> Fernet:
    if not SECRET_KEY or SECRET_KEY == "CHANGE_ME":
        raise RuntimeError("SECRET_KEY must be set in Render environment variables")
    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
