"""Tests for the trip -> arrival matcher (see the heuristic in ``delaydata/matcher.py``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from treinvertraging.db import get_engine, get_session_factory
from treinvertraging.delaydata import rdt
from treinvertraging.delaydata.matcher import MatchResult, match
from treinvertraging.domain import Source, TripDTO
from treinvertraging.models import Arrival, Base

AMS = ZoneInfo("Europe/Amsterdam")
FIXTURE = Path(__file__).parent.parent / "fixtures" / "rdt" / "services-sample.csv.gz"
MONTH_URL = "https://opendata.rijdendetreinen.nl/public/services/services-2025-09.csv.gz"


@pytest.fixture
def db(data_dir: Path):
    Base.metadata.create_all(get_engine())
    return get_session_factory()


def at(h: int, m: int, day: int = 1) -> datetime:
    return datetime(2025, 9, day, h, m, tzinfo=AMS)


def trip(to: str | None, checkin: datetime, checkout: datetime | None, frm: str = "UT") -> TripDTO:
    return TripDTO(
        source=Source.NS,
        card_id="3528xxxx",
        checkin_at=checkin,
        checkout_at=checkout,
        from_station=frm,
        to_station=to,
    )


def arr(
    station: str, train: str, planned: datetime, delay: int | None = 0, cancelled: bool = False
) -> Arrival:
    actual = None if cancelled or delay is None else planned + timedelta(minutes=delay)
    return Arrival(
        station=station,
        train_number=train,
        planned_at=planned.astimezone(UTC),
        actual_at=actual,
        delay_min=delay,
        cancelled=cancelled,
        source="rdt",
    )


def run(db, t: TripDTO, *arrivals: Arrival) -> MatchResult:
    with db() as s:
        s.add_all(arrivals)
        s.commit()
        return match(t, s)


def test_on_time_train(db) -> None:
    r = run(db, trip("ASD", at(14, 5), at(14, 50)), arr("ASD", "3047", at(14, 45), 0))
    assert (r.delay_min, r.cancelled) == (0, False)
    assert (
        "train 3047" in r.evidence[0]
        and "UT->ASD" in r.evidence[0]
        and r.evidence[0].startswith("rdt:2025-09")
    )


def test_delayed_train_planned_before_checkin_is_still_found(db) -> None:
    # planned 14:30 but 34 min late; traveller checked in at 14:35 and out at 15:10
    r = run(db, trip("ASD", at(14, 35), at(15, 10)), arr("ASD", "3045", at(14, 30), 34))
    assert r.delay_min == 34 and not r.cancelled
    assert r.evidence[0].endswith("+34")


def test_picks_arrival_closest_before_checkout(db) -> None:
    r = run(
        db,
        trip("ASD", at(14, 5), at(15, 10)),
        arr("ASD", "3045", at(14, 30), 34),  # actual 15:04, gap 6
        arr("ASD", "3047", at(14, 45), 0),  # gap 25
        arr("ASD", "3051", at(15, 45), 2),  # after checkout: excluded
    )
    assert r.delay_min == 34


def test_arrival_before_minimum_ride_time_is_ignored(db) -> None:
    r = run(db, trip("ASD", at(14, 0), at(14, 20)), arr("ASD", "1", at(14, 2), 0))
    assert r.delay_min is None
    assert "none fits" in r.evidence[0]


def test_cancelled_train_when_nothing_else_fits(db) -> None:
    r = run(db, trip("GVC", at(15, 40), at(16, 15), "RTD"), arr("GVC", "2250", at(16, 10), 0, cancelled=True))
    assert r.cancelled is True and r.delay_min is None
    assert "cancelled" in r.evidence[0]


def test_replacement_train_wins_over_cancelled_predecessor(db) -> None:
    r = run(
        db,
        trip("GVC", at(15, 40), at(16, 30), "RTD"),
        arr("GVC", "2250", at(16, 10), 0, cancelled=True),
        arr("GVC", "2252", at(16, 22), 3),
    )
    assert r.cancelled is False and r.delay_min == 3
    assert any("earlier train cancelled" in e for e in r.evidence)


def test_no_checkout_gives_no_delay(db) -> None:
    r = run(db, trip("ASD", at(14, 5), None), arr("ASD", "3045", at(14, 30), 34))
    assert r.delay_min is None and not r.cancelled and "no check-out" in r.evidence[0]


def test_no_destination(db) -> None:
    r = run(db, trip(None, at(14, 5), at(15, 0)))
    assert r.delay_min is None and "no destination" in r.evidence[0]


def test_no_data_for_station(db) -> None:
    r = run(db, trip("ASD", at(14, 5), at(15, 0)), arr("RTD", "9", at(14, 40), 0))
    assert r.delay_min is None and not r.cancelled and "no arrival data for ASD" in r.evidence[0]


def test_lowercase_destination_and_naive_datetimes(db) -> None:
    t = trip("asd", at(14, 5).replace(tzinfo=None), at(14, 50).replace(tzinfo=None))
    r = run(db, t, arr("ASD", "3047", at(14, 45), 4))
    assert r.delay_min == 4


def test_stale_arrival_far_before_checkout_is_dropped_or_flagged(db) -> None:
    # arrived 20 min before checkout: kept but flagged as low confidence
    r = run(db, trip("ASD", at(13, 0), at(15, 0)), arr("ASD", "7", at(14, 40), 0))
    assert r.delay_min == 0 and any("low confidence" in e for e in r.evidence)
    # arrived 60 min before checkout: dropped
    r2 = run(db, trip("ASD", at(13, 0), at(16, 0), "UT"), arr("ASD", "8", at(15, 0), 0))
    assert r2.delay_min is None


def test_crosscheck_mismatch_is_noted(db) -> None:
    # data says on time at 14:45 but checkout is 16 min later: implied +12 is fine,
    # while a 25-min stale arrival is flagged as mismatch via checkout-implied delay
    r = run(db, trip("ASD", at(14, 0), at(15, 12)), arr("ASD", "3047", at(14, 45), 0))
    assert r.delay_min == 0
    assert any(e.startswith("cross-check mismatch") for e in r.evidence)


def test_ambiguous_candidates_are_listed(db) -> None:
    r = run(
        db,
        trip("ASD", at(14, 0), at(14, 55)),
        arr("ASD", "10", at(14, 45), 0),
        arr("ASD", "11", at(14, 46), 0),
    )
    assert r.delay_min == 0 and any(e.startswith("ambiguous") for e in r.evidence)


@respx.mock
async def test_end_to_end_with_rdt_fixture(db) -> None:
    respx.get(MONTH_URL).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
    await rdt.import_month(2025, 9, {"ASD", "GVC"}, db)

    # IC 3045 UT->ASD, +34: the delayed trip
    with db() as s:
        r = match(trip("ASD", at(14, 5), at(15, 10)), s)
        assert r.delay_min == 34 and not r.cancelled
        assert "train 3045" in r.evidence[0]

        # IC 3049 was cancelled, but nothing else fits: reported cancelled
        r = match(trip("ASD", at(14, 40), at(15, 22)), s)
        assert r.cancelled and r.delay_min is None

        # Den Haag: cancelled 2250 replaced by 2252 (+3)
        r = match(trip("GVC", at(15, 40), at(16, 30), "RTD"), s)
        assert r.delay_min == 3 and not r.cancelled
