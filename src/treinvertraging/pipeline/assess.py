"""Assess ``detected`` trips: refresh delay data, match the train, apply the rules, set the state.

Which claims are looked at
    * every claim in state ``detected``;
    * ``ineligible`` claims whose latest assessment says ``no_delay_data`` or ``no_checkout`` and whose
      deadline has not passed yet, because the delay data (RDT publishes a month after it ended, the NS
      API archive only starts when the app runs) or the check-out may arrive later.

Delay data
    The stations needed are all ``to_station`` values of the trips being assessed. For every month
    those trips touch (check-in and check-out month) the RDT file is imported once per
    (month, station set); the imported stations are remembered in ``settings_kv`` under
    ``rdt.imported.YYYY-MM``. The *current* month is skipped (RDT publishes it after the month ends)
    and ``RdtNotAvailable`` is caught: the matcher then relies on the ``arrivals`` rows written by the NS API
    archiver, and when that has nothing either, ``rules.assess`` falls back to ``ns_delay_flag``.

Outcome of a trip
    ``eligible`` -> claim ``eligible``; reason ``deadline_passed:`` -> claim ``expired``; anything else
    -> ``ineligible``. A new ``assessments`` row is written only when the verdict changed.

Deduplication (NS vs OV-chipkaart)
    An NS trip and an OV trip are the same journey when the cards match and check-in is within
    2 minutes at the same from-station. Cards match when the ids are equal, the last 4 digits are equal,
    or one id is not a card number (an NS account id) and the NS account's stored card number ends in the
    same 4 digits. The NS trip is canonical: the OV duplicate becomes ``ineligible`` with reason
    ``duplicate:`` and the NS assessment gets ``corroborated_by:ovchip:<key>`` evidence. If the OV claim
    was already approved/submitted, the NS trip becomes the duplicate instead (never claim twice).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from treinvertraging import crypto
from treinvertraging import rules as rules_mod
from treinvertraging.db import session_scope
from treinvertraging.delaydata import matcher, rdt
from treinvertraging.domain import ClaimState, Source
from treinvertraging.models import Account, Assessment, Claim, SettingKV, Trip
from treinvertraging.pipeline.context import (
    add_event,
    as_utc,
    digits,
    local_today,
    local_tz,
    short_error,
    trip_to_dto,
)

log = logging.getLogger(__name__)

REASSESS_CODES = ("no_delay_data", "no_checkout")
DEDUP_WINDOW = timedelta(minutes=2)
RDT_KV_PREFIX = "rdt.imported."

RdtImport = Callable[[int, int, set[str]], Awaitable[object]]


@dataclass
class AssessResult:
    assessed: int = 0
    eligible: int = 0
    ineligible: int = 0
    expired: int = 0
    duplicates: int = 0
    rdt_imported: list[str] = field(default_factory=list)
    rdt_skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"assess: {self.assessed} trip(s): {self.eligible} eligible, {self.ineligible} ineligible, "
            f"{self.expired} expired, {self.duplicates} duplicate(s)"
        )


# ---------------------------------------------------------------------------------------------
# RDT import bookkeeping
# ---------------------------------------------------------------------------------------------


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _imported_stations(session: Session, year: int, month: int) -> set[str]:
    row = session.get(SettingKV, RDT_KV_PREFIX + _month_key(year, month))
    return {s for s in (row.value or "").split(",") if s} if row else set()


def _remember_import(session: Session, year: int, month: int, stations: set[str]) -> None:
    key = RDT_KV_PREFIX + _month_key(year, month)
    value = ",".join(sorted(stations | _imported_stations(session, year, month)))
    row = session.get(SettingKV, key)
    if row is None:
        session.add(SettingKV(key=key, value=value))
    else:
        row.value = value


def months_for_trips(trips: Iterable[Trip]) -> set[tuple[int, int]]:
    tz = local_tz()
    months: set[tuple[int, int]] = set()
    for t in trips:
        for dt in (t.checkin_at, t.checkout_at):
            if dt is not None:
                local = as_utc(dt).astimezone(tz)
                months.add((local.year, local.month))
    return months


async def _default_rdt_import(year: int, month: int, stations: set[str]) -> object:
    return await rdt.import_month(year, month, stations)


async def ensure_rdt(
    months: Iterable[tuple[int, int]],
    stations: set[str],
    *,
    today: date | None = None,
    rdt_import: RdtImport | None = None,
    force: bool = False,
) -> tuple[list[str], list[str]]:
    """Import RDT data for ``months`` x ``stations`` unless already done. Returns (imported, skipped)."""
    today = today or local_today()
    do_import = rdt_import or _default_rdt_import
    imported: list[str] = []
    skipped: list[str] = []
    stations = {s.strip().upper() for s in stations if s and s.strip()}
    if not stations:
        return imported, skipped
    for year, month in sorted(set(months)):
        label = _month_key(year, month)
        if (year, month) >= (today.year, today.month):
            skipped.append(f"{label} (current month, not published yet)")
            continue
        with session_scope() as s:
            have = _imported_stations(s, year, month)
        if not force and stations <= have:
            continue
        try:
            await do_import(year, month, stations)
        except rdt.RdtNotAvailable as exc:
            log.info("RDT %s not available: %s", label, exc)
            skipped.append(f"{label} (not available)")
            continue
        except Exception as exc:  # noqa: BLE001 - network problems must not block the assessment
            log.warning("RDT import %s failed: %s", label, short_error(exc))
            with session_scope() as s:
                add_event(s, "assess.rdt_failed", f"RDT {label}: {short_error(exc)}", level="warning")
            skipped.append(f"{label} (failed)")
            continue
        with session_scope() as s:
            _remember_import(s, year, month, stations)
        imported.append(label)
    return imported, skipped


# ---------------------------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------------------------


def _account_card_last4(session: Session, account_id: int) -> str:
    acc = session.get(Account, account_id)
    if acc is None or not acc.card_number_enc:
        return ""
    try:
        return digits(crypto.decrypt(acc.card_number_enc))[-4:]
    except Exception:  # noqa: BLE001
        return ""


def same_card(session: Session, a: Trip, b: Trip) -> bool:
    if a.card_id == b.card_id:
        return True
    da, db = digits(a.card_id), digits(b.card_id)
    if len(da) >= 4 and len(db) >= 4:
        return da[-4:] == db[-4:]
    # one side is an account id: compare through the stored card number of the account
    for card_side, other in ((a, b), (b, a)):
        if len(digits(card_side.card_id)) >= 4 and len(digits(other.card_id)) < 4:
            last4 = _account_card_last4(session, other.account_id)
            if last4 and last4 == digits(card_side.card_id)[-4:]:
                return True
    return False


def find_counterpart(session: Session, trip: Trip) -> Trip | None:
    """The trip of the *other* source that describes the same journey, if any."""
    other = Source.OVCHIP.value if trip.source == Source.NS.value else Source.NS.value
    cin = as_utc(trip.checkin_at)
    candidates = session.scalars(
        select(Trip).where(
            Trip.source == other,
            Trip.from_station == trip.from_station,
            Trip.checkin_at >= cin - DEDUP_WINDOW,
            Trip.checkin_at <= cin + DEDUP_WINDOW,
            Trip.key != trip.key,
        )
    ).all()
    for c in candidates:
        if same_card(session, trip, c):
            return c
    return None


def _latest_assessment(session: Session, trip_key: str) -> Assessment | None:
    return session.scalars(
        select(Assessment)
        .where(Assessment.trip_key == trip_key)
        .order_by(Assessment.assessed_at.desc(), Assessment.id.desc())
        .limit(1)
    ).first()


def _reason_code(reason: str | None) -> str:
    return (reason or "").split(":", 1)[0]


def _write_assessment(
    session: Session,
    trip_key: str,
    *,
    delay: int | None,
    cancelled: bool,
    evidence: list[str],
    eligible: bool,
    percent: int,
    expected: Decimal,
    reason: str,
    deadline: date,
    now: datetime,
) -> bool:
    """Store the verdict; returns True when it differs from the previous one (new row written)."""
    prev = _latest_assessment(session, trip_key)
    if (
        prev is not None
        and prev.eligible == eligible
        and prev.percent == percent
        and prev.arrival_delay_min == delay
        and prev.cancelled == cancelled
        and _reason_code(prev.reason) == _reason_code(reason)
    ):
        prev.assessed_at = now
        prev.evidence = list(dict.fromkeys([*(prev.evidence or []), *evidence]))
        prev.reason = reason
        return False
    session.add(
        Assessment(
            trip_key=trip_key,
            arrival_delay_min=delay,
            cancelled=cancelled,
            evidence=evidence,
            eligible=eligible,
            percent=percent,
            expected_eur=expected,
            reason=reason,
            deadline=deadline,
            assessed_at=now,
        )
    )
    return True


def _mark_duplicate(
    session: Session,
    trip: Trip,
    claim: Claim,
    canonical: Trip,
    rules: rules_mod.Rules,
    now: datetime,
    why: str,
) -> None:
    deadline = rules_mod.add_months(
        as_utc(trip.checkin_at).astimezone(local_tz()).date(), rules.deadline_months
    )
    reason = f"duplicate: {why} (trip {canonical.key[:8]})"
    _write_assessment(
        session,
        trip.key,
        delay=None,
        cancelled=False,
        evidence=[f"duplicate_of:{canonical.source}:{canonical.key[:8]}"],
        eligible=False,
        percent=0,
        expected=Decimal("0.00"),
        reason=reason,
        deadline=deadline,
        now=now,
    )
    claim.state = ClaimState.INELIGIBLE.value
    claim.expected_eur = Decimal("0.00")
    add_event(session, "assess.duplicate", reason, trip_key=trip.key)


_CLAIMED_STATES = {
    ClaimState.APPROVED.value,
    ClaimState.SUBMITTING.value,
    ClaimState.SUBMITTED.value,
    ClaimState.CONFIRMED.value,
    ClaimState.PAID.value,
}
_SUPERSEDABLE_STATES = {
    ClaimState.DETECTED.value,
    ClaimState.ELIGIBLE.value,
    ClaimState.AWAITING_APPROVAL.value,
}


def _dedup(
    session: Session, trip: Trip, claim: Claim, rules: rules_mod.Rules, now: datetime
) -> tuple[bool, Trip | None]:
    """Returns (trip_is_duplicate, corroborating_trip)."""
    other = find_counterpart(session, trip)
    if other is None:
        return False, None
    other_claim = session.scalar(select(Claim).where(Claim.trip_key == other.key))
    if trip.source == Source.OVCHIP.value:
        _mark_duplicate(session, trip, claim, other, rules, now, "same journey as the NS trip")
        prev = _latest_assessment(session, other.key)
        if prev is not None:
            tag = f"corroborated_by:ovchip:{trip.key[:8]}"
            if tag not in (prev.evidence or []):
                prev.evidence = [*(prev.evidence or []), tag]
        return True, other
    # trip is the NS one: canonical, unless the OV counterpart is already being claimed
    if other_claim is not None and other_claim.state in _CLAIMED_STATES:
        _mark_duplicate(session, trip, claim, other, rules, now, "already claimed via the OV-chipkaart trip")
        return True, other
    if other_claim is not None and other_claim.state in _SUPERSEDABLE_STATES:
        _mark_duplicate(session, other, other_claim, trip, rules, now, "same journey as the NS trip")
    return False, other


# ---------------------------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------------------------


def _pending_trip_keys(session: Session, today: date) -> list[str]:
    keys = list(
        session.scalars(
            select(Claim.trip_key).where(Claim.state == ClaimState.DETECTED.value).order_by(Claim.id)
        )
    )
    for claim in session.scalars(select(Claim).where(Claim.state == ClaimState.INELIGIBLE.value)):
        a = _latest_assessment(session, claim.trip_key)
        if a is not None and _reason_code(a.reason) in REASSESS_CODES and a.deadline >= today:
            keys.append(claim.trip_key)
    return keys


async def run_assess(
    *,
    today: date | None = None,
    rdt_import: RdtImport | None = None,
    rules: rules_mod.Rules | None = None,
) -> AssessResult:
    """Assess pending trips (see module docstring). ``rdt_import`` is injectable for tests."""
    today = today or local_today()
    rules = rules or rules_mod.load_rules()
    result = AssessResult()
    with session_scope() as s:
        keys = _pending_trip_keys(s, today)
        trips = [s.get(Trip, k) for k in keys]
        stations = {t.to_station.upper() for t in trips if t is not None and t.to_station}
        months = months_for_trips(t for t in trips if t is not None)
    if not keys:
        return result
    result.rdt_imported, result.rdt_skipped = await ensure_rdt(
        months, stations, today=today, rdt_import=rdt_import
    )
    now = datetime.now(UTC)
    for key in keys:
        with session_scope() as s:
            trip = s.get(Trip, key)
            claim = s.scalar(select(Claim).where(Claim.trip_key == key))
            if trip is None or claim is None:
                continue
            was_pending = claim.state in (ClaimState.DETECTED.value, ClaimState.INELIGIBLE.value)
            if not was_pending:  # changed since we listed it (e.g. dedup of the counterpart)
                continue
            result.assessed += 1
            duplicate, other = _dedup(s, trip, claim, rules, now)
            if duplicate:
                result.duplicates += 1
                continue
            dto = trip_to_dto(trip)
            m = matcher.match(dto, s)
            a = rules_mod.assess(dto, m.delay_min, m.cancelled, trip.ns_delay_flag, today=today, rules=rules)
            evidence = list(dict.fromkeys([*m.evidence, *a.evidence]))
            if other is not None:
                evidence.append(f"corroborated_by:{other.source}:{other.key[:8]}")
            changed = _write_assessment(
                s,
                trip.key,
                delay=a.arrival_delay_min,
                cancelled=a.cancelled,
                evidence=evidence,
                eligible=a.eligible,
                percent=a.percent,
                expected=a.expected_eur,
                reason=a.reason,
                deadline=a.deadline,
                now=now,
            )
            claim.expected_eur = a.expected_eur
            if a.eligible:
                claim.state = ClaimState.ELIGIBLE.value
                result.eligible += 1
                add_event(s, "assess.eligible", a.reason, trip_key=trip.key)
            elif _reason_code(a.reason) == "deadline_passed":
                claim.state = ClaimState.EXPIRED.value
                result.expired += 1
                add_event(s, "assess.expired", a.reason, level="warning", trip_key=trip.key)
            else:
                claim.state = ClaimState.INELIGIBLE.value
                result.ineligible += 1
                if changed:
                    add_event(s, "assess.ineligible", a.reason, trip_key=trip.key)
    with session_scope() as s:
        add_event(s, "assess.done", result.summary())
    return result
