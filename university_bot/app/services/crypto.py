"""Fernet encryption helpers for E-Class credentials at rest."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config.settings import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key.encode()
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        # Derive a valid Fernet key from any-length secret.
        digest = hashlib.sha256(key).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Cannot decrypt stored credentials (wrong ENCRYPTION_KEY?)") from exc
