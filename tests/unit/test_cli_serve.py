from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from tests.unit.pipeline_support import (
    add_account,
    env_migrated,  # noqa: F401 - fixture
    set_mode,
)
from treinvertraging import services
from treinvertraging.__main__ import app, serve_async
from treinvertraging.delaydata import rdt

runner = CliRunner()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_subprocess_answers_healthz_and_stops_on_sigterm(tmp_path: Path):
    data, cfg = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    cfg.mkdir()
    port = free_port()
    child_env = {
        **os.environ,
        "DATA_DIR": str(data),
        "CONFIG_DIR": str(cfg),
        "ALLOW_NO_AUTH": "1",
        "WEB_PASSWORD": "",
        "APPRISE_URLS": "",
        "NS_API_KEY": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "treinvertraging", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 40
        body = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
                if r.status_code == 200:
                    body = r.text
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        assert body == "ok", proc.stdout.read() if proc.poll() is not None else "no response"
        assert (data / "app.db").exists() and (data / "jobs.db").exists()  # migrated + scheduler started
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


async def test_serve_runs_web_and_scheduler_in_one_event_loop(env_migrated, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALLOW_NO_AUTH", "1")
    monkeypatch.setenv("WEB_PASSWORD", "")
    from treinvertraging import config

    config.reset_settings_cache()
    port = free_port()
    stop = asyncio.Event()
    server_task = asyncio.create_task(
        serve_async("127.0.0.1", port, stop=stop, persistent_jobs=False, install_signals=False)
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            for _ in range(100):
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/healthz")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
            assert r.text == "ok"
            # the scheduler lives in this very loop: the facade sees its jobs
            runs = await services.next_runs()
            assert {"daily_run", "canary", "watchdog"} <= {x["id"] for x in runs}
            # and the dashboard (which calls services.next_runs from a request handler) lists them
            page = await client.get(f"http://127.0.0.1:{port}/")
            assert page.status_code == 200 and "Dagelijkse run" in page.text
    finally:
        stop.set()
        await asyncio.wait_for(server_task, 20)
    assert await services.next_runs() == []


@pytest.fixture
def fake_cli(env_migrated, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto", TV_FAKE_ADAPTERS="1")

    async def rdt_stub(year, month, stations, *a, **k):
        return None

    monkeypatch.setattr(rdt, "import_month", rdt_stub)
    add_account()
    return env_migrated


def test_cli_sync_assess_claim_dry_run_then_claim(fake_cli):
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 0, r.output
    assert "2 new" in r.output
    r = runner.invoke(app, ["assess"])
    assert r.exit_code == 0 and "1 eligible" in r.output
    log = fake_cli.data_dir / "fake-submissions.jsonl"
    r = runner.invoke(app, ["claim", "--dry-run"])
    assert r.exit_code == 0 and "--dry-run" in r.output and "1 rehearsed" in r.output
    assert '"dry_run": true' in log.read_text()
    r = runner.invoke(app, ["claim"])
    assert r.exit_code == 0 and "1 submitted" in r.output
    r = runner.invoke(app, ["claim"])
    assert "0 submitted" in r.output
    assert len(log.read_text().splitlines()) == 2


def test_cli_canary_exit_codes(fake_cli, monkeypatch):
    r = runner.invoke(app, ["canary"])
    assert r.exit_code == 0 and "canary ok" in r.output
    monkeypatch.setenv("TV_FAKE_ADAPTERS", "")
    from treinvertraging.pipeline import canary as canary_mod

    async def failing(**kw):
        return canary_mod.CanaryResult(ok=False, errors={"x": "LoginRequired"})

    monkeypatch.setattr(canary_mod, "run_canary", failing)
    r = runner.invoke(app, ["canary"])
    assert r.exit_code == 1 and "FAILED" in r.output


def test_cli_reports_errors_with_exit_code_1(env_migrated, monkeypatch):  # noqa: F811
    async def boom() -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(services, "run_sync", boom)
    r = runner.invoke(app, ["sync"])
    assert r.exit_code == 1 and "kaboom" in r.output
