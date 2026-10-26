# ruff: noqa: F811  (pytest fixtures imported from tests.browser_utils)
"""Shared fixtures for the Mijn NS adapter tests (no tests in here).

``FakeNs`` serves the SYNTHETIC pages from ``tests/fixtures/ns`` on a ``LocalServer`` and keeps
state (blocked, OTP required, received logins/claims) that the tests flip.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from treinvertraging import crypto, models, selectors
from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.adapters.ns import NsAdapter
from treinvertraging.browser import takeover as tk
from treinvertraging.browser.runtime import BrowserRuntime
from treinvertraging.domain import Source, TripDTO

NS_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ns"
EMAIL = "jan.jansen@example.com"
PASSWORD = "s3cret-pw!"
OTP_CODE = "123456"
GOOD_IBAN = "NL91ABNA0417164300"
FOREIGN_IBAN = "DE89370400440532013000"  # valid checksum, rejected by the fake form (not Dutch)

# keep the tests fast: no long jitter / settle windows
FAST_SELECTORS: dict[str, Any] = {
    "timeouts": {"nav_s": 15, "action_s": 10, "api_s": 10},
    "typing": {"min_delay_ms": 1, "max_delay_ms": 3},
    "cookie": {"timeout_ms": 800},
    "login": {"settle_s": 6, "post_submit_s": 10, "takeover_timeout_s": 20, "otp": {"timeout_s": 5}},
    "nav": {"quiet_ms": 250, "idle_timeout_s": 5},
    "claim": {"step_timeout_s": 3, "success_timeout_s": 8, "heading_timeout_s": 5},
}


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _html(name: str) -> bytes:
    return (NS_FIXTURES / name).read_bytes()


class FakeNs:
    def __init__(self, server: LocalServer, *, otp: bool = False) -> None:
        self.server = server
        self.otp = otp
        self.blocked = False
        self.empty_login = False  # login page served as an empty shell (bot protection symptom)
        self.logins: list[dict[str, Any]] = []
        self.verifications: list[dict[str, Any]] = []
        self.claim_posts: list[dict[str, Any]] = []
        server.add_route("GET", "/mijnns", self._mijnns)
        server.add_route("GET", "/login", self._login)
        server.add_route("POST", "/api/login-step", self._login_step)
        server.add_route("POST", "/api/verify", self._verify)
        server.add_route("POST", "/api/claim-submit", self._claim_submit)
        self.set_json("/api/reishistorie", "travel_history.json")
        self.set_json("/api/aanvragen", "claims.json")

    def set_json(self, path: str, fixture: str) -> None:
        self.server.add_json(path, json.loads(_html(fixture)))

    # -- handlers --------------------------------------------------------------------------------

    def _mijnns(self, *_: Any) -> tuple[int, str, bytes]:
        if self.blocked:
            return 403, "text/html", b"<html><body><h1>Access Denied</h1>Reference #18.4f2b1c</body></html>"
        return 200, "text/html; charset=utf-8", _html("mijnns.html")

    def _login(self, *_: Any) -> tuple[int, str, bytes]:
        if self.empty_login:
            return 200, "text/html; charset=utf-8", b"<html><head></head><body></body></html>"
        return 200, "text/html; charset=utf-8", _html("login.html")

    def _json(self, data: dict[str, Any]) -> tuple[int, str, bytes]:
        return 200, "application/json", json.dumps(data).encode()

    def _login_step(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        payload = json.loads(body)
        self.logins.append(payload)
        if payload.get("email") != EMAIL or payload.get("password") != PASSWORD:
            return self._json({"status": "error"})
        return self._json({"status": "otp" if self.otp else "ok"})

    def _verify(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        payload = json.loads(body)
        self.verifications.append(payload)
        return self._json({"status": "ok" if payload.get("code") == OTP_CODE else "error"})

    def _claim_submit(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        payload = json.loads(body)
        self.claim_posts.append(payload)
        if not str(payload.get("iban", "")).startswith("NL"):
            return self._json({"ok": False, "error": "Het IBAN is ongeldig (alleen Nederlandse rekeningen)."})
        return self._json({"ok": True, "reference": "GTB-2026-12345"})

    # -- helpers for assertions ------------------------------------------------------------------

    def requested(self, prefix: str) -> list[str]:
        return [r.path for r in self.server.requests if r.path.startswith(prefix)]


class FakeOtp:
    """OtpProvider stub that records the calls."""

    def __init__(self, code: str = OTP_CODE) -> None:
        self.code = code
        self.calls: list[tuple[Any, datetime, float]] = []

    async def get_code(self, account: Any, since: datetime, timeout: float) -> str:
        self.calls.append((account, since, timeout))
        return self.code


@pytest.fixture(autouse=True)
def _clean_takeovers() -> Iterator[None]:
    tk.clear_takeovers()

    async def quiet(title: str, body: str) -> None:
        pass

    tk.set_default_notifier(quiet)
    yield
    tk.set_default_notifier(None)
    tk.clear_takeovers()


@pytest.fixture(autouse=True)
def _fast_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """No human-like pauses in tests (typing speed is configured through FAST_SELECTORS)."""
    from treinvertraging.adapters import ns as ns_module
    from treinvertraging.browser import human

    async def instant(*_: Any, **__: Any) -> float:
        return 0.0

    monkeypatch.setattr(human, "jitter_sleep", instant)
    monkeypatch.setattr(ns_module, "jitter_sleep", instant)


@pytest.fixture
def ns_server() -> Iterator[LocalServer]:
    server = LocalServer(NS_FIXTURES).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_ns(ns_server: LocalServer) -> FakeNs:
    return FakeNs(ns_server)


@pytest.fixture
def set_selectors(data_dir: Path) -> Callable[..., None]:
    """``set_selectors(login={...})`` writes CONFIG_DIR/selectors/ns.yaml on top of FAST_SELECTORS."""

    def _set(**overrides: Any) -> None:
        merged = _deep_merge(FAST_SELECTORS, overrides)
        d = data_dir.parent / "config" / "selectors"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ns.yaml").write_text(yaml.safe_dump(merged), encoding="utf-8")
        selectors.reset_cache()

    _set()
    return _set


def make_account(account_id: int = 1) -> models.Account:
    return models.Account(
        id=account_id,
        source="ns",
        label="Jan",
        username=EMAIL,
        password_enc=crypto.encrypt(PASSWORD),
        holder_name="J. Jansen",
        email=EMAIL,
        otp_provider="manual",
        enabled=True,
    )


@pytest.fixture
def make_adapter(
    ns_server: LocalServer, runtime: BrowserRuntime, set_selectors: Callable[..., None]
) -> Callable[..., NsAdapter]:
    def _make(
        otp: FakeOtp | None = None, takeover_timeout: float | None = None, account_id: int = 1
    ) -> NsAdapter:
        return NsAdapter(
            make_account(account_id),
            runtime=runtime,
            otp=otp,
            base_url=ns_server.base_url,
            takeover_timeout=takeover_timeout,
        )

    return _make


def claimant(iban: str = GOOD_IBAN) -> ClaimantInfo:
    return ClaimantInfo(card_number="3528012345678901", iban=iban, holder_name="J. Jansen", email=EMAIL)


def sample_trip(ref: str | None = "/claim.html?ref=T1", **kw: Any) -> TripDTO:
    base: dict[str, Any] = {
        "source": Source.NS,
        "card_id": "ns-1",
        "checkin_at": datetime.fromisoformat("2026-10-12T08:15:00+02:00"),
        "checkout_at": datetime.fromisoformat("2026-10-12T09:02:00+02:00"),
        "from_station": "UT",
        "to_station": "ASD",
        "ns_claim_ref": ref,
        "raw": {"from_name": "Utrecht Centraal"},
    }
    base.update(kw)
    return TripDTO(**base)
