from __future__ import annotations

import time

import pytest

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from treinvertraging.browser.runtime import BrowserRuntime


async def test_records_json_responses(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("rec1") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Laad reizen").click()
        r = await s.recorder.wait_for(r"/api/trips$", timeout=5)
        assert r.status == 200 and r.method == "GET"
        assert r.json() == {"trips": [{"id": 1, "from": "UT", "to": "ASD"}]}
        # non-JSON (the html page itself) is not recorded
        assert s.recorder.find(r"fetch_demo\.html") == []
        assert s.recorder.latest(r"/api/trips") is r


async def test_records_post_body_and_dump(page_server: LocalServer, runtime: BrowserRuntime, tmp_path) -> None:
    async with runtime.session("rec2") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Langzaam").click()
        await s.recorder.wait_idle(r"/xas/", quiet_ms=200, timeout=5)
        entry = s.recorder.find(r"/xas/", method="POST")[0]
        assert entry.request_json() == {"action": "slow"}
        assert entry.json() == {"n": 1}
        path = s.recorder.dump(tmp_path / "dump")
        assert path.name == "recorder.json" and "/xas/" in path.read_text()


async def test_wait_idle_waits_for_slow_post(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("rec3") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        t0 = time.monotonic()
        await s.page.get_by_role("button", name="Langzaam").click()
        await s.recorder.wait_idle(r"/xas/", quiet_ms=100, timeout=5)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.3  # POST has a 300 ms server delay
        assert await s.page.locator("#state").inner_text() == "done:1"
        assert s.recorder.pending(r"/xas/") == []


async def test_wait_idle_spans_gap_between_chained_posts(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("rec4") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Keten").click()
        # 150 ms gap between the two POSTs is shorter than quiet_ms, so we must see both
        await s.recorder.wait_idle(r"/xas/", quiet_ms=400, timeout=5)
        assert await s.page.locator("#state").inner_text() == "chain-done:2"
        assert len(page_server.requests_to("/xas/", "POST")) == 2


async def test_wait_idle_returns_quickly_when_nothing_pending(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("rec5") as s:
        await s.page.goto(page_server.url("/plain.html"))
        t0 = time.monotonic()
        await s.recorder.wait_idle(r"/xas/", quiet_ms=150, timeout=5)
        assert time.monotonic() - t0 < 1.0


async def test_wait_idle_ignores_non_matching_requests(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    page_server.add_json("/api/trips", {"trips": []}, delay=1.0)
    async with runtime.session("rec6") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Laad reizen").click()
        t0 = time.monotonic()
        await s.recorder.wait_idle(r"/xas/", quiet_ms=100, timeout=5)
        assert time.monotonic() - t0 < 0.9  # slow /api/trips is not matched
        await s.recorder.wait_idle(r"/api/trips", quiet_ms=100, timeout=5)
        assert s.recorder.find(r"/api/trips")


async def test_wait_idle_times_out(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    page_server.add_route("POST", "/xas/", lambda *_: (200, "application/json", b"{}"), delay=2.0)
    async with runtime.session("rec7") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Langzaam").click()
        with pytest.raises(TimeoutError):
            await s.recorder.wait_idle(r"/xas/", quiet_ms=100, timeout=0.5)


async def test_recorder_truncates_large_bodies(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    big = '{"x": "' + "a" * 5000 + '"}'
    page_server.add_route("GET", "/api/big", lambda *_: (200, "application/json", big.encode()))
    async with runtime.session("rec8") as s:
        s.recorder.max_body_bytes = 1000
        await s.page.goto(page_server.url("/plain.html"))
        await s.page.evaluate("fetch('/api/big')")
        r = await s.recorder.wait_for(r"/api/big", timeout=5)
        assert r.truncated and len(r.response_body) == 1000
        with pytest.raises(ValueError):
            r.json()
