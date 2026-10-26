"""WP-K: GtbvSubmitter against a local fake Mendix-like form (tests/fixtures/gtbv, SYNTHETIC)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.browser_utils import LocalServer, runtime  # noqa: F401  (runtime is a pytest fixture)
from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.adapters.gtbv import GtbvSubmitter
from treinvertraging.browser import human
from treinvertraging.browser import takeover as tk
from treinvertraging.domain import Source, TripDTO
from treinvertraging.errors import InvalidInput, LayoutChanged

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gtbv"
VALID_CARD = "3528012345678901"
INVALID_CARD = "3528999999999999"
REFERENCE = "GTBV-2026-123456"


class FakeForm:
    """Server side of the fake form: POST /xas/ (300 ms delay), plus the challenge status route."""

    def __init__(self, server: LocalServer) -> None:
        self.server = server
        self.log: list[dict[str, Any]] = []
        self.challenge = False
        self.challenge_shown = False
        self.solved = False
        server.add_route("POST", "/xas/", self._xas, delay=0.3)
        server.add_route("GET", "/challenge_status", self._status)

    def _xas(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        data = json.loads(body or b"{}")
        self.log.append(data)
        reply: dict[str, Any] = {"ok": True}
        action = data.get("action")
        if action == "card":
            reply["ok"] = data["card"] == VALID_CARD
            if reply["ok"] and self.challenge and not self.challenge_shown:
                self.challenge_shown = True
                reply["challenge"] = True
        elif action == "final":
            reply["reference"] = REFERENCE
        return 200, "application/json", json.dumps(reply).encode()

    def _status(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        return 200, "application/json", json.dumps({"solved": self.solved}).encode()

    def actions(self, action: str) -> list[dict[str, Any]]:
        return [d for d in self.log if d.get("action") == action]

    def step(self, name: str) -> dict[str, Any]:
        (item,) = [d for d in self.log if d.get("action") == "next" and d.get("step") == name]
        return item["data"]


@pytest.fixture
def form() -> Iterator[FakeForm]:
    server = LocalServer(FIXTURES).start()
    try:
        yield FakeForm(server)
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def no_sleep(min_s: float = 0.0, max_s: float = 0.0) -> float:
        return 0.0

    monkeypatch.setattr(human, "jitter_sleep", no_sleep)
    tk.clear_takeovers()

    async def quiet(title: str, body: str) -> None:
        pass

    tk.set_default_notifier(quiet)
    yield
    tk.set_default_notifier(None)
    tk.clear_takeovers()


def trip() -> TripDTO:
    return TripDTO(
        source=Source.NS,
        card_id="3528-XXXX",
        checkin_at=datetime(2026, 9, 14, 8, 12, tzinfo=ZoneInfo("Europe/Amsterdam")),
        checkout_at=datetime(2026, 9, 14, 9, 40, tzinfo=ZoneInfo("Europe/Amsterdam")),
        from_station="UT",
        to_station="ASD",
        fare_eur=Decimal("7.40"),
    )


def claimant(card: str = VALID_CARD) -> ClaimantInfo:
    return ClaimantInfo(
        card_number=card, iban="NL91 ABNA 0417 1643 00", holder_name="J. Jansen", email="jan@example.org"
    )


def submitter(runtime, form: FakeForm, url_suffix: str = "/") -> GtbvSubmitter:  # noqa: F811
    return GtbvSubmitter(
        runtime=runtime,
        url=form.server.url(url_suffix),
        typing_delay_ms=(1, 3),
        settle_quiet_ms=150,
        timeouts={"load_s": 15, "step_s": 5, "field_s": 5, "success_s": 10, "idle_s": 10},
    )


async def test_happy_path(runtime, form: FakeForm) -> None:  # noqa: F811
    result = await submitter(runtime, form).submit_claim(trip(), claimant(), dry_run=False)

    assert result.ok, result.message
    assert result.channel == "gtbv"
    assert result.reached_final_submit is True
    assert result.dry_run is False
    assert result.reference == REFERENCE
    assert result.screenshot and Path(result.screenshot).is_file()

    assert form.actions("cookie_reject"), "OneTrust banner was not rejected"
    assert form.actions("ticket")[0]["ticket"] == "OV-chipkaart / NS-Business Card"
    assert form.actions("card")[0]["card"] == VALID_CARD
    assert form.step("reis") == {
        "date": "14-09-2026",
        "time": "08:12",
        "from": "Utrecht Centraal",
        "to": "Amsterdam Centraal",
    }
    assert form.step("gegevens") == {
        "iban": "NL91ABNA0417164300",
        "holder": "J. Jansen",
        "email": "jan@example.org",
    }
    assert len(form.actions("final")) == 1


async def test_invalid_card_raises_invalid_input(runtime, form: FakeForm) -> None:  # noqa: F811
    with pytest.raises(InvalidInput) as exc:
        await submitter(runtime, form).submit_claim(trip(), claimant(INVALID_CARD), dry_run=False)
    assert "ongeldig" in str(exc.value)
    assert form.actions("card")[0]["card"] == INVALID_CARD
    assert not form.actions("final")


async def test_malformed_card_is_rejected_before_opening_the_browser(runtime, form: FakeForm) -> None:  # noqa: F811
    with pytest.raises(InvalidInput):
        await submitter(runtime, form).submit_claim(trip(), claimant("3528 12"), dry_run=False)
    assert form.server.requests == []


async def test_unknown_heading_raises_layout_changed_and_keeps_bundle(runtime, form: FakeForm) -> None:  # noqa: F811
    with pytest.raises(LayoutChanged) as exc:
        await submitter(runtime, form, "/?variant=newheading").submit_claim(trip(), claimant(), dry_run=False)
    assert "Jouw reis" in str(exc.value)  # visible headings are reported
    bundle = Path(exc.value.debug_dir)  # type: ignore[attr-defined]
    assert (bundle / "screenshot.png").is_file()
    assert (bundle / "page.html").is_file()
    html = (bundle / "page.html").read_text()
    assert VALID_CARD not in html and "NL91" not in html
    assert not form.actions("final")


async def test_dry_run_stops_before_final_submit(runtime, form: FakeForm) -> None:  # noqa: F811
    result = await submitter(runtime, form).submit_claim(trip(), claimant(), dry_run=True)

    assert result.ok and result.dry_run
    assert result.reached_final_submit is False
    assert result.reference is None
    assert result.screenshot and Path(result.screenshot).is_file()
    bundle = Path(result.screenshot).parent
    assert (bundle / "page.html").is_file() and (bundle / "meta.json").is_file()
    assert form.step("gegevens")["iban"] == "NL91ABNA0417164300"  # every step was filled
    assert form.actions("final") == [], "dry_run must never send the final submit"


async def test_challenge_iframe_triggers_takeover_then_continues(runtime, form: FakeForm) -> None:  # noqa: F811
    form.challenge = True
    taken: list[tk.Takeover] = []

    async def human_solver() -> None:
        for _ in range(400):
            active = tk.active_takeovers()
            if active:
                taken.append(active[0])
                form.solved = True  # the person solves the captcha in the live browser ...
                await asyncio.sleep(0.4)
                tk.complete_takeover(active[0].id)  # ... and clicks "Klaar"
                return
            await asyncio.sleep(0.05)

    solver = asyncio.create_task(human_solver())
    result = await submitter(runtime, form).submit_claim(trip(), claimant(), dry_run=False)
    await solver

    assert len(taken) == 1
    assert "recaptcha" in taken[0].reason.lower()
    assert result.ok and result.reference == REFERENCE
    assert len(form.actions("final")) == 1
