from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from treinvertraging.domain import Source, TripDTO
from treinvertraging.rules import (
    Rules,
    RulesError,
    add_months,
    assess,
    find_rules_file,
    load_rules,
    parse_rules,
)

AMS = ZoneInfo("Europe/Amsterdam")
REPO = Path(__file__).resolve().parents[2]
CHECKIN = datetime(2026, 9, 10, 8, 0, tzinfo=AMS)
TODAY = date(2026, 10, 26)


@pytest.fixture(scope="module")
def rules() -> Rules:
    return load_rules(REPO / "config.example" / "rules.yaml")


def make_trip(**kw) -> TripDTO:
    base = dict(
        source=Source.NS,
        card_id="3528XXXX",
        checkin_at=CHECKIN,
        checkout_at=datetime(2026, 9, 10, 9, 0, tzinfo=AMS),
        from_station="UT",
        to_station="ASD",
        fare_eur=Decimal("10.00"),
        operator="NS",
        product="Vol tarief",
    )
    base.update(kw)
    return TripDTO(**base)


# (id, trip kwargs, delay, cancelled, ns_flag, eligible, pct, expected_eur, reason code)
CASES = [
    ("delay-0", {}, 0, False, False, False, 0, "0.00", "below_threshold"),
    ("delay-15", {}, 15, False, False, False, 0, "0.00", "below_threshold"),
    ("delay-29", {}, 29, False, False, False, 0, "0.00", "below_threshold"),
    ("delay-30", {}, 30, False, False, True, 50, "5.00", "eligible"),
    ("delay-45", {}, 45, False, False, True, 50, "5.00", "eligible"),
    ("delay-59", {}, 59, False, False, True, 50, "5.00", "eligible"),
    ("delay-60", {}, 60, False, False, True, 100, "10.00", "eligible"),
    ("delay-180", {}, 180, False, False, True, 100, "10.00", "eligible"),
    ("negative-delay", {}, -5, False, False, False, 0, "0.00", "below_threshold"),
    ("rounding-half-up", {"fare_eur": Decimal("5.55")}, 30, False, False, True, 50, "2.78", "eligible"),
    ("rounding-down", {"fare_eur": Decimal("7.33")}, 30, False, False, True, 50, "3.67", "eligible"),
    ("cancelled-counts-as-60", {}, None, True, False, True, 100, "10.00", "eligible"),
    ("cancelled-with-small-delay", {}, 10, True, False, True, 100, "10.00", "eligible"),
    ("cancelled-bigger-delay", {}, 90, True, False, True, 100, "10.00", "eligible"),
    ("ns-flag-no-data-lowest-tier", {}, None, False, True, True, 50, "5.00", "eligible"),
    ("no-data-no-flag", {}, None, False, False, False, 0, "0.00", "no_delay_data"),
    ("no-data-flag-false", {}, None, False, None, False, 0, "0.00", "no_delay_data"),
    ("data-overrides-flag", {}, 12, False, True, False, 0, "0.00", "below_threshold"),
    ("flag-and-data-60", {}, 75, False, True, True, 100, "10.00", "eligible"),
    ("min-payout-exact", {"fare_eur": Decimal("4.40")}, 30, False, False, True, 50, "2.20", "eligible"),
    (
        "below-min-payout",
        {"fare_eur": Decimal("4.38")},
        30,
        False,
        False,
        False,
        0,
        "0.00",
        "below_min_payout",
    ),
    (
        "below-min-payout-100",
        {"fare_eur": Decimal("2.19")},
        60,
        False,
        False,
        False,
        0,
        "0.00",
        "below_min_payout",
    ),
    ("min-payout-100-ok", {"fare_eur": Decimal("2.20")}, 60, False, False, True, 100, "2.20", "eligible"),
    ("zero-fare", {"fare_eur": Decimal("0")}, 60, False, False, False, 0, "0.00", "below_min_payout"),
    ("unknown-fare-still-eligible", {"fare_eur": None}, 45, False, False, True, 50, "0.00", "eligible"),
    ("excluded-operator", {"operator": "Arriva"}, 90, False, False, False, 0, "0.00", "excluded_operator"),
    (
        "excluded-operator-case",
        {"operator": "keolis"},
        90,
        False,
        False,
        False,
        0,
        "0.00",
        "excluded_operator",
    ),
    ("operator-none-is-ns", {"operator": None}, 40, False, False, True, 50, "5.00", "eligible"),
    ("missing-checkout", {"checkout_at": None}, 90, False, False, False, 0, "0.00", "no_checkout"),
    ("missing-checkout-cancelled", {"checkout_at": None}, None, True, False, False, 0, "0.00", "no_checkout"),
    (
        "unknown-operator-uses-default",
        {"operator": "Foo Rail"},
        60,
        False,
        False,
        True,
        100,
        "10.00",
        "eligible",
    ),
]


