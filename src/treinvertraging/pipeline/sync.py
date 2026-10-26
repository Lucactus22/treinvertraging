"""Trip sync: pull trips from every enabled account and upsert them.

Each new trip gets a ``claims`` row in state ``detected``. Existing trips are refreshed
(``last_seen``, and fields that were still empty when first seen, such as the check-out). When such a
field changes on a trip that is still ``detected`` or ``ineligible``, the claim goes back to
``detected`` so ``assess`` looks at it again.

One account failing never stops the others; failures are logged as events and notified.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from treinvertraging import notify as notify_mod
from treinvertraging.adapters import registry
from treinvertraging.adapters.base import TripSource
from treinvertraging.db import session_scope
from treinvertraging.domain import ClaimState, TripDTO
from treinvertraging.models import Account, Claim, Trip
from treinvertraging.pipeline.context import add_event, as_utc, local_today, local_tz, short_error

log = logging.getLogger(__name__)

SYNC_LOOKBACK_DAYS = 95  # a bit more than the 3 month claim deadline

TripSourceFactory = Callable[[Account], TripSource]


@dataclass
class SyncResult:
    accounts: int = 0
    seen: int = 0
    new: int = 0
    updated: int = 0
    reset: int = 0  # claims sent back to ``detected`` because new trip data arrived
    failed: dict[str, str] = field(default_factory=dict)  # account label -> error

    def summary(self) -> str:
        text = (
            f"sync: {self.accounts} account(s), {self.seen} trip(s) seen, {self.new} new, "
            f"{self.updated} updated"
        )
        if self.reset:
            text += f", {self.reset} to reassess"
        if self.failed:
            text += "; FAILED: " + "; ".join(f"{k}: {v}" for k, v in self.failed.items())
        return text


def _jsonable(raw: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(raw, default=str))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=local_tz())


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is b
    return as_utc(a) == as_utc(b)


def upsert_trip(session, account: Account, dto: TripDTO, now: datetime) -> str:
    """Insert or refresh one trip. Returns ``"new"``, ``"updated"``, ``"reset"`` or ``"same"``."""
    dto = dto.model_copy(
        update={"checkin_at": _aware(dto.checkin_at), "checkout_at": _aware(dto.checkout_at)}
    )
    key = dto.key()
    row = session.get(Trip, key)
    raw = _jsonable(dto.raw)
    if row is None:
        session.add(
            Trip(
                key=key,
                account_id=account.id,
                source=dto.source.value,
                card_id=dto.card_id,
                checkin_at=dto.checkin_at,
                checkout_at=dto.checkout_at,
                from_station=dto.from_station,
                to_station=dto.to_station,
                fare_eur=dto.fare_eur,
                product=dto.product,
                operator=dto.operator,
                ns_delay_flag=dto.ns_delay_flag,
                ns_claim_ref=dto.ns_claim_ref,
                raw_json=raw,
                first_seen=now,
                last_seen=now,
            )
        )
        session.add(Claim(trip_key=key, state=ClaimState.DETECTED.value))
        add_event(session, "sync.new_trip", f"new trip {dto.from_station}->{dto.to_station}", trip_key=key)
        return "new"

    relevant_changed = (
        not _same_instant(row.checkout_at, dto.checkout_at)
        or row.to_station != dto.to_station
        or row.ns_delay_flag != dto.ns_delay_flag
        or row.fare_eur != dto.fare_eur
        or row.operator != dto.operator
        or row.product != dto.product
    )
    row.last_seen = now
    if relevant_changed:
        row.checkout_at = dto.checkout_at
        row.to_station = dto.to_station
        row.ns_delay_flag = dto.ns_delay_flag
        row.fare_eur = dto.fare_eur
        row.operator = dto.operator
        row.product = dto.product
    if dto.ns_claim_ref and row.ns_claim_ref != dto.ns_claim_ref:
        row.ns_claim_ref = dto.ns_claim_ref
    row.raw_json = raw
    if not relevant_changed:
        return "same"
    claim = session.scalar(select(Claim).where(Claim.trip_key == key))
    if claim is not None and claim.state in (ClaimState.DETECTED.value, ClaimState.INELIGIBLE.value):
        if claim.state != ClaimState.DETECTED.value:
            claim.state = ClaimState.DETECTED.value
            add_event(session, "sync.reassess", "trip data changed; will be assessed again", trip_key=key)
            return "reset"
    return "updated"


def enabled_accounts() -> list[Account]:
    with session_scope() as s:
        return list(s.scalars(select(Account).where(Account.enabled.is_(True)).order_by(Account.id)))


async def run_sync(
    *,
    since: date | None = None,
    today: date | None = None,
    trip_source_factory: TripSourceFactory | None = None,
    notify: bool = True,
) -> SyncResult:
    """Run the trip sync for all enabled accounts (see module docstring)."""
    today = today or local_today()
    since = since or today - timedelta(days=SYNC_LOOKBACK_DAYS)
    factory = trip_source_factory or registry.trip_source_for
    result = SyncResult()
    for account in enabled_accounts():
        result.accounts += 1
        try:
            source = factory(account)
            await source.ensure_logged_in()
            trips = await source.list_trips(since)
        except Exception as exc:  # noqa: BLE001 - one broken account must not stop the others
            log.warning("sync failed for account %s: %s", account.label, short_error(exc))
            result.failed[account.label] = short_error(exc)
            with session_scope() as s:
                add_event(s, "sync.failed", f"{account.label}: {short_error(exc)}", level="error")
            continue
        now = datetime.now(UTC)
        counts = {"new": 0, "updated": 0, "reset": 0, "same": 0}
        with session_scope() as s:
            for dto in trips:
                if not dto.from_station:
                    continue
                counts[upsert_trip(s, account, dto, now)] += 1
        result.seen += len(trips)
        result.new += counts["new"]
        result.updated += counts["updated"] + counts["reset"]
        result.reset += counts["reset"]
    with session_scope() as s:
        add_event(s, "sync.done", result.summary(), level="error" if result.failed else "info")
    if result.failed and notify:
        await notify_mod.notify(
            "Synchroniseren mislukt",
            "\n".join(f"{label}: {err}" for label, err in result.failed.items()),
            "error",
        )
    return result
