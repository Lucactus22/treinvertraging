"""Reconcile local claim states with what NS shows.

Sources (all best effort, any may be missing)
    * every ``ClaimStatusReader`` (Mijn NS "mijn aanvragen"), see ``adapters/registry.status_readers``;
    * NS confirmation e-mails, read with ``ImapOtpProvider.list_messages`` when IMAP is configured.

Status record format (``ClaimStatusReader.list_submitted_claims`` returns ``list[dict]``)
    Keys are matched case-insensitively and tolerantly; all are optional:

    ============  =====================================================================
    reference     ``reference``, ``ref``, ``referentie``, ``kenmerk``, ``claim_ref``, ``id``
    trip_key      ``trip_key`` (exact match when an adapter knows it)
    date          ``trip_date``, ``date``, ``datum``, ``travel_date``, ``checkin_at``
                  (``date``/``datetime``/ISO string/``dd-mm-yyyy``/``26 okt 2026``)
    from / to     ``from_station``/``from``/``van`` and ``to_station``/``to``/``naar``
                  (names or codes, resolved with ``stations.py``)
    status        ``status``/``state`` (free text, Dutch or English)
    amount        ``paid_eur``/``amount``/``bedrag``/``uitbetaald``
    ============  =====================================================================

    Matching: ``trip_key`` > ``reference`` > same local check-in date (plus stations when given), and only
    when exactly one claim fits. Status text: *uitbetaald/paid* -> ``paid``; *afgewezen/rejected/geweigerd*
    -> ``rejected``; anything else found -> ``confirmed``. States only move forward.

Stale ``submitting`` claims
    A claim in ``submitting`` that no source knows and that has not changed for 24 hours goes back to
    ``approved`` (it will be submitted again by the next claim run), or to ``needs_human`` after
    ``MAX_ATTEMPTS`` (3). ``attempts`` was already incremented when the claim entered ``submitting``.
    This rollback only happens when at least one source (reader or mailbox) could be queried successfully
    in this run; otherwise the claim stays and a warning is sent, because "not found" would mean nothing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from treinvertraging import notify as notify_mod
from treinvertraging.adapters import registry
from treinvertraging.adapters.base import ClaimStatusReader
from treinvertraging.db import session_scope
from treinvertraging.domain import ClaimState
from treinvertraging.models import Account, Claim, Trip
from treinvertraging.pipeline.context import (
    MAX_ATTEMPTS,
    add_event,
    as_utc,
    load_imap_config,
    local_tz,
    short_error,
)

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(hours=24)
_AUTO: Any = object()

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "reference": ("reference", "ref", "referentie", "referentienummer", "kenmerk", "claim_ref", "id"),
    "trip_key": ("trip_key",),
    "date": ("trip_date", "date", "datum", "travel_date", "reisdatum", "checkin_at"),
    "from": ("from_station", "from", "van"),
    "to": ("to_station", "to", "naar"),
    "status": ("status", "state"),
    "amount": ("paid_eur", "amount", "bedrag", "uitbetaald"),
}
_PAID = re.compile(r"uitbetaald|uitgekeerd|\bpaid\b|betaald|paid out", re.I)
_REJECTED = re.compile(r"afgewezen|geweigerd|niet toegekend|rejected|denied|declined", re.I)
_MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "maa": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
           "okt": 10, "nov": 11, "dec": 12}  # fmt: skip
_REFERENCE = re.compile(
    r"(?:referentie(?:nummer)?|kenmerk|aanvraagnummer|reference)\s*(?:is|:)?\s*([A-Z0-9][A-Z0-9\-]{4,})", re.I
)
_RANK = {
    ClaimState.SUBMITTING.value: 0,
    ClaimState.SUBMITTED.value: 1,
    ClaimState.CONFIRMED.value: 2,
    ClaimState.PAID.value: 3,
    ClaimState.REJECTED.value: 3,
}
_PENDING = (ClaimState.SUBMITTING.value, ClaimState.SUBMITTED.value, ClaimState.CONFIRMED.value)


@dataclass
class ReconcileResult:
    checked: int = 0
    confirmed: int = 0
    paid: int = 0
    rejected: int = 0
    rolled_back: int = 0
    needs_human: int = 0
    sources_ok: int = 0
    source_errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"reconcile: {self.checked} open claim(s): {self.confirmed} confirmed, {self.paid} paid, "
            f"{self.rejected} rejected, {self.rolled_back} back to approved, {self.needs_human} need a human"
        )
        if self.source_errors:
            text += "; source errors: " + "; ".join(self.source_errors)
        return text


# ---------------------------------------------------------------------------------------------
# record normalisation and matching (pure helpers)
# ---------------------------------------------------------------------------------------------


@dataclass
class Record:
    reference: str | None = None
    trip_key: str | None = None
    day: date | None = None
    from_code: str | None = None
    to_code: str | None = None
    status: ClaimState = ClaimState.CONFIRMED
    amount: Decimal | None = None
    source: str = "reader"


def _get(raw: dict, name: str) -> Any:
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for alias in _FIELD_ALIASES[name]:
        if lowered.get(alias) not in (None, ""):
            return lowered[alias]
    return None


def parse_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value.astimezone(local_tz()) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", text)
    if m and m.group(2).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def parse_amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = re.sub(r"[^\d,.\-]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def status_from_text(text: Any) -> ClaimState:
    s = str(text or "")
    if _REJECTED.search(s):
        return ClaimState.REJECTED
    if _PAID.search(s):
        return ClaimState.PAID
    return ClaimState.CONFIRMED


def _station_code(value: Any) -> str | None:
    if not value:
        return None
    from treinvertraging.stations import get_station_index

    return get_station_index().code_for(str(value))


def normalise_record(raw: dict, source: str = "reader") -> Record:
    return Record(
        reference=(str(_get(raw, "reference")) if _get(raw, "reference") is not None else None),
        trip_key=(str(_get(raw, "trip_key")) if _get(raw, "trip_key") is not None else None),
        day=parse_day(_get(raw, "date")),
        from_code=_station_code(_get(raw, "from")),
        to_code=_station_code(_get(raw, "to")),
        status=status_from_text(_get(raw, "status")),
        amount=parse_amount(_get(raw, "amount")),
        source=source,
    )


def match_record(rec: Record, pairs: Sequence[tuple[Claim, Trip]]) -> Claim | None:
    if rec.trip_key:
        for claim, _ in pairs:
            if claim.trip_key == rec.trip_key:
                return claim
    if rec.reference:
        for claim, _ in pairs:
            if claim.reference and claim.reference.casefold() == rec.reference.casefold():
                return claim
    if rec.day is None:
        return None
    tz = local_tz()
    fits = [
        claim
        for claim, trip in pairs
        if as_utc(trip.checkin_at).astimezone(tz).date() == rec.day
        and (rec.from_code is None or rec.from_code == trip.from_station.upper())
        and (rec.to_code is None or rec.to_code == (trip.to_station or "").upper())
    ]
    return fits[0] if len(fits) == 1 else None


def mail_reference(text: str) -> str | None:
    m = _REFERENCE.search(text)
    return m.group(1) if m else None


def _station_names(code: str | None) -> list[str]:
    if not code:
        return []
    from treinvertraging.stations import get_station_index

    st = get_station_index().get(code)
    if st is None:
        return []
    return [n for n in {st.name_long, st.name_medium} if n and len(n) > 3]


def mail_matches_trip(text: str, trip: Trip) -> bool:
    """High-confidence match: the mail names one of the trip's stations or its date."""
    low = text.casefold()
    for code in (trip.from_station, trip.to_station):
        if any(name.casefold() in low for name in _station_names(code)):
            return True
    day = as_utc(trip.checkin_at).astimezone(local_tz()).date()
    return f"{day:%d-%m-%Y}" in text or f"{day:%Y-%m-%d}" in text


