from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import inspect, select, text

from treinvertraging.db import get_engine, init_db, session_scope
from treinvertraging.models import Account, Base, Claim, Trip

EXPECTED = {"accounts", "trips", "assessments", "claims", "events", "arrivals", "settings_kv"}


def test_init_db_creates_tables(data_dir: Path) -> None:
    init_db()
    assert (data_dir / "app.db").exists()
    tables = set(inspect(get_engine()).get_table_names())
    assert EXPECTED <= tables
    assert "alembic_version" in tables
    assert EXPECTED == set(Base.metadata.tables)


def test_init_db_is_idempotent(data_dir: Path) -> None:
    init_db()
    init_db()


def test_wal_and_foreign_keys(data_dir: Path) -> None:
    init_db()
    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_orm_roundtrip(data_dir: Path) -> None:
    init_db()
    with session_scope() as s:
        acc = Account(source="ns", label="me", card_number_enc="enc", email="a@b.nl")
        s.add(acc)
        s.flush()
        s.add(
            Trip(
                key="k1",
                account_id=acc.id,
                source="ns",
                card_id="c",
                checkin_at=datetime(2026, 9, 14, 6, 30, tzinfo=UTC),
                from_station="UT",
                fare_eur=Decimal("12.40"),
            )
        )
        s.add(Claim(trip_key="k1", state="detected", channel="gtbv", expected_eur=Decimal("6.20")))
    with session_scope() as s:
        trip = s.scalars(select(Trip)).one()
        assert trip.checkin_at == datetime(2026, 9, 14, 6, 30, tzinfo=UTC)
        assert trip.fare_eur == Decimal("12.40")
        assert trip.claim is not None and trip.claim.channel == "gtbv"
        assert trip.account.card_number_enc == "enc"
