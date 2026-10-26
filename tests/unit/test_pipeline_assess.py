from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select

from tests.unit.pipeline_support import (
    CARD,
    TODAY,
    TZ,
    add_account,
    add_arrival,
    claim_by_key,
    count,
    env,  # noqa: F401 - fixture
    events,
    make_trip,
)
from treinvertraging import db
from treinvertraging.adapters.fake import FakeTripSource
from treinvertraging.delaydata import rdt
from treinvertraging.domain import ClaimState, Source
from treinvertraging.models import Assessment, Claim, SettingKV
from treinvertraging.pipeline import assess, sync


async def _sync(acc: int, trips, source: Source = Source.NS) -> None:
    src = FakeTripSource(trips, source=source)
    await sync.run_sync(today=TODAY, trip_source_factory=lambda a: src if a.id == acc else FakeTripSource([]))


class RdtStub:
    def __init__(self, missing: set[tuple[int, int]] = frozenset()) -> None:
        self.calls: list[tuple[int, int, set[str]]] = []
        self.missing = missing

    async def __call__(self, year: int, month: int, stations: set[str]):
        self.calls.append((year, month, set(stations)))
        if (year, month) in self.missing:
            raise rdt.RdtNotAvailable("nope")


def latest(key: str) -> Assessment:
    with db.session_scope() as s:
        return s.scalars(
            select(Assessment).where(Assessment.trip_key == key).order_by(Assessment.id.desc())
        ).first()


async def test_delayed_trip_with_rdt_data_is_eligible(env):  # noqa: F811
    acc = add_account()
    trip = make_trip(flag=None)
    await _sync(acc, [trip])
    add_arrival(delay=34)  # planned 09:00, +34 -> 09:34, check-out at 09:40
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.eligible == 1
    a = latest(trip.key())
    assert a.eligible and a.percent == 50 and str(a.expected_eur) == "10.00" and a.arrival_delay_min == 34
    assert any("train 3045" in e for e in a.evidence)
    assert claim_by_key(trip.key()).state == ClaimState.ELIGIBLE.value
    assert str(claim_by_key(trip.key()).expected_eur) == "10.00"
    assert [e.kind for e in events("assess.")] == ["assess.eligible", "assess.done"]


async def test_falls_back_to_ns_flag_without_delay_data(env):  # noqa: F811
    acc = add_account()
    trip = make_trip(flag=True)
    await _sync(acc, [trip])
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    a = latest(trip.key())
    assert a.eligible and "ns_flag" in a.evidence
    assert claim_by_key(trip.key()).state == ClaimState.ELIGIBLE.value


async def test_no_data_is_ineligible_then_reassessed_when_data_arrives(env):  # noqa: F811
    acc = add_account()
    trip = make_trip(flag=None)
    await _sync(acc, [trip])
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert claim_by_key(trip.key()).state == ClaimState.INELIGIBLE.value
    assert latest(trip.key()).reason.startswith("no_delay_data")
    # same data again: no duplicate assessment rows
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert count(Assessment) == 1
    add_arrival(delay=62, planned=datetime(2026, 10, 20, 8, 40, tzinfo=TZ))
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.eligible == 1
    assert claim_by_key(trip.key()).state == ClaimState.ELIGIBLE.value
    assert latest(trip.key()).percent == 100
    assert count(Assessment) == 2


async def test_below_threshold_is_ineligible_and_final(env):  # noqa: F811
    acc = add_account()
    trip = make_trip(flag=None)
    await _sync(acc, [trip])
    add_arrival(delay=12)
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert latest(trip.key()).reason.startswith("below_threshold")
    assert claim_by_key(trip.key()).state == ClaimState.INELIGIBLE.value
    # below_threshold is not re-assessed
    add_arrival(train="9", delay=70, planned=datetime(2026, 10, 20, 9, 5, tzinfo=TZ))
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.assessed == 0


