from __future__ import annotations

import stat
from pathlib import Path

import pytest
from cryptography.fernet import InvalidToken

from treinvertraging import crypto


def test_roundtrip_and_key_file(data_dir: Path) -> None:
    token = crypto.encrypt("geheim wachtwoord \u20ac")
    assert "geheim" not in token
    assert crypto.decrypt(token) == "geheim wachtwoord \u20ac"
    key_file = data_dir / "secret.key"
    assert key_file.exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_key_is_persistent(data_dir: Path) -> None:
    token = crypto.encrypt("x")
    key = (data_dir / "secret.key").read_bytes()
    assert crypto.decrypt(token) == "x"
    assert (data_dir / "secret.key").read_bytes() == key


def test_wrong_key_fails(data_dir: Path, tmp_path: Path) -> None:
    token = crypto.encrypt("x")
    (data_dir / "secret.key").unlink()
    with pytest.raises(InvalidToken):
        crypto.decrypt(token)
