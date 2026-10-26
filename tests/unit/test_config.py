from __future__ import annotations

import os
from pathlib import Path

import pytest

from treinvertraging.config import Settings


def test_defaults(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("CLAIM_MODE", "HEADLESS", "RUN_WINDOW"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.CLAIM_MODE == "approve"
    assert s.HEADLESS is False
    assert s.RUN_WINDOW == "07:00-22:00"
    assert s.SUBMIT_CHANNELS == ["gtbv", "mijnns"]
    assert s.db_path == data_dir / "app.db"


def test_yaml_and_env_precedence(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Path(os.environ["CONFIG_DIR"])
    (cfg / "settings.yaml").write_text(
        "CLAIM_MODE: dry_run\nPUBLIC_URL: http://nas:8080\nSUBMIT_CHANNELS: [mijnns]\n"
    )
    monkeypatch.setenv("PUBLIC_URL", "http://env:1")
    s = Settings(_env_file=None)
    assert s.CLAIM_MODE == "dry_run"
    assert s.PUBLIC_URL == "http://env:1"
    assert s.SUBMIT_CHANNELS == ["mijnns"]


def test_channels_from_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBMIT_CHANNELS", "mijnns,gtbv")
    monkeypatch.setenv("APPRISE_URLS", "a://x, b://y")
    s = Settings(_env_file=None)
    assert s.SUBMIT_CHANNELS == ["mijnns", "gtbv"]
    assert s.apprise_urls == ["a://x", "b://y"]