async def test_deadline_passed_marks_expired(env):  # noqa: F811
    acc = add_account()
    trip = make_trip(day=date(2026, 7, 25), flag=True)
    await _sync(acc, [trip])
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.expired == 1
    assert claim_by_key(trip.key()).state == ClaimState.EXPIRED.value
    assert latest(trip.key()).reason.startswith("deadline_passed")
    assert any(e.kind == "assess.expired" and e.level == "warning" for e in events("assess."))


async def test_rdt_import_months_stations_and_memory(env):  # noqa: F811
    acc = add_account()
    t1 = make_trip(
        day=date(2026, 9, 29), hour=23, minute=50, minutes=30, to="ASD", flag=True
    )  # crosses month end
    t2 = make_trip(day=date(2026, 10, 20), to="RTD", flag=True)
    t3 = make_trip(day=date(2026, 10, 26), to="UT", flag=True)  # current month: skipped
    await _sync(acc, [t1, t2, t3])
    stub = RdtStub()
    res = await assess.run_assess(today=TODAY, rdt_import=stub)
    months = sorted((y, m) for y, m, _ in stub.calls)
    assert months == [(2026, 9), (2026, 10)] or months == [(2026, 9)]
    # the current month (2026-10) is never imported
    assert (2026, 10) not in months
    assert all(st == {"ASD", "RTD", "UT"} for _, _, st in stub.calls)
    assert any("2026-10" in s and "current month" in s for s in res.rdt_skipped)
    with db.session_scope() as s:
        assert s.get(SettingKV, "rdt.imported.2026-09").value == "ASD,RTD,UT"
    # same stations again: nothing re-imported
    add_arrival()
    t4 = make_trip(day=date(2026, 9, 5), to="ASD", flag=True)
    await _sync(acc, [t4])
    stub2 = RdtStub()
    await assess.run_assess(today=TODAY, rdt_import=stub2)
    assert stub2.calls == []


async def test_rdt_not_available_and_errors_are_tolerated(env):  # noqa: F811
    acc = add_account()
    await _sync(
        acc, [make_trip(day=date(2026, 9, 10), flag=True), make_trip(day=date(2026, 8, 10), flag=True)]
    )

    async def boom(year: int, month: int, stations: set[str]):
        if month == 8:
            raise ConnectionError("network down")
        raise rdt.RdtNotAvailable("not yet")

    res = await assess.run_assess(today=TODAY, rdt_import=boom)
    assert res.eligible == 2  # NS flag fallback
    assert any("not available" in s for s in res.rdt_skipped) and any("failed" in s for s in res.rdt_skipped)
    assert any(e.kind == "assess.rdt_failed" for e in events("assess."))
    with db.session_scope() as s:  # failures are not remembered, so they are retried later
        assert s.scalars(select(SettingKV).where(SettingKV.key.like("rdt.imported.%"))).first() is None


async def test_dedup_ov_trip_defers_to_ns_trip(env):  # noqa: F811
    ns = add_account(Source.NS, card=None)
    ov = add_account(Source.OVCHIP, card=CARD)
    ns_trip = make_trip(source=Source.NS, card_id="ns-account-77", flag=True)
    ov_trip = make_trip(source=Source.OVCHIP, card_id=CARD, minute=16, flag=True)  # 1 minute later
    await _sync(ns, [ns_trip])
    await _sync(ov, [ov_trip], Source.OVCHIP)
    # NS account has no card number: ids cannot be compared -> NOT treated as the same journey
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.duplicates == 0 and res.eligible == 2


async def test_dedup_by_last4_digits(env):  # noqa: F811
    ns = add_account(Source.NS, card=None)
    ov = add_account(Source.OVCHIP)
    ns_trip = make_trip(source=Source.NS, card_id="3528 **** **** 0001", flag=True)
    ov_trip = make_trip(source=Source.OVCHIP, card_id=CARD, minute=16, flag=True)
    await _sync(ns, [ns_trip])
    await _sync(ov, [ov_trip], Source.OVCHIP)
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.duplicates == 1 and res.eligible == 1
    assert claim_by_key(ns_trip.key()).state == ClaimState.ELIGIBLE.value
    assert claim_by_key(ov_trip.key()).state == ClaimState.INELIGIBLE.value
    assert latest(ov_trip.key()).reason.startswith("duplicate")
    assert any(e.startswith("corroborated_by:ovchip:") for e in latest(ns_trip.key()).evidence)