@pytest.mark.parametrize(
    ("kw", "delay", "cancelled", "flag", "eligible", "pct", "expected", "code"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_assess_table(rules, kw, delay, cancelled, flag, eligible, pct, expected, code):
    trip = make_trip(**kw)
    a = assess(trip, delay, cancelled, flag, today=TODAY, rules=rules)
    assert a.eligible is eligible
    assert a.percent == pct
    assert a.expected_eur == Decimal(expected)
    assert a.reason.startswith(code + ":")
    assert a.trip_key == trip.key()
    assert a.cancelled is cancelled
    assert a.deadline == date(2026, 12, 10)


def test_unknown_fare_reason_mentions_it(rules):
    a = assess(make_trip(fare_eur=None), 45, False, False, today=TODAY, rules=rules)
    assert "unknown" in a.reason


def test_evidence_and_delay_recorded(rules):
    a = assess(make_trip(), 34, False, True, today=TODAY, rules=rules)
    assert a.arrival_delay_min == 34
    assert "delay:+34min" in a.evidence and "ns_flag" in a.evidence
    c = assess(make_trip(), None, True, None, today=TODAY, rules=rules)
    assert c.arrival_delay_min is None and c.evidence == ["cancelled"]


def test_ns_flag_trusted_only_without_data_notes_assumption(rules):
    a = assess(make_trip(), None, False, True, today=TODAY, rules=rules)
    assert "trusting NS flag" in a.reason


def test_trust_ns_flag_can_be_disabled(rules):
    r = rules.model_copy(update={"trust_ns_flag_without_data": False})
    a = assess(make_trip(), None, False, True, today=TODAY, rules=r)
    assert not a.eligible and a.reason.startswith("no_delay_data")


@pytest.mark.parametrize(
    ("today", "eligible", "code"),
    [
        (date(2026, 12, 10), True, "eligible"),  # last day
        (date(2026, 12, 11), False, "deadline_passed"),
        (date(2027, 3, 1), False, "deadline_passed"),
    ],
)
def test_deadline(rules, today, eligible, code):
    a = assess(make_trip(), 60, False, False, today=today, rules=rules)
    assert a.eligible is eligible
    assert a.reason.startswith(code + ":")
    assert a.deadline == date(2026, 12, 10)
    if not eligible:
        assert a.expected_eur == Decimal("0.00") and a.percent == 0


def test_deadline_not_reported_when_delay_too_small(rules):
    a = assess(make_trip(), 5, False, False, today=date(2030, 1, 1), rules=rules)
    assert a.reason.startswith("below_threshold:")


def test_deadline_uses_amsterdam_date_of_checkin(rules):
    # 23:30 UTC on 30 Nov is 00:30 on 1 Dec in Amsterdam
    trip = make_trip(checkin_at=datetime(2026, 11, 30, 23, 30, tzinfo=ZoneInfo("UTC")))
    a = assess(trip, 60, False, False, today=TODAY, rules=rules)
    assert a.deadline == date(2027, 3, 1)


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 9, 10), 3, date(2026, 12, 10)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),
        (date(2023, 11, 30), 3, date(2024, 2, 29)),
        (date(2026, 12, 31), 2, date(2027, 2, 28)),
        (date(2026, 1, 31), 0, date(2026, 1, 31)),
    ],
)
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected


def test_default_today_is_used_when_omitted(rules):
    a = assess(make_trip(checkin_at=datetime(2020, 1, 1, 8, 0, tzinfo=AMS)), 60, False, False, rules=rules)
    assert a.reason.startswith("deadline_passed:")


def test_require_checkout_can_be_disabled(rules):
    r = rules.model_copy(update={"require_checkout": False})
    a = assess(make_trip(checkout_at=None), 60, False, False, today=TODAY, rules=r)
    assert a.eligible


