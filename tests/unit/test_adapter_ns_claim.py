# ruff: noqa: F811  (pytest fixtures imported from the support modules)
from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from tests.unit.test_adapter_ns_support import (  # noqa: F401
    FOREIGN_IBAN,
    GOOD_IBAN,
    FakeNs,
    _clean_takeovers,
    _fast_human,
    claimant,
    fake_ns,
    make_adapter,
    ns_server,
    sample_trip,
    set_selectors,
)
from tests.unit.test_adapter_ns_trips import DOM_SELECTORS
from treinvertraging.adapters.ns import NsAdapter
from treinvertraging.errors import InvalidInput, LayoutChanged


async def test_dry_run_stops_before_final_click(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    res = await make_adapter().submit_claim(sample_trip(), claimant(), dry_run=True)
    assert res.ok and res.dry_run and not res.reached_final_submit
    assert res.channel == "mijnns" and res.reference is None
    assert res.screenshot and res.screenshot.endswith(".png")
    from pathlib import Path

    assert Path(res.screenshot).stat().st_size > 1000
    assert fake_ns.claim_posts == []  # the final submit was never clicked
    assert fake_ns.requested("/claim.html")


async def test_submit_success_returns_reference(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    res = await make_adapter().submit_claim(sample_trip(), claimant(), dry_run=False)
    assert res.ok and res.reached_final_submit and not res.dry_run
    assert res.reference == "GTB-2026-12345"
    assert res.channel == "mijnns" and res.screenshot
    assert fake_ns.claim_posts == [{"ref": "T1", "iban": GOOD_IBAN, "holder": "J. Jansen"}]


async def test_existing_iban_and_holder_are_not_overwritten(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    other = "NL02ABNA0123456789"
    res = await make_adapter().submit_claim(
        sample_trip("/claim.html?ref=PREFILLED"), claimant(other), dry_run=False
    )
    assert res.ok
    # the form came prefilled by NS: our values were not typed over it
    assert fake_ns.claim_posts == [{"ref": "PREFILLED", "iban": GOOD_IBAN, "holder": "Prefilled Houder"}]


async def test_claim_ref_as_id_uses_url_template(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(claim={"url_template": "/claim.html?ref={ref}"})
    res = await make_adapter().submit_claim(sample_trip("T9"), claimant(), dry_run=False)
    assert res.ok and fake_ns.claim_posts[0]["ref"] == "T9"


async def test_invalid_iban_checksum_rejected_locally(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    with pytest.raises(InvalidInput, match="IBAN"):
        await make_adapter().submit_claim(sample_trip(), claimant("NL91ABNA0417164301"), dry_run=False)
    assert fake_ns.logins == []  # failed before even opening a browser


async def test_site_side_iban_rejection_raises_invalid_input(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    with pytest.raises(InvalidInput, match="ongeldig"):
        await make_adapter().submit_claim(sample_trip(), claimant(FOREIGN_IBAN), dry_run=False)
    assert len(fake_ns.claim_posts) == 1


async def test_required_checkbox_missing_raises_layout_changed(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(claim={"checkboxes": ["bestaat niet"]})
    with pytest.raises(LayoutChanged, match="checkbox"):
        await make_adapter().submit_claim(sample_trip(), claimant(), dry_run=False)
    assert fake_ns.claim_posts == []


async def test_unknown_heading_raises_layout_changed(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(claim={"expect_heading": "Totaal iets anders"})
    with pytest.raises(LayoutChanged, match="heading"):
        await make_adapter().submit_claim(sample_trip(), claimant(), dry_run=True)


async def test_claim_url_outside_allowed_hosts_is_refused(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    with pytest.raises(LayoutChanged, match="allowed hosts"):
        await make_adapter().submit_claim(
            sample_trip("https://evil.example.org/claim"), claimant(), dry_run=True
        )


async def test_no_confirmation_after_click_reports_reached_final_submit(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(claim={"success_regex": "dit staat er nooit", "success_timeout_s": 2})
    res = await make_adapter().submit_claim(sample_trip(), claimant(), dry_run=False)
    assert not res.ok and res.reached_final_submit  # pipeline must reconcile, not retry blindly
    assert len(fake_ns.claim_posts) == 1


async def test_open_claim_via_trip_row_when_no_ref(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(**DOM_SELECTORS, claim={"trip_link": {"role": "link", "name": "geld terug"}})
    res = await make_adapter().submit_claim(sample_trip(ref=None), claimant(), dry_run=True)
    assert res.ok and res.dry_run
    assert fake_ns.requested("/reishistorie_dom.html") and fake_ns.requested("/claim.html")


async def test_no_ref_and_no_dom_config_raises_layout_changed(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    with pytest.raises(LayoutChanged, match="ns_claim_ref"):
        await make_adapter().submit_claim(sample_trip(ref=None), claimant(), dry_run=True)