async def test_dedup_through_account_card_number(env):  # noqa: F811
    ns = add_account(Source.NS, card=CARD)  # the NS account knows its linked card
    ov = add_account(Source.OVCHIP)
    ns_trip = make_trip(source=Source.NS, card_id="ns-account-77", flag=True)
    ov_trip = make_trip(source=Source.OVCHIP, card_id=CARD, flag=True)
    await _sync(ns, [ns_trip])
    await _sync(ov, [ov_trip], Source.OVCHIP)
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.duplicates == 1


async def test_two_travellers_with_different_cards_are_not_deduplicated(env):  # noqa: F811
    ns = add_account(Source.NS, card=None)
    ov = add_account(Source.OVCHIP)
    await _sync(ns, [make_trip(source=Source.NS, card_id="3528000000009999", flag=True)])
    await _sync(ov, [make_trip(source=Source.OVCHIP, card_id=CARD, flag=True)], Source.OVCHIP)
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.duplicates == 0 and res.eligible == 2


async def test_dedup_ns_trip_defers_when_ov_claim_already_in_flight(env):  # noqa: F811
    ov = add_account(Source.OVCHIP)
    ns = add_account(Source.NS, card=None)
    ov_trip = make_trip(source=Source.OVCHIP, card_id=CARD, flag=True)
    await _sync(ov, [ov_trip], Source.OVCHIP)
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    with db.session_scope() as s:
        s.scalar(select(Claim).where(Claim.trip_key == ov_trip.key())).state = ClaimState.SUBMITTED.value
    ns_trip = make_trip(source=Source.NS, card_id="3528 **** **** 0001", minute=14, flag=True)
    await _sync(ns, [ns_trip])
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.duplicates == 1
    assert claim_by_key(ns_trip.key()).state == ClaimState.INELIGIBLE.value
    assert claim_by_key(ov_trip.key()).state == ClaimState.SUBMITTED.value


async def test_ns_trip_supersedes_unclaimed_ov_trip(env):  # noqa: F811
    ov = add_account(Source.OVCHIP)
    ns = add_account(Source.NS, card=None)
    ov_trip = make_trip(source=Source.OVCHIP, card_id=CARD, flag=True)
    await _sync(ov, [ov_trip], Source.OVCHIP)
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert claim_by_key(ov_trip.key()).state == ClaimState.ELIGIBLE.value
    ns_trip = make_trip(source=Source.NS, card_id="3528 **** **** 0001", flag=True)
    await _sync(ns, [ns_trip])
    await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert claim_by_key(ns_trip.key()).state == ClaimState.ELIGIBLE.value
    assert claim_by_key(ov_trip.key()).state == ClaimState.INELIGIBLE.value


@pytest.mark.parametrize("minutes_off,expected_dups", [(2, 1), (3, 0)])
async def test_dedup_window_is_two_minutes(env, minutes_off, expected_dups):  # noqa: F811
    ns = add_account(Source.NS, card=None)
    ov = add_account(Source.OVCHIP)
    await _sync(ns, [make_trip(source=Source.NS, card_id="3528 **** **** 0001", flag=True)])
    await _sync(
        ov, [make_trip(source=Source.OVCHIP, card_id=CARD, minute=15 + minutes_off, flag=True)], Source.OVCHIP
    )
    res = await assess.run_assess(today=TODAY, rdt_import=RdtStub())
    assert res.duplicates == expected_dups


async def test_nothing_pending_does_not_touch_rdt(env):  # noqa: F811
    stub = RdtStub()
    res = await assess.run_assess(today=TODAY, rdt_import=stub)
    assert res.assessed == 0 and stub.calls == []
