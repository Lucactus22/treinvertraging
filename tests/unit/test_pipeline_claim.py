from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.unit.pipeline_support import (
    CARD,
    IBAN,
    TODAY,
    add_account,
    add_arrival,  # noqa: F401
    claim_by_key,
    claim_row,
    env,  # noqa: F401 - fixture
    events,
    make_trip,
    no_sleep,
    seed_claim,
    set_mode,
)
from treinvertraging import db, services
from treinvertraging.adapters.fake import FakeSubmitter, FakeTripSource
from treinvertraging.domain import ClaimState, Source, SubmitResult
from treinvertraging.errors import (
    BlockedByBotProtection,
    InvalidInput,
    LayoutChanged,
    LoginRequired,
    TakeoverTimeout,
)
from treinvertraging.models import Account
from treinvertraging.pipeline import assess, claim, sync
from treinvertraging.pipeline.context import ClaimantIncomplete, claimant_for

E = ClaimState  # short alias


def trips(n: int):
    """n distinct trips (one per day)."""
    return [make_trip(day=date(2026, 10, 1) + timedelta(days=i)) for i in range(n)]


async def run(submitters, **kw) -> claim.ClaimRunResult:
    kw.setdefault("today", TODAY)
    kw.setdefault("sleep", no_sleep)
    kw.setdefault("jitter", lambda: 0.0)
    return await claim.run_claim(submitters=submitters, **kw)


# ---------------------------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------------------------


async def test_approve_mode_waits_for_approval_then_submits_once(env):  # noqa: F811
    acc = add_account()
    cid, key = seed_claim(E.ELIGIBLE, account_id=acc)
    gtbv = FakeSubmitter("gtbv")

    res = await run([gtbv])
    assert res.mode == "approve" and res.awaiting_approval == 1 and res.submitted == 0
    assert claim_row(cid).state == E.AWAITING_APPROVAL.value
    assert gtbv.calls == []
    assert env.titles() == ["Claims wachten op goedkeuring"]
    assert "http://tv.test:8080/claims" in env.sent[0]["body"]

    assert await services.approve_claim(cid) == "approved"
    res = await run([gtbv])
    assert res.submitted == 1
    row = claim_row(cid)
    assert row.state == E.SUBMITTED.value and row.channel == "gtbv" and row.attempts == 1
    assert row.reference == "FAKE-GTBV-0001" and row.submitted_at is not None
    assert len(gtbv.calls) == 1 and gtbv.calls[0]["dry_run"] is False
    call_who = gtbv.calls[0]["who"]
    assert (call_who.card_number, call_who.iban) == (CARD, IBAN)
    assert gtbv.calls[0]["trip"].key() == key
    assert "Claim ingediend" in env.titles()

    # idempotent: running again never submits again
    await run([gtbv])
    await run([gtbv])
    assert len(gtbv.calls) == 1


async def test_rejected_claims_are_never_submitted(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.AWAITING_APPROVAL, account_id=acc)
    await services.reject_claim(cid)
    gtbv = FakeSubmitter("gtbv")
    await run([gtbv])
    assert gtbv.calls == [] and claim_row(cid).state == E.REJECTED.value


async def test_auto_mode_submits_eligible_directly_and_only_once(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto")
    acc = add_account()
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc)
    gtbv = FakeSubmitter("gtbv")
    await run([gtbv])
    await run([gtbv])
    assert len(gtbv.calls) == 1
    assert claim_row(cid).state == E.SUBMITTED.value


async def test_auto_after_n_approved(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "approve", AUTO_AFTER_N_APPROVED="2")
    acc = add_account()
    t = trips(4)
    seed_claim(E.SUBMITTED, account_id=acc, trip=t[0], approved_event=True)
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc, trip=t[2])
    gtbv = FakeSubmitter("gtbv")
    # 1 human-approved success < 2: still asks for approval
    res = await run([gtbv])
    assert res.mode == "approve" and claim_row(cid).state == E.AWAITING_APPROVAL.value
    # a second approved + successful claim flips the switch
    seed_claim(E.CONFIRMED, account_id=acc, trip=t[1], approved_event=True)
    cid2, _ = seed_claim(E.ELIGIBLE, account_id=acc, trip=t[3])
    res = await run([gtbv])
    assert res.mode == "auto"
    assert claim_row(cid2).state == E.SUBMITTED.value
    assert claim_row(cid).state == E.AWAITING_APPROVAL.value  # already waiting claims stay for the human