def test_product_profile_overrides(rules):
    r = Rules.model_validate(
        {
            **rules.model_dump(mode="json"),
            "thresholds": {
                "ns_domestic": [{"min": 30, "pct": 50}, {"min": 60, "pct": 100}],
                "special": [{"min": 20, "pct": 25}],
            },
            "product_profiles": {"Vol tarief": "special"},
        }
    )
    a = assess(make_trip(), 25, False, False, today=TODAY, rules=r)
    assert a.eligible and a.percent == 25 and a.expected_eur == Decimal("2.50")


def test_subscription_disabled_uses_normal_rules(rules):
    a = assess(make_trip(product="Altijd Vrij", fare_eur=None), 45, False, False, today=TODAY, rules=rules)
    assert a.eligible and a.percent == 50 and a.expected_eur == Decimal("0.00")


def test_subscription_enabled_percent_of_monthly_amount(rules):
    data = rules.model_dump(mode="json")
    data["subscription_products"] = {
        "enabled": True,
        "products": [
            {
                "match": "altijd vrij",
                "monthly_amount_eur": "400.00",
                "thresholds": [{"min": 30, "pct": 1}, {"min": 60, "pct": 2}],
            }
        ],
    }
    r = Rules.model_validate(data)
    a = assess(make_trip(product="NS Altijd Vrij", fare_eur=None), 65, False, False, today=TODAY, rules=r)
    assert a.eligible and a.percent == 2 and a.expected_eur == Decimal("8.00")
    low = assess(make_trip(product="Altijd Vrij"), 31, False, False, today=TODAY, rules=r)
    assert low.eligible and low.expected_eur == Decimal("4.00")
    data["subscription_products"]["products"][0]["monthly_amount_eur"] = "100.00"
    r2 = Rules.model_validate(data)
    below = assess(make_trip(product="Altijd Vrij"), 31, False, False, today=TODAY, rules=r2)
    assert not below.eligible and below.reason.startswith("below_min_payout:")  # 1% of 100 = 1.00


# --------------------------------------------------------------------------- loading


def test_bundled_defaults_identical_to_example():
    example = REPO / "config.example" / "rules.yaml"
    bundled = REPO / "src" / "treinvertraging" / "defaults" / "rules.yaml"
    assert example.read_text(encoding="utf-8") == bundled.read_text(encoding="utf-8")


def test_example_rules_values(rules):
    assert rules.deadline_months == 3
    assert rules.min_payout_eur == Decimal("2.20")
    assert [(t.min, t.pct) for t in rules.thresholds["ns_domestic"]] == [(30, 50), (60, 100)]
    assert not rules.subscription_products.enabled


def test_config_dir_overrides_bundled(data_dir):
    custom = (
        (REPO / "config.example" / "rules.yaml")
        .read_text(encoding="utf-8")
        .replace("min_payout_eur: 2.20", "min_payout_eur: 5.00")
    )
    (data_dir.parent / "config" / "rules.yaml").write_text(custom, encoding="utf-8")
    assert load_rules().min_payout_eur == Decimal("5.00")


def test_falls_back_to_bundled_when_config_dir_empty(data_dir):
    assert find_rules_file().name == "rules.yaml"
    assert "defaults" in find_rules_file().parts
    assert load_rules().deadline_months == 3


def test_edits_are_picked_up(tmp_path):
    f = tmp_path / "rules.yaml"
    text = (REPO / "config.example" / "rules.yaml").read_text(encoding="utf-8")
    f.write_text(text, encoding="utf-8")
    assert load_rules(f).deadline_months == 3
    f.write_text(text.replace("deadline_months: 3", "deadline_months: 6"), encoding="utf-8")
    import os

    os.utime(f, ns=(1, f.stat().st_mtime_ns + 10_000_000))
    assert load_rules(f).deadline_months == 6


def test_assess_loads_rules_itself(data_dir):
    a = assess(make_trip(), 60, False, False, today=TODAY)
    assert a.eligible and a.percent == 100


@pytest.mark.parametrize(
    "text",
    [
        "- just a list",
        "thresholds: {a: [{min: 30, pct: 150}]}\ndefault_profile: a",
        "thresholds: {a: [{min: 30, pct: 50}]}\ndefault_profile: missing",
        "thresholds: {a: [{min: 30, pct: 50}, {min: 30, pct: 100}]}\ndefault_profile: a",
        "thresholds: [unclosed",
    ],
)
def test_invalid_rules_raise(text):
    with pytest.raises(RulesError):
        parse_rules(text)


def test_missing_file_raises(tmp_path):
    with pytest.raises(RulesError):
        find_rules_file(tmp_path / "nope.yaml")
