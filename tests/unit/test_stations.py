from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from treinvertraging.stations import (
    FALLBACK_STATIONS_URL,
    STATIONS_INDEX_URL,
    StationIndex,
    StationsError,
    cache_path,
    latest_station_csv_url,
    normalize,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "stations" / "stations-sample.csv"
INDEX_HTML = (
    '<a href="../">../</a>\n'
    '<a href="stations-2022-01.csv">stations-2022-01.csv</a>\n'
    '<a href="stations-2023-09-nl.csv">stations-2023-09-nl.csv</a>\n'
    '<a href="stations-2023-09.csv">stations-2023-09.csv</a>\n'
    '<a href="stations-2024-03-nl.csv">stations-2024-03-nl.csv</a>\n'
)


@pytest.fixture(scope="module")
def idx() -> StationIndex:
    return StationIndex.from_csv(FIXTURE)


def test_fixture_loaded(idx):
    assert len(idx) == 31
    assert "UT" in idx and "ut" in idx and "XX" not in idx


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("Utrecht Centraal", "UT"),
        ("Utrecht C.", "UT"),
        ("utrecht c", "UT"),
        ("UTRECHT CS", "UT"),
        ("Utrecht Centraal station", "UT"),
        ("  Utrecht   Centraal ", "UT"),
        ("Utrecht", "UT"),  # city name -> biggest station
        ("Amsterdam Centraal", "ASD"),
        ("Amsterdam C.", "ASD"),
        ("Amsterdam", "ASD"),
        ("Amsterdm C", "ASD"),  # official short name
        ("Amsterdam Zuid", "ASDZ"),
        ("Amsterdam Bijlmer ArenA", "ASB"),
        ("Amsterdam Bijlmer Arena", "ASB"),
        ("'s-Hertogenbosch", "HT"),
        ("s-Hertogenbosch", "HT"),
        ("Den Bosch", "HT"),
        ("Dn Bosch O", "HTO"),
        ("Den Haag Centraal", "GVC"),
        ("Den Haag HS", "GV"),
        ("Den Haag", "GVC"),
        ("Rotterdam Centraal", "RTD"),
        ("Rotterdam", "RTD"),
        ("Schiphol", "SHL"),
        ("Schiphol Airport", "SHL"),
        ("Eindhoven", "EHV"),
        ("Eindhoven Centraal", "EHV"),
        ("Breda", "BD"),
        ("Breda-Prinsenbeek", "BDPB"),
        ("Breda Prinsenbeek", "BDPB"),
        ("Leiden Centraal", "LEDN"),
        ("Leiden", "LEDN"),
        ("Brussel-Zuid Midi", "BRUSZ"),
        # raw station codes
        ("UT", "UT"),
        ("ASD", "ASD"),
        # typos (fuzzy)
        ("Utrech Centraal", "UT"),
        ("Rotterdam Cetnraal", "RTD"),
        ("Amsterdam Slotrdijk", "ASS"),
    ],
)
def test_code_for(idx, name, code):
    assert idx.code_for(name) == code


@pytest.mark.parametrize("name", [None, "", "   ", "Foo Bar Baz", "Xyzzy", "Ut", "Ede"])
def test_code_for_unknown(idx, name):
    assert idx.code_for(name) is None


def test_ambiguous_fuzzy_returns_none():
    from treinvertraging.stations import Station

    st = [
        Station("AAA", "Zandvoort Noord", "Zandvoort Noord", "Zandvoort Noord", type="stoptreinstation"),
        Station("AAB", "Zandvoort Zuid", "Zandvoort Zuid", "Zandvoort Zuid", type="stoptreinstation"),
    ]
    i = StationIndex(st)
    assert i.code_for("Zandvoort") is None  # same rank -> ambiguous
    assert i.code_for("Zandvoort Noord") == "AAA"


def test_threshold_controls_fuzzy(idx):
    strict = StationIndex.from_csv(FIXTURE, fuzzy_threshold=100)
    assert strict.code_for("Utrech Centraal") is None
    assert strict.code_for("Utrecht Centraal") == "UT"  # exact never needs fuzzy


def test_aliases():
    i = StationIndex.from_csv(FIXTURE, aliases={"Het Centraal": "ASD"})
    assert i.code_for("het centraal") == "ASD"
    with pytest.raises(KeyError):
        i.add_alias("nope", "ZZZZ")


def test_name_for(idx):
    assert idx.name_for("UT") == "Utrecht Centraal"
    assert idx.name_for("ut") == "Utrecht Centraal"
    assert idx.name_for("UT", "medium") == "Utrecht C."
    assert idx.name_for("UT", "short") == "Utrecht C"
    assert idx.name_for("XX") is None
    assert idx.get("UT").country == "NL"
    assert idx.get("BRUSZ").country == "B"