async def test_auto_after_n_zero_never_switches(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "approve", AUTO_AFTER_N_APPROVED="0")
    acc = add_account()
    seed_claim(E.SUBMITTED, account_id=acc, trip=trips(2)[0], approved_event=True)
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc, trip=trips(2)[1])
    await run([FakeSubmitter("gtbv")])
    assert claim_row(cid).state == E.AWAITING_APPROVAL.value


# ---------------------------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------------------------


async def test_dry_run_flag_rehearses_without_state_change(env):  # noqa: F811
    acc = add_account()
    cid, key = seed_claim(E.APPROVED, account_id=acc)
    cid2, _ = seed_claim(E.ELIGIBLE, account_id=acc, trip=trips(2)[1])
    gtbv = FakeSubmitter("gtbv")
    res = await run([gtbv], dry_run=True)
    assert res.rehearsed == 2 and res.submitted == 0
    assert [c["dry_run"] for c in gtbv.calls] == [True, True]
    assert claim_row(cid).state == E.APPROVED.value and claim_row(cid).attempts == 0
    assert claim_row(cid2).state == E.ELIGIBLE.value  # not even moved to awaiting_approval
    assert [e.kind for e in events("claim.dry_run")] == ["claim.dry_run", "claim.dry_run"]
    # explicit --dry-run rehearses again every time
    await run([gtbv], dry_run=True)
    assert len(gtbv.calls) == 4


async def test_claim_mode_dry_run_rehearses_each_claim_once(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "dry_run")
    acc = add_account()
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc)
    gtbv = FakeSubmitter("gtbv")
    res = await run([gtbv])
    assert res.rehearsed == 1 and gtbv.calls[0]["dry_run"] is True
    await run([gtbv])
    assert len(gtbv.calls) == 1
    assert claim_row(cid).state == E.ELIGIBLE.value
    assert env.sent == []


async def test_dry_run_failure_is_logged_not_fatal(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv", always=LayoutChanged("heading?"))
    await run([gtbv], dry_run=True)
    ev = events("claim.dry_run")[0]
    assert ev.level == "warning" and "LayoutChanged" in ev.message
    assert claim_row(cid).state == E.APPROVED.value


# ---------------------------------------------------------------------------------------------
# channel ladder
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [BlockedByBotProtection("akamai"), LayoutChanged("heading")])
async def test_falls_through_to_next_channel_when_first_did_not_submit(env, exc):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv", always=exc)
    mijnns = FakeSubmitter("mijnns")
    res = await run([gtbv, mijnns])
    assert res.submitted == 1
    row = claim_row(cid)
    assert row.state == E.SUBMITTED.value and row.channel == "mijnns" and row.attempts == 1
    assert len(gtbv.calls) == 1 and len(mijnns.calls) == 1
    assert any(e.kind == "claim.channel_failed" for e in events("claim."))


