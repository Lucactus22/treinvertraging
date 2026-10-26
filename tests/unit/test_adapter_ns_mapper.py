"""Pure unit tests for the tolerant Mijn NS JSON mapper and helpers (no browser)."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests.unit.test_adapter_ns_support import NS_FIXTURES
from treinvertraging.adapters.ns import (
    _Mapping,
    map_claims,
    map_trips,
    mask_card,
    parse_datetime,
    parse_eur,
    valid_iban,
)

AMS = ZoneInfo("Europe/Amsterdam")


def _load(name: str):
    return json.loads((NS_FIXTURES / name).read_text(encoding="utf-8"))


def _mapping(**kw) -> _Mapping:
    return _Mapping(default_card_id="ns-1", **kw)


def test_nested_shape_maps_trips() -> None:
    trips = map_trips(_load("travel_history.json"), _mapping())
    assert len(trips) == 3
    t1 = trips[0]
    assert t1.source == "ns"
    assert t1.from_station == "UT" and t1.to_station == "ASD"
    assert t1.checkin_at == datetime(2026, 10, 12, 8, 15, tzinfo=AMS)
    assert t1.checkout_at == datetime(2026, 10, 12, 9, 2, tzinfo=AMS)
    assert t1.checkin_at.utcoffset().total_seconds() == 7200
    assert t1.fare_eur == Decimal("9.40")  # fareInCents
    assert t1.product == "Dal Voordeel" and t1.operator == "NS"
    assert t1.ns_delay_flag is True
    assert t1.ns_claim_ref == "/claim.html?ref=T1"
    assert t1.card_id == "3528********8901"  # masked card from the data
    assert t1.raw["from_name"] == "Utrecht Centraal"  # raw name kept
    assert t1.raw["record"]["id"] == "t1"
    t2 = trips[1]
    assert t2.ns_delay_flag is False and t2.ns_claim_ref is None
    assert trips[2].checkin_at.year == 2025 and trips[2].ns_delay_flag is None


def test_flat_dutch_shape_with_time_only_fields() -> None:
    trips = map_trips(_load("travel_history_flat.json"), _mapping())
    assert len(trips) == 2
    a, b = trips
    assert a.checkin_at == datetime(2026, 10, 13, 17, 5, tzinfo=AMS)
    assert a.checkout_at == datetime(2026, 10, 13, 17, 52, tzinfo=AMS)
    assert (a.from_station, a.to_station) == ("ASD", "UT")
    assert a.fare_eur == Decimal("9.40")
    assert a.ns_delay_flag is True and a.ns_claim_ref == "GT-7781"
    assert a.card_id == "ns-1"  # no card in the data -> account fallback
    # check-out after midnight rolls over to the next day
    assert b.checkin_at == datetime(2026, 10, 14, 23, 50, tzinfo=AMS)
    assert b.checkout_at == datetime(2026, 10, 15, 0, 30, tzinfo=AMS)
    assert b.fare_eur == Decimal("12.10") and b.ns_delay_flag is False
    assert b.to_station == "EHV"


def test_card_from_data_can_be_disabled() -> None:
    trips = map_trips(_load("travel_history.json"), _mapping(card_from_data=False))
    assert {t.card_id for t in trips} == {"ns-1"}


def test_unknown_stations_are_kept_as_raw_text() -> None:
    data = {"x": [{"checkInTime": "2026-10-12T08:15:00Z", "checkInStation": "Nergens Oost", "fare": 5.5}]}
    (t,) = map_trips(data, _mapping())
    assert t.from_station == "Nergens Oost" and t.to_station is None
    assert t.checkin_at == datetime(2026, 10, 12, 10, 15, tzinfo=AMS)  # Z -> Amsterdam
    assert t.fare_eur == Decimal("5.50")


def test_records_without_checkin_or_station_are_ignored() -> None:
    assert map_trips({"a": [{"foo": 1}, {"checkIn": "2026-10-12T08:00:00"}], "b": "x"}, _mapping()) == []
    assert map_trips("not a dict", _mapping()) == []
    assert map_trips(None, _mapping()) == []


@pytest.mark.parametrize(
    ("value", "key", "unit", "expected"),
    [
        (940, "fareInCents", "auto", "9.40"),
        (940, "fare", "auto", "9.40"),  # whole number >= 100 -> cents
        (9, "fare", "auto", "9.00"),  # small whole number -> euros
        (9.4, "fare", "auto", "9.40"),
        (940, "fare", "euros", "940.00"),
        (9, "fare", "cents", "0.09"),
        ("€ 9,40", "price", "auto", "9.40"),
        ("12,10", "bedrag", "auto", "12.10"),
        ("1.234,50", "bedrag", "auto", "1234.50"),
        ("9.40", "amount", "auto", "9.40"),
        ("-9,40", "amount", "auto", "9.40"),
        ({"amount": 470, "currency": "EUR"}, "bedrag", "auto", "4.70"),
        ({"cents": 470}, "bedrag", "auto", "4.70"),
        (True, "fare", "auto", None),
        ("gratis", "fare", "auto", None),
        (None, "fare", "auto", None),
    ],
)
def test_parse_eur(value, key, unit, expected) -> None:
    got = parse_eur(value, key=key, unit=unit)
    assert (None if got is None else str(got)) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-12T08:15:00+02:00", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("2026-10-12T06:15:00Z", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("2026-10-12 08:15", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("12-10-2026 08:15", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("12/10/2026 08.15", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("ma 12 okt. 2026 08:15", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        (1791785700000, datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        (1791785700, datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("1791785700000", datetime(2026, 10, 12, 8, 15, tzinfo=AMS)),
        ("2026-10-12", None),  # date only
        ("08:15", None),  # time only
        ("hallo", None),
        (35, None),
        (True, None),
    ],
)
def test_parse_datetime(value, expected) -> None:
    assert parse_datetime(value) == expected


def test_delay_and_claim_markers() -> None:
    base = {"checkIn": {"station": "Utrecht Centraal", "time": "2026-10-12T08:15:00+02:00"}, "price": 5.5}
    (t,) = map_trips({**base, "isDelayed": True}, _mapping())
    assert t.ns_delay_flag is True
    (t,) = map_trips({**base, "compensation": {"eligible": False}}, _mapping())
    assert t.ns_delay_flag is False
    (t,) = map_trips({**base, "claimLink": {"href": "https://www.ns.nl/geld-terug/abc"}}, _mapping())
    assert t.ns_claim_ref == "https://www.ns.nl/geld-terug/abc" and t.ns_delay_flag is True
    (t,) = map_trips({**base, "claimable": True, "claimStatus": "open"}, _mapping())
    assert t.ns_delay_flag is True and t.ns_claim_ref is None  # claimStatus is not a handle


def test_fare_unit_override() -> None:
    data = {"checkIn": {"station": "Utrecht Centraal", "time": "2026-10-12T08:15:00+02:00"}, "fare": 940}
    assert map_trips(data, _mapping(fare_unit="euros"))[0].fare_eur == Decimal("940.00")
    assert map_trips(data, _mapping())[0].fare_eur == Decimal("9.40")


def test_claims_mapper() -> None:
    claims = map_claims(_load("claims.json"), _mapping())
    assert [c["reference"] for c in claims] == ["GTB-2026-12345", "GTB-2026-00077"]
    c = claims[0]
    assert c["date"] == "2026-10-12" and (c["from"], c["to"]) == ("UT", "ASD")
    assert c["status"] == "In behandeling" and c["amount"] == "4.70"
    assert claims[1]["amount"] == "12.10"
    # trip records are not mistaken for claims
    assert map_claims(_load("travel_history.json"), _mapping()) == []


def test_mask_card_and_iban() -> None:
    assert mask_card("3528 0123 4567 8901") == "3528********8901"
    assert mask_card("3528********8901") == "3528********8901"
    assert mask_card("12") is None and mask_card("abc") is None
    assert valid_iban("NL91 ABNA 0417 1643 00")
    assert not valid_iban("NL91ABNA0417164301")
    assert not valid_iban("garbage") and not valid_iban("")
    assert date(2026, 10, 12).isoformat() == "2026-10-12"
