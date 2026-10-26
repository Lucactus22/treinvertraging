"""Notifications via Apprise plus the daily/weekly digest text."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from treinvertraging.config import get_settings
from treinvertraging.domain import ClaimState
from treinvertraging.models import Assessment, Claim, Event, Trip

log = logging.getLogger(__name__)

# Our level names -> Apprise notify types.
_LEVEL_TO_TYPE = {
    "info": "info",
    "success": "success",
    "warning": "warning",
    "warn": "warning",
    "error": "failure",
    "critical": "failure",
    "failure": "failure",
}

_PENDING_STATES = (
    ClaimState.ELIGIBLE,
    ClaimState.AWAITING_APPROVAL,
    ClaimState.APPROVED,
    ClaimState.SUBMITTING,
)
_FINAL_STATES = (
    ClaimState.SUBMITTED,
    ClaimState.CONFIRMED,
    ClaimState.PAID,
    ClaimState.REJECTED,
    ClaimState.EXPIRED,
    ClaimState.INELIGIBLE,
)


def _eur(value: Decimal | float | int | None) -> str:
    amount = Decimal(value or 0).quantize(Decimal("0.01"))
    return f"€ {amount:.2f}".replace(".", ",")


async def notify(
    title: str,
    body: str,
    level: str = "info",
    attach: str | Path | Sequence[str | Path] | None = None,
) -> bool:
    """Send a notification through every URL in ``APPRISE_URLS``.

    Never raises. Returns True when at least one service accepted the message. With no URLs
    configured it logs the message and returns False.
    """
    urls = get_settings().apprise_urls
    if not urls:
        log.info("notify (no APPRISE_URLS configured) [%s] %s: %s", level, title, body)
        return False
    try:
        import apprise

        ap = apprise.Apprise()
        added = 0
        for url in urls:
            if ap.add(url):
                added += 1
            else:
                log.warning("Apprise: ignoring invalid notification URL (scheme %s)", url.split(":", 1)[0])
        if not added:
            log.warning("notify: no valid Apprise URL, dropping [%s] %s", level, title)
            return False
        attachments: list[str] | None = None
        if attach is not None:
            items = [attach] if isinstance(attach, str | Path) else list(attach)
            attachments = [str(p) for p in items if Path(p).exists()] or None
        ok = await ap.async_notify(
            title=title,
            body=body,
            notify_type=_LEVEL_TO_TYPE.get(level.lower(), "info"),
            attach=attachments,
        )
        if not ok:
            log.warning("notify: Apprise reported a failure for %r", title)
        return bool(ok)
    except Exception:
        log.exception("notify: sending %r failed", title)
        return False


def build_digest(session: Session, *, since: datetime | None = None, now: datetime | None = None) -> str:
    """Plain-text digest (Dutch): new eligible trips, euros expected/paid, failures, deadlines."""
    now = now or datetime.now(UTC)
    since = since or now - timedelta(days=1)
    lines: list[str] = ["Treinvertraging overzicht", ""]

    # New eligible assessments since the last digest.
    new_rows = session.execute(
        select(Assessment, Trip)
        .join(Trip, Trip.key == Assessment.trip_key)
        .where(Assessment.eligible.is_(True), Assessment.assessed_at >= since)
        .order_by(Assessment.assessed_at)
    ).all()
    seen: set[str] = set()
    new_total = Decimal(0)
    new_lines: list[str] = []
    for a, t in new_rows:
        if a.trip_key in seen:
            continue
        seen.add(a.trip_key)
        new_total += Decimal(a.expected_eur or 0)
        delay = f"{a.arrival_delay_min} min" if a.arrival_delay_min is not None else "geannuleerd"
        new_lines.append(
            f"- {t.checkin_at:%d-%m %H:%M} {t.from_station}→{t.to_station or '?'}: "
            f"{delay}, {a.percent}% = {_eur(a.expected_eur)}"
        )
    lines.append(f"Nieuwe in aanmerking komende reizen: {len(new_lines)} ({_eur(new_total)})")
    lines.extend(new_lines)

    claims = list(session.scalars(select(Claim)))
    pending = sum((Decimal(c.expected_eur or 0) for c in claims if c.state in _PENDING_STATES), Decimal(0))
    claimed = sum(
        (
            Decimal(c.expected_eur or 0)
            for c in claims
            if c.state in (ClaimState.SUBMITTED, ClaimState.CONFIRMED)
        ),
        Decimal(0),
    )
    paid = sum(
        (Decimal(c.paid_eur or 0) for c in claims if c.state == ClaimState.PAID),
        Decimal(0),
    )
    lines += [
        "",
        f"Nog in te dienen / te keuren: {_eur(pending)}",
        f"Ingediend, wacht op uitbetaling: {_eur(claimed)}",
        f"Uitbetaald: {_eur(paid)}",
    ]

    awaiting = [c for c in claims if c.state == ClaimState.AWAITING_APPROVAL]
    if awaiting:
        lines += ["", f"Wacht op jouw goedkeuring: {len(awaiting)} claim(s)"]

    failures = [c for c in claims if c.state in (ClaimState.NEEDS_HUMAN, ClaimState.REJECTED)]
    errors = list(
        session.scalars(
            select(Event).where(Event.ts >= since, Event.level.in_(("error", "critical"))).order_by(Event.ts)
        )
    )
    if failures or errors:
        lines += ["", "Aandachtspunten:"]
        for c in failures:
            label = "afgewezen" if c.state == ClaimState.REJECTED else "handmatige actie nodig"
            lines.append(f"- claim #{c.id}: {label}" + (f" ({c.last_error})" if c.last_error else ""))
        for e in errors[:10]:
            lines.append(f"- {e.ts:%d-%m %H:%M} {e.kind}: {e.message}")

    soon = _deadline_warnings(session, today=now.date())
    if soon:
        lines += ["", "Deadlines binnen 14 dagen:"]
        lines += [f"- {t.checkin_at:%d-%m} {t.from_station}→{t.to_station or '?'}: uiterlijk {d:%d-%m-%Y}" for t, d in soon]

    return "\n".join(lines)


def _deadline_warnings(session: Session, *, today: date) -> list[tuple[Trip, date]]:
    """Eligible trips whose claim is not yet confirmed and whose deadline is within 14 days."""
    open_states = (
        ClaimState.ELIGIBLE,
        ClaimState.AWAITING_APPROVAL,
        ClaimState.APPROVED,
        ClaimState.SUBMITTING,
        ClaimState.NEEDS_HUMAN,
    )
    rows = session.execute(
        select(Trip, Assessment.deadline)
        .join(Assessment, Assessment.trip_key == Trip.key)
        .join(Claim, Claim.trip_key == Trip.key)
        .where(
            Assessment.eligible.is_(True),
            Claim.state.in_(open_states),
            Assessment.deadline <= today + timedelta(days=14),
            Assessment.deadline >= today,
        )
        .order_by(Assessment.deadline)
    ).all()
    out: dict[str, tuple[Trip, date]] = {}
    for t, d in rows:
        out.setdefault(t.key, (t, d))
    return list(out.values())
