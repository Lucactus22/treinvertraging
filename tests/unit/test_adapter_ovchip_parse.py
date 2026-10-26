"""Pure parsing/pairing tests for the OV-chipkaart adapter (no browser).

The CSV fixtures are SYNTHETIC: handwritten in the assumed layout of the OV-chipkaart export
(Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie;Klasse;Product;Opmerkingen;Naam;Kaartnummer).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from treinvertraging.adapters import ovchip
from treinvertraging.adapters.ovchip import (
    parse_amount,
    parse_csv,
    parse_date,
    parse_time,
    rows_from_json,
    rows_from_table,
    rows_to_trips,
    trips_from_csv,
)
from treinvertraging.domain import Source
from treinvertraging.errors import LayoutChanged
from treinvertraging.stations import StationIndex

FIX = Path(__file__).parent.parent / "fixtures" / "ovchip"
AMS = ZoneInfo("Europe/Amsterdam")


@pytest.fixture(scope="module")
def stations() -> StationIndex:
    return StationIndex.bundled()


def at(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=AMS)


# --------------------------------------------------------------------------- values


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("€ 3,45", Decimal("3.45")),
        ("3,45", Decimal("3.45")),
        ("€3,45", Decimal("3.45")),
        ("\xa0€\xa012,00", Decimal("12.00")),
        ("-3,45", Decimal("-3.45")),
        ("€ -3,45", Decimal("-3.45")),
        ("1.234,56", Decimal("1234.56")),
        ("1.234", Decimal("1234")),
        ("3.45", Decimal("3.45")),
        ("0,00", Decimal("0.00")),
        ("+50,00", Decimal("50.00")),
        ("", None),
        ("   ", None),
        ("n.v.t.", None),
        (None, None),
    ],
)
def test_parse_amount(text: str | None, expected: Decimal | None) -> None:
    assert parse_amount(text) == expected


def test_parse_date_and_time_variants() -> None:
    assert parse_date("05-10-2026") == date(2026, 10, 5)
    assert parse_date("5/10/2026") == date(2026, 10, 5)
    assert parse_date("05.10.26") == date(2026, 10, 5)
    assert parse_date("2026-10-05") == date(2026, 10, 5)
    assert parse_date("ma 05-10-2026") == date(2026, 10, 5)
    assert parse_date("maandag 5 oktober 2026") == date(2026, 10, 5)
    assert parse_date("31-02-2026") is None
    assert parse_date("") is None
    assert parse_time("08:15") == parse_time("8.15") == parse_time("08:15:00")
    assert parse_time("05-10-2026 23:40").hour == 23  # type: ignore[union-attr]
    assert parse_time("24:10") is None
    assert parse_time("") is None


# --------------------------------------------------------------------------- csv


def test_full_row_export(stations: StationIndex) -> None:
    data = b"\xef\xbb\xbf" + (FIX / "export_full.csv").read_bytes()  # BOM like Excel/Windows exports
    trips = trips_from_csv(data, stations=stations, account_last4="1234")
    # 7 data lines: 1 top-up, 1 other card, 1 missing check-out -> 5 trips for card 1234
    assert [t.checkin_at for t in trips] == [
        at(2026, 10, 5, 8, 15),
        at(2026, 10, 6, 23, 40),
        at(2026, 10, 7, 17, 30),
        at(2026, 10, 8, 7, 50),
        at(2026, 10, 9, 18, 5),
    ]
    first, midnight, missing, thousands, bus = trips
    assert (first.from_station, first.to_station) == ("UT", "ASD")
    assert first.checkout_at == at(2026, 10, 5, 9, 2)
    assert first.fare_eur == Decimal("9.60")
    assert first.product == "NS Dal Voordeel"
    assert first.operator == "NS"
    assert first.source is Source.OVCHIP
    assert first.card_id == "**** **** **** 1234"
    assert first.checkin_at.tzinfo is not None
    # original names stay in raw
    assert first.raw["from_name"] == "Utrecht Centraal"
    assert first.raw["rows"][0]["Vertrek"] == "Utrecht Centraal"
    assert first.raw["incomplete"] is False

    # check-out after midnight lands on the next day
    assert midnight.checkout_at == at(2026, 10, 7, 0, 35)
    assert (midnight.from_station, midnight.to_station) == ("ASD", "UT")

    # missing check-out: kept, without destination/fare
    assert missing.checkout_at is None and missing.to_station is None and missing.fare_eur is None
    assert missing.raw["incomplete"] is True

    assert thousands.fare_eur == Decimal("1234.56")
    assert thousands.to_station == "AMF"

    # non-NS operator is kept with the operator set; bus-stop style names are not fuzzy matched to stations
    assert bus.operator == "Arriva"
    assert bus.from_station == "UTVR"
    assert bus.to_station == "Utrecht, Jaarbeursplein"
    assert bus.raw["to_station_unresolved"] is True


def test_other_cards_are_filtered_only_when_card_known(stations: StationIndex) -> None:
    data = (FIX / "export_full.csv").read_bytes()
    all_cards = trips_from_csv(data, stations=stations)
    assert len(all_cards) == 6  # the 5678 trip is included when no card is configured
    other = [t for t in all_cards if t.checkin_at == at(2026, 10, 9, 12, 0)]
    assert other and other[0].card_id == "**** **** **** 5678"
    assert len(trips_from_csv(data, stations=stations, account_last4="5678")) == 1


def test_split_rows_pairing(stations: StationIndex) -> None:
    trips = trips_from_csv((FIX / "export_split.csv").read_bytes(), stations=stations, account_last4="1234")
    by_in = {t.checkin_at: t for t in trips}
    assert list(by_in) == sorted(by_in)

    # NS leg ends at 10:40, the HTM check-in at the same minute starts a new trip (transfer between operators)
    ns = by_in[at(2026, 10, 5, 10, 0)]
    assert (ns.from_station, ns.to_station) == ("RTD", "GVC")
    assert ns.checkout_at == at(2026, 10, 5, 10, 40)
    assert ns.fare_eur == Decimal("8.15") and ns.operator == "NS"
    tram = by_in[at(2026, 10, 5, 10, 40)]
    assert tram.operator == "HTM"
    assert tram.checkout_at == at(2026, 10, 5, 10, 55)
    assert tram.to_station == "Scheveningen, Haringkade"
    assert tram.fare_eur == Decimal("1.40")

    # check-in followed by another check-in: the first one has a missing check-out
    lost = by_in[at(2026, 10, 6, 20, 0)]
    assert lost.checkout_at is None and lost.from_station == "UT"
    # check-in 23:50, check-out 00:30 next day
    night = by_in[at(2026, 10, 6, 23, 50)]
    assert night.checkout_at == at(2026, 10, 7, 0, 30)
    assert night.fare_eur == Decimal("11.90")

    # the orphan check-out (08:00, card 1234, no check-in) is dropped, the 9999 card is ignored
    assert at(2026, 10, 8, 7, 0) not in by_in and len(trips) == 4
    assert all("9999" not in t.card_id for t in trips)


def test_since_filter_and_dedup(stations: StationIndex) -> None:
    data = (FIX / "export_full.csv").read_bytes()
    trips = trips_from_csv(data, stations=stations, account_last4="1234", since=date(2026, 10, 7))
    assert [t.checkin_at.date() for t in trips] == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    rows = parse_csv(data) + parse_csv(data)  # overlapping exports
    assert len(rows_to_trips(rows, stations=stations, account_last4="1234")) == 5


def test_trip_keys_are_stable_and_unique(stations: StationIndex) -> None:
    data = (FIX / "export_full.csv").read_bytes()
    a = trips_from_csv(data, stations=stations, account_last4="1234")
    b = trips_from_csv(data, stations=stations, account_last4="1234")
    assert [t.key() for t in a] == [t.key() for t in b]
    assert len({t.key() for t in a}) == len(a)


def test_max_trip_duration_separates_distant_rows(stations: StationIndex) -> None:
    csv_text = (
        "Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie;Klasse;Product;Opmerkingen;Naam;Kaartnummer\n"
        "05-10-2026;06:00;Utrecht Centraal;;;;Check-in;2e;NS;;X;3528 **** **** 1234\n"
        "05-10-2026;;;22:00;Amsterdam Centraal;9,00;Check-uit;2e;NS;;X;3528 **** **** 1234\n"
    )
    trips = trips_from_csv(csv_text, stations=stations)
    assert len(trips) == 1 and trips[0].checkout_at is None  # 16h later is not "the" check-out


def test_cp1252_and_comma_delimited_variants(stations: StationIndex) -> None:
    text = (
        "Datum,Check-in,Vertrek,Check-uit,Bestemming,Bedrag,Transactie\n"
        '05-10-2026,08:15,Utrecht Centraal,09:02,Amsterdam Centraal,"9,60",Reis\n'
    )
    trips = trips_from_csv(text.encode("cp1252"), stations=stations)
    assert len(trips) == 1 and trips[0].fare_eur == Decimal("9.60")


def test_header_aliases_and_preamble(stations: StationIndex) -> None:
    text = (
        "Reisoverzicht 3528 **** **** 1234\n"
        "\n"
        "datum;Incheck;Van;Uitcheck;Naar;Prijs;Type\n"
        "5-10-2026;8:15;Utrecht Centraal;9:02;Amsterdam Centraal;9,60;Reis\n"
    )
    trips = trips_from_csv(text, stations=stations)
    assert len(trips) == 1 and (trips[0].from_station, trips[0].to_station) == ("UT", "ASD")


def test_unrecognised_header_raises_layout_changed() -> None:
    with pytest.raises(LayoutChanged):
        parse_csv("foo;bar;baz\n1;2;3\n")
    assert parse_csv("") == []


def test_header_only_export_is_empty_not_an_error(stations: StationIndex) -> None:
    header = "Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie\n"
    assert trips_from_csv(header, stations=stations) == []


def test_single_station_column_layout(stations: StationIndex) -> None:
    """A check-out row that names its station in the "Vertrek" column is still understood."""
    text = (
        "Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie\n"
        "05-10-2026;08:15;Utrecht Centraal;;;;Check-in\n"
        "05-10-2026;;Amsterdam Centraal;09:02;;9,60;Check-uit\n"
    )
    trips = trips_from_csv(text, stations=stations)
    assert len(trips) == 1 and trips[0].to_station == "ASD" and trips[0].fare_eur == Decimal("9.60")


def test_dst_change_night(stations: StationIndex) -> None:
    """Last Sunday of October 2026 (25th): 03:00 -> 02:00; times keep their wall-clock meaning."""
    text = (
        "Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie\n"
        "24-10-2026;23:30;Utrecht Centraal;00:40;Amsterdam Centraal;9,00;Reis\n"
    )
    (trip,) = trips_from_csv(text, stations=stations)
    assert trip.checkin_at == at(2026, 10, 24, 23, 30) and trip.checkout_at == at(2026, 10, 25, 0, 40)


def test_raw_is_json_serialisable(stations: StationIndex) -> None:
    import json

    for t in trips_from_csv((FIX / "export_split.csv").read_bytes(), stations=stations):
        json.dumps(t.raw)


# --------------------------------------------------------------------------- table / json


def test_rows_from_table_like_dom(stations: StationIndex) -> None:
    table = [
        ["Datum", "Check-in", "Vertrek", "Check-uit", "Bestemming", "Bedrag"],
        ["ma 5 okt 2026", "08:15", "Utrecht Centraal", "09:02", "Amsterdam Centraal", "€ 9,60"],
        ["", "", "", "", "", ""],
    ]
    trips = rows_to_trips(rows_from_table(table), stations=stations)
    assert len(trips) == 1 and trips[0].fare_eur == Decimal("9.60")
    with pytest.raises(LayoutChanged):
        rows_from_table([["a", "b"], ["1", "2"]])


def test_rows_from_json_nested_and_iso(stations: StationIndex) -> None:
    payload = {
        "data": {
            "transactions": [
                {
                    "checkIn": {
                        "time": "2026-10-05T08:15:00+02:00",
                        "location": {"name": "Utrecht Centraal"},
                    },
                    "checkOut": {
                        "time": "2026-10-05T09:02:00+02:00",
                        "location": {"name": "Amsterdam Centraal"},
                    },
                    "amount": 9.6,
                    "product": "NS Dal Voordeel",
                    "cardNumber": "3528 **** **** 1234",
                },
                {"type": "Saldo opgeladen", "amount": 50},
            ]
        }
    }
    rows = rows_from_json(payload)
    assert len(rows) == 1
    (trip,) = rows_to_trips(rows, stations=stations, account_last4="1234", source_format="json")
    assert trip.checkin_at == at(2026, 10, 5, 8, 15) and trip.checkout_at == at(2026, 10, 5, 9, 2)
    assert (trip.from_station, trip.to_station) == ("UT", "ASD")
    assert trip.fare_eur == Decimal("9.6")
    assert rows_from_json({"nothing": [1, 2, 3]}) == []


def test_module_has_no_hardcoded_site_selectors() -> None:
    """Guard for the agent rule: no CSS ids of the OV site in code (they live in ovchip.yaml)."""
    src = Path(ovchip.__file__).read_text()
    for needle in ("#usernameUserInput", "#password", "#CybotCookiebot", "usernameUserInput"):
        assert needle not in src
