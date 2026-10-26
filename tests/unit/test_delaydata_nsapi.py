"""Tests for the NS API client and the archiver. No network: httpx is mocked with respx.

``tests/fixtures/rdt/nsapi-arrivals-ASD.json`` is SYNTHETIC (a plausible response, not recorded).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from treinvertraging.config import reset_settings_cache
from treinvertraging.db import get_engine, get_session_factory
from treinvertraging.delaydata import archiver, nsapi
from treinvertraging.models import Arrival, Base, Trip

FIXTURE = Path(__file__).parent.parent / "fixtures" / "rdt" / "nsapi-arrivals-ASD.json"
URL = nsapi.NS_ARRIVALS_URL


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def db(data_dir: Path):
    Base.metadata.create_all(get_engine())
    return get_session_factory()


def test_parse_arrivals(payload: dict) -> None:
    recs = nsapi.parse_arrivals(payload, "asd")
    assert [r.train_number for r in recs] == ["3045", "3049", "1750"]  # bus without number skipped
    late, cancelled, ontime = recs
    assert late.station == "ASD"
    assert late.planned_at == datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
    assert late.actual_at == datetime(2026, 10, 26, 14, 4, tzinfo=UTC)
    assert late.delay_min == 34 and not late.cancelled
    assert cancelled.cancelled and cancelled.actual_at is None and cancelled.delay_min is None
    assert ontime.delay_min == 0  # "+01:00" offset style also parses


def test_parse_rejects_unexpected_payload() -> None:
    with pytest.raises(nsapi.NsApiError):
        nsapi.parse_arrivals({"payload": {"departures": []}}, "UT")


@respx.mock
async def test_fetch_arrivals_sends_key_and_station(payload: dict) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=payload))
    async with nsapi.NsApiClient("secret-key") as client:
        recs = await client.fetch_arrivals("asd")
    assert len(recs) == 3
    req = route.calls.last.request
    assert req.headers["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert req.url.params["station"] == "ASD"


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, nsapi.NsApiAuthError),
        (403, nsapi.NsApiAuthError),
        (429, nsapi.NsApiRateLimited),
        (500, nsapi.NsApiError),
    ],
)
async def test_fetch_arrivals_errors(status: int, exc: type[Exception]) -> None:
    respx.get(URL).mock(return_value=httpx.Response(status, headers={"retry-after": "30"}))
    async with nsapi.NsApiClient("k") as client:
        with pytest.raises(exc):
            await client.fetch_arrivals("UT")


@respx.mock
async def test_network_error_wrapped() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    async with nsapi.NsApiClient("k") as client:
        with pytest.raises(nsapi.NsApiError):
            await client.fetch_arrivals("UT")


async def test_missing_key_raises() -> None:
    async with nsapi.NsApiClient("") as client:
        with pytest.raises(nsapi.NsApiAuthError):
            await client.fetch_arrivals("UT")


def test_upsert_updates_latest_but_never_overwrites_rdt(db, payload: dict) -> None:
    recs = nsapi.parse_arrivals(payload, "ASD")
    assert nsapi.upsert_arrivals(recs, db) == 3
    # poll again with a later prediction for the delayed train
    newer = nsapi.ArrivalRecord(
        "ASD", "3045", recs[0].planned_at, recs[0].actual_at.replace(minute=10), 40, False
    )
    nsapi.upsert_arrivals([newer], db)
    with db() as s:
        rows = s.execute(select(Arrival)).scalars().all()
        assert len(rows) == 3
        row = next(r for r in rows if r.train_number == "3045")
        assert row.delay_min == 40 and row.source == "nsapi"
        row.source, row.delay_min = "rdt", 34
        s.commit()
    nsapi.upsert_arrivals([newer], db)
    with db() as s:
        row = s.execute(select(Arrival).where(Arrival.train_number == "3045")).scalar_one()
        assert row.source == "rdt" and row.delay_min == 34


def _add_trips(db, destinations: list[str | None]) -> None:
    from treinvertraging.models import Account

    with db() as s:
        s.add(Account(id=1, source="ns", label="t"))
        s.flush()
        for i, dest in enumerate(destinations):
            s.add(
                Trip(
                    key=f"k{i}",
                    account_id=1,
                    source="ns",
                    card_id="c",
                    checkin_at=datetime(2026, 10, 1, 8, i, tzinfo=UTC),
                    from_station="UT",
                    to_station=dest,
                )
            )
        s.commit()


def test_frequent_destinations_ranks_and_adds_extras(db, monkeypatch: pytest.MonkeyPatch) -> None:
    _add_trips(db, ["ASD", "ASD", "ASD", "RTD", "RTD", "GVC", None, "ZL"])
    assert archiver.frequent_destinations(2, session_factory=db) == ["ASD", "RTD"]
    assert archiver.frequent_destinations(3, session_factory=db) == [
        "ASD",
        "RTD",
        "GVC",
    ]  # tie -> alphabetical
    monkeypatch.setenv("ARCHIVE_EXTRA_STATIONS", "ut, asd")
    reset_settings_cache()
    assert archiver.frequent_destinations(2, session_factory=db) == ["ASD", "RTD", "UT"]
    with db() as s:
        assert archiver.frequent_destinations(1, s) == ["ASD", "UT"]


def test_extra_stations_from_settings_yaml(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHIVE_EXTRA_STATIONS", raising=False)
    cfg = Path(__import__("os").environ["CONFIG_DIR"])
    (cfg / "settings.yaml").write_text("archive_extra_stations: [ut, hlm]\n", encoding="utf-8")
    assert archiver.extra_stations() == ["UT", "HLM"]


@respx.mock
async def test_archive_once_polls_each_station_and_stores(db, payload: dict) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=payload))
    async with nsapi.NsApiClient("k") as client:
        result = await archiver.archive_once(
            ["asd", "UT", "ASD"], client=client, session_factory=db, pause_s=0
        )
    assert route.call_count == 2  # duplicates removed
    assert result.stored == {"ASD": 3, "UT": 3} and not result.errors and not result.aborted
    with db() as s:
        assert len(s.execute(select(Arrival)).scalars().all()) == 6  # same trains stored per station


@respx.mock
async def test_archive_once_isolates_errors_and_aborts_on_rate_limit(db, payload: dict) -> None:
    responses = {
        "AAA": httpx.Response(500),
        "BBB": httpx.Response(200, json=payload),
        "CCC": httpx.Response(429),
        "DDD": httpx.Response(200, json=payload),
    }
    respx.get(URL).mock(side_effect=lambda req: responses[req.url.params["station"]])
    async with nsapi.NsApiClient("k") as client:
        result = await archiver.archive_once(list(responses), client=client, session_factory=db, pause_s=0)
    assert result.stored == {"BBB": 3}
    assert set(result.errors) == {"AAA", "CCC"}
    assert result.aborted  # DDD never polled


async def test_archive_once_without_key_is_noop(db) -> None:
    async with nsapi.NsApiClient("") as client:
        result = await archiver.archive_once(["UT"], client=client, session_factory=db, pause_s=0)
    assert result.aborted and not result.stored


@respx.mock
async def test_archive_once_defaults_to_frequent_destinations(db, payload: dict) -> None:
    _add_trips(db, ["ASD", "ASD", "RTD"])
    respx.get(URL).mock(return_value=httpx.Response(200, json=payload))
    async with nsapi.NsApiClient("k") as client:
        result = await archiver.archive_once(client=client, session_factory=db, pause_s=0)
    assert list(result.stored) == ["ASD", "RTD"]
