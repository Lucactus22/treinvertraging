from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.unit.pipeline_support import (
    TODAY,
    add_account,
    claim_row,
    env,  # noqa: F401 - fixture
    events,
    make_trip,
    no_sleep,
    seed_claim,
)
from treinvertraging import db
from treinvertraging.adapters.fake import FakeStatusReader, FakeSubmitter
from treinvertraging.domain import ClaimState
from treinvertraging.models import Claim
from treinvertraging.otp.imap import parse_message
from treinvertraging.pipeline import claim, reconcile

E = ClaimState
FIXTURES = Path(__file__).parent.parent / "fixtures" / "otp"
H = timedelta(hours=1)


class FakeMail:
    def __init__(self, mails=None, error: Exception | None = None) -> None:
        self.mails = mails or []
        self.error = error
        self.calls = 0

    async def list_messages(self, since, *, senders=None, subject_regex=None, limit=30):
        self.calls += 1
        if self.error:
            raise self.error
        return self.mails


def rec(**kw):
    base = {"date": "2026-10-20", "from_station": "Utrecht Centraal", "to_station": "Amsterdam Centraal"}
    return {**base, **kw}


async def test_crash_between_submitting_and_submit_is_recovered_by_reconcile(env):  # noqa: F811
    """The process died after 'submitting' was committed: NS never saw it, so after 24h it is retried."""
    acc = add_account()
    cid, _ = seed_claim(E.SUBMITTING, account_id=acc, attempts=1, updated_ago=25 * H)
    reader = FakeStatusReader([])
    res = await reconcile.run_reconcile(readers=[reader], mail_provider=None)
    assert res.rolled_back == 1
    row = claim_row(cid)
    assert row.state == E.APPROVED.value and row.attempts == 1
    # the next claim run submits it again, exactly once
    gtbv = FakeSubmitter("gtbv")
    await claim.run_claim(submitters=[gtbv], today=TODAY, sleep=no_sleep)
    assert len(gtbv.calls) == 1 and claim_row(cid).state == E.SUBMITTED.value
    assert claim_row(cid).attempts == 2


async def test_crash_but_ns_did_receive_it_becomes_confirmed_not_resubmitted(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.SUBMITTING, account_id=acc, attempts=1, updated_ago=2 * H)
    reader = FakeStatusReader([rec(reference="CL-1001", status="In behandeling")])
    res = await reconcile.run_reconcile(readers=[reader], mail_provider=None)
    assert res.confirmed == 1
    row = claim_row(cid)
    assert row.state == E.CONFIRMED.value and row.reference == "CL-1001"
    assert reader.calls  # asked with a date
    gtbv = FakeSubmitter("gtbv")
    await claim.run_claim(submitters=[gtbv], today=TODAY, mode="auto")
    assert gtbv.calls == []


async def test_full_crash_scenario_through_claim_run(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.APPROVED, account_id=acc)
    crashing = FakeSubmitter("gtbv", always=RuntimeError("container killed"))
    await claim.run_claim(submitters=[crashing], today=TODAY, sleep=no_sleep)
    assert claim_row(cid).state == E.SUBMITTING.value
    # NS did get it
    await reconcile.run_reconcile(readers=[FakeStatusReader([rec()])], mail_provider=None)
    assert claim_row(cid).state == E.CONFIRMED.value


async def test_fresh_submitting_claim_is_left_alone(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.SUBMITTING, account_id=acc, attempts=1, updated_ago=H)
    res = await reconcile.run_reconcile(readers=[FakeStatusReader([])], mail_provider=None)
    assert res.rolled_back == 0 and claim_row(cid).state == E.SUBMITTING.value


async def test_stale_claim_after_three_attempts_needs_human(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.SUBMITTING, account_id=acc, attempts=3, updated_ago=30 * H)
    res = await reconcile.run_reconcile(readers=[FakeStatusReader([])], mail_provider=None)
    assert res.needs_human == 1
    assert claim_row(cid).state == E.NEEDS_HUMAN.value
    assert "Claim vraagt om aandacht" in env.titles()


async def test_no_rollback_when_no_source_could_be_queried(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.SUBMITTING, account_id=acc, attempts=1, updated_ago=48 * H)
    bad = FakeStatusReader(error=RuntimeError("login failed"))
    res = await reconcile.run_reconcile(readers=[bad], mail_provider=FakeMail(error=OSError("imap down")))
    assert res.sources_ok == 0 and len(res.source_errors) == 2
    assert claim_row(cid).state == E.SUBMITTING.value  # "not found" would mean nothing here
    assert events("reconcile.cannot_verify")
    assert "Claim niet te controleren" in env.titles()


async def test_one_failing_reader_does_not_hide_the_other(env):  # noqa: F811
    acc = add_account()
    cid, _ = seed_claim(E.SUBMITTED, account_id=acc, submitted_ago=H)
    res = await reconcile.run_reconcile(
        readers=[FakeStatusReader(error=RuntimeError("x")), FakeStatusReader([rec(status="ontvangen")])],
        mail_provider=None,
    )
    assert res.sources_ok == 1 and res.confirmed == 1
    assert claim_row(cid).state == E.CONFIRMED.value


