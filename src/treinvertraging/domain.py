"""Pydantic DTOs and enums shared by all modules (contract owned by WP-A, see PLAN.md section 3)."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class Source(StrEnum):
    NS = "ns"
    OVCHIP = "ovchip"


class ClaimState(StrEnum):
    DETECTED = "detected"  # trip found, not yet assessed
    INELIGIBLE = "ineligible"
    ELIGIBLE = "eligible"  # meets rules, waiting for policy (dry_run/approve/auto)
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUBMITTING = "submitting"  # set BEFORE clicking submit
    SUBMITTED = "submitted"  # site confirmed receipt
    CONFIRMED = "confirmed"  # seen in "mijn aanvragen"
    PAID = "paid"
    REJECTED = "rejected"
    NEEDS_HUMAN = "needs_human"  # ladder exhausted
    EXPIRED = "expired"


class TripDTO(BaseModel):
    source: Source
    card_id: str  # masked card number or NS account id
    checkin_at: datetime  # tz-aware Europe/Amsterdam
    checkout_at: datetime | None = None
    from_station: str  # canonical station code (e.g. "UT")
    to_station: str | None = None
    fare_eur: Decimal | None = None  # amount actually charged
    product: str | None = None  # e.g. "Dal Voordeel", "Vol tarief"
    operator: str | None = None  # "NS", "Arriva", ...
    ns_delay_flag: bool | None = None  # NS itself marks it as delayed/claimable
    ns_claim_ref: str | None = None  # opaque handle to open NS's prefilled form
    raw: dict = Field(default_factory=dict)  # original JSON/row for debugging

    def key(self) -> str:
        """Deterministic key: sha256(source|card_id|checkin_at.isoformat()|from_station)."""
        payload = f"{self.source.value}|{self.card_id}|{self.checkin_at.isoformat()}|{self.from_station}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DelayAssessment(BaseModel):
    trip_key: str
    arrival_delay_min: int | None
    cancelled: bool
    evidence: list[str]  # e.g. ["rdt:2026-09 train 3045 UT->ASD +34", "ns_flag"]
    eligible: bool
    percent: int  # 0/50/100
    expected_eur: Decimal
    reason: str
    deadline: date


class SubmitResult(BaseModel):
    ok: bool
    reference: str | None
    screenshot: str | None
    message: str
    channel: str  # "gtbv" | "mijnns"
    reached_final_submit: bool  # True once the final submit button was clicked
    dry_run: bool = False
