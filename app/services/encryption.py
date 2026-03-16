"""
Fernet symmetric encryption for Binance API credentials.

The FERNET_KEY is a 32-byte URL-safe base64-encoded key.
Generate once with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Store ONLY in .env — never in code, DB, or version control.
"""
import os
from flask import current_app
from cryptography.fernet import Fernet, InvalidToken


def _get_fernet() -> Fernet:
    key = current_app.config.get("FERNET_KEY") or os.environ.get("FERNET_KEY")
    if not key:
        raise RuntimeError("FERNET_KEY is not configured. Set it in .env before starting the app.")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return URL-safe base64 ciphertext."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt ciphertext. Raises InvalidToken on tampering."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Decryption failed — data may be corrupted or key changed.") from exc
