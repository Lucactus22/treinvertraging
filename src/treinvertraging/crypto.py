"""Fernet secret box. The key lives at ``DATA_DIR/secret.key`` (mode 0600), auto-generated."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

from treinvertraging.config import get_settings

KEY_FILENAME = "secret.key"


def key_path() -> Path:
    return get_settings().DATA_DIR / KEY_FILENAME


def load_or_create_key(path: Path | None = None) -> bytes:
    """Return the Fernet key, creating it (mode 0600) on first use."""
    path = path or key_path()
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    return key


def _fernet() -> Fernet:
    return Fernet(load_or_create_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a string; returns a URL-safe token string suitable for a DB column."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Inverse of :func:`encrypt`. Raises ``cryptography.fernet.InvalidToken`` on a wrong key."""
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
