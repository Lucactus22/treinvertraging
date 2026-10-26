from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from treinvertraging.domain import ClaimState, Source, TripDTO

AMS = ZoneInfo("Europe/Amsterdam")


def make_trip(**kw) -> TripDTO:
    base = {
        "source": Source.NS,
        "card_id": "3528 **** 1234",
        "checkin_at": datetime(2026, 9, 14, 8, 30, tzinfo=AMS),
        "checkout_at": None,
        "from_station": "UT",
        "to_station": "ASD",
        "fare_eur": None,
        "product": None,
        "operator": "NS",
        "ns_delay_flag": None,
        "ns_claim_ref": None,
        "raw": {},
    }
    base.update(kw)
    return TripDTO(**base)


def test_key_is_stable_and_hex() -> None:
    a, b = make_trip(), make_trip()
    assert a.key() == b.key()
    assert len(a.key()) == 64
    int(a.key(), 16)


def test_key_ignores_non_identity_fields() -> None:
    a = make_trip()
    b = make_trip(to_station="RTD", fare_eur=None, raw={"x": 1}, ns_delay_flag=True)
    assert a.key() == b.key()


def test_key_depends_on_identity_fields() -> None:
    base = make_trip().key()
    assert make_trip(source=Source.OVCHIP).key() != base
    assert make_trip(card_id="other").key() != base
    assert make_trip(from_station="ASD").key() != base
    assert make_trip(checkin_at=datetime(2026, 9, 14, 8, 31, tzinfo=AMS)).key() != base


def test_key_matches_documented_formula() -> None:
    import hashlib

    t = make_trip()
    expected = hashlib.sha256(f"ns|{t.card_id}|{t.checkin_at.isoformat()}|UT".encode()).hexdigest()
    assert t.key() == expected


def test_key_timezone_handling() -> None:
    # The isoformat includes the offset, so the same wall-clock time in different zones differs...
    ams = make_trip(checkin_at=datetime(2026, 9, 14, 8, 30, tzinfo=AMS))
    utc_wall = make_trip(checkin_at=datetime(2026, 9, 14, 8, 30, tzinfo=UTC))
    assert ams.key() != utc_wall.key()
    # ...while an identical instant expressed with the identical offset is stable.
    fixed = make_trip(checkin_at=datetime(2026, 9, 14, 8, 30, tzinfo=timezone(timedelta(hours=2))))
    assert fixed.key() == ams.key()  # Amsterdam is +02:00 in September (CEST)
    assert "+02:00" in ams.checkin_at.isoformat()


def test_claim_state_values() -> None:
    assert ClaimState.AWAITING_APPROVAL == "awaiting_approval"
    assert {s.value for s in ClaimState} >= {"detected", "submitting", "needs_human", "expired"}
