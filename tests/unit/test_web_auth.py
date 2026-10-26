from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_web_support import PASSWORD, USER
from treinvertraging import config
from treinvertraging.web.app import create_app

pytest_plugins = ["tests.unit.test_web_support"]

PROTECTED = ["/", "/trips", "/claims", "/accounts", "/accounts/new", "/run", "/otp", "/debug/", "/static/style.css"]


def test_healthz_is_public(anon_client: TestClient) -> None:
    r = anon_client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.parametrize("path", PROTECTED)
def test_auth_required(anon_client: TestClient, path: str) -> None:
    r = anon_client.get(path)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_rejected(anon_client: TestClient) -> None:
    assert anon_client.get("/", auth=(USER, "nope")).status_code == 401
    assert anon_client.get("/", auth=("other", PASSWORD)).status_code == 401


def test_post_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.post("/claims/1/approve").status_code == 401
    assert anon_client.post("/run/sync").status_code == 401


@pytest.mark.parametrize("path", PROTECTED)
def test_pages_render_with_auth(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_cross_origin_post_blocked(client: TestClient) -> None:
    r = client.post("/run/sync", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = client.post("/run/sync", headers={"Origin": "http://testserver"})
    assert ok.status_code == 303


def test_refuses_to_start_without_password(
    web_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_PASSWORD", "")
    config.reset_settings_cache()
    with pytest.raises(RuntimeError, match="WEB_PASSWORD"):
        create_app()


def test_allow_no_auth(web_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("WEB_PASSWORD", "")
    monkeypatch.setenv("ALLOW_NO_AUTH", "1")
    config.reset_settings_cache()
    with caplog.at_level(logging.WARNING):
        app = create_app()
    assert any("ZONDER authenticatie" in r.message for r in caplog.records)
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/healthz").status_code == 200