# ---------------------------------------------------------------------------------------------
# applying results
# ---------------------------------------------------------------------------------------------


def _apply(
    session, claim: Claim, rec: Record, result: ReconcileResult, notices: list[tuple[str, str, str]]
) -> bool:
    """Move ``claim`` forward according to ``rec``. Returns True if anything changed."""
    new = rec.status.value
    changed = False
    if rec.reference and not claim.reference:
        claim.reference = rec.reference[:255]
        changed = True
    if new == ClaimState.PAID.value and claim.state == ClaimState.PAID.value:
        if claim.paid_eur is None and rec.amount is not None:
            claim.paid_eur = rec.amount
            changed = True
        return changed
    if _RANK.get(new, 0) <= _RANK.get(claim.state, 0):
        return changed
    old = claim.state
    claim.state = new
    changed = True
    if new == ClaimState.PAID.value:
        claim.paid_eur = rec.amount if rec.amount is not None else claim.expected_eur
        result.paid += 1
        kind = "claim.paid"
        notices.append(("Uitbetaald", f"Claim #{claim.id} is uitbetaald ({claim.paid_eur} EUR).", "success"))
    elif new == ClaimState.REJECTED.value:
        result.rejected += 1
        kind = "claim.rejected_by_ns"
        claim.last_error = "afgewezen door NS"
        notices.append(("Claim afgewezen", f"NS heeft claim #{claim.id} afgewezen.", "error"))
    else:
        result.confirmed += 1
        kind = "claim.confirmed"
    add_event(
        session,
        kind,
        f"{old} -> {new} (via {rec.source})",
        level="error" if new == ClaimState.REJECTED.value else "info",
        trip_key=claim.trip_key,
    )
    return changed


# ---------------------------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------------------------


