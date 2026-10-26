from __future__ import annotations

import asyncio
from datetime import date

import pytest

from tests.unit.pipeline_support import (
    add_account,
    claim_row,
    env,  # noqa: F401 - fixture
    events,
    make_trip,
    seed_claim,
    set_mode,
)
from treinvertraging import db, services
from treinvertraging.delaydata import rdt
from treinvertraging.domain import ClaimState
from treinvertraging.models import Claim

E = ClaimState


@pytest.fixture
def fake_env(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto", TV_FAKE_ADAPTERS="1")

    async def rdt_stub(year, month, stations, *a, **k):
        return None

    monkeypatch.setattr(rdt, "import_month", rdt_stub)
    return env


async def test_approve_and_reject_transitions(env):  # noqa: F811
    acc = add_account()
    a, _ = seed_claim(E.AWAITING_APPROVAL, account_id=acc)
    assert await services.approve_claim(a) == "approved"
    assert claim_row(a).state == "approved"
    with pytest.raises(services.InvalidTransition):
        await services.approve_claim(a)
    with pytest.raises(services.ClaimNotFound):
        await services.approve_claim(9999)
    b, _ = seed_claim(E.AWAITING_APPROVAL, account_id=acc, trip=make_trip(day=date(2026, 10, 21)))
    assert await services.reject_claim(b) == "rejected"
    assert {e.kind for e in events("claim.")} == {"claim.approved", "claim.rejected"}


@pytest.mark.parametrize("state", [E.NEEDS_HUMAN, E.REJECTED, E.EXPIRED])
async def test_retry_requeues_as_approved_with_attempts_reset(env, state):  # noqa: F811
    acc = add_account()
    cid, key = seed_claim(state, account_id=acc, attempts=3)
    with db.session_scope() as s:
        s.get(Claim, cid).last_error = "boom"
    assert await services.retry_claim(cid) == "approved"
    row = claim_row(cid)
    assert row.state == "approved" and row.attempts == 0 and row.last_error is None
    assert events("claim.retry")[0].trip_key == key


@pytest.mark.parametrize("state", [E.SUBMITTED, E.SUBMITTING, E.ELIGIBLE, E.PAID, E.AWAITING_APPROVAL])
async def test_retry_refuses_other_states(env, state):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(state, account_id=acc)
    with pytest.raises(services.InvalidTransition):
        await services.retry_claim(cid)
    assert claim_row(cid).state == state.value
    with pytest.raises(services.ClaimNotFound):
        await services.retry_claim(424242)


async def test_retried_expired_claim_is_submitted_not_expired_again(fake_env):
    from treinvertraging.pipeline import claim

    acc = add_account()
    cid, _ = seed_claim(E.EXPIRED, account_id=acc, deadline=date(2026, 1, 1))
    await services.retry_claim(cid)
    await claim.run_deadline_watchdog()
    assert claim_row(cid).state == "approved"
    summary = await services.run_claim()
    assert "1 submitted" in summary
    assert claim_row(cid).state == "submitted"


async def test_run_services_return_summaries_with_fake_adapters(fake_env):
    add_account()
    assert (await services.run_sync()).startswith("sync: 1 account(s), 2 trip(s) seen, 2 new")
    assert (await services.run_assess()).startswith("assess: 2 trip(s): 1 eligible, 1 ineligible")
    out = await services.run_claim(dry_run=True)
    assert "1 rehearsed (dry run)" in out and "0 submitted" in out
    out = await services.run_claim()
    assert "1 submitted" in out
    assert "0 submitted" in await services.run_claim()
    assert (await services.run_canary()).startswith("canary ok")
    assert (await services.run_reconcile()).startswith("reconcile: 1 open claim(s)")
    lines = (fake_env.data_dir / "fake-submissions.jsonl").read_text().splitlines()
    assert len(lines) == 2  # one dry run + one real submission


async def test_concurrent_claim_runs_submit_once(fake_env):
    add_account()
    await services.run_sync()
    await services.run_assess()
    results = await asyncio.gather(services.run_claim(), services.run_claim())
    assert sorted("1 submitted" in r for r in results) == [False, True]
    real = [
        ln
        for ln in (fake_env.data_dir / "fake-submissions.jsonl").read_text().splitlines()
        if '"dry_run": false' in ln
    ]
    assert len(real) == 1


async def test_test_login_and_calibration(fake_env):
    acc = add_account(label="Mijn NS")
    assert await services.test_login(acc) == "Ingelogd bij 'Mijn NS'."
    with pytest.raises(LookupError):
        await services.test_login(999)
    with pytest.raises(NotImplementedError):
        await services.start_calibration(acc, "ns")


async def test_next_runs_empty_without_scheduler(env):  # noqa: F811
    assert await services.next_runs() == []
