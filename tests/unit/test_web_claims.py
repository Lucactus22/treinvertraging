from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.test_web_support import seed_claim
from treinvertraging import db, services
from treinvertraging.models import Claim, Event

pytest_plugins = ["tests.unit.test_web_support"]


def _state(claim_id: int) -> str:
    with db.session_scope() as s:
        return s.get(Claim, claim_id).state


def test_approve_transition(client: TestClient) -> None:
    cid = seed_claim("awaiting_approval")
    r = client.post(f"/claims/{cid}/approve")
    assert r.status_code == 303
    assert _state(cid) == "approved"
    with db.session_scope() as s:
        events = list(s.scalars(select(Event).where(Event.kind == "claim.approved")))
    assert len(events) == 1 and events[0].trip_key == "k1"
    assert "goedgekeurd" in client.get(r.headers["location"]).text


def test_reject_transition(client: TestClient) -> None:
    cid = seed_claim("awaiting_approval")
    client.post(f"/claims/{cid}/reject")
    assert _state(cid) == "rejected"


@pytest.mark.parametrize("state", ["eligible", "approved", "paid", "submitting"])
def test_approve_wrong_state_is_refused(client: TestClient, state: str) -> None:
    cid = seed_claim(state)
    r = client.post(f"/claims/{cid}/approve")
    assert r.status_code == 303
    assert _state(cid) == state
    assert "Niet+mogelijk" in r.headers["location"]


def test_approve_unknown_claim(client: TestClient) -> None:
    r = client.post("/claims/999/approve")
    assert "bestaat+niet" in r.headers["location"]


def test_approve_is_not_repeatable(client: TestClient) -> None:
    cid = seed_claim("awaiting_approval")
    client.post(f"/claims/{cid}/approve")
    client.post(f"/claims/{cid}/reject")  # already approved -> refused
    assert _state(cid) == "approved"


def test_retry_stub_shows_flash(client: TestClient) -> None:
    cid = seed_claim("needs_human")
    r = client.post(f"/claims/{cid}/retry")
    assert r.status_code == 303
    assert "nog+niet+beschikbaar" in r.headers["location"]
    assert _state(cid) == "needs_human"


def test_htmx_returns_flash_fragment(client: TestClient) -> None:
    cid = seed_claim("awaiting_approval")
    r = client.post(f"/claims/{cid}/approve", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'class="flash success"' in r.text and "<html" not in r.text


def test_claims_page_buttons(client: TestClient) -> None:
    seed_claim("awaiting_approval", key="a")
    seed_claim("needs_human", key="b")
    seed_claim("paid", key="c")
    page = client.get("/claims").text
    assert page.count("/approve") == 1 and page.count("/reject") == 1
    assert page.count("/retry") == 1
    assert "Wacht op goedkeuring" in page and "Uitbetaald" in page


def test_claims_page_links_debug_files(client: TestClient, web_env) -> None:
    cid = seed_claim("needs_human")
    bundle = web_env / "debug" / "20261001-submit"
    bundle.mkdir(parents=True)
    (bundle / "screenshot.png").write_bytes(b"\x89PNG")
    (bundle / "page.html").write_text("<script>alert(1)</script>")
    with db.session_scope() as s:
        s.get(Claim, cid).debug_dir = str(bundle)
    page = client.get("/claims").text
    assert "/debug/20261001-submit/screenshot.png" in page
    assert "/debug/20261001-submit/page.html" in page


def test_trips_page_and_filters(client: TestClient) -> None:
    seed_claim("awaiting_approval", key="a")
    seed_claim("ineligible", key="b", eligible=False, delay=5, expected="0")
    seed_claim("paid", key="c")
    allp = client.get("/trips").text
    assert "rdt:2026-10 train 3045" in allp and "34 min" in allp and "5 min" in allp
    assert "UT" in allp and "ASD" in allp
    # date shown in Europe/Amsterdam (10:00 CEST for 08:00 UTC)
    assert "01-10-2026 10:00" in allp
    assert client.get("/trips?filter=eligible").text.count("<tr>") == 2  # header + 1
    assert client.get("/trips?filter=ineligible").text.count("<tr>") == 2
    assert client.get("/trips?filter=claimed").text.count("<tr>") == 2
    assert client.get("/trips?filter=bogus").status_code == 200


def test_dashboard_totals(client: TestClient) -> None:
    seed_claim("awaiting_approval", key="a", expected="10.00")
    seed_claim("approved", key="b", expected="2.50")
    seed_claim("confirmed", key="c", expected="20.00")
    cid = seed_claim("paid", key="d", expected="30.00")
    with db.session_scope() as s:
        from decimal import Decimal

        s.get(Claim, cid).paid_eur = Decimal("29.00")
    from treinvertraging.web import queries

    with db.session_scope() as s:
        stats = queries.dashboard_stats(s)
    assert str(stats.pending_eur) == "12.50"
    assert str(stats.claimed_eur) == "50.00"
    assert str(stats.paid_eur) == "29.00"
    assert stats.awaiting_approval == 1
    page = client.get("/").text
    assert "€ 12,50" in page and "€ 29,00" in page
    assert "wachten op jouw goedkeuring" in page
    assert "Planning nog niet beschikbaar" in page


def test_dashboard_deadline_warning_and_canary(client: TestClient) -> None:
    soon = date.today() + timedelta(days=5)
    seed_claim("awaiting_approval", key="a", deadline=soon)
    seed_claim("confirmed", key="b", deadline=soon)  # confirmed: no warning
    with db.session_scope() as s:
        s.add(Event(level="error", kind="canary.failed", message="login geblokkeerd"))
    page = client.get("/").text
    assert "Deadlines binnen 14 dagen" in page and "nog 5 dagen" in page
    assert page.count("nog 5 dagen") == 1
    assert "login geblokkeerd" in page


def test_dashboard_uses_next_runs(client: TestClient, monkeypatch) -> None:
    from datetime import UTC, datetime

    async def fake() -> list[dict]:
        return [{"job": "sync", "next_run": datetime(2026, 10, 27, 12, 0, tzinfo=UTC)}]

    monkeypatch.setattr(services, "next_runs", fake)
    assert "27-10-2026 13:00" in client.get("/").text


async def test_services_approve_reject_direct(web_env) -> None:
    cid = seed_claim("awaiting_approval")
    assert await services.approve_claim(cid) == "approved"
    with pytest.raises(services.InvalidTransition):
        await services.reject_claim(cid)
    with pytest.raises(services.ClaimNotFound):
        await services.approve_claim(12345)
    with pytest.raises(NotImplementedError):
        await services.run_sync()