async def test_paid_and_rejected_statuses(env):  # noqa: F811
    acc = add_account()
    t1 = make_trip(day=date(2026, 10, 20))
    t2 = make_trip(day=date(2026, 10, 21))
    paid, _ = seed_claim(E.CONFIRMED, account_id=acc, trip=t1, expected="10.00")
    rejected, _ = seed_claim(E.SUBMITTED, account_id=acc, trip=t2, expected="10.00")
    reader = FakeStatusReader(
        [
            rec(status="Uitbetaald", amount="€ 9,80"),
            rec(date="21-10-2026", status="Afgewezen"),
        ]
    )
    res = await reconcile.run_reconcile(readers=[reader], mail_provider=None)
    assert (res.paid, res.rejected) == (1, 1)
    assert claim_row(paid).state == E.PAID.value and claim_row(paid).paid_eur == Decimal("9.80")
    assert claim_row(rejected).state == E.REJECTED.value
    assert {"Uitbetaald", "Claim afgewezen"} <= set(env.titles())
    # states only move forward: a stale "ontvangen" record does not undo them
    await reconcile.run_reconcile(readers=[FakeStatusReader([rec(status="ontvangen")])], mail_provider=None)
    assert claim_row(paid).state == E.PAID.value


async def test_matching_by_reference_and_trip_key_and_ambiguity(env):  # noqa: F811
    acc = add_account()
    t1 = make_trip(day=date(2026, 10, 20))
    t2 = make_trip(day=date(2026, 10, 20), hour=17, frm="ASD", to="UT")
    c1, k1 = seed_claim(E.SUBMITTED, account_id=acc, trip=t1, reference="REF-A", submitted_ago=H)
    c2, k2 = seed_claim(E.SUBMITTED, account_id=acc, trip=t2, submitted_ago=H)
    # a record with only a date is ambiguous (two trips that day) -> ignored
    await reconcile.run_reconcile(readers=[FakeStatusReader([{"date": "2026-10-20", "status": "ontvangen"}])],
                                  mail_provider=None)  # fmt: skip
    assert claim_row(c1).state == claim_row(c2).state == E.SUBMITTED.value
    # reference and trip_key match exactly
    await reconcile.run_reconcile(
        readers=[FakeStatusReader([{"reference": "ref-a", "status": "uitbetaald"},
                                   {"trip_key": k2, "status": "ontvangen"}])],
        mail_provider=None,
    )  # fmt: skip
    assert claim_row(c1).state == E.PAID.value
    assert claim_row(c2).state == E.CONFIRMED.value
    assert k1 != k2


async def test_confirmation_mail_confirms_submitted_claim_and_stores_reference(env):  # noqa: F811
    acc = add_account()
    mail = parse_message((FIXTURES / "ns_confirmation.eml").read_bytes())  # 2026-10-26 10:15 UTC
    assert mail.date == datetime(2026, 10, 26, 10, 15, tzinfo=UTC)
    cid, _ = seed_claim(E.SUBMITTED, account_id=acc, submitted_ago=timedelta(0))
    with db.session_scope() as s:
        s.get(Claim, cid).submitted_at = datetime(2026, 10, 26, 10, 10, tzinfo=UTC)
    provider = FakeMail([mail])
    res = await reconcile.run_reconcile(
        readers=[], mail_provider=provider, now=datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    )
    assert provider.calls == 1 and res.confirmed == 1
    row = claim_row(cid)
    assert row.state == E.CONFIRMED.value and row.reference == "GTBV-2026-445566"


async def test_confirmation_mail_outside_window_or_ambiguous_is_ignored(env):  # noqa: F811
    acc = add_account()
    mail = parse_message((FIXTURES / "ns_confirmation.eml").read_bytes())
    t1 = make_trip(day=date(2026, 10, 20))
    t2 = make_trip(day=date(2026, 10, 21))
    submitted = mail.date - timedelta(minutes=5)
    c1, _ = seed_claim(E.SUBMITTED, account_id=acc, trip=t1)
    c2, _ = seed_claim(E.SUBMITTED, account_id=acc, trip=t2)
    with db.session_scope() as s:
        for cid in (c1, c2):
            s.get(Claim, cid).submitted_at = submitted
    res = await reconcile.run_reconcile(readers=[], mail_provider=FakeMail([mail]), now=mail.date + H)
    assert res.confirmed == 0  # two candidates, mail names neither: do not guess
    # mail that names the trip's date picks the right claim
    mail2 = parse_message(
        b"From: NS <noreply@ns.nl>\nSubject: Aanvraag ontvangen\nDate: Mon, 26 Oct 2026 11:15:00 +0100\n"
        b"Content-Type: text/plain; charset=utf-8\n\nReis van 21-10-2026. Referentie: ABC-12345\n"
    )
    res = await reconcile.run_reconcile(readers=[], mail_provider=FakeMail([mail2]), now=mail.date + H)
    assert res.confirmed == 1
    assert claim_row(c2).state == E.CONFIRMED.value and claim_row(c2).reference == "ABC-12345"
    assert claim_row(c1).state == E.SUBMITTED.value


async def test_nothing_pending_makes_no_remote_calls(env):  # noqa: F811
    reader = FakeStatusReader([])
    mail = FakeMail()
    res = await reconcile.run_reconcile(readers=[reader], mail_provider=mail)
    assert res.checked == 0 and reader.calls == [] and mail.calls == 0


def test_parsers():
    assert reconcile.parse_day("26 okt 2026") == date(2026, 10, 26)
    assert reconcile.parse_day("26-10-2026") == date(2026, 10, 26)
    assert reconcile.parse_day(datetime(2026, 10, 26, 23, 30, tzinfo=UTC)) == date(2026, 10, 27)  # Amsterdam
    assert reconcile.parse_amount("€ 1.234,50") == Decimal("1234.50")
    assert reconcile.parse_amount(7.5) == Decimal("7.50")
    assert reconcile.status_from_text("Uitbetaald") == E.PAID
    assert reconcile.status_from_text("Je aanvraag is afgewezen") == E.REJECTED
    assert reconcile.status_from_text("In behandeling") == E.CONFIRMED
    assert reconcile.mail_reference("Je referentienummer is GTBV-2026-445566.") == "GTBV-2026-445566"
