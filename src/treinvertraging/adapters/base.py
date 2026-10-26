"""Adapter Protocols (contract owned by WP-A, see PLAN.md section 3)."""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from pydantic import BaseModel

from treinvertraging.domain import Source, SubmitResult, TripDTO


class ClaimantInfo(BaseModel):
    """From the account row, decrypted at use."""

    card_number: str  # 16 digits, "3528..."
    iban: str
    holder_name: str
    email: str
    ticket_type: Literal["ov_chipkaart"] = "ov_chipkaart"


class TripSource(Protocol):
    """ns.py, ovchip.py"""

    source: Source

    async def ensure_logged_in(self) -> None: ...

    async def list_trips(self, since: date) -> list[TripDTO]: ...


class ClaimSubmitter(Protocol):
    """gtbv.py (primary), ns.py (fallback)"""

    channel: str  # "gtbv" | "mijnns"

    async def submit_claim(self, trip: TripDTO, who: ClaimantInfo, *, dry_run: bool) -> SubmitResult: ...


class ClaimStatusReader(Protocol):
    """ns.py ("mijn aanvragen"), otp/imap.py (confirmation mails)"""

    async def list_submitted_claims(self, since: date) -> list[dict]: ...
