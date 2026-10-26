"""Importer for the Rijden de Treinen (RDT) open data archive (CC BY 4.0).

The archive publishes one gzipped CSV per month (``services-YYYY-MM.csv.gz``) and one per
year (``services-YYYY.csv.gz``) at https://opendata.rijdendetreinen.nl/public/services/.
A file has one row per stop of a train service (verified against the real September 2025
file on 2026-10-26):

* ``Service:Date``                  ``2025-09-01``
* ``Stop:Station code``             upper case, ``UT``, ``ASD``, ...
* ``Stop:Arrival time``             ISO 8601 with offset, ``2025-09-01T03:17:00+02:00``;
                                    empty at the first stop of a service
* ``Stop:Arrival delay``            integer minutes (empty when there is no arrival)
* ``Stop:Arrival cancelled``        ``true`` / ``false`` (empty when there is no arrival)
* ``Stop:Departure time/delay/cancelled``  the same for the departure; empty at the last stop

``import_month`` downloads the file (cached on disk with ETag / Last-Modified revalidation),
stream-parses it and upserts the stops of the requested stations into ``arrivals`` with
``source="rdt"``. RDT is authoritative: it overwrites rows written earlier by the live NS API
archiver for the same (station, train number, planned time).
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from treinvertraging.config import get_settings
from treinvertraging.db import get_session_factory
from treinvertraging.errors import LayoutChanged, TreinvertragingError
from treinvertraging.models import Arrival

log = logging.getLogger(__name__)

RDT_BASE_URL = "https://opendata.rijdendetreinen.nl/public/services"
RDT_TIMEOUT = httpx.Timeout(30.0, read=120.0)
BATCH_SIZE = 2000

REQUIRED_COLUMNS = (
    "Service:Date",
    "Service:Train number",
    "Stop:Station code",
    "Stop:Arrival time",
    "Stop:Arrival delay",
    "Stop:Arrival cancelled",
    "Stop:Departure time",
    "Stop:Departure delay",
    "Stop:Departure cancelled",
)


class RdtNotAvailable(TreinvertragingError):
    """Neither the monthly nor the yearly RDT file exists (yet) for the requested month."""


@dataclass
class ImportStats:
    year: int
    month: int
    url: str
    yearly: bool = False  # True when the yearly fallback file was used
    from_cache: bool = False  # True when the server answered 304 and the cached file was used
    rows_read: int = 0
    rows_kept: int = 0  # stop rows for the requested stations and month
    rows_upserted: int = 0


def cache_dir() -> Path:
    d = get_settings().DATA_DIR / "rdt-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def monthly_url(year: int, month: int, base_url: str = RDT_BASE_URL) -> str:
    return f"{base_url}/services-{year:04d}-{month:02d}.csv.gz"


def yearly_url(year: int, base_url: str = RDT_BASE_URL) -> str:
    return f"{base_url}/services-{year:04d}.csv.gz"


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def _read_sidecar(path: Path) -> dict[str, str]:
    try:
        data = json.loads(_sidecar(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> tuple[bool, bool] | None:
    """Fetch ``url`` into ``dest`` with conditional-GET caching.

    Returns ``None`` on 404, else ``(ok, from_cache)``. The file is written to ``dest.part`` and
    moved into place only when complete, so an interrupted download never corrupts the cache.
    """
    headers: dict[str, str] = {}
    meta = _read_sidecar(dest) if dest.exists() else {}
    if meta.get("etag"):
        headers["If-None-Match"] = meta["etag"]
    elif meta.get("last_modified"):
        headers["If-Modified-Since"] = meta["last_modified"]

    part = dest.with_name(dest.name + ".part")
    try:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code == 404:
                return None
            if resp.status_code == 304 and dest.exists():
                return True, True
            resp.raise_for_status()
            expected = resp.headers.get("content-length")
            written = 0
            with part.open("wb") as fh:
                async for chunk in resp.aiter_raw():
                    fh.write(chunk)
                    written += len(chunk)
            if expected is not None and expected.isdigit() and int(expected) != written:
                raise httpx.TransportError(f"truncated download: {written} of {expected} bytes")
            new_meta = {
                "etag": resp.headers.get("etag", ""),
                "last_modified": resp.headers.get("last-modified", ""),
                "url": url,
            }
        os.replace(part, dest)
        _sidecar(dest).write_text(json.dumps(new_meta), encoding="utf-8")
        return True, False
    finally:
        part.unlink(missing_ok=True)


def _parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def _parse_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _row_to_arrival(row: dict[str, str]) -> dict[str, Any] | None:
    """Convert one RDT stop row into an ``arrivals`` values dict, or None if it has no times.

    planned_at is the arrival time, or the departure time at the first stop of a service.
    """
    arr_time = row["Stop:Arrival time"].strip()
    if arr_time:
        planned_s, delay_s, cancelled_s = arr_time, row["Stop:Arrival delay"], row["Stop:Arrival cancelled"]
    else:
        planned_s = row["Stop:Departure time"].strip()
        delay_s, cancelled_s = row["Stop:Departure delay"], row["Stop:Departure cancelled"]
    if not planned_s:
        return None
    try:
        planned = datetime.fromisoformat(planned_s)
    except ValueError:
        return None
    if planned.tzinfo is None:
        return None
    cancelled = _parse_bool(cancelled_s)
    delay = _parse_int(delay_s) or 0
    return {
        "station": row["Stop:Station code"].strip().upper(),
        "train_number": row["Service:Train number"].strip(),
        "planned_at": planned,
        "actual_at": None if cancelled else planned + timedelta(minutes=delay),
        "delay_min": delay,
        "cancelled": cancelled,
        "source": "rdt",
    }


def _flush(session: Session, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    stmt = sqlite_insert(Arrival)
    stmt = stmt.on_conflict_do_update(
        index_elements=["station", "train_number", "planned_at"],
        set_={
            "actual_at": stmt.excluded.actual_at,
            "delay_min": stmt.excluded.delay_min,
            "cancelled": stmt.excluded.cancelled,
            "source": stmt.excluded.source,
        },
    )
    session.execute(stmt, rows)
    rows.clear()


def parse_and_upsert(
    path: Path,
    stations: set[str],
    session_factory: sessionmaker[Session],
    *,
    month_prefix: str | None = None,
) -> ImportStats:
    """Blocking: stream-parse ``path`` (gzip CSV) and upsert matching stops. Run in a thread.

    ``month_prefix`` (``"2025-09"``) restricts rows by ``Service:Date``; it is used with the
    yearly file. Returns stats with ``year``/``month``/``url`` left to the caller.
    """
    wanted = {s.strip().upper() for s in stations if s and s.strip()}
    stats = ImportStats(year=0, month=0, url=str(path))
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh, session_factory() as session:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise LayoutChanged(f"RDT file {path.name} lacks expected columns: {missing}")
        batch: list[dict[str, Any]] = []
        for row in reader:
            stats.rows_read += 1
            if row["Stop:Station code"].strip().upper() not in wanted:
                continue
            if month_prefix and not row["Service:Date"].startswith(month_prefix):
                continue
            values = _row_to_arrival(row)
            if values is None:
                continue
            stats.rows_kept += 1
            batch.append(values)
            if len(batch) >= BATCH_SIZE:
                stats.rows_upserted += len(batch)
                _flush(session, batch)
        stats.rows_upserted += len(batch)
        _flush(session, batch)
        session.commit()
    return stats


async def import_month(
    year: int,
    month: int,
    stations: Iterable[str],
    session_factory: sessionmaker[Session] | None = None,
    *,
    base_url: str = RDT_BASE_URL,
    client: httpx.AsyncClient | None = None,
) -> ImportStats:
    """Download (or revalidate the cached copy of) the RDT file for ``year``-``month`` and import it.

    Tries the monthly file first and falls back to the yearly file when the monthly one answers
    404 (RDT publishes the monthly file only after the month ended and may only have a yearly
    file for older periods). Only stop rows for ``stations`` (station codes) are kept. Raises
    ``RdtNotAvailable`` when neither file exists.
    """
    station_set = {s.strip().upper() for s in stations if s and s.strip()}
    sf = session_factory or get_session_factory()
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=RDT_TIMEOUT, follow_redirects=True)
    try:
        candidates = [(monthly_url(year, month, base_url), False), (yearly_url(year, base_url), True)]
        for url, yearly in candidates:
            dest = cache_dir() / url.rsplit("/", 1)[1]
            result = await _download(http, url, dest)
            if result is None:
                log.info("RDT file not found: %s", url)
                continue
            _, from_cache = result
            prefix = f"{year:04d}-{month:02d}" if yearly else None
            stats = await asyncio.to_thread(parse_and_upsert, dest, station_set, sf, month_prefix=prefix)
            stats.year, stats.month, stats.url = year, month, url
            stats.yearly, stats.from_cache = yearly, from_cache
            log.info(
                "RDT %s: read %d rows, kept %d, upserted %d (cache=%s)",
                dest.name,
                stats.rows_read,
                stats.rows_kept,
                stats.rows_upserted,
                from_cache,
            )
            return stats
        raise RdtNotAvailable(f"no RDT services file for {year:04d}-{month:02d} (monthly or yearly)")
    finally:
        if own_client:
            await http.aclose()
