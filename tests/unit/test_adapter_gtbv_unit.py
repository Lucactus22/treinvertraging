"""WP-K: pure helpers of adapters/gtbv.py (no browser)."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import yaml

from treinvertraging import selectors
from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.adapters.gtbv import DEFAULT_URL, GtbvSubmitter, build_context, load_config, resolve_path
from treinvertraging.domain import Source, TripDTO


@pytest.fixture
def ctx() -> dict:
    trip = TripDTO(
        source=Source.OVCHIP,
        card_id="x",
        checkin_at=datetime(2026, 1, 5, 7, 5, tzinfo=ZoneInfo("UTC")),  # 08:05 in Amsterdam
        from_station="ASD",
        to_station=None,
        fare_eur=Decimal("12.5"),
    )
    who = ClaimantInfo(
        card_number="3528 0123 4567 8901", iban="nl91 abna 0417 1643 00", holder_name="A", email="a@b.nl"
    )
    return build_context(trip, who)


def test_derived_values(ctx: dict) -> None:
    d = ctx["derived"]
    assert d["checkin_date_nl"] == "05-01-2026"
    assert d["checkin_time"] == "08:05"  # converted to Europe/Amsterdam
    assert d["checkin_date_iso"] == "2026-01-05"
    assert d["from_station_name"] == "Amsterdam Centraal"
    assert d["to_station_name"] is None
    assert d["iban_compact"] == "NL91ABNA0417164300"
    assert d["iban_spaced"] == "NL91 ABNA 0417 1643 00"
    assert d["card_number"] == "3528012345678901"
    assert d["fare_eur"] == "12,50"
    assert d["delay_minutes"] is None


def test_resolve_path(ctx: dict) -> None:
    assert resolve_path(ctx, "derived.checkin_time") == "08:05"
    assert resolve_path(ctx, "who.holder_name") == "A"
    assert resolve_path(ctx, "trip.from_station") == "ASD"
    assert resolve_path(ctx, "derived.nope") is None
    assert resolve_path(ctx, "who.__class__") is None  # private attributes are not reachable
    assert resolve_path(ctx, "assessment.eligible") is None


def test_default_config_is_valid(data_dir) -> None:
    selectors.reset_cache()
    cfg = load_config()
    assert cfg.url == DEFAULT_URL
    assert cfg.card_prefill == "3528"
    assert cfg.start_next_button == "Naar reisgegevens"
    assert [s.name for s in cfg.steps] == ["reisgegevens", "gegevens", "controle"]
    assert [s.final for s in cfg.steps] == [False, False, True]
    assert all(f.value_from or f.has_value for s in cfg.steps for f in s.fields)
    for text in ("Vul een OV-chipkaartnummer in.", "Dit nummer is ongeldig"):
        assert re.search(cfg.start_error_regex, text, re.I)


def test_user_selectors_override_defaults(data_dir) -> None:
    user = selectors.get_settings().CONFIG_DIR / "selectors"
    user.mkdir(parents=True, exist_ok=True)
    (user / "gtbv.yaml").write_text(yaml.safe_dump({"url": "http://x.test/", "success_regex": "klaar"}))
    selectors.reset_cache()
    cfg = load_config()
    assert cfg.url == "http://x.test/" and cfg.success_regex == "klaar"
    assert load_config(url="http://y.test/").url == "http://y.test/"  # constructor wins
    assert GtbvSubmitter().channel == "gtbv"
