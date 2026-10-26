"""Archive live arrivals for the stations the household travels to.

The NS API has no history, so a scheduler job calls ``archive_once`` every ~10 minutes with
``frequent_destinations(n)``. Each call polls one request per station (sequentially, with a
short pause) to stay inside the free-tier quota. The stored rows bridge the weeks before RDT
publishes the month.

Extra stations to always archive can be set with ``ARCHIVE_EXTRA_STATIONS`` (comma/space
separated station codes) as an environment variable or in ``settings.yaml``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from treinvertraging.config import get_settings
from treinvertraging.db import get_session_factory
from treinvertraging.delaydata.nsapi import (
    NsApiAuthError,
    NsApiClient,
    NsApiError,
    NsApiRateLimited,
    upsert_arrivals,
)
from treinvertraging.models import Trip

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 10
POLL_PAUSE_S = 0.5


@dataclass
class ArchiveResult:
    stored: dict[str, int] = field(default_factory=dict)  # station -> records stored
    errors: dict[str, str] = field(default_factory=dict)  # station -> error message
    aborted: bool = False  # stopped early (bad key / rate limit)


def _split_codes(raw: object) -> list[str]:
    if isinstance(raw, str):
        items: Iterable[object] = raw.replace(",", " ").split()
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    return [str(i).strip().upper() for i in items if str(i).strip()]


def extra_stations() -> list[str]:
    """Extra station codes from ``ARCHIVE_EXTRA_STATIONS`` (settings attr, env var or settings.yaml)."""
    settings = get_settings()
    value = getattr(settings, "ARCHIVE_EXTRA_STATIONS", None)
    if value is None:
        value = os.environ.get("ARCHIVE_EXTRA_STATIONS")
    if value is None:
        try:
            data = yaml.safe_load((settings.CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if isinstance(data, dict):
            value = next((v for k, v in data.items() if str(k).lower() == "archive_extra_stations"), None)
    return _split_codes(value)


def frequent_destinations(
    n: int = DEFAULT_TOP_N,
    session: Session | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> list[str]:
    """Top ``n`` ``trips.to_station`` values by trip count, plus the configured extra stations.

    Ties are broken alphabetically so the result is stable. Extras are appended (deduplicated).
    """
    stmt = (
        select(Trip.to_station)
        .where(Trip.to_station.is_not(None), Trip.to_station != "")
        .group_by(Trip.to_station)
        .order_by(func.count().desc(), Trip.to_station)
        .limit(max(n, 0))
    )
    if session is not None:
        found = session.execute(stmt).scalars().all()
    else:
        with (session_factory or get_session_factory())() as s:
            found = s.execute(stmt).scalars().all()
    result: list[str] = []
    for code in [*(c.strip().upper() for c in found if c), *extra_stations()]:
        if code and code not in result:
            result.append(code)
    return result


async def archive_once(
    stations: Iterable[str] | None = None,
    *,
    client: NsApiClient | None = None,
    session_factory: sessionmaker[Session] | None = None,
    pause_s: float = POLL_PAUSE_S,
) -> ArchiveResult:
    """Poll the NS arrivals board of each station once and store the observed times.

    ``stations=None`` uses ``frequent_destinations()``. A missing API key makes this a no-op
    (logged); an auth failure or rate limit aborts the remaining stations; other per-station
    errors are recorded in ``errors`` and polling continues.
    """
    codes = list(
        dict.fromkeys(
            _split_codes(list(stations))
            if stations is not None
            else frequent_destinations(session_factory=session_factory)
        )
    )
    result = ArchiveResult()
    if not codes:
        return result
    own_client = client is None
    api = client or NsApiClient()
    try:
        if not api.api_key:
            log.warning("NS_API_KEY not set; skipping arrivals archive")
            result.aborted = True
            return result
        for i, code in enumerate(codes):
            if i and pause_s > 0:
                await asyncio.sleep(pause_s)
            try:
                records = await api.fetch_arrivals(code)
            except (NsApiAuthError, NsApiRateLimited) as exc:
                log.warning("arrivals archive aborted at %s: %s", code, exc)
                result.errors[code] = str(exc)
                result.aborted = True
                break
            except NsApiError as exc:
                log.warning("arrivals fetch failed for %s: %s", code, exc)
                result.errors[code] = str(exc)
                continue
            result.stored[code] = await asyncio.to_thread(upsert_arrivals, records, session_factory)
    finally:
        if own_client:
            await api.aclose()
    return result
