from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from treinvertraging import config, db


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated DATA_DIR/CONFIG_DIR with fresh settings and engine caches."""
    d = tmp_path / "data"
    c = tmp_path / "config"
    d.mkdir()
    c.mkdir()
    monkeypatch.setenv("DATA_DIR", str(d))
    monkeypatch.setenv("CONFIG_DIR", str(c))
    config.reset_settings_cache()
    db.dispose_engines()
    yield d
    db.dispose_engines()
    config.reset_settings_cache()
