"""Read-side queries for the web UI (DB only, no side effects)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from treinvertraging.config import get_settings
from treinvertraging.domain import ClaimState
from treinvertraging.models import Account, Assessment, Claim, Event, SettingKV, Trip

STATE_LABELS: dict[str, str] = {
    ClaimState.DETECTED: "Gevonden",
    ClaimState.INELIGIBLE: "Niet in aanmerking",
    ClaimState.ELIGIBLE: "In aanmerking",
    ClaimState.AWAITING_APPROVAL: "Wacht op goedkeuring",
    ClaimState.APPROVED: "Goedgekeurd",
    ClaimState.SUBMITTING: "Wordt ingediend",
    ClaimState.SUBMITTED: "Ingediend",
    ClaimState.CONFIRMED: "Bevestigd",
    ClaimState.PAID: "Uitbetaald",
    ClaimState.REJECTED: "Afgewezen",
    ClaimState.NEEDS_HUMAN: "Handmatige actie nodig",
    ClaimState.EXPIRED: "Verlopen",
}
STATE_LABELS = {str(k.value): v for k, v in STATE_LABELS.items()}

PENDING_STATES = (
    ClaimState.ELIGIBLE.value,
    ClaimState.AWAITING_APPROVAL.value,
    ClaimState.APPROVED.value,
    ClaimState.SUBMITTING.value,
)
CLAIMED_STATES = (
    ClaimState.SUBMITTED.value,
    ClaimState.CONFIRMED.value,
    ClaimState.PAID.value,
)
OPEN_FOR_DEADLINE = (*PENDING_STATES, ClaimState.NEEDS_HUMAN.value)

TRIP_FILTERS: dict[str, str] = {
    "all": "Alle",
    "eligible": "In aanmerking",
    "claimed": "Ingediend",
    "ineligible": "Niet in aanmerking",
}


def _dec(v: Any) -> Decimal:
    return Decimal(v or 0)


@dataclass
class DashboardStats:
    claimed_eur: Decimal = Decimal(0)
    paid_eur: Decimal = Decimal(0)
    pending_eur: Decimal = Decimal(0)
    counts: dict[str, int] = field(default_factory=dict)
    trips_total: int = 0
    awaiting_approval: int = 0
    deadlines: list[tuple[Trip, date, int]] = field(default_factory=list)
    canary: Event | None = None
    recent_events: list[Event] = field(default_factory=list)


def dashboard_stats(session: Session, *, today: date | None = None) -> DashboardStats:
    today = today or datetime.now(UTC).date()
    stats = DashboardStats()
    rows = session.execute(
        select(Claim.state, func.count(), func.coalesce(func.sum(Claim.expected_eur), 0),
               func.coalesce(func.sum(Claim.paid_eur), 0)).group_by(Claim.state)
    ).all()
    for state, count, expected, paid in rows:
        stats.counts[state] = count
        if state in CLAIMED_STATES:
            stats.claimed_eur += _dec(expected)
        if state == ClaimState.PAID.value:
            stats.paid_eur += _dec(paid)
        if state in PENDING_STATES:
            stats.pending_eur += _dec(expected)
    stats.awaiting_approval = stats.counts.get(ClaimState.AWAITING_APPROVAL.value, 0)
    stats.trips_total = session.scalar(select(func.count()).select_from(Trip)) or 0

    deadline_rows = session.execute(
        select(Trip, Assessment.deadline)
        .join(Assessment, Assessment.trip_key == Trip.key)
        .join(Claim, Claim.trip_key == Trip.key)
        .where(
            Assessment.eligible.is_(True),
            Claim.state.in_(OPEN_FOR_DEADLINE),
            Assessment.deadline <= today + timedelta(days=14),
        )
        .order_by(Assessment.deadline)
    ).all()
    seen: set[str] = set()
    for trip, deadline in deadline_rows:
        if trip.key in seen:
            continue
        seen.add(trip.key)
        stats.deadlines.append((trip, deadline, (deadline - today).days))

    stats.canary = session.scalars(
        select(Event).where(Event.kind.like("canary%")).order_by(Event.ts.desc(), Event.id.desc()).limit(1)
    ).first()
    stats.recent_events = list(
        session.scalars(select(Event).order_by(Event.ts.desc(), Event.id.desc()).limit(10))
    )
    return stats


@dataclass
class TripRow:
    trip: Trip
    assessment: Assessment | None
    claim: Claim | None


def list_trips(session: Session, flt: str = "all", *, limit: int = 500) -> list[TripRow]:
    stmt = (
        select(Trip)
        .outerjoin(Claim, Claim.trip_key == Trip.key)
        .options(selectinload(Trip.assessments), selectinload(Trip.claim))
        .order_by(Trip.checkin_at.desc())
        .limit(limit)
    )
    if flt == "eligible":
        stmt = stmt.where(
            Claim.state.in_(
                (
                    ClaimState.ELIGIBLE.value,
                    ClaimState.AWAITING_APPROVAL.value,
                    ClaimState.APPROVED.value,
                    ClaimState.NEEDS_HUMAN.value,
                )
            )
        )
    elif flt == "claimed":
        stmt = stmt.where(Claim.state.in_((ClaimState.SUBMITTING.value, *CLAIMED_STATES)))
    elif flt == "ineligible":
        stmt = stmt.where(Claim.state == ClaimState.INELIGIBLE.value)
    rows: list[TripRow] = []
    for trip in session.scalars(stmt).unique():
        latest = max(trip.assessments, key=lambda a: (a.assessed_at, a.id), default=None)
        rows.append(TripRow(trip, latest, trip.claim))
    return rows


def list_claims(session: Session, *, limit: int = 300) -> list[Claim]:
    stmt = (
        select(Claim)
        .options(selectinload(Claim.trip).selectinload(Trip.assessments))
        .order_by(Claim.updated_at.desc(), Claim.id.desc())
        .limit(limit)
    )
    claims = list(session.scalars(stmt))
    order = {ClaimState.AWAITING_APPROVAL.value: 0, ClaimState.NEEDS_HUMAN.value: 1}
    claims.sort(key=lambda c: order.get(c.state, 2))  # stable: keeps updated_at order within group
    return claims


def latest_assessment(trip: Trip) -> Assessment | None:
    return max(trip.assessments, key=lambda a: (a.assessed_at, a.id), default=None)


def list_accounts(session: Session) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.id)))


# --- debug bundles -----------------------------------------------------------------------


def debug_root() -> Path:
    return (get_settings().DATA_DIR / "debug").resolve()


def safe_debug_path(rel: str) -> Path | None:
    """Resolve ``rel`` below the debug root; ``None`` if it escapes the root or does not exist."""
    root = debug_root()
    try:
        target = (root / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if target != root and not target.is_relative_to(root):
        return None
    return target if target.exists() else None


def debug_rel(debug_dir: str | None) -> str | None:
    """Relative path (below the debug root) for a ``Claim.debug_dir`` value, if valid."""
    if not debug_dir:
        return None
    root = debug_root()
    p = Path(debug_dir)
    candidate = p if p.is_absolute() else root / p
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved.relative_to(root).as_posix()


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def debug_files(debug_dir: str | None, *, limit: int = 30) -> list[dict[str, Any]]:
    rel = debug_rel(debug_dir)
    if rel is None:
        return []
    target = safe_debug_path(rel)
    if target is None or not target.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(target.iterdir()):
        if f.is_file():
            out.append(
                {
                    "name": f.name,
                    "url": f"/debug/{rel}/{f.name}",
                    "image": f.suffix.lower() in IMAGE_SUFFIXES,
                }
            )
        if len(out) >= limit:
            break
    return out


# --- settings_kv (IMAP settings for the OTP provider) ----------------------------------------

IMAP_KEYS = ("host", "port", "user", "folder", "senders", "code_regex")


def kv_get_all(session: Session, prefix: str) -> dict[str, str]:
    rows = session.scalars(select(SettingKV).where(SettingKV.key.like(f"{prefix}%")))
    return {r.key[len(prefix) :]: r.value or "" for r in rows}


def kv_set(session: Session, key: str, value: str | None) -> None:
    row = session.get(SettingKV, key)
    if row is None:
        session.add(SettingKV(key=key, value=value))
    else:
        row.value = value
