"""Shared fixtures/helpers for the web tests (no tests of its own)."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from treinvertraging import config, db
from treinvertraging.models import Account, Assessment, Base, Claim, Trip
from treinvertraging.web.app import create_app

USER = "admin"
PASSWORD = "s3cret-pw"


@pytest.fixture
def web_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WEB_USERNAME", USER)
    monkeypatch.setenv("WEB_PASSWORD", PASSWORD)
    monkeypatch.setenv("APPRISE_URLS", "")
    monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)
    config.reset_settings_cache()
    Base.metadata.create_all(db.get_engine())
    return data_dir


@pytest.fixture
def client(web_env: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), follow_redirects=False) as c:
        c.headers["Authorization"] = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        yield c


@pytest.fixture
def anon_client(web_env: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def seed_claim(
    state: str = "awaiting_approval",
    *,
    key: str = "k1",
    delay: int = 34,
    expected: str = "7.50",
    eligible: bool = True,
    deadline: date | None = None,
) -> int:
    """Insert account + trip + assessment + claim; return the claim id."""
    with db.session_scope() as s:
        acc = s.get(Account, 1)
        if acc is None:
            acc = Account(id=1, source="ns", label="Test", username="u@example.com")
            s.add(acc)
            s.flush()
        trip = Trip(
            key=key,
            account_id=acc.id,
            source="ns",
            card_id="3528",
            checkin_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
            checkout_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
            from_station="UT",
            to_station="ASD",
            fare_eur=Decimal("15.00"),
        )
        s.add(trip)
        s.flush()
        s.add(
            Assessment(
                trip_key=key,
                arrival_delay_min=delay,
                cancelled=False,
                evidence=["rdt:2026-10 train 3045 UT->ASD +34", "ns_flag"],
                eligible=eligible,
                percent=50 if eligible else 0,
                expected_eur=Decimal(expected),
                reason="30 min of meer",
                deadline=deadline or date(2027, 1, 1),
            )
        )
        claim = Claim(trip_key=key, state=state, expected_eur=Decimal(expected))
        s.add(claim)
        s.flush()
        return claim.id