async def run_reconcile(
    *,
    readers: Sequence[ClaimStatusReader] | None = None,
    mail_provider: Any = _AUTO,
    now: datetime | None = None,
    stale_after: timedelta = STALE_AFTER,
    notify: bool = True,
) -> ReconcileResult:
    """See module docstring. ``mail_provider=None`` disables the mailbox; default: use IMAP if configured."""
    now = now or datetime.now(UTC)
    result = ReconcileResult()
    with session_scope() as s:
        pairs = [
            (c, t)
            for c, t in s.execute(
                select(Claim, Trip).join(Trip, Trip.key == Claim.trip_key).where(Claim.state.in_(_PENDING))
            ).all()
        ]
        accounts = list(s.scalars(select(Account).where(Account.enabled.is_(True)).order_by(Account.id)))
        imap_cfg = load_imap_config(s) if mail_provider is _AUTO else None
    result.checked = len(pairs)
    if not pairs:
        return result

    earliest = min(as_utc(c.submitted_at or c.created_at) for c, _ in pairs)
    since_dt = max(earliest - timedelta(days=1), now - timedelta(days=100))
    since = since_dt.astimezone(local_tz()).date()

    records: list[Record] = []
    if readers is None:
        readers = registry.status_readers(accounts, imap=imap_cfg)
    for reader in readers:
        try:
            raw_records = await reader.list_submitted_claims(since)
        except Exception as exc:  # noqa: BLE001 - one broken source must not hide the others
            log.warning("status reader failed: %s", short_error(exc))
            result.source_errors.append(short_error(exc))
            continue
        result.sources_ok += 1
        records.extend(normalise_record(r, "mijn aanvragen") for r in raw_records)

    mail_records: list[tuple[Any, str]] = []
    if mail_provider is _AUTO and imap_cfg is not None:
        from treinvertraging.otp.imap import ImapOtpProvider

        mail_provider = ImapOtpProvider(imap_cfg)
    elif mail_provider is _AUTO:
        mail_provider = None
    if mail_provider is not None:
        try:
            mails = await mail_provider.list_messages(
                since_dt,
                senders=("ns.nl",),
                subject_regex=r"aanvraag|geld terug|vertraging|ontvangen|bevestiging",
            )
            result.sources_ok += 1
            mail_records = [(m, f"{m.subject}\n{m.text}") for m in mails]
        except Exception as exc:  # noqa: BLE001
            log.warning("mailbox read failed: %s", type(exc).__name__)
            result.source_errors.append(f"mailbox: {type(exc).__name__}")

    notices: list[tuple[str, str, str]] = []
    with session_scope() as s:
        live = {c.id: s.get(Claim, c.id) for c, _ in pairs}
        live_pairs = [(live[c.id], t) for c, t in pairs]
        # 1. status records
        for rec in records:
            claim = match_record(rec, live_pairs)
            if claim is not None:
                _apply(s, claim, rec, result, notices)
        # 2. confirmation mails (only for claims not confirmed yet)
        unconfirmed = [
            (c, t)
            for c, t in live_pairs
            if c.state in (ClaimState.SUBMITTING.value, ClaimState.SUBMITTED.value)
        ]
        used: set[int] = set()
        for mail, text in sorted(mail_records, key=lambda x: x[0].date or now):
            mdate = mail.date or now
            window = [
                (c, t)
                for c, t in unconfirmed
                if c.id not in used
                and as_utc(c.submitted_at or c.updated_at) - timedelta(minutes=10)
                <= mdate
                <= as_utc(c.submitted_at or c.updated_at) + timedelta(days=3)
            ]
            ref = mail_reference(text)
            strong = [(c, t) for c, t in window if mail_matches_trip(text, t)]
            pick = strong[0][0] if len(strong) == 1 else None
            if pick is None and not strong and len(window) == 1:
                pick = window[0][0]  # low confidence: the only open claim around that time
            if pick is None:
                continue
            used.add(pick.id)
            rec = Record(reference=ref, status=ClaimState.CONFIRMED, source="confirmation mail")
            _apply(s, pick, rec, result, notices)
        # 3. stale submitting claims
        for claim, trip in live_pairs:
            if claim.state != ClaimState.SUBMITTING.value:
                continue
            if now - as_utc(claim.updated_at) < stale_after:
                continue
            if result.sources_ok == 0:
                add_event(
                    s,
                    "reconcile.cannot_verify",
                    f"claim #{claim.id} stuck in submitting but no status source is reachable",
                    level="warning",
                    trip_key=claim.trip_key,
                )
                notices.append(
                    (
                        "Claim niet te controleren",
                        f"Claim #{claim.id} staat al >24 uur op 'indienen' en NS is niet te raadplegen.",
                        "warning",
                    )
                )
                continue
            if (claim.attempts or 0) >= MAX_ATTEMPTS:
                claim.state = ClaimState.NEEDS_HUMAN.value
                claim.last_error = f"niet teruggevonden bij NS na {claim.attempts} pogingen"
                result.needs_human += 1
                add_event(s, "reconcile.needs_human", claim.last_error, level="error", trip_key=claim.trip_key)
                notices.append(("Claim vraagt om aandacht", f"Claim #{claim.id}: {claim.last_error}", "error"))
            else:
                claim.state = ClaimState.APPROVED.value
                claim.last_error = "niet teruggevonden bij NS; opnieuw ingepland"
                result.rolled_back += 1
                add_event(
                    s,
                    "reconcile.rollback",
                    f"not found remotely after {stale_after}; back to approved (attempts={claim.attempts})",
                    level="warning",
                    trip_key=claim.trip_key,
                )
        add_event(s, "reconcile.done", result.summary())
    if notify:
        for title, body, level in notices:
            await notify_mod.notify(title, body, level)
    return result
