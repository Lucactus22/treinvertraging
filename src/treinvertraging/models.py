"""ORM tables (contract owned by WP-A, see PLAN.md section 3)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes on SQLite: stored as naive UTC, returned as aware UTC.

    Naive inputs are assumed to be UTC. Convert to Europe/Amsterdam at the edges.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))  # domain.Source value
    label: Mapped[str] = mapped_column(String(100))
    username: Mapped[str] = mapped_column(String(255), default="")
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_number_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    iban_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    holder_name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    otp_provider: Mapped[str] = mapped_column(String(32), default="manual")  # "manual" | "imap"
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    trips: Mapped[list[Trip]] = relationship(back_populates="account")


class Trip(Base):
    __tablename__ = "trips"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # TripDTO.key()
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    source: Mapped[str] = mapped_column(String(16))
    card_id: Mapped[str] = mapped_column(String(64))
    checkin_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    checkout_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    from_station: Mapped[str] = mapped_column(String(32))
    to_station: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fare_eur: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    product: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ns_delay_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ns_claim_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    account: Mapped[Account] = relationship(back_populates="trips")
    assessments: Mapped[list[Assessment]] = relationship(back_populates="trip")
    claim: Mapped[Claim | None] = relationship(back_populates="trip", uselist=False)


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_key: Mapped[str] = mapped_column(ForeignKey("trips.key"), index=True)
    arrival_delay_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    expected_eur: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    reason: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[date] = mapped_column(Date)
    assessed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    trip: Mapped[Trip] = relationship(back_populates="assessments")


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("trip_key", name="uq_claims_trip_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_key: Mapped[str] = mapped_column(ForeignKey("trips.key"))
    state: Mapped[str] = mapped_column(String(32), default="detected", index=True)  # ClaimState value
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "gtbv" | "mijnns"
    expected_eur: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    paid_eur: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    trip: Mapped[Trip] = relationship(back_populates="claim")


class Event(Base):
    """Audit log."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    trip_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class Arrival(Base):
    __tablename__ = "arrivals"
    __table_args__ = (
        UniqueConstraint("station", "train_number", "planned_at", name="uq_arrivals_station_train_planned"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station: Mapped[str] = mapped_column(String(32), index=True)
    train_number: Mapped[str] = mapped_column(String(32))
    planned_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    actual_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    delay_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16))  # "rdt" | "nsapi"


class SettingKV(Base):
    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
