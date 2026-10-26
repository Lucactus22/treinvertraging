"""Match a trip to the train it arrived on and derive the arrival delay at the destination.

Heuristic (``match``)
=====================
Input: a ``TripDTO`` and a SQLAlchemy ``Session`` holding the ``arrivals`` table (filled by
``rdt.py`` and ``archiver.py``). The delay that counts for compensation is the delay at the
*final destination* (``trip.to_station``), so we look for the arrival there.

1. No destination, or no check-out time: the train cannot be identified (the arrivals table
   has no origin, so any arrival after check-in could be it). Result: ``delay_min=None``.
2. Candidate arrivals are rows at ``to_station`` with a planned time in
   ``[checkin - 3h, checkout + 5min]``. The planned time is deliberately *not* required to be
   after check-in: a badly delayed train is boarded after its planned arrival time at the
   destination would already have passed. Instead the *effective* time (actual arrival; for
   cancelled trains the planned time) must satisfy ``checkin + 5min <= effective <= checkout + 5min``
   (a train cannot arrive before the minimum ride time, nor after the traveller checked out;
   the 5 minutes absorb clock differences between OV-chip readers and the timetable).
3. Ranking: the traveller checks out shortly after the train arrives, so the best candidate
   is the one whose effective time is closest before check-out (smallest ``gap = checkout -
   effective``, with a small negative gap tolerated). Candidates more than 30 minutes before
   check-out are dropped, and gaps above 15 minutes are flagged as low confidence in the
   evidence. A cancelled train only wins if it fits more than 3 minutes better than the best
   train that actually ran, so a cancelled train followed by a replacement the traveller took
   is not reported as cancelled (the evidence mentions the cancelled predecessor instead).
4. Result: ``delay_min`` is the stored arrival delay of the chosen train (or
   ``actual - planned`` if only times are known); ``cancelled`` is its cancelled flag, and
   ``delay_min`` is None for a cancelled train (the stored delay of a cancelled stop is noise).
   As a cross-check the check-out implied delay ``checkout - planned - 4min walking slack`` is
   compared with the stored delay; a difference over 20 minutes adds a mismatch note to the
   evidence (the stored delay is still used, it is the authoritative number).
5. No matching row: ``delay_min=None``, ``cancelled=False``, evidence says whether the station
   has any arrival data around that time at all (no data at all vs. no fitting train).

Known limitation: arrivals are not tied to an origin station, so two trains arriving within a
few minutes of each other at a large station can be confused. The ranking prefers the arrival
closest before check-out, which is right for the common case; the evidence lists the
alternative when two candidates are within 3 minutes of each other.

Evidence strings look like ``rdt:2025-09 train 3045 UT->ASD planned 14:30 +34`` (times in the
configured timezone).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from treinvertraging.config import get_settings
from treinvertraging.domain import TripDTO
from treinvertraging.models import Arrival

log = logging.getLogger(__name__)

MIN_RIDE = timedelta(minutes=5)
CHECKOUT_SLACK = timedelta(minutes=5)
EARLY_PLANNED_BOUND = timedelta(hours=3)
MAX_GAP = timedelta(minutes=30)
LOW_CONFIDENCE_GAP = timedelta(minutes=15)
WALK_SLACK_MIN = 4
CROSSCHECK_TOLERANCE_MIN = 20
AMBIGUITY_WINDOW = timedelta(minutes=3)
CANCELLED_PREDECESSOR_WINDOW = timedelta(minutes=45)


@dataclass
class MatchResult:
    delay_min: int | None
    cancelled: bool
    evidence: list[str] = field(default_factory=list)


def _aware(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=tz)


def _effective(a: Arrival) -> datetime:
    """When the train actually arrived (planned time if cancelled or unknown)."""
    if a.cancelled:
        return a.planned_at
    if a.actual_at is not None:
        return a.actual_at
    if a.delay_min is not None:
        return a.planned_at + timedelta(minutes=a.delay_min)
    return a.planned_at


def _delay(a: Arrival) -> int | None:
    if a.delay_min is not None:
        return a.delay_min
    if a.actual_at is not None:
        return round((a.actual_at - a.planned_at).total_seconds() / 60)
    return None


def _describe(a: Arrival, trip: TripDTO, tz: ZoneInfo) -> str:
    planned_local = a.planned_at.astimezone(tz)
    prefix = f"{a.source}:{planned_local:%Y-%m}"
    base = f"{prefix} train {a.train_number} {trip.from_station}->{a.station} planned {planned_local:%H:%M}"
    if a.cancelled:
        return f"{base} cancelled"
    delay = _delay(a)
    return f"{base} {delay:+d}" if delay is not None else f"{base} (delay unknown)"


def match(trip: TripDTO, session: Session) -> MatchResult:
    """Find the arrival at the trip's destination and return its delay. See the module docstring."""
    tz = ZoneInfo(get_settings().TIMEZONE)
    to_station = (trip.to_station or "").strip().upper()
    if not to_station:
        return MatchResult(None, False, ["no destination station on trip; cannot look up arrival"])
    if trip.checkout_at is None:
        return MatchResult(
            None, False, [f"no check-out time; cannot identify the train arriving at {to_station}"]
        )

    checkin = _aware(trip.checkin_at, tz)
    checkout = _aware(trip.checkout_at, tz)
    if checkout < checkin:
        return MatchResult(None, False, ["check-out is before check-in; cannot match"])

    rows = list(
        session.execute(
            select(Arrival)
            .where(
                Arrival.station == to_station,
                Arrival.planned_at >= checkin - EARLY_PLANNED_BOUND,
                Arrival.planned_at <= checkout + CHECKOUT_SLACK,
            )
            .order_by(Arrival.planned_at)
        ).scalars()
    )
    if not rows:
        return MatchResult(None, False, [f"no arrival data for {to_station} around {checkin:%Y-%m-%d %H:%M}"])

    scored: list[tuple[timedelta, bool, Arrival]] = []
    for a in rows:
        eff = _aware(_effective(a), tz)
        if eff < checkin + MIN_RIDE or eff > checkout + CHECKOUT_SLACK:
            continue
        gap = max(checkout - eff, timedelta(0))
        if gap > MAX_GAP:
            continue
        scored.append((gap, a.cancelled, a))
    if not scored:
        return MatchResult(
            None,
            False,
            [f"{len(rows)} arrival(s) at {to_station} nearby, none fits check-in/out window"],
        )

    # Smallest gap wins; among near-equal gaps prefer a train that actually ran.
    scored.sort(key=lambda t: (t[0], t[1]))
    best_gap, _, best = scored[0]
    # A cancelled train beats a running one only if it is clearly the closest fit.
    running = [s for s in scored if not s[1]]
    if best.cancelled and running and running[0][0] - best_gap <= AMBIGUITY_WINDOW:
        best_gap, _, best = running[0]

    evidence = [_describe(best, trip, tz)]
    if best_gap > LOW_CONFIDENCE_GAP:
        evidence.append(f"low confidence: train arrived {int(best_gap.total_seconds() // 60)} min before check-out")
    others = [s for s in scored if s[2].id != best.id and s[0] - best_gap <= AMBIGUITY_WINDOW]
    if others:
        evidence.append("ambiguous: also close to check-out: " + "; ".join(_describe(s[2], trip, tz) for s in others))

    delay = None if best.cancelled else _delay(best)
    if best.cancelled:
        evidence.append(f"arrival at {to_station} cancelled")
    else:
        planned = _aware(best.planned_at, tz)
        implied = round((checkout - planned).total_seconds() / 60) - WALK_SLACK_MIN
        if delay is not None and abs(implied - delay) > CROSSCHECK_TOLERANCE_MIN:
            evidence.append(f"cross-check mismatch: check-out implies about +{implied} min, data says {delay:+d}")
        elif delay is not None:
            evidence.append(f"check-out consistent (implies about {implied:+d} min)")
        # cancelled predecessor: a train that did not run shortly before the one taken
        for a in rows:
            if a.cancelled and a.id != best.id:
                pa = _aware(a.planned_at, tz)
                if timedelta(0) < planned - pa <= CANCELLED_PREDECESSOR_WINDOW:
                    evidence.append(f"note: {_describe(a, trip, tz)} (earlier train cancelled, not counted)")

    return MatchResult(delay, best.cancelled, evidence)
