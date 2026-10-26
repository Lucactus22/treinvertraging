"""Tests for the RDT importer. No network: httpx is mocked with respx.

The fixture ``tests/fixtures/rdt/services-sample.csv.gz`` is handcrafted in the real RDT column
format (header and value formats verified against the real services-2025-09 file); the
train data itself is synthetic.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from treinvertraging.db import get_engine, get_session_factory
from treinvertraging.delaydata import rdt
from treinvertraging.errors import LayoutChanged
from treinvertraging.models import Arrival, Base

FIXTURE = Path(__file__).parent.parent / "fixtures" / "rdt" / "services-sample.csv.gz"
MONTH_URL = "https://opendata.rijdendetreinen.nl/public/services/services-2025-09.csv.gz"
YEAR_URL = "https://opendata.rijdendetreinen.nl/public/services/services-2025.csv.gz"


@pytest.fixture
def db(data_dir: Path):
    Base.metadata.create_all(get_engine())
    return get_session_factory()


def _get(db, station: str, train: str, planned_utc: datetime) -> Arrival | None:
    with db() as s:
        return s.execute(
            select(Arrival).where(
                Arrival.station == station, Arrival.train_number == train, Arrival.planned_at == planned_utc
            )
        ).scalar_one_or_none()


def test_fixture_is_real_gzip_with_real_header() -> None:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
    assert header[0] == "Service:RDT-ID"
    assert "Stop:Arrival cancelled" in header
    assert len(header) == 20


@respx.mock
async def test_import_month_filters_stations_and_maps_fields(db) -> None:
    respx.get(MONTH_URL).mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes(), headers={"etag": '"v1"'})
    )
    stats = await rdt.import_month(2025, 9, {"UT", "ASD"}, db)

    assert not stats.yearly and not stats.from_cache
    assert stats.rows_read == 38
    with db() as s:
        stations = set(s.execute(select(Arrival.station).distinct()).scalars())
        assert stations == {"UT", "ASD"}
        assert s.execute(select(func.count()).select_from(Arrival)).scalar_one() == stats.rows_upserted

    # delayed arrival: planned 14:30+02:00 = 12:30Z, +34 min
    a = _get(db, "ASD", "3045", datetime(2025, 9, 1, 12, 30, tzinfo=UTC))
    assert a is not None
    assert a.delay_min == 34 and not a.cancelled and a.source == "rdt"
    assert a.actual_at == datetime(2025, 9, 1, 13, 4, tzinfo=UTC)

    # first stop has no arrival: the departure time is used as planned_at
    dep = _get(db, "UT", "3045", datetime(2025, 9, 1, 12, 2, tzinfo=UTC))
    assert dep is not None and dep.delay_min == 33

    # cancelled arrival: flag set, no actual time
    c = _get(db, "ASD", "3049", datetime(2025, 9, 1, 13, 15, tzinfo=UTC))
    assert c is not None and c.cancelled and c.actual_at is None

    # after-midnight arrival keeps its real date
    night = _get(db, "UT", "1080", datetime(2025, 9, 1, 22, 12, tzinfo=UTC))
    assert night is not None and night.delay_min == 8


@respx.mock
async def test_import_is_idempotent_and_overrides_nsapi_rows(db) -> None:
    planned = datetime(2025, 9, 1, 12, 30, tzinfo=UTC)
    with db() as s:
        s.add(Arrival(station="ASD", train_number="3045", planned_at=planned, delay_min=1, source="nsapi"))
        s.commit()
    respx.get(MONTH_URL).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
    await rdt.import_month(2025, 9, {"ASD"}, db)
    await rdt.import_month(2025, 9, {"ASD"}, db)

    with db() as s:
        rows = s.execute(select(Arrival).where(Arrival.station == "ASD")).scalars().all()
        keys = {(r.train_number, r.planned_at) for r in rows}
        assert len(keys) == len(rows)
    a = _get(db, "ASD", "3045", planned)
    assert a is not None and a.delay_min == 34 and a.source == "rdt"


@respx.mock
async def test_second_import_uses_etag_and_cache(db, data_dir: Path) -> None:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=FIXTURE.read_bytes(),
            headers={"etag": '"v1"', "last-modified": "Sat, 04 Oct 2025 01:01:13 GMT"},
        )

    respx.get(MONTH_URL).mock(side_effect=handler)
    first = await rdt.import_month(2025, 9, {"UT"}, db)
    second = await rdt.import_month(2025, 9, {"UT"}, db)

    assert not first.from_cache and second.from_cache
    assert second.rows_upserted == first.rows_upserted > 0
    assert "if-none-match" not in calls[0] and calls[1]["if-none-match"] == '"v1"'
    cached = data_dir / "rdt-cache" / "services-2025-09.csv.gz"
    assert cached.read_bytes() == FIXTURE.read_bytes()
    assert (data_dir / "rdt-cache" / "services-2025-09.csv.gz.meta.json").exists()
    assert not list((data_dir / "rdt-cache").glob("*.part"))


@respx.mock
async def test_last_modified_used_when_no_etag(db) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-modified-since"))
        if seen[-1]:
            return httpx.Response(304)
        return httpx.Response(
            200, content=FIXTURE.read_bytes(), headers={"last-modified": "Sat, 04 Oct 2025 01:01:13 GMT"}
        )

    respx.get(MONTH_URL).mock(side_effect=handler)
    await rdt.import_month(2025, 9, {"UT"}, db)
    stats = await rdt.import_month(2025, 9, {"UT"}, db)
    assert stats.from_cache
    assert seen == [None, "Sat, 04 Oct 2025 01:01:13 GMT"]


@respx.mock
async def test_falls_back_to_yearly_file_and_only_imports_requested_month(db) -> None:
    monthly = respx.get(MONTH_URL).mock(return_value=httpx.Response(404))
    respx.get(YEAR_URL).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
    stats = await rdt.import_month(2025, 9, {"ASD"}, db)

    assert monthly.called
    assert stats.yearly and stats.url == YEAR_URL
    with db() as s:
        planned = s.execute(select(Arrival.planned_at)).scalars().all()
    assert planned and all(p.month == 9 for p in planned)  # the October rows were skipped


@respx.mock
async def test_missing_files_raise(db) -> None:
    respx.get(MONTH_URL).mock(return_value=httpx.Response(404))
    respx.get(YEAR_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(rdt.RdtNotAvailable):
        await rdt.import_month(2025, 9, {"UT"}, db)


@respx.mock
async def test_truncated_download_is_not_cached(db, data_dir: Path) -> None:
    body = FIXTURE.read_bytes()
    respx.get(MONTH_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-length": str(len(body) + 50)})
    )
    with pytest.raises(httpx.HTTPError):
        await rdt.import_month(2025, 9, {"UT"}, db)
    assert not list((data_dir / "rdt-cache").glob("services-*"))


@respx.mock
async def test_unexpected_columns_raise_layout_changed(db) -> None:
    bad = gzip.compress(b"a,b,c\n1,2,3\n")
    respx.get(MONTH_URL).mock(return_value=httpx.Response(200, content=bad))
    with pytest.raises(LayoutChanged):
        await rdt.import_month(2025, 9, {"UT"}, db)


@respx.mock
async def test_empty_station_set_imports_nothing(db) -> None:
    respx.get(MONTH_URL).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
    stats = await rdt.import_month(2025, 9, set(), db)
    assert stats.rows_kept == 0 and stats.rows_read == 38
