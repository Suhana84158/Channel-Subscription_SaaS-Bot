import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import SECRET_KEY


def _fernet() -> Fernet:
    if not SECRET_KEY or SECRET_KEY == "CHANGE_ME":
        raise RuntimeError(
            "SECRET_KEY is not configured. Set a strong SECRET_KEY in Render environment variables."
        )

    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value:
        raise ValueError("Secret value cannot be empty.")
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    if not value:
        raise ValueError("Encrypted value cannot be empty.")
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt secret. SECRET_KEY may have changed.") from exc
