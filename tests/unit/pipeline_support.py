"""Shared fixtures and builders for the pipeline/services tests (no tests of its own).

Import the fixtures into a test module with ``from tests.unit.pipeline_support import env``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from treinvertraging import config, crypto, db
from treinvertraging import notify as notify_mod
from treinvertraging.domain import ClaimState, Source, TripDTO
from treinvertraging.models import Account, Arrival, Assessment, Base, Claim, Event, Trip

TZ = ZoneInfo("Europe/Amsterdam")
TODAY = date(2026, 10, 26)
CARD = "3528000000000001"
IBAN = "NL91ABNA0417164300"


@dataclass
class Env:
    data_dir: Path
    sent: list[dict[str, Any]] = field(default_factory=list)  # recorded notifications

    def titles(self) -> list[str]:
        return [n["title"] for n in self.sent]


def _make_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch, *, migrate: bool) -> Env:
    for name in ("TV_FAKE_ADAPTERS", "TV_FAKE_FAIL", "TV_FAKE_TRIPS_FILE", "TV_FAKE_STATUS_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPRISE_URLS", "")
    monkeypatch.setenv("CLAIM_MODE", "approve")
    monkeypatch.setenv("AUTO_AFTER_N_APPROVED", "5")
    monkeypatch.setenv("NS_API_KEY", "")
    monkeypatch.setenv("PUBLIC_URL", "http://tv.test:8080")
    config.reset_settings_cache()
    if migrate:
        db.init_db()  # what the CLI / serve do (alembic)
    else:
        Base.metadata.create_all(db.get_engine())
    e = Env(data_dir)

    async def fake_notify(title: str, body: str, level: str = "info", attach: Any = None) -> bool:
        e.sent.append({"title": title, "body": body, "level": level, "attach": attach})
        return True

    monkeypatch.setattr(notify_mod, "notify", fake_notify)
    return e


@pytest.fixture
def env(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Fresh tmp DB with tables, notifications recorded instead of sent, no fake-adapter env."""
    return _make_env(data_dir, monkeypatch, migrate=False)


@pytest.fixture
def env_migrated(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Like ``env`` but the schema comes from alembic (for tests that run the CLI, which migrates)."""
    return _make_env(data_dir, monkeypatch, migrate=True)


def set_mode(monkeypatch: pytest.MonkeyPatch, mode: str, **extra: str) -> None:
    monkeypatch.setenv("CLAIM_MODE", mode)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    config.reset_settings_cache()


def add_account(
    source: Source = Source.NS,
    label: str | None = None,
    *,
    card: str | None = CARD,
    iban: str | None = IBAN,
    holder: str = "J. Jansen",
    email: str = "jan@example.com",
    enabled: bool = True,
    otp: str = "manual",
) -> int:
    with db.session_scope() as s:
        acc = Account(
            source=source.value,
            label=label or f"{source.value}-account",
            username="jan@example.com",
            password_enc=crypto.encrypt("pw"),
            card_number_enc=crypto.encrypt(card) if card else None,
            iban_enc=crypto.encrypt(iban) if iban else None,
            holder_name=holder,
            email=email,
            otp_provider=otp,
            enabled=enabled,
        )
        s.add(acc)
        s.flush()
        return acc.id


def make_trip(
    *,
    source: Source = Source.NS,
    card_id: str = CARD,
    day: date = date(2026, 10, 20),
    hour: int = 8,
    minute: int = 15,
    minutes: int = 85,
    frm: str = "UT",
    to: str | None = "ASD",
    fare: str | None = "20.00",
    flag: bool | None = True,
    checkout: bool = True,
    operator: str = "NS",
    product: str | None = "Vol tarief",
    claim_ref: str | None = "ref-1",
) -> TripDTO:
    cin = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return TripDTO(
        source=source,
        card_id=card_id,
        checkin_at=cin,
        checkout_at=cin + timedelta(minutes=minutes) if checkout else None,
        from_station=frm,
        to_station=to,
        fare_eur=Decimal(fare) if fare is not None else None,
        product=product,
        operator=operator,
        ns_delay_flag=flag,
        ns_claim_ref=claim_ref,
        raw={"n": 1},
    )


def add_arrival(
    station: str = "ASD",
    train: str = "3045",
    planned: datetime | None = None,
    delay: int = 34,
    cancelled: bool = False,
    source: str = "rdt",
) -> None:
    planned = planned or datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    with db.session_scope() as s:
        s.add(
            Arrival(
                station=station,
                train_number=train,
                planned_at=planned,
                actual_at=None if cancelled else planned + timedelta(minutes=delay),
                delay_min=None if cancelled else delay,
                cancelled=cancelled,
                source=source,
            )
        )


def seed_claim(
    state: ClaimState | str,
    *,
    account_id: int,
    trip: TripDTO | None = None,
    expected: str = "10.00",
    deadline: date | None = None,
    eligible: bool = True,
    attempts: int = 0,
    channel: str | None = None,
    updated_ago: timedelta | None = None,
    submitted_ago: timedelta | None = None,
    approved_event: bool = False,
    reference: str | None = None,
    final_submit_event: bool = False,
) -> tuple[int, str]:
    """Insert trip + eligible assessment + claim directly in ``state``. Returns (claim_id, trip_key)."""
    trip = trip or make_trip()
    key = trip.key()
    deadline = deadline or date(2027, 1, 20)
    now = datetime.now(UTC)
    with db.session_scope() as s:
        s.add(
            Trip(
                key=key,
                account_id=account_id,
                source=trip.source.value,
                card_id=trip.card_id,
                checkin_at=trip.checkin_at,
                checkout_at=trip.checkout_at,
                from_station=trip.from_station,
                to_station=trip.to_station,
                fare_eur=trip.fare_eur,
                product=trip.product,
                operator=trip.operator,
                ns_delay_flag=trip.ns_delay_flag,
                ns_claim_ref=trip.ns_claim_ref,
                raw_json={},
            )
        )
        s.flush()
        s.add(
            Assessment(
                trip_key=key,
                arrival_delay_min=34,
                cancelled=False,
                evidence=["seed"],
                eligible=eligible,
                percent=50,
                expected_eur=Decimal(expected),
                reason="eligible: seeded",
                deadline=deadline,
            )
        )
        claim = Claim(
            trip_key=key,
            state=str(state),
            expected_eur=Decimal(expected),
            attempts=attempts,
            channel=channel,
            reference=reference,
            created_at=now - timedelta(days=3),
            updated_at=now - (updated_ago or timedelta(0)),
            submitted_at=(now - submitted_ago) if submitted_ago else None,
        )
        s.add(claim)
        s.flush()
        if approved_event:
            s.add(Event(kind="claim.approved", message="seed", trip_key=key))
        if final_submit_event:
            s.add(Event(kind="claim.submitting", message="seed", trip_key=key))
            s.add(Event(kind="claim.final_submit_clicked", message="seed", trip_key=key))
        return claim.id, key


def claim_row(claim_id: int) -> Claim:
    with db.session_scope() as s:
        c = s.get(Claim, claim_id)
        assert c is not None
        return c


def claim_by_key(key: str) -> Claim:
    with db.session_scope() as s:
        c = s.scalar(select(Claim).where(Claim.trip_key == key))
        assert c is not None
        return c


def events(kind_prefix: str = "") -> list[Event]:
    with db.session_scope() as s:
        return list(s.scalars(select(Event).where(Event.kind.like(f"{kind_prefix}%")).order_by(Event.id)))


def count(model: type) -> int:
    with db.session_scope() as s:
        return len(list(s.scalars(select(model))))


async def no_sleep(_: float) -> None:
    return None