def test_roundtrip_all_codes(idx):
    for code in idx.codes():
        assert idx.code_for(idx.name_for(code)) == code


def test_normalize():
    assert normalize("Utrecht C.") == "utrecht centraal"
    assert normalize("’s-Hertogenbosch") == "s hertogenbosch"
    assert normalize("Zürich HB") == "zurich hb"
    assert normalize("C") == "c"


def test_bad_csv():
    with pytest.raises(StationsError):
        StationIndex.from_csv_text("a,b\n1,2\n")
    with pytest.raises(StationsError):
        StationIndex.from_csv_text("code,name_long\n")


def test_bundled_list_works():
    i = StationIndex.bundled()
    assert len(i) > 300
    assert i.code_for("Utrecht Centraal") == "UT"
    assert i.code_for("Zwolle") == "ZL"


# --------------------------------------------------------------------------- cache / download


def test_load_prefers_cache_then_bundled(tmp_path):
    assert len(StationIndex.load(tmp_path)) > 300  # no cache -> bundled
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(FIXTURE.read_bytes())
    assert len(StationIndex.load(tmp_path)) == 31
    path.write_text("garbage")
    assert len(StationIndex.load(tmp_path)) > 300  # corrupt cache ignored


def test_latest_station_csv_url():
    assert latest_station_csv_url(INDEX_HTML) == STATIONS_INDEX_URL + "stations-2023-09.csv"
    assert latest_station_csv_url("<html></html>") is None


def _big_csv() -> str:
    # The real list has > 500 stations; the downloader refuses tiny files.
    bundled = Path(__file__).resolve().parents[2] / "src" / "treinvertraging" / "defaults" / "stations.csv"
    return bundled.read_text(encoding="utf-8")


@respx.mock
async def test_refresh_downloads_latest(tmp_path):
    csv_text = _big_csv()
    respx.get(STATIONS_INDEX_URL).mock(return_value=httpx.Response(200, text=INDEX_HTML))
    route = respx.get(STATIONS_INDEX_URL + "stations-2023-09.csv").mock(
        return_value=httpx.Response(200, content=csv_text.encode())
    )
    index = StationIndex.from_csv(FIXTURE)
    assert len(index) == 31
    path = await index.refresh(tmp_path)
    assert route.called
    assert path == tmp_path / "rdt-cache" / "stations.csv"
    assert path.read_text(encoding="utf-8") == csv_text
    assert len(index) > 300 and index.code_for("Zwolle") == "ZL"
    assert not list(path.parent.glob("*.tmp"))
    assert len(StationIndex.load(tmp_path)) == len(index)


@respx.mock
async def test_refresh_falls_back_to_default_url_when_index_fails(tmp_path):
    respx.get(STATIONS_INDEX_URL).mock(return_value=httpx.Response(500))
    respx.get(FALLBACK_STATIONS_URL).mock(return_value=httpx.Response(200, content=_big_csv().encode()))
    path = await StationIndex().refresh(tmp_path)
    assert path.is_file()


@respx.mock
async def test_refresh_failure_keeps_old_cache(tmp_path):
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(FIXTURE.read_bytes())
    respx.get(STATIONS_INDEX_URL).mock(return_value=httpx.Response(200, text=INDEX_HTML))
    respx.get(STATIONS_INDEX_URL + "stations-2023-09.csv").mock(
        return_value=httpx.Response(200, text="<html>nope</html>")
    )
    respx.get(FALLBACK_STATIONS_URL).mock(return_value=httpx.Response(404))
    index = StationIndex.from_csv(FIXTURE)
    with pytest.raises(StationsError):
        await index.refresh(tmp_path)
    assert path.read_bytes() == FIXTURE.read_bytes()
    assert len(index) == 31


@respx.mock
async def test_refresh_rejects_tiny_list(tmp_path):
    respx.get(STATIONS_INDEX_URL).mock(return_value=httpx.Response(200, text=INDEX_HTML))
    respx.get(STATIONS_INDEX_URL + "stations-2023-09.csv").mock(
        return_value=httpx.Response(200, content=FIXTURE.read_bytes())
    )
    respx.get(FALLBACK_STATIONS_URL).mock(return_value=httpx.Response(200, content=FIXTURE.read_bytes()))
    with pytest.raises(StationsError):
        await StationIndex().refresh(tmp_path)
    assert not cache_path(tmp_path).exists()
