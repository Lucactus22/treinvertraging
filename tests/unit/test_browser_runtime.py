# ruff: noqa: F811  (pytest fixtures imported from tests.browser_utils)
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.browser_utils import LocalServer, make_runtime, page_server, runtime  # noqa: F401
from treinvertraging.browser.runtime import BrowserRuntime


async def test_session_basics(page_server: LocalServer, runtime: BrowserRuntime, data_dir: Path) -> None:
    async with runtime.session(7) as s:
        assert s.account_id == "7"
        assert s.profile_dir == data_dir / "profiles" / "7"
        await s.page.goto(page_server.url("/plain.html"))
        assert await s.page.title() == "Plain"
        assert await s.page.evaluate("navigator.language") == "nl-NL"
        assert await s.page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "Europe/Amsterdam"
        assert await s.page.evaluate("navigator.webdriver") is False
        assert await s.page.evaluate("[innerWidth, innerHeight]") == [1366, 768]
    assert not runtime.is_busy(7)


async def test_profile_persists_cookies_between_sessions(
    page_server: LocalServer, runtime: BrowserRuntime
) -> None:
    async with runtime.session("p1") as s:
        await s.page.goto(page_server.url("/plain.html"))
        await s.context.add_cookies(
            [{"name": "sid", "value": "abc", "url": page_server.base_url, "expires": 2_000_000_000}]
        )
        await s.page.evaluate("localStorage.setItem('k', 'v')")
    async with runtime.session("p1") as s:
        await s.page.goto(page_server.url("/plain.html"))
        assert {c["name"]: c["value"] for c in await s.context.cookies()}.get("sid") == "abc"
        assert await s.page.evaluate("localStorage.getItem('k')") == "v"
    async with runtime.session("p2") as s:  # other account: isolated profile
        await s.page.goto(page_server.url("/plain.html"))
        assert await s.context.cookies() == []


async def test_same_account_sessions_are_serialised(
    page_server: LocalServer, runtime: BrowserRuntime
) -> None:
    order: list[str] = []

    async def worker(name: str) -> None:
        async with runtime.session("shared") as s:
            order.append(f"start-{name}")
            await s.page.goto(page_server.url("/plain.html"))
            await asyncio.sleep(0.2)
            order.append(f"end-{name}")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (["start-a", "end-a", "start-b", "end-b"], ["start-b", "end-b", "start-a", "end-a"])


async def test_different_accounts_run_in_parallel(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    order: list[str] = []

    async def worker(name: str) -> None:
        async with runtime.session(name):
            order.append(f"start-{name}")
            await asyncio.sleep(0.3)
            order.append(f"end-{name}")

    await asyncio.gather(worker("x"), worker("y"))
    assert order[:2] == ["start-x", "start-y"] or order[:2] == ["start-y", "start-x"]


async def test_lock_timeout(runtime: BrowserRuntime) -> None:
    async with runtime.session("busy"):
        assert runtime.is_busy("busy")
        with pytest.raises(TimeoutError):
            async with runtime.session("busy", lock_timeout=0.1):
                pass


async def test_lock_released_after_exception(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    with pytest.raises(RuntimeError):
        async with runtime.session("err"):
            raise RuntimeError("boom")
    async with runtime.session("err", lock_timeout=5) as s:
        await s.page.goto(page_server.url("/plain.html"))


async def test_ephemeral_session_uses_gtbv_profile(
    page_server: LocalServer, runtime: BrowserRuntime, data_dir: Path
) -> None:
    async with runtime.ephemeral_session() as s:
        assert s.profile_dir == data_dir / "profiles" / "gtbv"
        await s.page.goto(page_server.url("/plain.html"))


async def test_stale_singleton_lock_is_cleared(
    page_server: LocalServer, runtime: BrowserRuntime, data_dir: Path
) -> None:
    prof = data_dir / "profiles" / "stale"
    prof.mkdir(parents=True)
    (prof / "SingletonLock").symlink_to("somehost-12345")  # dangling symlink, as Chromium leaves it
    async with runtime.session("stale") as s:
        await s.page.goto(page_server.url("/plain.html"))


async def test_stop_closes_open_sessions_and_lazy_start(page_server: LocalServer, data_dir: Path) -> None:
    rt = make_runtime(data_dir)
    assert not rt.started
    async with rt.session("lazy") as s:  # starts lazily
        await s.page.goto(page_server.url("/plain.html"))
    assert rt.started
    await rt.stop()
    assert not rt.started
    await rt.stop()  # idempotent


async def test_session_new_page_is_recorded(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("tabs") as s:
        p2 = await s.new_page()
        await p2.goto(page_server.url("/fetch_demo.html"))
        await p2.get_by_role("button", name="Laad reizen").click()
        await s.recorder.wait_for(r"/api/trips", timeout=5)
