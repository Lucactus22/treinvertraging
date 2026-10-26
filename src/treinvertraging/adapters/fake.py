"""Fake adapters for tests and for the Docker end-to-end run (``TV_FAKE_ADAPTERS=1``).

They live in ``src`` (not ``tests``) because WP-J's container test uses them through
``adapters/registry.py``. Nothing here touches the network or a browser.

Configuration through the environment (only read by ``registry.py`` when ``TV_FAKE_ADAPTERS=1``):

``TV_FAKE_TRIPS_FILE``
    JSON file with a list of trips (``TripDTO`` fields, ISO datetimes). Default
    ``<DATA_DIR>/fake_trips.json`` if it exists, else ``default_trips()``: one delayed trip
    (``ns_delay_flag=True``, 12.40 EUR) and one punctual trip, dated 5 days ago.
``TV_FAKE_FAIL``
    ``channel:kind[,channel:kind]``; ``kind`` is ``blocked``, ``layout``, ``invalid`` or ``error``.
    Every submission on that channel then fails with the matching exception.
``TV_FAKE_SUBMISSIONS_FILE``
    JSONL log of submissions (default ``<DATA_DIR>/fake-submissions.jsonl``), so a test can assert on
    submissions made by another process. No secrets are written (card number: last 4 digits only).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.domain import Source, SubmitResult, TripDTO
from treinvertraging.errors import (
    BlockedByBotProtection,
    InvalidInput,
    LayoutChanged,
    LoginRequired,
)

log = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Amsterdam")

FAILURE_KINDS: dict[str, type[Exception]] = {
    "blocked": BlockedByBotProtection,
    "layout": LayoutChanged,
    "invalid": InvalidInput,
    "login": LoginRequired,
    "error": RuntimeError,
}


def default_trips(source: Source = Source.NS, *, today: date | None = None) -> list[TripDTO]:
    """Two trips 5 days ago: one NS-flagged as delayed (claimable), one on time."""
    day = (today or datetime.now(_TZ).date()) - timedelta(days=5)
    card = "FAKE-NS" if source is Source.NS else "3528000000000001"

    def at(h: int, m: int) -> datetime:
        return datetime(day.year, day.month, day.day, h, m, tzinfo=_TZ)

    return [
        TripDTO(
            source=source,
            card_id=card,
            checkin_at=at(8, 15),
            checkout_at=at(9, 40),
            from_station="UT",
            to_station="ASD",
            fare_eur=Decimal("12.40"),
            product="Vol tarief",
            operator="NS",
            ns_delay_flag=True,
            ns_claim_ref=f"fake-claim-{day.isoformat()}",
            raw={"fake": True, "delayed": True},
        ),
        TripDTO(
            source=source,
            card_id=card,
            checkin_at=at(17, 5),
            checkout_at=at(18, 0),
            from_station="ASD",
            to_station="UT",
            fare_eur=Decimal("12.40"),
            product="Vol tarief",
            operator="NS",
            ns_delay_flag=False,
            raw={"fake": True, "delayed": False},
        ),
    ]


def trips_from_json(text: str, source: Source) -> list[TripDTO]:
    """Parse ``[{...TripDTO fields...}]``; ``source`` is the default when an item has none."""
    items = json.loads(text)
    out: list[TripDTO] = []
    for item in items:
        item = dict(item)
        item.setdefault("source", source.value)
        trip = TripDTO.model_validate(item)
        if trip.checkin_at.tzinfo is None:
            trip = trip.model_copy(update={"checkin_at": trip.checkin_at.replace(tzinfo=_TZ)})
        if trip.checkout_at is not None and trip.checkout_at.tzinfo is None:
            trip = trip.model_copy(update={"checkout_at": trip.checkout_at.replace(tzinfo=_TZ)})
        out.append(trip)
    return out


class FakeTripSource:
    """``TripSource`` returning a configured list of trips (optionally failing)."""

    def __init__(
        self,
        trips: Iterable[TripDTO] | None = None,
        *,
        source: Source = Source.NS,
        login_error: Exception | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.source = source
        self.trips: list[TripDTO] = list(trips) if trips is not None else default_trips(source)
        self.login_error = login_error
        self.list_error = list_error
        self.login_calls = 0
        self.list_calls: list[date] = []

    async def ensure_logged_in(self) -> None:
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error

    async def list_trips(self, since: date) -> list[TripDTO]:
        self.list_calls.append(since)
        if self.list_error is not None:
            raise self.list_error
        return [t for t in self.trips if t.checkin_at.date() >= since]

    # ClaimStatusReader is implemented by FakeStatusReader; keep this class a pure trip source.


class FakeSubmitter:
    """``ClaimSubmitter`` that records its calls.

    ``outcomes`` is consumed one entry per call: ``None`` = success, an exception instance = raise
    it, a ``SubmitResult`` = return it as is. When the queue is empty the submitter succeeds, unless
    ``always`` is set (same entry types, applied to every remaining call).
    """

    def __init__(
        self,
        channel: str = "gtbv",
        *,
        outcomes: Sequence[Exception | SubmitResult | None] | None = None,
        always: Exception | SubmitResult | None = None,
        log_path: Path | None = None,
        reference_prefix: str = "FAKE",
    ) -> None:
        self.channel = channel
        self.outcomes: list[Exception | SubmitResult | None] = list(outcomes or [])
        self.always = always
        self.log_path = log_path
        self.reference_prefix = reference_prefix
        self.calls: list[dict[str, Any]] = []

    def _record(self, trip: TripDTO, who: ClaimantInfo, dry_run: bool) -> None:
        self.calls.append({"trip": trip, "who": who, "dry_run": dry_run, "channel": self.channel})
        if self.log_path is not None:
            line = {
                "ts": datetime.now(UTC).isoformat(),
                "channel": self.channel,
                "dry_run": dry_run,
                "trip_key": trip.key(),
                "from": trip.from_station,
                "to": trip.to_station,
                "card_last4": who.card_number[-4:],
            }
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line) + "\n")
            except OSError:
                log.warning("cannot write fake submission log %s", self.log_path)

    async def submit_claim(self, trip: TripDTO, who: ClaimantInfo, *, dry_run: bool) -> SubmitResult:
        self._record(trip, who, dry_run)
        outcome = self.outcomes.pop(0) if self.outcomes else self.always
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, SubmitResult):
            return outcome
        n = len(self.calls)
        return SubmitResult(
            ok=True,
            reference=None if dry_run else f"{self.reference_prefix}-{self.channel.upper()}-{n:04d}",
            screenshot=None,
            message="dry run: stopped before the final submit" if dry_run else "fake submission accepted",
            channel=self.channel,
            reached_final_submit=not dry_run,
            dry_run=dry_run,
        )


class FakeStatusReader:
    """``ClaimStatusReader`` returning configured records (see ``pipeline/reconcile.py`` for the format)."""

    def __init__(self, records: Iterable[dict] | None = None, *, error: Exception | None = None) -> None:
        self.records: list[dict] = list(records or [])
        self.error = error
        self.calls: list[date] = []

    async def list_submitted_claims(self, since: date) -> list[dict]:
        self.calls.append(since)
        if self.error is not None:
            raise self.error
        return list(self.records)


def failures_from_env(value: str | None) -> dict[str, Exception]:
    """Parse ``TV_FAKE_FAIL`` (``gtbv:blocked,mijnns:layout``) into ``{channel: exception}``."""
    out: dict[str, Exception] = {}
    for part in (value or "").replace(" ", "").split(","):
        if not part or ":" not in part:
            continue
        channel, kind = part.split(":", 1)
        exc_type = FAILURE_KINDS.get(kind.lower())
        if exc_type is not None:
            out[channel] = exc_type(f"fake {kind} failure on {channel}")
    return out
