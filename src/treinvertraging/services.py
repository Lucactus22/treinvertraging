"""Thin facade between the web UI and the pipeline.

WP-E owns the signatures. Most functions are stubs that raise ``NotImplementedError``;
WP-G replaces the bodies (keep the signatures). The web routes catch ``NotImplementedError``
and show a flash message instead of failing.

``approve_claim`` / ``reject_claim`` are simple DB state transitions and are implemented here.
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
    "run_sync",
    "start_calibration",
    "test_login",
]


class ClaimNotFound(LookupError):
    """No claim with that id."""


class InvalidTransition(ValueError):
    """The claim is not in a state from which the requested transition is allowed."""


# ---------------------------------------------------------------------------
# Pipeline runs (WP-G implements). Each returns a short human readable summary.
# ---------------------------------------------------------------------------


async def run_sync() -> str:
    """Run the trip sync for every enabled account now. Returns a one-line summary."""
    raise NotImplementedError("services.run_sync is implemented by WP-G")


async def run_assess() -> str:
    """Assess all ``detected`` trips now. Returns a one-line summary."""
    raise NotImplementedError("services.run_assess is implemented by WP-G")


async def run_claim() -> str:
    """Apply the claim policy (dry_run/approve/auto) to eligible claims now."""
    raise NotImplementedError("services.run_claim is implemented by WP-G")


async def run_canary() -> str:
    """Run the read-only canary (login + 1-day list_trips) now."""
    raise NotImplementedError("services.run_canary is implemented by WP-G")


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


async def retry_claim(claim_id: int) -> str:
    """Re-queue a failed claim (``needs_human``/``rejected``...) for another submission attempt.

    WP-G decides which source states are allowed and what happens to ``attempts``.
    """
    raise NotImplementedError("services.retry_claim is implemented by WP-G")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def test_login(account_id: int) -> str:
    """Try ``ensure_logged_in`` for the account. Returns a message; raises on failure."""
    raise NotImplementedError("services.test_login is implemented by WP-G")


async def start_calibration(account_id: int, channel: str) -> str:
    """Start the calibration walkthrough for an account.

    ``channel`` is ``"ns"``, ``"ovchip"`` or ``"gtbv"``. Returns a message telling the user
    where to continue (noVNC).
    """
    raise NotImplementedError("services.start_calibration is implemented by WP-G")


# ---------------------------------------------------------------------------
# Scheduler info
# ---------------------------------------------------------------------------


async def next_runs() -> list[dict[str, Any]]:
    """Upcoming scheduled jobs, soonest first: ``[{"job": str, "next_run": datetime | None}, ...]``."""
    raise NotImplementedError("services.next_runs is implemented by WP-G")
