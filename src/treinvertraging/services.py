"""Facade between the web UI / CLI and the pipeline (signatures owned by WP-E, bodies by WP-G).

Pipeline runs take the process-wide pipeline lock, so a manual "run now" never overlaps with the
daily run (or another manual run) and no claim can be submitted twice concurrently.
Each ``run_*`` returns a short human readable summary.
"""

from __future__ import annotations

from typing import Any

from treinvertraging.db import session_scope
from treinvertraging.domain import ClaimState
from treinvertraging.models import Claim, Event

__all__ = [
    "ClaimNotFound",
    "InvalidTransition",
    "approve_claim",
    "next_runs",
    "reject_claim",
    "retry_claim",
    "run_assess",
    "run_canary",
    "run_claim",
    "run_reconcile",
    "run_sync",
    "start_calibration",
    "test_login",
]


class ClaimNotFound(LookupError):
    """No claim with that id."""


class InvalidTransition(ValueError):
    """The claim is not in a state from which the requested transition is allowed."""


# ---------------------------------------------------------------------------
# Pipeline runs. Each returns a short human readable summary.
# ---------------------------------------------------------------------------


async def run_sync() -> str:
    """Run the trip sync for every enabled account now. Returns a one-line summary."""
    from treinvertraging.pipeline import sync
    from treinvertraging.pipeline.context import pipeline_lock

    async with pipeline_lock():
        return (await sync.run_sync()).summary()


async def run_assess() -> str:
    """Assess all ``detected`` trips now. Returns a one-line summary."""
    from treinvertraging.pipeline import assess
    from treinvertraging.pipeline.context import pipeline_lock

    async with pipeline_lock():
        return (await assess.run_assess()).summary()


async def run_claim(*, dry_run: bool = False) -> str:
    """Apply the claim policy (dry_run/approve/auto) to eligible claims now.

    ``dry_run=True`` (CLI ``claim --dry-run``) fills the forms but never clicks the final submit.
    """
    from treinvertraging.pipeline import claim
    from treinvertraging.pipeline.context import pipeline_lock

    async with pipeline_lock():
        return (await claim.run_claim(dry_run=dry_run)).summary()


async def run_canary() -> str:
    """Run the read-only canary (login + 1-day list_trips) now."""
    from treinvertraging.pipeline import canary

    return (await canary.run_canary()).summary()


async def run_reconcile() -> str:
    """Compare submitted claims with what NS shows (mijn aanvragen / confirmation mails)."""
    from treinvertraging.pipeline import reconcile
    from treinvertraging.pipeline.context import pipeline_lock

    async with pipeline_lock():
        return (await reconcile.run_reconcile()).summary()


# ---------------------------------------------------------------------------
# Claim transitions
# ---------------------------------------------------------------------------


def _transition(claim_id: int, expected: ClaimState, new: ClaimState, kind: str) -> str:
    with session_scope() as s:
        claim = s.get(Claim, claim_id)
        if claim is None:
            raise ClaimNotFound(f"claim {claim_id} not found")
        if claim.state != expected:
            raise InvalidTransition(
                f"claim {claim_id} is '{claim.state}', expected '{expected.value}' for {kind}"
            )
        claim.state = new.value
        message = f"claim {claim_id}: {expected.value} -> {new.value}"
        s.add(Event(level="info", kind=kind, message=message, trip_key=claim.trip_key))
    return new.value


async def approve_claim(claim_id: int) -> str:
    """``awaiting_approval`` -> ``approved``. Returns the new state.

    Raises ``ClaimNotFound`` / ``InvalidTransition``. The next claim run submits it.
    """
    return _transition(claim_id, ClaimState.AWAITING_APPROVAL, ClaimState.APPROVED, "claim.approved")


async def reject_claim(claim_id: int) -> str:
    """``awaiting_approval`` -> ``rejected`` (rejected by the user, never submitted)."""
    return _transition(claim_id, ClaimState.AWAITING_APPROVAL, ClaimState.REJECTED, "claim.rejected")


RETRYABLE_STATES = (ClaimState.NEEDS_HUMAN, ClaimState.REJECTED, ClaimState.EXPIRED)


async def retry_claim(claim_id: int) -> str:
    """Re-queue a failed claim for another submission attempt: -> ``approved``, ``attempts`` reset to 0.

    Allowed from ``needs_human``, ``rejected`` (by you or by NS) and ``expired``: pressing retry is an
    explicit decision, so the claim is treated as approved. The next claim run (any mode) submits it;
    ``approved`` claims are not expired again by the deadline watchdog, so a late claim can still be
    tried (NS may refuse it). Raises ``ClaimNotFound`` / ``InvalidTransition``.
    """
    with session_scope() as s:
        claim = s.get(Claim, claim_id)
        if claim is None:
            raise ClaimNotFound(f"claim {claim_id} not found")
        if claim.state not in {st.value for st in RETRYABLE_STATES}:
            raise InvalidTransition(
                f"claim {claim_id} is '{claim.state}'; retry is only possible from "
                + ", ".join(st.value for st in RETRYABLE_STATES)
            )
        old = claim.state
        claim.state = ClaimState.APPROVED.value
        claim.attempts = 0
        claim.last_error = None
        s.add(
            Event(
                level="info",
                kind="claim.retry",
                message=f"claim {claim_id}: {old} -> approved (retry, attempts reset)",
                trip_key=claim.trip_key,
            )
        )
    return ClaimState.APPROVED.value


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def test_login(account_id: int) -> str:
    """Try ``ensure_logged_in`` for the account. Returns a message; raises on failure."""
    from treinvertraging.adapters import registry
    from treinvertraging.models import Account

    with session_scope() as s:
        account = s.get(Account, account_id)
    if account is None:
        raise LookupError(f"account {account_id} not found")
    source = registry.trip_source_for(account)
    await source.ensure_logged_in()
    return f"Ingelogd bij '{account.label}'."


async def start_calibration(account_id: int, channel: str) -> str:
    """Start the calibration walkthrough for an account (``ns``, ``ovchip`` or ``gtbv``).

    Not implemented yet: WP-J wires this to the adapters' ``calibrate()`` methods.
    """
    raise NotImplementedError("services.start_calibration is wired up by WP-J")


# ---------------------------------------------------------------------------
# Scheduler info
# ---------------------------------------------------------------------------


async def next_runs() -> list[dict[str, Any]]:
    """Upcoming scheduled jobs, soonest first: ``[{"job": str, "next_run": datetime | None}, ...]``.

    Empty when no scheduler runs in this process (e.g. a one-off CLI command).
    """
    from treinvertraging.pipeline.scheduler import get_scheduler

    sched = get_scheduler()
    return sched.next_runs() if sched is not None else []
