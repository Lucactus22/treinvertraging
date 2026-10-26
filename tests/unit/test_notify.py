from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.unit.test_web_support import seed_claim
from treinvertraging import config, db, notify
from treinvertraging.models import Claim, Event

pytest_plugins = ["tests.unit.test_web_support"]


async def test_notify_noop_without_urls(web_env, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="treinvertraging.notify"):
        assert await notify.notify("Titel", "Body") is False
    assert any("no APPRISE_URLS" in r.message for r in caplog.records)


async def test_notify_sends_through_apprise(web_env, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("APPRISE_URLS", "json://localhost/a, ntfy://topic")
    config.reset_settings_cache()
    sent: list[dict] = []
    added: list[str] = []

    class FakeApprise:
        def add(self, url: str) -> bool:
            added.append(url)
            return True

        async def async_notify(self, **kw) -> bool:
            sent.append(kw)
            return True

    import apprise

    monkeypatch.setattr(apprise, "Apprise", FakeApprise)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    assert await notify.notify("Mislukt", "kapot", level="error", attach=[shot, tmp_path / "missing.png"])
    assert added == ["json://localhost/a", "ntfy://topic"]
    assert sent[0]["notify_type"] == "failure"
    assert sent[0]["attach"] == [str(shot)]
    assert sent[0]["title"] == "Mislukt"


async def test_notify_never_raises(web_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPRISE_URLS", "json://localhost/a")
    config.reset_settings_cache()

    class Boom:
        def add(self, url: str) -> bool:
            raise RuntimeError("nope")

    import apprise

    monkeypatch.setattr(apprise, "Apprise", Boom)
    assert await notify.notify("t", "b") is False


async def test_notify_invalid_url_dropped(web_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPRISE_URLS", "not-a-url")
    config.reset_settings_cache()
    assert await notify.notify("t", "b") is False


def test_build_digest(web_env) -> None:
    now = datetime.now(UTC)
    seed_claim("awaiting_approval", key="a", expected="10.00", deadline=now.date() + timedelta(days=3))
    seed_claim("paid", key="b", expected="20.00")
    seed_claim("needs_human", key="c", expected="5.00")
    with db.session_scope() as s:
        paid = s.query(Claim).filter_by(trip_key="b").one()
        paid.paid_eur = Decimal("19.00")
        s.add(Event(level="error", kind="canary.failed", message="login geblokkeerd"))
        s.query(Claim).filter_by(trip_key="c").one().last_error = "LayoutChanged"
    with db.session_scope() as s:
        text = notify.build_digest(s, now=now + timedelta(minutes=1))
    assert "Nieuwe in aanmerking komende reizen: 3" in text
    assert "UT→ASD" in text
    assert "Uitbetaald: € 19,00" in text
    assert "Nog in te dienen / te keuren: € 10,00" in text
    assert "Wacht op jouw goedkeuring: 1" in text
    assert "claim #3: handmatige actie nodig (LayoutChanged)" in text
    assert "canary.failed: login geblokkeerd" in text
    assert "Deadlines binnen 14 dagen" in text


def test_build_digest_empty(web_env) -> None:
    with db.session_scope() as s:
        text = notify.build_digest(s)
    assert "Nieuwe in aanmerking komende reizen: 0" in text
    assert "Uitbetaald: € 0,00" in text
    assert date.today().year  # keep the import used
