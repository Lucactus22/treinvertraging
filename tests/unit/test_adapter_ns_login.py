# ruff: noqa: F811  (pytest fixtures imported from the support modules)
from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from tests.unit.test_adapter_ns_support import (  # noqa: F401
    EMAIL,
    OTP_CODE,
    PASSWORD,
    FakeNs,
    FakeOtp,
    _clean_takeovers,
    _fast_human,
    fake_ns,
    make_adapter,
    ns_server,
    set_selectors,
)
from treinvertraging import crypto
from treinvertraging.adapters.ns import NsAdapter
from treinvertraging.browser import takeover as tk
from treinvertraging.errors import BlockedByBotProtection, LoginRequired, TakeoverTimeout


async def _wait_takeover() -> tk.Takeover:
    for _ in range(600):
        if tk.active_takeovers():
            return tk.active_takeovers()[0]
        await asyncio.sleep(0.05)
    raise AssertionError("no takeover requested")


async def test_login_success_and_session_persists(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    adapter = make_adapter()
    await adapter.ensure_logged_in()
    assert len(fake_ns.logins) == 1
    login = fake_ns.logins[0]
    assert (login["email"], login["password"]) == (EMAIL, PASSWORD)
    assert login["remember"] is True  # "remember me" ticked
    assert login["csrfToken"] == "synthetic-csrf-token"  # hidden field travels along
    assert fake_ns.verifications == []
    # persistent profile: a second call finds the session and does not log in again
    await adapter.ensure_logged_in()
    assert len(fake_ns.logins) == 1
    assert tk.active_takeovers() == []


async def test_login_with_otp_step(fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]) -> None:
    fake_ns.otp = True
    otp = FakeOtp()
    adapter = make_adapter(otp=otp)
    await adapter.ensure_logged_in()
    assert len(otp.calls) == 1
    account, since, timeout = otp.calls[0]
    assert account.id == 1 and since.tzinfo is not None and timeout > 0
    assert fake_ns.verifications == [{"code": OTP_CODE}]


async def test_wrong_password_raises_login_required_without_takeover(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    adapter = make_adapter()
    adapter.account.password_enc = crypto.encrypt("wrong")
    with pytest.raises(LoginRequired, match="rejected the credentials"):
        await adapter.ensure_logged_in()
    assert len(fake_ns.logins) == 1  # no retry: it would risk locking the account
    assert tk.active_takeovers() == []


async def test_wrong_otp_code_raises_login_required(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    fake_ns.otp = True
    adapter = make_adapter(otp=FakeOtp(code="000000"))
    with pytest.raises(LoginRequired, match="verification code"):
        await adapter.ensure_logged_in()


async def test_otp_timeout_becomes_login_required(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    from treinvertraging.otp.base import OtpTimeout

    class Never:
        async def get_code(self, account, since, timeout):
            raise OtpTimeout("nothing")

    fake_ns.otp = True
    with pytest.raises(LoginRequired, match="no verification code"):
        await make_adapter(otp=Never()).ensure_logged_in()


async def test_block_page_triggers_takeover_then_login_succeeds(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    fake_ns.blocked = True  # HTTP 403 "Access Denied" on /mijnns
    adapter = make_adapter()
    task = asyncio.create_task(adapter.ensure_logged_in())
    t = await _wait_takeover()
    assert "bot protection" in t.reason and "403" in t.reason
    assert fake_ns.logins == []  # nothing was typed into a blocked site
    fake_ns.blocked = False  # the human solved the challenge
    assert tk.complete_takeover(t.id)
    await asyncio.wait_for(task, 60)
    assert len(fake_ns.logins) == 1  # automatic login ran again after the takeover


async def test_empty_login_page_counts_as_block(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    fake_ns.empty_login = True  # login page with no form at all (583-byte shell)
    adapter = make_adapter(takeover_timeout=0.5)
    with pytest.raises(TakeoverTimeout):
        await adapter.ensure_logged_in()
    assert tk.active_takeovers() == []


async def test_still_blocked_after_takeover_raises(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter]
) -> None:
    fake_ns.blocked = True
    adapter = make_adapter()
    task = asyncio.create_task(adapter.ensure_logged_in())
    t = await _wait_takeover()
    tk.complete_takeover(t.id)  # human says done, but the site still blocks
    with pytest.raises(BlockedByBotProtection):
        await asyncio.wait_for(task, 60)


async def test_logged_in_text_marker_is_honoured(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], set_selectors: Callable[..., None]
) -> None:
    set_selectors(login={"logged_in_text": "Welkom bij Mijn NS"})
    await make_adapter().ensure_logged_in()
    assert len(fake_ns.logins) == 1


async def test_debug_bundle_written_on_failure(
    fake_ns: FakeNs, make_adapter: Callable[..., NsAdapter], data_dir
) -> None:
    adapter = make_adapter()
    adapter.account.password_enc = crypto.encrypt("wrong")
    with pytest.raises(LoginRequired):
        await adapter.ensure_logged_in()
    bundles = list((data_dir / "debug").glob("*ns-login*"))
    assert bundles, "a failed login keeps a debug bundle"
    html = (bundles[0] / "page.html").read_text(encoding="utf-8")
    assert "wrong" not in html and PASSWORD not in html
