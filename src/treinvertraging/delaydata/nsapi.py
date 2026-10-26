"""Client for the NS Reisinformatie API v2 (live arrivals) plus a parser and upsert helper.

``GET https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/arrivals?station=UT`` with the
``Ocp-Apim-Subscription-Key`` header returns the upcoming (and just-passed) arrivals of a
station. The payload shape handled here::

    {"payload": {"source": "PPV", "arrivals": [{
        "origin": "Amsterdam Centraal", "name": "NS 1234",
        "plannedDateTime": "2026-10-26T14:07:00+0100",
        "actualDateTime": "2026-10-26T14:12:00+0100",
        "plannedTrack": "5", "cancelled": false,
        "product": {"number": "1234", "categoryCode": "IC", "operatorName": "NS", ...}}]}}

The API has no history, so ``archiver.py`` polls it and stores the latest observed
``actualDateTime`` per train; that is the last prediction before the train left the board and
is close to the real arrival time. RDT data (``rdt.py``) replaces these rows once published.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from treinvertraging.config import get_settings
from treinvertraging.db import get_session_factory
from treinvertraging.errors import TreinvertragingError
from treinvertraging.models import Arrival

log = logging.getLogger(__name__)

NS_ARRIVALS_URL = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/arrivals"
NS_TIMEOUT = httpx.Timeout(15.0)


class NsApiError(TreinvertragingError):
    """The NS API call failed (network, bad key, unexpected status or payload)."""


class NsApiAuthError(NsApiError):
    """401/403: missing or invalid subscription key."""


class NsApiRateLimited(NsApiError):
    """429: free-tier quota exhausted. Callers should stop polling for a while."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ArrivalRecord:
    station: str
    train_number: str
    planned_at: datetime
    actual_at: datetime | None
    delay_min: int | None
    cancelled: bool


def _parse_dt(value: Any) -> datetime | None:
    """Parse NS timestamps (``2026-10-26T14:07:00+0100``; ``+01:00`` and ``Z`` also accepted)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _train_number(item: dict[str, Any]) -> str | None:
    product = item.get("product")
    if isinstance(product, dict):
        number = product.get("number")
        if number not in (None, ""):
            return str(number).strip()
    name = item.get("name")
    if isinstance(name, str):
        m = re.search(r"(\d+)\s*$", name)
        if m:
            return m.group(1)
    return None


def parse_arrivals(payload: dict[str, Any], station: str) -> list[ArrivalRecord]:
    """Turn an API v2 arrivals response into records for ``station``.

    Items without a train number or a (tz-aware) planned time are skipped. For cancelled trains
    ``actual_at`` and ``delay_min`` are None.
    """
    body = payload.get("payload", payload)
    items = body.get("arrivals") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise NsApiError("unexpected NS API payload: no 'arrivals' list")
    code = station.strip().upper()
    out: list[ArrivalRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        number = _train_number(item)
        planned = _parse_dt(item.get("plannedDateTime"))
        if number is None or planned is None:
            continue
        cancelled = bool(item.get("cancelled"))
        actual = None if cancelled else _parse_dt(item.get("actualDateTime"))
        delay = None if actual is None else round((actual - planned).total_seconds() / 60)
        out.append(ArrivalRecord(code, number, planned, actual, delay, cancelled))
    return out


class NsApiClient:
    """Thin async wrapper around the arrivals endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = NS_ARRIVALS_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else get_settings().NS_API_KEY
        self.base_url = base_url
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(timeout=NS_TIMEOUT)

    async def __aenter__(self) -> NsApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def fetch_arrivals(self, station: str, *, max_journeys: int = 40) -> list[ArrivalRecord]:
        """Fetch current arrivals for a station code (e.g. ``"UT"``)."""
        if not self.api_key:
            raise NsApiAuthError("NS_API_KEY is not configured")
        params = {"station": station.strip().upper(), "maxJourneys": str(max_journeys)}
        try:
            resp = await self._client.get(
                self.base_url, params=params, headers={"Ocp-Apim-Subscription-Key": self.api_key}
            )
        except httpx.HTTPError as exc:
            raise NsApiError(f"NS API request failed for {station}: {exc!r}") from exc
        if resp.status_code in (401, 403):
            raise NsApiAuthError(f"NS API rejected the subscription key (HTTP {resp.status_code})")
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after", "")
            raise NsApiRateLimited("NS API rate limit reached", float(retry) if retry.isdigit() else None)
        if resp.status_code != 200:
            raise NsApiError(f"NS API returned HTTP {resp.status_code} for {station}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise NsApiError("NS API returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise NsApiError("NS API returned a non-object JSON body")
        return parse_arrivals(data, station)


def upsert_arrivals(
    records: Iterable[ArrivalRecord], session_factory: sessionmaker[Session] | None = None
) -> int:
    """Store records with ``source="nsapi"``. Never overwrites a row that came from RDT.

    Re-polling the same train updates ``actual_at``/``delay_min``/``cancelled`` (latest wins).
    Returns the number of records submitted.
    """
    rows = [
        {
            "station": r.station,
            "train_number": r.train_number,
            "planned_at": r.planned_at,
            "actual_at": r.actual_at,
            "delay_min": r.delay_min,
            "cancelled": r.cancelled,
            "source": "nsapi",
        }
        for r in records
    ]
    if not rows:
        return 0
    stmt = sqlite_insert(Arrival)
    stmt = stmt.on_conflict_do_update(
        index_elements=["station", "train_number", "planned_at"],
        set_={
            "actual_at": stmt.excluded.actual_at,
            "delay_min": stmt.excluded.delay_min,
            "cancelled": stmt.excluded.cancelled,
            "source": stmt.excluded.source,
        },
        where=Arrival.source != "rdt",
    )
    sf = session_factory or get_session_factory()
    with sf() as session:
        session.execute(stmt, rows)
        session.commit()
    return len(rows)
