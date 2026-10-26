"""Claim policy and submission, plus the deadline watchdog.

Policy (``CLAIM_MODE``, overridable with ``mode=``)
    ``dry_run``  every ``eligible``/``approved`` claim is *rehearsed* once: the submitter runs with
                 ``dry_run=True`` (fills the form, stops before the final button) and an event
                 ``claim.dry_run`` records the result. States do not change.
    ``approve``  ``eligible`` -> ``awaiting_approval`` (one notification with a link to /claims).
                 ``approved`` claims (approved in the UI) are submitted.
    ``auto``     ``eligible`` and ``approved`` claims are submitted. ``approve`` behaves like ``auto``
                 once ``AUTO_AFTER_N_APPROVED`` (> 0) claims that a human approved have been submitted
                 successfully (states submitted/confirmed/paid with a ``claim.approved`` event).

``claim --dry-run`` (``dry_run=True``) forces rehearsal in any mode, also for claims rehearsed before.

Submission of one claim (never twice: only ``eligible``/``approved`` claims are picked up, and the
state is committed as ``submitting`` *before* the submitter is called)
    1. state ``submitting``, ``attempts += 1``, ``channel`` = first channel; commit.
    2. call the submitters of ``SUBMIT_CHANNELS`` in order. Fall through to the next channel only when
       the error is ``BlockedByBotProtection`` or ``LayoutChanged`` and the attempt did not reach the
       final submit (adapter exceptions are assumed to be pre-submit unless they carry
       ``reached_final_submit = True``).
    3. outcome: ``ok`` -> ``submitted`` (reference, ``submitted_at``); ``InvalidInput`` or every channel
       blocked/changed -> ``needs_human`` (notification with the debug bundle); transient failure that
       provably did not submit (login required, takeover timeout, ``ok=False`` without final submit) ->
       back to ``approved`` while ``attempts < 3``, else ``needs_human``; anything unexpected, or a
       failure *after* the final submit -> stays ``submitting`` for ``reconcile`` to resolve.

At most ``max_per_run`` (5) claims are processed per run, most urgent deadline first, with a random
pause (``jitter()``) between them.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from treinvertraging import notify as notify_mod
from treinvertraging.adapters import registry
from treinvertraging.adapters.base import ClaimantInfo, ClaimSubmitter
from treinvertraging.config import get_settings
from treinvertraging.db import session_scope
from treinvertraging.domain import ClaimState, SubmitResult
from treinvertraging.errors import BlockedByBotProtection, InvalidInput, LayoutChanged, LoginRequired, TakeoverTimeout
from treinvertraging.models import Account, Assessment, Claim, Event, Trip
from treinvertraging.pipeline.context import (
    MAX_ATTEMPTS,
    ClaimantIncomplete,
    add_event,
    claimant_for,
    debug_dir_of,
    find_debug_dir,
    local_today,
    short_error,
    trip_to_dto,
)

log = logging.getLogger(__name__)

MAX_PER_RUN = 5
FALL_THROUGH = (BlockedByBotProtection, LayoutChanged)
TRANSIENT = (LoginRequired, TakeoverTimeout)
DEADLINE_WARNING_DAYS = 14

SubmittersFactory = Callable[[Account], list[ClaimSubmitter]]
Sleep = Callable[[float], Awaitable[None]]


def default_jitter() -> float:
    return random.uniform(20.0, 75.0)


@dataclass
class ClaimRunResult:
    mode: str = ""
    dry_run: bool = False
    awaiting_approval: int = 0
    submitted: int = 0
    rehearsed: int = 0
    needs_human: int = 0
    retry_later: int = 0
    unresolved: int = 0  # left in ``submitting`` for reconcile
    expired: int = 0
    skipped: int = 0  # over the per-run cap
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.submitted} submitted",
            f"{self.rehearsed} rehearsed (dry run)",
            f"{self.awaiting_approval} awaiting approval",
            f"{self.needs_human} need a human",
            f"{self.retry_later} to retry",
            f"{self.unresolved} unresolved (reconcile)",
            f"{self.expired} expired",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} left for the next run (cap)")
        return f"claim [{self.mode}{' --dry-run' if self.dry_run else ''}]: " + ", ".join(parts)


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def approved_success_count(session) -> int:
    """Claims submitted successfully that a human approved (basis for ``AUTO_AFTER_N_APPROVED``)."""
    approved_keys = select(Event.trip_key).where(Event.kind == "claim.approved", Event.trip_key.is_not(None))
    return (
        session.scalar(
            select(func.count())
            .select_from(Claim)
            .where(
                Claim.state.in_(
                    (ClaimState.SUBMITTED.value, ClaimState.CONFIRMED.value, ClaimState.PAID.value)
                ),
                Claim.trip_key.in_(approved_keys),
            )
        )
        or 0
    )


def effective_mode(session, mode: str) -> str:
    if mode != "approve":
        return mode
    n = get_settings().AUTO_AFTER_N_APPROVED
    if n > 0 and approved_success_count(session) >= n:
        return "auto"
    return mode


def _latest_assessment(session, trip_key: str) -> Assessment | None:
    return session.scalars(
        select(Assessment)
        .where(Assessment.trip_key == trip_key)
        .order_by(Assessment.assessed_at.desc(), Assessment.id.desc())
        .limit(1)
    ).first()


def _claims_url() -> str:
    return get_settings().PUBLIC_URL.rstrip("/") + "/claims"


def _eur(value: object) -> str:
    return f"€ {Decimal(str(value or 0)):.2f}".replace(".", ",")


def _describe(trip: Trip, claim: Claim) -> str:
    return f"{trip.from_station}→{trip.to_station or '?'} ({_eur(claim.expected_eur)})"


def _reached_final(exc: BaseException) -> bool:
    return bool(getattr(exc, "reached_final_submit", False))


def _first_png(directory: str | None) -> str | None:
    if not directory:
        return None
    try:
        pngs = sorted(Path(directory).glob("*.png"))
    except OSError:
        return None
    return str(pngs[0]) if pngs else None


# ---------------------------------------------------------------------------------------------
# deadline handling
# ---------------------------------------------------------------------------------------------


async def run_deadline_watchdog(*, today: date | None = None, notify: bool = True) -> dict[str, list[str]]:
    """Expire claims whose deadline has passed and alert on those close to it.

    * ``eligible`` / ``awaiting_approval`` / ``needs_human`` claims past their deadline become
      ``expired`` (``approved`` ones are left alone: a human wants them tried).
    * Claims that are not yet submitted (eligible, awaiting_approval, approved, submitting,
      needs_human) with a deadline within 14 days trigger one loud notification.
    Returns ``{"expired": [...], "warned": [...]}`` (human readable lines).
    """
    today = today or local_today()
    expire_states = (
        ClaimState.ELIGIBLE.value,
        ClaimState.AWAITING_APPROVAL.value,
        ClaimState.NEEDS_HUMAN.value,
    )
    open_states = (*expire_states, ClaimState.APPROVED.value, ClaimState.SUBMITTING.value)
    expired: list[str] = []
    warned: list[str] = []
    with session_scope() as s:
        for claim in s.scalars(select(Claim).where(Claim.state.in_(open_states)).order_by(Claim.id)):
            a = _latest_assessment(s, claim.trip_key)
            if a is None or not a.eligible:
                continue
            trip = s.get(Trip, claim.trip_key)
            label = f"{trip.checkin_at:%d-%m} {_describe(trip, claim)}"
            if a.deadline < today and claim.state in expire_states:
                claim.state = ClaimState.EXPIRED.value
                claim.last_error = f"deadline {a.deadline.isoformat()} passed"
                expired.append(f"{label}: deadline was {a.deadline:%d-%m-%Y}")
                add_event(
                    s, "claim.expired", f"deadline {a.deadline} passed", level="error", trip_key=claim.trip_key
                )
            elif today <= a.deadline <= today + timedelta(days=DEADLINE_WARNING_DAYS):
                days = (a.deadline - today).days
                warned.append(f"{label}: nog {days} dag(en), status {claim.state}")
                add_event(
                    s,
                    "watchdog.deadline",
                    f"deadline {a.deadline} in {days} day(s), state {claim.state}",
                    level="warning",
                    trip_key=claim.trip_key,
                )
    if notify and expired:
        await notify_mod.notify(
            "Claim verlopen!",
            "De termijn is verstreken voor:\n" + "\n".join(f"- {line}" for line in expired),
            "error",
        )
    if notify and warned:
        await notify_mod.notify(
            "Claimdeadline nadert",
            "Nog niet ingediend, deadline binnen 14 dagen:\n"
            + "\n".join(f"- {line}" for line in warned)
            + f"\n{_claims_url()}",
            "error",
        )
    return {"expired": expired, "warned": warned}


# ---------------------------------------------------------------------------------------------
# submission
# ---------------------------------------------------------------------------------------------


@dataclass
class _Job:
    claim_id: int
    trip_key: str
    account: Account
    who: ClaimantInfo | None
    who_error: str | None
    label: str
    deadline: date | None


def _load_job(session, claim_id: int) -> _Job | None:
    claim = session.get(Claim, claim_id)
    if claim is None:
        return None
    trip = session.get(Trip, claim.trip_key)
    account = session.get(Account, trip.account_id)
    accounts = list(session.scalars(select(Account).where(Account.enabled.is_(True)).order_by(Account.id)))
    who, err = None, None
    try:
        who = claimant_for(account, accounts)
    except ClaimantIncomplete as exc:
        err = str(exc)
    a = _latest_assessment(session, claim.trip_key)
    return _Job(
        claim_id=claim.id,
        trip_key=claim.trip_key,
        account=account,
        who=who,
        who_error=err,
        label=f"{trip.checkin_at:%d-%m} {_describe(trip, claim)}",
        deadline=a.deadline if a else None,
    )


def _set_claim(claim_id: int, **fields) -> None:
    with session_scope() as s:
        claim = s.get(Claim, claim_id)
        for k, v in fields.items():
            setattr(claim, k, v)


async def _needs_human(job: _Job, reason: str, debug_dir: str | None) -> None:
    _set_claim(job.claim_id, state=ClaimState.NEEDS_HUMAN.value, last_error=reason, debug_dir=debug_dir)
    with session_scope() as s:
        add_event(s, "claim.needs_human", reason, level="error", trip_key=job.trip_key)
    await notify_mod.notify(
        "Claim vraagt om aandacht",
        f"{job.label}: {reason}\n{_claims_url()}",
        "error",
        attach=_first_png(debug_dir),
    )


async def _attempt(
    job: _Job,
    submitter_list: Sequence[ClaimSubmitter],
    *,
    dry_run: bool,
) -> tuple[SubmitResult | None, BaseException | None, str | None, bool]:
    """Run the channel ladder. Returns (result, error, debug_dir, ladder_exhausted)."""
    assert job.who is not None
    with session_scope() as s:
        trip = s.get(Trip, job.trip_key)
        dto = trip_to_dto(trip)
    last_error: BaseException | None = None
    debug_dir: str | None = None
    for submitter in submitter_list:
        started = datetime.now(UTC)
        if not dry_run:
            _set_claim(job.claim_id, channel=submitter.channel)
        try:
            result = await submitter.submit_claim(dto, job.who, dry_run=dry_run)
        except FALL_THROUGH as exc:
            debug_dir = debug_dir_of(exc) or find_debug_dir(started) or debug_dir
            last_error = exc
            if _reached_final(exc):
                return None, exc, debug_dir, False
            log.warning("channel %s failed (%s); trying the next channel", submitter.channel, type(exc).__name__)
            with session_scope() as s:
                add_event(
                    s,
                    "claim.channel_failed",
                    f"{submitter.channel}: {short_error(exc)}",
                    level="warning",
                    trip_key=job.trip_key,
                )
            continue
        except Exception as exc:  # noqa: BLE001 - InvalidInput, transient and unexpected errors end the ladder
            return None, exc, debug_dir_of(exc) or find_debug_dir(started), False
        return result, None, debug_dir_of(result) or find_debug_dir(started), False
    return None, last_error, debug_dir, True


async def _rehearse(job: _Job, submitter_list: Sequence[ClaimSubmitter], result: ClaimRunResult) -> None:
    if job.who is None:
        with session_scope() as s:
            add_event(
                s, "claim.dry_run", f"{job.label}: {job.who_error}", level="warning", trip_key=job.trip_key
            )
        result.notes.append(f"{job.label}: {job.who_error}")
        return
    res, err, debug_dir, _ = await _attempt(job, submitter_list, dry_run=True)
    with session_scope() as s:
        if res is not None:
            level = "info" if res.ok else "warning"
            msg = f"{job.label}: dry run via {res.channel}: {res.message}"
        else:
            level = "warning"
            msg = f"{job.label}: dry run failed: {short_error(err) if err else 'no channel available'}"
        add_event(s, "claim.dry_run", msg, level=level, trip_key=job.trip_key)
        if debug_dir:
            claim = s.get(Claim, job.claim_id)
            claim.debug_dir = debug_dir
    result.rehearsed += 1
    log.info("claim dry run: %s", msg)


async def _submit(job: _Job, submitter_list: Sequence[ClaimSubmitter], result: ClaimRunResult) -> None:
    if job.who is None:
        await _needs_human(job, job.who_error or "onvolledige gegevens", None)
        result.needs_human += 1
        return
    with session_scope() as s:
        claim = s.get(Claim, job.claim_id)
        claim.state = ClaimState.SUBMITTING.value
        claim.attempts = (claim.attempts or 0) + 1
        claim.channel = submitter_list[0].channel
        claim.last_error = None
        attempts = claim.attempts
        add_event(s, "claim.submitting", f"{job.label} (attempt {attempts})", trip_key=job.trip_key)

    res, err, debug_dir, _exhausted = await _attempt(job, submitter_list, dry_run=False)

    if res is not None and res.ok:
        _set_claim(
            job.claim_id,
            state=ClaimState.SUBMITTED.value,
            reference=res.reference,
            channel=res.channel or None,
            submitted_at=datetime.now(UTC),
            last_error=None,
            debug_dir=debug_dir,
        )
        with session_scope() as s:
            add_event(
                s, "claim.submitted", f"{job.label} via {res.channel}: {res.message}", trip_key=job.trip_key
            )
        result.submitted += 1
        await notify_mod.notify(
            "Claim ingediend",
            f"{job.label} via {res.channel}" + (f", referentie {res.reference}" if res.reference else ""),
            "success",
        )
        return

    if res is not None and res.reached_final_submit:
        # clicked submit but no confirmation: do NOT retry blindly, let reconcile look it up
        _set_claim(job.claim_id, last_error=f"no confirmation: {res.message}"[:500], debug_dir=debug_dir)
        with session_scope() as s:
            add_event(
                s,
                "claim.unconfirmed",
                f"{job.label}: submit clicked but not confirmed ({res.message})",
                level="warning",
                trip_key=job.trip_key,
            )
        result.unresolved += 1
        return

    if res is not None:  # ok=False and provably before the final submit
        reason = f"{res.channel}: {res.message}"[:500]
        await _retry_or_escalate(job, attempts, reason, debug_dir, result)
        return

    if err is None:
        await _needs_human(job, "geen indienkanaal beschikbaar", debug_dir)
        result.needs_human += 1
    elif isinstance(err, InvalidInput):
        await _needs_human(job, f"formulier weigert de gegevens: {err}", debug_dir)
        result.needs_human += 1
    elif isinstance(err, FALL_THROUGH):
        if _reached_final(err):
            _set_claim(job.claim_id, last_error=short_error(err), debug_dir=debug_dir)
            result.unresolved += 1
        else:
            await _needs_human(job, f"alle kanalen mislukt: {short_error(err)}", debug_dir)
            result.needs_human += 1
    elif isinstance(err, TRANSIENT) and not _reached_final(err):
        await _retry_or_escalate(job, attempts, short_error(err), debug_dir, result)
    else:
        # unexpected error: the submit may or may not have happened. Leave ``submitting``.
        log.exception("unexpected submit error for claim %s", job.claim_id, exc_info=err)
        _set_claim(job.claim_id, last_error=short_error(err), debug_dir=debug_dir)
        with session_scope() as s:
            add_event(
                s,
                "claim.unresolved",
                f"{job.label}: {short_error(err)}; left in submitting for reconcile",
                level="error",
                trip_key=job.trip_key,
            )
        result.unresolved += 1


async def _retry_or_escalate(
    job: _Job, attempts: int, reason: str, debug_dir: str | None, result: ClaimRunResult
) -> None:
    if attempts >= MAX_ATTEMPTS:
        await _needs_human(job, f"{reason} (na {attempts} pogingen)", debug_dir)
        result.needs_human += 1
        return
    _set_claim(job.claim_id, state=ClaimState.APPROVED.value, last_error=reason[:500], debug_dir=debug_dir)
    with session_scope() as s:
        add_event(
            s,
            "claim.retry_later",
            f"{job.label}: {reason}; attempt {attempts}/{MAX_ATTEMPTS}",
            level="warning",
            trip_key=job.trip_key,
        )
    result.retry_later += 1


async def run_claim(
    *,
    dry_run: bool = False,
    mode: str | None = None,
    submitters: Sequence[ClaimSubmitter] | None = None,
    submitters_factory: SubmittersFactory | None = None,
    jitter: Callable[[], float] | None = None,
    sleep: Sleep | None = None,
    max_per_run: int = MAX_PER_RUN,
    today: date | None = None,
    notify: bool = True,
) -> ClaimRunResult:
    """Apply the claim policy (see module docstring)."""
    today = today or local_today()
    settings = get_settings()
    base_mode = mode or settings.CLAIM_MODE
    jitter = jitter or default_jitter
    sleep = sleep or asyncio.sleep
    result = ClaimRunResult(dry_run=dry_run)

    if submitters is not None:
        fixed = list(submitters)
        submitters_factory = lambda _account: fixed  # noqa: E731
    elif submitters_factory is None:
        channels = settings.SUBMIT_CHANNELS

        def submitters_factory(account: Account) -> list[ClaimSubmitter]:
            return registry.submitters(channels, account=account)

    with session_scope() as s:
        run_mode = effective_mode(s, base_mode)
    result.mode = run_mode
    rehearse = dry_run or run_mode == "dry_run"

    # 1. expire what is too late, move eligible -> awaiting approval (approve mode)
    with session_scope() as s:
        eligible = list(
            s.scalars(select(Claim).where(Claim.state == ClaimState.ELIGIBLE.value).order_by(Claim.id))
        )
        keep: list[int] = []
        approval_lines: list[str] = []
        for claim in eligible:
            a = _latest_assessment(s, claim.trip_key)
            trip = s.get(Trip, claim.trip_key)
            if a is not None and a.deadline < today:
                claim.state = ClaimState.EXPIRED.value
                claim.last_error = f"deadline {a.deadline.isoformat()} passed"
                add_event(
                    s, "claim.expired", f"deadline {a.deadline} passed", level="error", trip_key=claim.trip_key
                )
                result.expired += 1
                continue
            if not rehearse and run_mode == "approve":
                claim.state = ClaimState.AWAITING_APPROVAL.value
                add_event(s, "claim.awaiting_approval", "waiting for approval", trip_key=claim.trip_key)
                approval_lines.append(f"{trip.checkin_at:%d-%m} {_describe(trip, claim)}")
                result.awaiting_approval += 1
            else:
                keep.append(claim.id)
    if result.expired and notify:
        await notify_mod.notify(
            "Claim verlopen!", f"{result.expired} claim(s) zijn verlopen (termijn verstreken).", "error"
        )
    if approval_lines and notify:
        await notify_mod.notify(
            "Claims wachten op goedkeuring",
            "\n".join(f"- {line}" for line in approval_lines) + f"\nKeur goed: {_claims_url()}",
            "info",
        )

    # 2. pick what to process now
    with session_scope() as s:
        states = [ClaimState.APPROVED.value]
        if rehearse or run_mode == "auto":
            states.append(ClaimState.ELIGIBLE.value)
        candidates = list(
            s.scalars(select(Claim).where(Claim.state.in_(states)).order_by(Claim.id))
        )
        queue: list[tuple[date, int]] = []
        for claim in candidates:
            if rehearse and not dry_run:
                # scheduled dry-run mode: one rehearsal per claim
                seen = s.scalar(
                    select(func.count())
                    .select_from(Event)
                    .where(Event.kind == "claim.dry_run", Event.trip_key == claim.trip_key)
                )
                if seen:
                    continue
            a = _latest_assessment(s, claim.trip_key)
            queue.append((a.deadline if a else date.max, claim.id))
        queue.sort()
    ids = [cid for _, cid in queue]
    result.skipped = max(len(ids) - max_per_run, 0)
    ids = ids[:max_per_run]

    # 3. process
    for n, claim_id in enumerate(ids):
        with session_scope() as s:
            job = _load_job(s, claim_id)
            fresh = s.get(Claim, claim_id)
            ok_state = fresh is not None and fresh.state in (
                ClaimState.APPROVED.value,
                ClaimState.ELIGIBLE.value,
            )
        if job is None or not ok_state:
            continue
        submitter_list = list(submitters_factory(job.account))
        if not submitter_list:
            with session_scope() as s:
                add_event(
                    s,
                    "claim.no_channel",
                    f"{job.label}: no submit channel available ({get_settings().SUBMIT_CHANNELS})",
                    level="error",
                    trip_key=job.trip_key,
                )
            result.notes.append(f"{job.label}: no submit channel")
            continue
        if n > 0:
            await sleep(jitter())
        if rehearse:
            await _rehearse(job, submitter_list, result)
        else:
            await _submit(job, submitter_list, result)

    with session_scope() as s:
        add_event(s, "claim.done", result.summary())
    return result
