"""End-to-end test of the real Docker image with fake adapters (no network, no real accounts).

Skipped unless ``TV_E2E_DOCKER=1``. It builds the image, starts a container with
``TV_FAKE_ADAPTERS=1 CLAIM_MODE=dry_run``, runs ``sync``, ``assess`` and ``claim --dry-run`` inside it and
checks the database, the fake submission log, the web UI and noVNC.

Environment
    TV_E2E_DOCKER=1        enable
    TV_EXTRA_CA=<file>     CA bundle for builds behind a TLS-intercepting proxy
                           (``docker build --secret id=extra_ca,src=$TV_EXTRA_CA``)
    TV_E2E_IMAGE=<image>   use an existing image instead of building one
    TV_E2E_KEEP=1          leave the container running afterwards (debugging)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("TV_E2E_DOCKER") != "1", reason="set TV_E2E_DOCKER=1 to run")

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("TV_E2E_IMAGE") or "treinvertraging:e2e"
CARD = "3528000000000001"


def docker(*args: str, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} failed ({res.returncode}):\n{res.stdout}\n{res.stderr}"
        )
    return res


def get(url: str, *, auth: tuple[str, str] | None = None) -> int:
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(":".join(auth).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture(scope="module")
def image() -> str:
    if os.environ.get("TV_E2E_IMAGE"):
        return IMAGE
    cmd = ["build", "-t", IMAGE, str(ROOT)]
    extra_ca = os.environ.get("TV_EXTRA_CA")
    if extra_ca:
        cmd[1:1] = ["--secret", f"id=extra_ca,src={extra_ca}"]
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    res = subprocess.run(["docker", *cmd], capture_output=True, text=True, timeout=1800, env=env)
    assert res.returncode == 0, f"docker build failed:\n{res.stdout[-3000:]}\n{res.stderr[-3000:]}"
    return IMAGE


@pytest.fixture(scope="module")
def container(image: str) -> Iterator[str]:
    name = f"tv-e2e-{uuid.uuid4().hex[:8]}"
    docker(
        "run", "-d", "--init", "--name", name, "--shm-size", "1g",
        "-e", "TV_FAKE_ADAPTERS=1", "-e", "CLAIM_MODE=dry_run", "-e", "WEB_PASSWORD=test",
        "-e", "VNC_PASSWORD=vncpass",
        "-p", "127.0.0.1::8080", "-p", "127.0.0.1::6080",
        image,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 240
        status = ""
        while time.monotonic() < deadline:
            status = docker("inspect", "--format", "{{.State.Health.Status}}", name).stdout.strip()
            if status == "healthy":
                break
            running = docker("inspect", "--format", "{{.State.Running}}", name).stdout.strip()
            assert running == "true", f"container died:\n{docker('logs', name, check=False).stdout[-3000:]}"
            time.sleep(3)
        assert status == "healthy", f"never healthy ({status}):\n{docker('logs', name).stdout[-3000:]}"
        yield name
    finally:
        if os.environ.get("TV_E2E_KEEP") != "1":
            docker("rm", "-f", name, check=False)


def port(name: str, container_port: int) -> int:
    out = docker("port", name, f"{container_port}/tcp").stdout.split()
    return int(out[0].rsplit(":", 1)[1])


def exec_app(name: str, *args: str, timeout: float = 300) -> str:
    # as the `app` user: the files under /data must not become root-owned
    res = docker("exec", "-u", "app", name, *args, timeout=timeout)
    return res.stdout


def sql(name: str, query: str) -> list[list]:
    code = (
        "import sqlite3,json,sys;"
        "c=sqlite3.connect('/data/app.db');"
        "print(json.dumps([list(r) for r in c.execute(sys.argv[1])]))"
    )
    return json.loads(exec_app(name, "python", "-c", code, query))


def test_pipeline_ui_and_vnc(container: str) -> None:
    # entrypoint seeding: rules.yaml and the selector reference copies
    config = exec_app(container, "ls", "/config", "/config/selectors")
    assert "rules.yaml" in config
    for site in ("ns", "ovchip", "gtbv"):
        assert f"{site}.yaml.example" in config
    assert "ns.yaml\n" not in config  # reference copies only: the user's own files are never created

    exec_app(
        container, "treinvertraging", "account", "add", "--source", "ns", "--label", "E2E",
        "--password", "pw", "--card-number", CARD, "--iban", "NL91ABNA0417164300",
        "--holder-name", "T. Est", "--email", "t@example.com",
    )  # fmt: skip
    assert "E2E" in exec_app(container, "treinvertraging", "account", "list")

    assert "sync" in exec_app(container, "treinvertraging", "sync")
    assert sql(container, "select count(*) from trips") == [[2]]
    assert sql(container, "select state, count(*) from claims group by state") == [["detected", 2]]

    exec_app(container, "treinvertraging", "assess")
    states = sql(
        container,
        "select t.from_station, c.state from claims c join trips t on t.key=c.trip_key "
        "order by t.from_station",
    )
    assert states == [["ASD", "ineligible"], ["UT", "eligible"]]  # delayed trip (NS flag) vs punctual trip

    out = exec_app(container, "treinvertraging", "claim", "--dry-run")
    assert "1 rehearsed" in out and "0 submitted" in out
    # a dry run never changes the claim state ...
    assert sql(
        container, "select state from claims c join trips t on t.key=c.trip_key where t.from_station='UT'"
    ) == [["eligible"]]
    # ... but the fake submitter recorded it
    lines = exec_app(container, "cat", "/data/fake-submissions.jsonl").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert len(entries) == 1
    assert entries[0]["dry_run"] is True and entries[0]["card_last4"] == CARD[-4:]
    assert entries[0]["from"] == "UT" and entries[0]["to"] == "ASD"
    assert sql(container, "select count(*) from events where kind='claim.dry_run'") == [[1]]

    # web UI
    web = port(container, 8080)
    assert get(f"http://127.0.0.1:{web}/healthz") == 200
    assert get(f"http://127.0.0.1:{web}/") == 401
    assert get(f"http://127.0.0.1:{web}/", auth=("admin", "test")) == 200
    assert get(f"http://127.0.0.1:{web}/claims", auth=("admin", "test")) == 200
    assert get(f"http://127.0.0.1:{web}/", auth=("admin", "wrong")) == 401

    # noVNC
    assert get(f"http://127.0.0.1:{port(container, 6080)}/vnc.html") == 200

    # files written by `docker exec -u app` belong to the app user
    assert exec_app(container, "stat", "-c", "%U", "/data/app.db").strip() == "app"
