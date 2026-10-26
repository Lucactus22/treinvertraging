# ruff: noqa: F811  (pytest fixtures imported from tests.browser_utils)
from __future__ import annotations

import pytest

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from treinvertraging.browser import human
from treinvertraging.browser.runtime import BrowserRuntime


async def test_jitter_sleep_range() -> None:
    d = await human.jitter_sleep(0.01, 0.03)
    assert 0.01 <= d <= 0.03


async def test_human_type_types_with_varying_delays(
    page_server: LocalServer, runtime: BrowserRuntime
) -> None:
    async with runtime.session("h1") as s:
        await s.page.goto(page_server.url("/typing.html"))
        field = s.page.get_by_label("Naam")
        await human.human_type(field, "Jan de Vries", min_delay_ms=20, max_delay_ms=60)
        assert await field.input_value() == "Jan de Vries"  # old value cleared
        times = (await s.page.evaluate("window.keyTimes"))[-len("Jan de Vries") :]  # skip Ctrl+A/Backspace
        assert len(times) == len("Jan de Vries")
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert min(gaps) >= 10  # ms; typed one by one, not instantly
        assert len({round(g) for g in gaps}) > 3  # jittered, not constant


async def test_human_type_without_clear(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("h2") as s:
        await s.page.goto(page_server.url("/typing.html"))
        field = s.page.get_by_label("Naam")
        await human.human_type(field, "X", min_delay_ms=1, max_delay_ms=2, clear=False)
        assert (await field.input_value()).endswith("X")
        assert (await field.input_value()).startswith("oud")


async def test_human_click(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("h3") as s:
        await s.page.goto(page_server.url("/typing.html"))
        await human.human_click(s.page.get_by_role("button", name="Klik"))
        assert await s.page.locator("#clicked").inner_text() == "yes"


@pytest.mark.parametrize(
    ("reject", "expected"),
    [(False, "accepted"), (True, "rejected")],
)
async def test_dismiss_onetrust(
    page_server: LocalServer, runtime: BrowserRuntime, reject: bool, expected: str
) -> None:
    async with runtime.session("h4") as s:
        await s.page.goto(page_server.url("/onetrust.html"))
        assert await human.dismiss_cookie_banner(s.page, reject=reject, timeout=2000)
        assert await s.page.title() == expected
        assert not await human.dismiss_cookie_banner(s.page, reject=reject, timeout=600)  # gone now


@pytest.mark.parametrize(("reject", "expected"), [(False, "accepted"), (True, "rejected")])
async def test_dismiss_banner_role_fallback(
    page_server: LocalServer, runtime: BrowserRuntime, reject: bool, expected: str
) -> None:
    async with runtime.session("h5") as s:
        await s.page.goto(page_server.url("/cookies_role.html"))
        assert await human.dismiss_cookie_banner(s.page, reject=reject, timeout=2000)
        assert await s.page.title() == expected


async def test_dismiss_banner_absent(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("h6") as s:
        await s.page.goto(page_server.url("/plain.html"))
        assert await human.dismiss_cookie_banner(s.page, timeout=600) is False


async def test_dismiss_banner_custom_selectors(page_server: LocalServer, runtime: BrowserRuntime) -> None:
    async with runtime.session("h7") as s:
        await s.page.goto(page_server.url("/onetrust.html"))
        assert await human.dismiss_cookie_banner(
            s.page, selectors=["#onetrust-reject-all-handler"], timeout=1500
        )
        assert await s.page.title() == "rejected"
