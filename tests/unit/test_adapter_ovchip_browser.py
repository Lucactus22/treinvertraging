# ruff: noqa: F811  (pytest fixtures imported from tests.browser_utils)
"""OV-chipkaart adapter against local fake sites (headless Chromium, no internet).

The fake sites are SYNTHETIC stand-ins:

* ``site``  plays www.ov-chipkaart.nl: ``/api/auth/session``, ``/nl/mijn-ov-chip`` (card picker, Cookiebot),
  ``/nl/mijn-ov-chip/reishistorie`` (CSV link, JSON XHR or plain table depending on ``state["mode"]``);
* ``login`` plays login.ov-chipkaart.nl (WSO2 ``authenticationendpoint/login.do``), a different origin.

Both share one ``state`` dict (the LocalServer helper cannot set cookies, so the session is a flag).
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from treinvertraging import selectors
from treinvertraging.adapters.ovchip import OvchipAdapter
from treinvertraging.browser import takeover as tk
from treinvertraging.browser.runtime import BrowserRuntime
from treinvertraging.domain import Source
from treinvertraging.errors import BlockedByBotProtection, InvalidInput, LayoutChanged
from treinvertraging.otp.base import OtpTimeout
from treinvertraging.stations import StationIndex

FIX = Path(__file__).parent.parent / "fixtures" / "ovchip"
PASSWORD = "s3cret-pw"


def _read(name: str, **subs: str) -> bytes:
    text = (FIX / name).read_text(encoding="utf-8")
    for k, v in subs.items():
        text = text.replace("{" + k + "}", v)
    return text.encode("utf-8")


def _html(body: bytes, status: int = 200) -> tuple[int, str, bytes]:
    return status, "text/html; charset=utf-8", body


@dataclass
class Account:
    id: int = 1
    username: str = "jan@example.nl"
    password: str = PASSWORD
    card_number: str | None = "3528012345671111"
    otp_provider: str = "manual"


@dataclass
class FakeOtp:
    code: str = "123456"
    fail: bool = False
    calls: list[datetime] = field(default_factory=list)

    async def get_code(self, account: Any, since: datetime, timeout: float) -> str:
        self.calls.append(since)
        if self.fail:
            raise OtpTimeout("no mail")
        return self.code


class FakeOvSite:
    def __init__(self, site: LocalServer, login: LocalServer) -> None:
        self.site, self.login = site, login
        self.state: dict[str, Any] = {
            "logged_in": False,
            "blocked": None,  # None | "shell" | "403" (every page of the site)
            "blocked_history": None,  # same, but only for the Reishistorie page
            "mode": "csv",  # csv | json | dom
            "card": "9999",  # card currently selected on the site
            "otp": None,  # required OTP code, if any
            "consent": 0,
            "login_posts": 0,
            "csv_queries": [],
        }
        site.add_route("GET", "/api/auth/session", self._session)
        site.add_route("GET", "/nl/mijn-ov-chip", self._home)
        site.add_route("GET", "/nl/mijn-ov-chip/reishistorie", self._history)
        site.add_route("GET", "/api/select-card", self._select_card)
        site.add_route("GET", "/api/consent", self._consent)
        site.add_route("GET", "/download/reishistorie.csv", self._csv)
        site.add_route("GET", "/api/transactions", self._transactions)
        login.add_route("GET", "/authenticationendpoint/login.do", lambda *_: _html(self._login_page()))
        login.add_route("POST", "/authenticationendpoint/commonauth", self._commonauth)
        login.add_route("POST", "/authenticationendpoint/otp", self._otp)

    # -- helpers

    def _login_page(self, error: str = "") -> bytes:
        return _read("login.html", error=error, error_display="block" if error else "none")

    def _after_login(self) -> tuple[int, str, bytes]:
        self.state["logged_in"] = True
        return _html(_read("redirect.html", url=self.site.url("/nl/mijn-ov-chip")))

    def _blocked(self) -> tuple[int, str, bytes] | None:
        if self.state["blocked"] == "shell":
            return _html(_read("shell.html"))
        if self.state["blocked"] == "403":
            return 403, "text/html", b"<html><body>Forbidden</body></html>"
        return None

    # -- site routes

    def _session(self, *_: Any) -> tuple[int, str, bytes]:
        data = {"user": {"name": "Jan"}, "expires": "2027-01-01"} if self.state["logged_in"] else {}
        return 200, "application/json", json.dumps(data).encode()

    def _home(self, *_: Any) -> tuple[int, str, bytes]:
        if blocked := self._blocked():
            return blocked
        if not self.state["logged_in"]:
            url = self.login.url("/authenticationendpoint/login.do?sessionDataKey=abc")
            return _html(_read("redirect.html", url=url))
        return _html(_read("home.html"))

    def _history(self, *_: Any) -> tuple[int, str, bytes]:
        if self.state["blocked_history"] == "shell":
            return _html(_read("shell.html"))
        if blocked := self._blocked():
            return blocked
        return _html(_read(f"history_{self.state['mode']}.html"))

    def _select_card(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        self.state["card"] = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["card"][0]
        return 200, "application/json", b"{}"

    def _consent(self, *_: Any) -> tuple[int, str, bytes]:
        self.state["consent"] += 1
        return 200, "application/json", b"{}"

    def _csv(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        self.state["csv_queries"].append(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query))
        if self.state["card"] == "1111":
            text = (FIX / "export_full.csv").read_text(encoding="utf-8").replace("1234", "1111")
        else:
            text = (
                "Datum;Check-in;Vertrek;Check-uit;Bestemming;Bedrag;Transactie;Klasse;Product;Opmerkingen;"
                "Naam;Kaartnummer\n"
                "01-10-2026;09:00;Breda;09:30;Dordrecht;4,00;Reis;2e;NS;;P;3528 **** **** 9999\n"
            )
        return 200, "text/csv; charset=utf-8", b"\xef\xbb\xbf" + text.encode("utf-8")

    def _transactions(self, *_: Any) -> tuple[int, str, bytes]:
        payload = {
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
                },
                {"type": "Saldo opgeladen", "amount": 50},
            ]
        }
        return 200, "application/json", json.dumps(payload).encode()

    # -- login routes

    def _commonauth(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        self.state["login_posts"] += 1
        form = urllib.parse.parse_qs(body.decode())
        if form.get("username") != ["jan@example.nl"] or form.get("password") != [PASSWORD]:
            return _html(self._login_page("Ongeldige gebruikersnaam of wachtwoord"))
        if self.state["otp"]:
            return _html(_read("otp.html", error="", error_display="none"))
        return self._after_login()

    def _otp(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        form = urllib.parse.parse_qs(body.decode())
        if form.get("otp") == [self.state["otp"]]:
            return self._after_login()
        return _html(_read("otp.html", error="Ongeldige code", error_display="block"))


@pytest.fixture
def login_server() -> Iterator[LocalServer]:
    s = LocalServer().start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def fake(page_server: LocalServer, login_server: LocalServer) -> FakeOvSite:
    return FakeOvSite(page_server, login_server)


@pytest.fixture(autouse=True)
def _takeover_clean() -> Iterator[None]:
    async def quiet(title: str, body: str) -> None:
        return None

    tk.clear_takeovers()
    tk.set_default_notifier(quiet)
    yield
    tk.clear_takeovers()
    tk.set_default_notifier(None)


@pytest.fixture(scope="module")
def stations() -> StationIndex:
    return StationIndex.bundled()


def make_adapter(
    fake: FakeOvSite,
    runtime: BrowserRuntime,
    stations: StationIndex,
    *,
    otp: FakeOtp | None = None,
    **account: Any,
) -> OvchipAdapter:
    adapter = OvchipAdapter(
        Account(**account),
        runtime=runtime,
        otp=otp,
        base_url=fake.site.base_url,
        stations=stations,
    )
    # keep the tests fast: no human-like delays
    adapter.type_delay_ms = (1, 3)
    adapter.pause_s = (0.0, 0.0)
    adapter.cookie_timeout_ms = 700
    adapter.settle_timeout_s = 12
    adapter.block_check_after_s = 0.8
    adapter.nav_timeout_ms = 10_000
    return adapter


async def wait_takeover(timeout: float = 30) -> tk.Takeover:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if tk.active_takeovers():
            return tk.active_takeovers()[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no takeover was requested")


# --------------------------------------------------------------------------- ensure_logged_in


async def test_login_through_wso2_form(fake, runtime, stations) -> None:
    adapter = make_adapter(fake, runtime, stations)
    await adapter.ensure_logged_in()
    assert fake.state["logged_in"] is True
    assert fake.state["login_posts"] == 1
    assert fake.login.requests_to("/authenticationendpoint/login.do")  # went through the other origin
    assert fake.state["consent"] == 0  # no banner on those pages


async def test_existing_session_skips_login(fake, runtime, stations) -> None:
    fake.state["logged_in"] = True
    adapter = make_adapter(fake, runtime, stations)
    await adapter.ensure_logged_in()
    assert fake.site.requests_to("/api/auth/session")
    assert not fake.site.requests_to("/nl/mijn-ov-chip")  # not even navigated
    assert not fake.login.requests


async def test_wrong_password_raises_invalid_input_without_retry(fake, runtime, stations) -> None:
    adapter = make_adapter(fake, runtime, stations, password="nope")
    with pytest.raises(InvalidInput):
        await adapter.ensure_logged_in()
    assert fake.state["login_posts"] == 1
    assert not tk.active_takeovers()


async def test_missing_credentials_raise_invalid_input(fake, runtime, stations) -> None:
    adapter = make_adapter(fake, runtime, stations, password="")
    with pytest.raises(InvalidInput):
        await adapter.ensure_logged_in()
    assert not fake.login.requests


async def test_otp_step_uses_provider(fake, runtime, stations) -> None:
    fake.state["otp"] = "123456"
    otp = FakeOtp()
    before = datetime.now(UTC)
    await make_adapter(fake, runtime, stations, otp=otp).ensure_logged_in()
    assert fake.state["logged_in"] is True
    assert len(otp.calls) == 1
    assert otp.calls[0] <= datetime.now(UTC) and (before - otp.calls[0]).total_seconds() < 60


async def test_otp_timeout_asks_for_takeover(fake, runtime, stations) -> None:
    fake.state["otp"] = "123456"
    adapter = make_adapter(fake, runtime, stations, otp=FakeOtp(fail=True))
    task = asyncio.create_task(adapter.ensure_logged_in())
    t = await wait_takeover()
    assert "verification code" in t.reason
    fake.state["logged_in"] = True  # the human typed the code
    tk.complete_takeover(t.id)
    await asyncio.wait_for(task, 20)


@pytest.mark.parametrize("kind", ["shell", "403"])
async def test_block_asks_takeover_then_succeeds(fake, runtime, stations, kind) -> None:
    fake.state["blocked"] = kind
    adapter = make_adapter(fake, runtime, stations)
    task = asyncio.create_task(adapter.ensure_logged_in())
    t = await wait_takeover()
    assert t.account == "1" and "OV-chipkaart" in t.reason
    # the human fixes things in noVNC: site reachable again and logged in
    fake.state["blocked"] = None
    fake.state["logged_in"] = True
    assert tk.complete_takeover(t.id)
    await asyncio.wait_for(task, 20)
    assert not tk.active_takeovers()
    debug = list((runtime.profiles_dir.parent / "debug").glob("*ovchip-login*"))
    assert debug, "a debug bundle is kept when a takeover was needed"


async def test_block_after_takeover_raises(fake, runtime, stations) -> None:
    fake.state["blocked"] = "shell"
    adapter = make_adapter(fake, runtime, stations)
    task = asyncio.create_task(adapter.ensure_logged_in())
    t = await wait_takeover()
    tk.complete_takeover(t.id)  # human gave up: still blocked
    with pytest.raises(BlockedByBotProtection):
        await asyncio.wait_for(task, 30)


async def test_block_after_takeover_but_human_unblocked_then_normal_login(fake, runtime, stations) -> None:
    fake.state["blocked"] = "shell"
    adapter = make_adapter(fake, runtime, stations)
    task = asyncio.create_task(adapter.ensure_logged_in())
    t = await wait_takeover()
    fake.state["blocked"] = None  # block lifted but not logged in: second pass logs in itself
    tk.complete_takeover(t.id)
    await asyncio.wait_for(task, 30)
    assert fake.state["logged_in"] is True and fake.state["login_posts"] == 1


# --------------------------------------------------------------------------- list_trips


def _assert_csv_trips(trips: list) -> None:
    assert [t.from_station for t in trips] == ["UT", "ASD", "UT", "UT", "UTVR"]
    assert all(t.source is Source.OVCHIP and t.card_id == "**** **** **** 1111" for t in trips)
    assert trips[0].to_station == "ASD" and str(trips[0].fare_eur) == "9.60"
    assert trips[2].checkout_at is None  # missing check-out kept
    assert trips[4].operator == "Arriva"
    assert all(t.raw["format"] == "csv" for t in trips)


async def test_list_trips_csv_full_flow_from_logged_out(fake, runtime, stations) -> None:
    adapter = make_adapter(fake, runtime, stations)
    trips = await adapter.list_trips(date(2026, 10, 1))
    _assert_csv_trips(trips)
    # Cookiebot dismissed on the home page
    assert fake.state["consent"] == 1
    # the card ending 1111 was picked (the picker defaults to 9999)
    assert fake.state["card"] == "1111"
    # the period was applied: the export link carries the requested range
    (query,) = fake.state["csv_queries"]
    assert query["from"] == ["2026-10-01"]
    assert query["to"][0] >= "2026-10-01"  # defaults to today


async def test_list_trips_since_filters(fake, runtime, stations) -> None:
    fake.state["logged_in"] = True
    trips = await make_adapter(fake, runtime, stations).list_trips(date(2026, 10, 8))
    assert [t.checkin_at.day for t in trips] == [8, 9]


async def test_list_trips_without_known_card_number_takes_what_is_shown(fake, runtime, stations) -> None:
    fake.state.update(logged_in=True, card="1111")
    trips = await make_adapter(fake, runtime, stations, card_number=None).list_trips(date(2026, 10, 1))
    assert not fake.site.requests_to("/api/select-card")
    # no card filter: the second card in the export is included, card ids come from the rows
    assert len(trips) == 6
    assert {t.card_id for t in trips} == {"**** **** **** 1111", "**** **** **** 5678"}


async def test_list_trips_json_fallback(fake, runtime, stations) -> None:
    fake.state.update(logged_in=True, mode="json")
    (trip,) = await make_adapter(fake, runtime, stations).list_trips(date(2026, 10, 1))
    assert (trip.from_station, trip.to_station) == ("UT", "ASD")
    assert trip.raw["format"] == "json" and str(trip.fare_eur) == "9.6"


async def test_list_trips_dom_fallback(fake, runtime, stations) -> None:
    fake.state.update(logged_in=True, mode="dom")
    trips = await make_adapter(fake, runtime, stations).list_trips(date(2026, 10, 1))
    assert [(t.from_station, t.to_station) for t in trips] == [("UT", "ASD"), ("ASD", "UT")]
    assert trips[1].checkout_at is not None and trips[1].checkout_at.day == 7
    assert all(t.raw["format"] == "dom" for t in trips)


async def test_list_trips_api_replay_when_configured(fake, runtime, stations, data_dir) -> None:
    fake.state.update(logged_in=True, mode="dom")
    user_dir = selectors.get_settings().CONFIG_DIR / "selectors"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "ovchip.yaml").write_text(
        yaml.safe_dump({"history": {"api": {"url": "/api/transactions?card={card}&from={from}&to={to}"}}})
    )
    selectors.reset_cache()
    try:
        (trip,) = await make_adapter(fake, runtime, stations).list_trips(date(2026, 10, 1))
    finally:
        selectors.reset_cache()
    assert trip.raw["format"] == "api"
    replay = [r for r in fake.site.requests_to("/api/transactions") if "from=2026-10-01" in r.path]
    assert replay and "card=1111" in replay[0].path


async def test_list_trips_layout_changed_when_nothing_works(fake, runtime, stations, monkeypatch) -> None:
    fake.state.update(logged_in=True, mode="json")
    # JSON endpoint pattern that matches nothing, no export, no table -> LayoutChanged + debug bundle
    user_dir = selectors.get_settings().CONFIG_DIR / "selectors"
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "ovchip.yaml").write_text(
        yaml.safe_dump({"history": {"api": {"regex": "nothing-like-this"}}})
    )
    selectors.reset_cache()
    try:
        with pytest.raises(LayoutChanged):
            await make_adapter(fake, runtime, stations).list_trips(date(2026, 10, 1))
    finally:
        selectors.reset_cache()
    assert list((runtime.profiles_dir.parent / "debug").glob("*ovchip-list-trips*"))


async def test_list_trips_blocked_history_page_takes_over(fake, runtime, stations) -> None:
    fake.state.update(logged_in=True, blocked_history="shell")
    adapter = make_adapter(fake, runtime, stations)
    task = asyncio.create_task(adapter.list_trips(date(2026, 10, 1)))
    t = await wait_takeover()
    fake.state["blocked_history"] = None  # human solved the challenge
    tk.complete_takeover(t.id)
    trips = await asyncio.wait_for(task, 60)
    _assert_csv_trips(trips)


async def test_list_trips_history_still_blocked_after_takeover_raises(fake, runtime, stations) -> None:
    fake.state.update(logged_in=True, blocked_history="shell")
    task = asyncio.create_task(make_adapter(fake, runtime, stations).list_trips(date(2026, 10, 1)))
    t = await wait_takeover()
    tk.complete_takeover(t.id)
    with pytest.raises(BlockedByBotProtection):
        await asyncio.wait_for(task, 60)


def test_credentials_from_encrypted_account_fields(data_dir) -> None:
    from treinvertraging.crypto import encrypt

    class Row:
        id = 5
        username = "jan@example.nl"
        password_enc = encrypt("pw-from-db")
        card_number_enc = encrypt("3528 0123 4567 8901")
        otp_provider = "manual"

    adapter = OvchipAdapter(Row())
    assert adapter._credentials().password == "pw-from-db"
    assert adapter.card_last4 == "8901"
    assert adapter.source is Source.OVCHIP
