from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from tests.unit.pipeline_support import (
    TODAY,
    add_account,
    claim_by_key,
    count,
    env,  # noqa: F401 - fixture
    events,
    make_trip,
)
from treinvertraging import db
from treinvertraging.adapters.fake import FakeTripSource
from treinvertraging.domain import ClaimState, Source
from treinvertraging.errors import LoginRequired
from treinvertraging.models import Account, Claim, Trip
from treinvertraging.pipeline import sync


def _factory(sources: dict[int, FakeTripSource]):
    return lambda account: sources[account.id]


async def test_sync_creates_trips_and_detected_claims(env):  # noqa: F811
    acc = add_account()
    src = FakeTripSource([make_trip(), make_trip(hour=17, frm="ASD", to="UT")])
    res = await sync.run_sync(today=TODAY, trip_source_factory=_factory({acc: src}))
    assert (res.accounts, res.seen, res.new) == (1, 2, 2)
    assert src.login_calls == 1
    assert src.list_calls == [TODAY - timedelta(days=95)]
    assert count(Trip) == 2
    with db.session_scope() as s:
        states = {c.state for c in s.scalars(select(Claim))}
        assert states == {ClaimState.DETECTED.value}
        trip = s.scalar(select(Trip).where(Trip.from_station == "UT"))
        assert trip.key == make_trip().key()
        assert trip.raw_json == {"n": 1}
        assert trip.account_id == acc


async def test_sync_is_idempotent(env):  # noqa: F811
    acc = add_account()
    src = FakeTripSource([make_trip()])
    for _ in range(3):
        await sync.run_sync(today=TODAY, trip_source_factory=_factory({acc: src}))
    assert count(Trip) == 1
    assert count(Claim) == 1


async def test_sync_skips_disabled_accounts_and_isolates_failures(env):  # noqa: F811
    bad = add_account(label="bad")
    good = add_account(Source.OVCHIP, label="good", card="3528000000000002")
    add_account(label="off", enabled=False)
    sources = {
        bad: FakeTripSource(login_error=LoginRequired("nope")),
        good: FakeTripSource(
            [make_trip(source=Source.OVCHIP, card_id="3528000000000002")], source=Source.OVCHIP
        ),
    }
    res = await sync.run_sync(today=TODAY, trip_source_factory=_factory(sources))
    assert res.accounts == 2
    assert res.new == 1
    assert "bad" in res.failed and "LoginRequired" in res.failed["bad"]
    assert any(e.kind == "sync.failed" and e.level == "error" for e in events("sync."))
    assert env.titles() == ["Synchroniseren mislukt"]


async def test_new_checkout_sends_ineligible_claim_back_to_detected(env):  # noqa: F811
    acc = add_account()
    await sync.run_sync(
        today=TODAY, trip_source_factory=_factory({acc: FakeTripSource([make_trip(checkout=False)])})
    )
    key = make_trip(checkout=False).key()
    with db.session_scope() as s:
        s.scalar(select(Claim).where(Claim.trip_key == key)).state = ClaimState.INELIGIBLE.value
    res = await sync.run_sync(today=TODAY, trip_source_factory=_factory({acc: FakeTripSource([make_trip()])}))
    assert res.reset == 1
    assert claim_by_key(key).state == ClaimState.DETECTED.value
    with db.session_scope() as s:
        assert s.get(Trip, key).checkout_at is not None


async def test_changed_trip_does_not_touch_claims_in_flight(env):  # noqa: F811
    acc = add_account()
    await sync.run_sync(
        today=TODAY, trip_source_factory=_factory({acc: FakeTripSource([make_trip(flag=False)])})
    )
    key = make_trip().key()
    with db.session_scope() as s:
        s.scalar(select(Claim).where(Claim.trip_key == key)).state = ClaimState.SUBMITTED.value
    await sync.run_sync(
        today=TODAY, trip_source_factory=_factory({acc: FakeTripSource([make_trip(flag=True)])})
    )
    assert claim_by_key(key).state == ClaimState.SUBMITTED.value
    with db.session_scope() as s:
        assert s.get(Account, acc) is not None
        assert s.get(Trip, key).ns_delay_flag is True