async def test_no_fall_through_when_first_attempt_reached_final_submit(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    exc = LayoutChanged("confirmation page unknown")
    exc.reached_final_submit = True
    gtbv = FakeSubmitter("gtbv", always=exc)
    mijnns = FakeSubmitter("mijnns")
    res = await run([gtbv, mijnns])
    assert mijnns.calls == []
    assert res.unresolved == 1
    assert claim_row(cid).state == E.SUBMITTING.value  # reconcile decides
    # a later claim run must not resubmit it
    await run([gtbv, mijnns])
    assert len(gtbv.calls) == 1 and mijnns.calls == []


@pytest.mark.parametrize("exc_type", [LoginRequired, TakeoverTimeout])
async def test_no_fall_through_on_other_errors_and_retry_later(env, exc_type):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv", always=exc_type("x"))
    mijnns = FakeSubmitter("mijnns")
    res = await run([gtbv, mijnns])
    assert mijnns.calls == [] and res.retry_later == 1
    row = claim_row(cid)
    assert row.state == E.APPROVED.value and row.attempts == 1 and exc_type.__name__ in row.last_error


async def test_transient_failures_escalate_after_three_attempts(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv", always=TakeoverTimeout("nobody came"))
    for _ in range(3):
        await run([gtbv])
    row = claim_row(cid)
    assert row.state == E.NEEDS_HUMAN.value and row.attempts == 3
    assert "Claim vraagt om aandacht" in env.titles()
    await run([gtbv])
    assert len(gtbv.calls) == 3  # needs_human is not picked up again


async def test_invalid_input_goes_to_needs_human_without_retry_or_fall_through(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv", always=InvalidInput("Vul een OV-chipkaartnummer in."))
    mijnns = FakeSubmitter("mijnns")
    res = await run([gtbv, mijnns])
    assert res.needs_human == 1 and mijnns.calls == []
    row = claim_row(cid)
    assert row.state == E.NEEDS_HUMAN.value and "OV-chipkaartnummer" in row.last_error


async def test_all_channels_blocked_goes_to_needs_human_with_debug_dir(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    bundle = env.data_dir / "debug" / "20261026-gtbv"
    bundle.mkdir(parents=True)
    (bundle / "screenshot.png").write_bytes(b"png")
    exc = BlockedByBotProtection("captcha")
    exc.debug_dir = str(bundle)
    res = await run(
        [FakeSubmitter("gtbv", always=exc), FakeSubmitter("mijnns", always=BlockedByBotProtection("x"))]
    )
    assert res.needs_human == 1
    row = claim_row(cid)
    assert row.state == E.NEEDS_HUMAN.value and row.debug_dir
    assert env.sent[-1]["level"] == "error"


async def test_ok_false_before_final_submit_retries_ok_false_after_stays_submitting(env):  # noqa: F811
    acc = add_account()
    t = trips(2)
    c1, _ = seed_claim(E.APPROVED, account_id=acc, trip=t[0])
    c2, _ = seed_claim(E.APPROVED, account_id=acc, trip=t[1])
    before = SubmitResult(ok=False, reference=None, screenshot=None, message="form refused",
                          channel="gtbv", reached_final_submit=False)  # fmt: skip
    after = SubmitResult(ok=False, reference=None, screenshot=None, message="no confirmation page",
                         channel="gtbv", reached_final_submit=True)  # fmt: skip
    await run([FakeSubmitter("gtbv", outcomes=[before, after])])
    assert claim_row(c1).state == E.APPROVED.value
    assert claim_row(c2).state == E.SUBMITTING.value
    assert "no confirmation" in claim_row(c2).last_error


async def test_unexpected_error_leaves_claim_submitting_and_never_retries(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv", always=RuntimeError("browser crashed after click?"))
    res = await run([gtbv])
    assert res.unresolved == 1
    assert claim_row(cid).state == E.SUBMITTING.value
    await run([gtbv])
    assert len(gtbv.calls) == 1


# ---------------------------------------------------------------------------------------------
# cap, jitter, deadline, claimant
# ---------------------------------------------------------------------------------------------


async def test_max_five_per_run_with_jitter_between_submissions(env):  # noqa: F811
    acc = add_account()
    ids = [seed_claim(E.APPROVED, account_id=acc, trip=t)[0] for t in trips(7)]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    gtbv = FakeSubmitter("gtbv")
    res = await run([gtbv], sleep=sleep, jitter=lambda: 42.0)
    assert res.submitted == 5 and res.skipped == 2
    assert sleeps == [42.0] * 4  # between submissions, not before the first or after the last
    assert [claim_row(i).state for i in ids].count(E.APPROVED.value) == 2
    await run([gtbv], sleep=sleep, jitter=lambda: 1.0)
    assert len(gtbv.calls) == 7


async def test_most_urgent_deadline_goes_first(env):  # noqa: F811
    acc = add_account()
    t = trips(3)
    late, _ = seed_claim(E.APPROVED, account_id=acc, trip=t[0], deadline=date(2027, 1, 30))
    urgent, _ = seed_claim(E.APPROVED, account_id=acc, trip=t[1], deadline=date(2026, 11, 1))
    mid, _ = seed_claim(E.APPROVED, account_id=acc, trip=t[2], deadline=date(2026, 12, 1))
    gtbv = FakeSubmitter("gtbv")
    await run([gtbv], max_per_run=2)
    assert claim_row(urgent).state == E.SUBMITTED.value and claim_row(mid).state == E.SUBMITTED.value
    assert claim_row(late).state == E.APPROVED.value


async def test_eligible_claim_past_deadline_expires_instead_of_submitting(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto")
    acc = add_account()
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc, deadline=date(2026, 10, 25))
    gtbv = FakeSubmitter("gtbv")
    res = await run([gtbv])
    assert res.expired == 1 and gtbv.calls == []
    assert claim_row(cid).state == E.EXPIRED.value
    assert "Claim verlopen!" in env.titles()


async def test_missing_card_or_iban_needs_human(env):  # noqa: F811
    acc = add_account(card=None)
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    gtbv = FakeSubmitter("gtbv")
    await run([gtbv])
    assert gtbv.calls == []
    row = claim_row(cid)
    assert row.state == E.NEEDS_HUMAN.value and "kaartnummer" in row.last_error


async def test_ns_trip_uses_card_of_another_enabled_account(env):  # noqa: F811
    ns = add_account(Source.NS, card=None, iban=None, holder="", email="")
    add_account(Source.OVCHIP, card="3528 1111 2222 3333", iban="NL02ABNA0123456789", holder="Ov Holder",
                email="ov@example.com")  # fmt: skip
    add_account(Source.OVCHIP, label="disabled", card="3528999999999999", enabled=False)
    cid, _ = seed_claim(E.APPROVED, account_id=ns)
    gtbv = FakeSubmitter("gtbv")
    await run([gtbv])
    who = gtbv.calls[0]["who"]
    assert who.card_number == "3528111122223333" and who.iban == "NL02ABNA0123456789"
    assert who.holder_name == "Ov Holder" and who.email == "ov@example.com"
    assert claim_row(cid).state == E.SUBMITTED.value


def test_claimant_for_prefers_own_values_and_raises_when_incomplete(env):  # noqa: F811
    own = add_account(Source.NS, label="a", card="3528000000000042")
    other = add_account(Source.OVCHIP, label="b", card="3528000000000099", iban="NL02ABNA0123456789")
    with db.session_scope() as s:
        a, b = s.get(Account, own), s.get(Account, other)
        who = claimant_for(a, [a, b])
        assert who.card_number == "3528000000000042" and who.iban == IBAN
        empty = Account(id=99, source="ns", label="x", enabled=True)
        with pytest.raises(ClaimantIncomplete):
            claimant_for(empty, [empty])


async def test_channels_come_from_settings_via_registry(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto", TV_FAKE_ADAPTERS="1", SUBMIT_CHANNELS="mijnns,gtbv")
    acc = add_account()
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc)
    res = await claim.run_claim(today=TODAY, sleep=no_sleep, jitter=lambda: 0.0)
    assert res.submitted == 1 and claim_row(cid).channel == "mijnns"


async def test_no_channel_available_leaves_claim_untouched(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto")
    acc = add_account()
    cid, _ = seed_claim(E.ELIGIBLE, account_id=acc)
    res = await claim.run_claim(submitters=[], today=TODAY)
    assert res.submitted == 0 and claim_row(cid).state == E.ELIGIBLE.value
    assert events("claim.no_channel")


# ---------------------------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------------------------


async def test_deadline_watchdog_alerts_and_expires(env):  # noqa: F811
    acc = add_account()
    t = trips(4)
    near, _ = seed_claim(E.AWAITING_APPROVAL, account_id=acc, trip=t[0], deadline=TODAY + timedelta(days=10))
    far, _ = seed_claim(E.AWAITING_APPROVAL, account_id=acc, trip=t[1], deadline=TODAY + timedelta(days=40))
    late, _ = seed_claim(E.NEEDS_HUMAN, account_id=acc, trip=t[2], deadline=TODAY - timedelta(days=1))
    done, _ = seed_claim(E.CONFIRMED, account_id=acc, trip=t[3], deadline=TODAY + timedelta(days=2))
    out = await claim.run_deadline_watchdog(today=TODAY)
    assert len(out["warned"]) == 1 and len(out["expired"]) == 1
    assert claim_row(near).state == E.AWAITING_APPROVAL.value
    assert claim_row(far).state == E.AWAITING_APPROVAL.value
    assert claim_row(late).state == E.EXPIRED.value
    assert claim_row(done).state == E.CONFIRMED.value
    assert sorted(env.titles()) == ["Claim verlopen!", "Claimdeadline nadert"]
    assert all(n["level"] == "error" for n in env.sent)
    assert {e.kind for e in events("watchdog.")} == {"watchdog.deadline"}


async def test_watchdog_leaves_approved_claims_and_stays_quiet_when_nothing_to_report(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc, deadline=TODAY - timedelta(days=3))
    out = await claim.run_deadline_watchdog(today=TODAY)
    assert out == {"expired": [], "warned": []}
    assert claim_row(cid).state == E.APPROVED.value and env.sent == []


# ---------------------------------------------------------------------------------------------
# whole pipeline
# ---------------------------------------------------------------------------------------------


async def test_full_pipeline_sync_assess_claim_is_idempotent(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto")
    acc = add_account()
    src = FakeTripSource([make_trip(flag=True), make_trip(hour=17, frm="ASD", to="UT", flag=False)])
    gtbv = FakeSubmitter("gtbv")

    async def rdt_stub(year, month, stations):
        return None

    for _ in range(2):
        await sync.run_sync(today=TODAY, trip_source_factory=lambda a: src)
        await assess.run_assess(today=TODAY, rdt_import=rdt_stub)
        await run([gtbv])
    assert len(gtbv.calls) == 1
    assert claim_by_key(make_trip().key()).state == E.SUBMITTED.value
    assert claim_by_key(make_trip(hour=17, frm="ASD", to="UT", flag=False).key()).state == E.INELIGIBLE.value
    assert acc > 0
