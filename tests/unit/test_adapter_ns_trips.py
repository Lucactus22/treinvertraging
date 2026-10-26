# ruff: noqa: F811  (pytest fixtures imported from the support modules)
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from tests.unit.test_adapter_ns_support import (  # noqa: F401
    FakeNs,
    _clean_takeovers,
    fake_ns,
    make_adapter,
    ns_server,
    set_selectors,
)
from treinvertraging.adapters.ns import NsAdapter
from treinvertraging.errors import LayoutChanged

SINCE = date(2025, 1, 1)

DOM_SELECTORS = {
    "nav": {"travel_history_links": ["Reisoverzicht"], "claims_links": ["Aanvragenlijst"]},
    "dom": {
        "item": "li.trip",
        "fields": {
            "date": "span.date",
            "checkin_time": "span.in",
            "checkout_time": "span.out",
            "from": "span.from",
            "to": "span.to",
            "fare": "span.fare",
            "product": "span.product",
            "delay": "span.delay",
            "claim_link": "a.claim@href",
        },
    },
    "claims_dom": {
        "item": "tr.claim-row",
        "fields": {
            "date": "td.d",
            "from": "td.f",
            "to": "td.t",
            "status": "td.s",
            "reference": "td.r",
            "amount": "td.a",
        },
    },
}


async def test_list_trips_from_recorded_json(fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]) -> None:
    adapter = make_adapter()
    trips = await adapter.list_trips(SINCE)
    assert len(fake_ns.logins) == 1
    assert len(fake_ns.requested("/api/reishistorie")) == 1  # discovered by navigating, not replayed
    assert [(t.from_station, t.to_station) for t in trips] == [("UT", "ASD"), ("ASD", "UT"), ("UT", "RTD")]
    t1 = trips[0]
    assert t1.fare_eur == Decimal("9.40") and t1.ns_delay_flag is True
    assert t1.ns_claim_ref == "/claim.html?ref=T1"
    assert t1.raw["strategy"] == "recorder"
    assert t1.key() == trips[0].key()


async def test_list_trips_filters_by_since(fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]) -> None:
    trips = await make_adapter().list_trips(date(2026, 10, 13))
    assert [t.checkin_at.day for t in trips] == [13]


async def test_list_trips_flat_json_shape(fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]) -> None:
    fake_ns.set_json("/api/reishistorie", "travel_history_flat.json")
    trips = await make_adapter().list_trips(SINCE)
    assert len(trips) == 2
    assert trips[0].ns_claim_ref == "GT-7781" and trips[0].card_id == "ns-1"


async def test_api_replay_is_preferred(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(api={"travel_history": "/api/reishistorie?from={since}&to={today}"})
    trips = await make_adapter().list_trips(SINCE)
    assert len(trips) == 3
    calls = fake_ns.requested("/api/reishistorie")
    assert len(calls) == 1 and calls[0].startswith("/api/reishistorie?from=2025-01-01&to=")
    assert fake_ns.requested("/reishistorie.html") == []  # never navigated to the page
    assert trips[0].raw["strategy"] == "api"


async def test_api_failure_falls_back_to_navigation(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(api={"travel_history": "/api/does-not-exist"})
    trips = await make_adapter().list_trips(SINCE)
    assert len(trips) == 3 and trips[0].raw["strategy"] == "recorder"
    assert fake_ns.requested("/reishistorie.html")


async def test_list_trips_dom_fallback(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(**DOM_SELECTORS)
    trips = await make_adapter().list_trips(SINCE)
    assert len(trips) == 2
    t1, t2 = trips
    assert t1.raw["strategy"] == "dom"
    assert (t1.from_station, t1.to_station) == ("UT", "ASD")
    assert t1.checkin_at.isoformat() == "2026-10-12T08:15:00+02:00"
    assert t1.checkout_at.isoformat() == "2026-10-12T09:02:00+02:00"
    assert t1.fare_eur == Decimal("9.40") and t1.product == "Dal Voordeel"
    assert t1.ns_delay_flag is True and t1.ns_claim_ref == "/claim.html?ref=T1"
    assert t1.card_id == "ns-1"
    assert t2.ns_delay_flag is None and t2.ns_claim_ref is None
    assert t2.checkin_at.isoformat() == "2026-10-13T17:05:00+02:00"


async def test_unparseable_history_raises_layout_changed_with_bundle(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], data_dir
) -> None:
    fake_ns.server.add_json("/api/reishistorie", {"something": ["else"]})
    with pytest.raises(LayoutChanged, match="no trips"):
        await make_adapter().list_trips(SINCE)
    assert list((data_dir / "debug").glob("*ns-list-trips*"))


async def test_missing_history_link_raises_layout_changed(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(nav={"travel_history_links": ["Bestaat niet"]})
    with pytest.raises(LayoutChanged, match="no link found"):
        await make_adapter().list_trips(SINCE)


async def test_list_submitted_claims_from_recorded_json(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    claims = await make_adapter().list_submitted_claims(SINCE)
    assert [c["reference"] for c in claims] == ["GTB-2026-12345", "GTB-2026-00077"]
    c = claims[0]
    assert (c["date"], c["from"], c["to"]) == ("2026-10-12", "UT", "ASD")
    assert c["status"] == "In behandeling" and c["amount"] == "4.70"
    assert {"date", "from", "to", "status", "reference", "amount"} <= c.keys()


async def test_list_submitted_claims_since_filter(fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]) -> None:
    claims = await make_adapter().list_submitted_claims(date(2026, 10, 1))
    assert [c["reference"] for c in claims] == ["GTB-2026-12345"]


async def test_list_submitted_claims_dom_fallback(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(**DOM_SELECTORS)
    claims = await make_adapter().list_submitted_claims(SINCE)
    assert [(c["reference"], c["status"], c["amount"]) for c in claims] == [
        ("GTB-2026-12345", "In behandeling", "4.70"),
        ("GTB-2026-00077", "Uitbetaald", "12.10"),
    ]
    assert (claims[0]["date"], claims[0]["from"], claims[0]["to"]) == ("2026-10-12", "UT", "ASD")
