from __future__ import annotations

import logging
import sys
import types

import pytest
from fastapi.testclient import TestClient

from treinvertraging import services
from treinvertraging.web import logbuffer

pytest_plugins = ["tests.unit.test_web_support"]


# --- debug bundles ------------------------------------------------------------------------------


@pytest.fixture
def bundle(web_env):
    d = web_env / "debug" / "20261001-submit"
    d.mkdir(parents=True)
    (d / "shot.png").write_bytes(b"\x89PNG\r\n")
    (d / "page.html").write_text("<script>alert(1)</script>")
    (web_env / "secret.key").write_text("TOPSECRET")
    (web_env / "debug-evil").mkdir()
    (web_env / "debug-evil" / "x.txt").write_text("sibling dir with the same prefix")
    return d


def test_debug_serves_files(client: TestClient, bundle) -> None:
    r = client.get("/debug/20261001-submit/shot.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    listing = client.get("/debug/").text
    assert "20261001-submit/" in listing
    assert "shot.png" in client.get("/debug/20261001-submit").text


def test_debug_html_is_never_served_as_html(client: TestClient, bundle) -> None:
    r = client.get("/debug/20261001-submit/page.html")
    assert r.headers["content-type"].startswith("text/plain")
    assert "sandbox" in r.headers["content-security-policy"]


@pytest.mark.parametrize(
    "path",
    [
        "/debug/../secret.key",
        "/debug/%2e%2e/secret.key",
        "/debug/..%2fsecret.key",
        "/debug/20261001-submit/../../secret.key",
        "/debug/20261001-submit/%2e%2e/%2e%2e/secret.key",
        "/debug/../debug-evil/x.txt",
        "/debug//etc/passwd",
        "/debug/nope",
    ],
)
def test_debug_path_traversal_blocked(client: TestClient, bundle, path: str) -> None:
    r = client.get(path)
    assert r.status_code in (404, 400)
    assert "TOPSECRET" not in r.text and "sibling dir" not in r.text and "root:" not in r.text


def test_debug_symlink_escape_blocked(client: TestClient, bundle, web_env) -> None:
    (bundle / "link").symlink_to(web_env / "secret.key")
    assert client.get("/debug/20261001-submit/link").status_code == 404


def test_static_traversal_blocked(client: TestClient) -> None:
    assert client.get("/static/..%2froutes.py").status_code == 404
    assert client.get("/static/style.css").status_code == 200


# --- run page --------------------------------------------------------------------------------


def test_run_stub_flash(client: TestClient, monkeypatch) -> None:
    async def not_implemented() -> str:
        raise NotImplementedError("stub")

    monkeypatch.setattr(services, "run_sync", not_implemented)  # the UI must still cope with a stub
    r = client.post("/run/sync")
    assert r.status_code == 303 and r.headers["location"].startswith("/run?")
    assert "nog niet beschikbaar" in client.get(r.headers["location"]).text
    assert client.post("/run/bogus").status_code == 404


def test_run_calls_service_and_shows_result(client: TestClient, monkeypatch) -> None:
    calls: list[str] = []

    async def fake_sync() -> str:
        calls.append("sync")
        return "3 nieuwe reizen"

    monkeypatch.setattr(services, "run_sync", fake_sync)
    r = client.post("/run/sync", headers={"HX-Request": "true"})
    assert "3 nieuwe reizen" in r.text and calls == ["sync"]


def test_run_failure_is_reported(client: TestClient, monkeypatch) -> None:
    async def boom() -> str:
        raise RuntimeError("kapot")

    monkeypatch.setattr(services, "run_canary", boom)
    r = client.post("/run/canary", headers={"HX-Request": "true"})
    assert 'class="flash error"' in r.text and "kapot" in r.text


def test_run_page_and_log_tail(client: TestClient) -> None:
    logging.getLogger("treinvertraging.test").info("hallo logboek")
    assert "Logboek" in client.get("/run").text
    assert "hallo logboek" in client.get("/run/logs").text


# --- takeover / OTP (WP-D modules are optional) ---------------------------------------------


def test_banners_empty_without_wp_d_modules(client: TestClient, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "treinvertraging.browser.takeover", None)
    monkeypatch.setitem(sys.modules, "treinvertraging.otp.manual", None)
    r = client.get("/partials/banners")
    assert r.status_code == 200 and r.text.strip() == ""
    assert client.get("/otp").status_code == 200


def _fake_modules(monkeypatch):
    done: list = []
    codes: list = []
    takeover = types.ModuleType("treinvertraging.browser.takeover")
    takeover.active_takeovers = lambda: [
        {"id": "t1", "account": "Lucas NS", "reason": "Akamai-uitdaging", "since": None}
    ]

    async def complete(tid):
        done.append(tid)

    takeover.complete_takeover = complete
    manual = types.ModuleType("treinvertraging.otp.manual")
    manual.pending_prompts = lambda: [{"id": 7, "account": "Lucas NS", "message": "NS-code"}]

    def submit(pid, code):
        codes.append((pid, code))

    manual.submit_code = submit
    import treinvertraging.browser as browser_pkg
    import treinvertraging.otp as otp_pkg

    monkeypatch.setitem(sys.modules, "treinvertraging.browser.takeover", takeover)
    monkeypatch.setitem(sys.modules, "treinvertraging.otp.manual", manual)
    monkeypatch.setattr(browser_pkg, "takeover", takeover, raising=False)
    monkeypatch.setattr(otp_pkg, "manual", manual, raising=False)
    return done, codes


def test_takeover_banner_and_done(client: TestClient, monkeypatch) -> None:
    done, _ = _fake_modules(monkeypatch)
    banner = client.get("/partials/banners").text
    assert "Akamai-uitdaging" in banner and "Lucas NS" in banner
    assert ":6080/vnc.html" in banner and "/takeover/t1/done" in banner
    assert "/otp" in banner
    r = client.post("/takeover/t1/done")
    assert r.status_code == 303 and done == ["t1"]
    r = client.post("/takeover/unknown/done")
    assert done == ["t1"] and "niet+%28meer%29+actief" in r.headers["location"]


def test_otp_submit(client: TestClient, monkeypatch) -> None:
    _, codes = _fake_modules(monkeypatch)
    page = client.get("/otp").text
    assert "/otp/7" in page and "NS-code" in page
    client.post("/otp/7", data={"code": " 123456 "})
    assert codes == [(7, "123456")]
    client.post("/otp/7", data={"code": ""})
    client.post("/otp/99", data={"code": "1"})
    assert codes == [(7, "123456")]


# --- log buffer ------------------------------------------------------------------------------


def test_ringbuffer_capacity_and_after() -> None:
    h = logbuffer.RingBufferHandler(capacity=3)
    lg = logging.getLogger("tv.ring")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(h)
    try:
        for i in range(5):
            lg.info("m%d", i)
    finally:
        lg.removeHandler(h)
    lines = h.lines_after(0)
    assert [ln.message for ln in lines] == ["m2", "m3", "m4"]
    assert [ln.message for ln in h.lines_after(lines[1].seq)] == ["m4"]


async def test_stream_yields_backlog_then_new() -> None:
    import asyncio

    h = logbuffer.RingBufferHandler()
    lg = logging.getLogger("tv.stream")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(h)
    try:
        lg.info("one")
        gen = logbuffer.stream(h, poll=0.01)
        assert "one" in await anext(gen)
        lg.info("two")
        assert "two" in await asyncio.wait_for(anext(gen), 2)
        await gen.aclose()
    finally:
        lg.removeHandler(h)
