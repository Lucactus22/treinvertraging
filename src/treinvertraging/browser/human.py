"""Human-like interaction helpers (jittered waits, typing, clicking, cookie banners)."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Iterable, Sequence

from playwright.async_api import Locator, Page

# Known consent-manager buttons (ids are stable vendor ids, not NS page selectors).
ONETRUST_ACCEPT = "#onetrust-accept-btn-handler"
ONETRUST_REJECT = "#onetrust-reject-all-handler"
COOKIEBOT_ACCEPT = "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"
COOKIEBOT_REJECT = "#CybotCookiebotDialogBodyButtonDecline"

_ACCEPT_NAMES = re.compile(
    r"^(alle cookies accepteren|alles accepteren|accepteren|akkoord|accept all|allow all)$", re.I
)
_REJECT_NAMES = re.compile(
    r"^(weigeren|alles weigeren|alle cookies weigeren|alleen noodzakelijke( cookies)?|reject all|decline)$",
    re.I,
)


async def jitter_sleep(min_s: float = 0.4, max_s: float = 1.4) -> float:
    """Sleep a random time in [min_s, max_s]; returns the time slept."""
    d = random.uniform(min_s, max(min_s, max_s))
    await asyncio.sleep(d)
    return d


async def scroll_into_view(target: Locator) -> None:
    await target.scroll_into_view_if_needed()
    await jitter_sleep(0.05, 0.2)


async def human_click(target: Locator, *, timeout: float = 15_000) -> None:
    """Scroll into view, hover with a short pause, then click at a slightly random point."""
    await target.scroll_into_view_if_needed(timeout=timeout)
    box = await target.bounding_box()
    page = target.page
    if box and box["width"] > 4 and box["height"] > 4:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x, y, steps=random.randint(4, 12))
        await jitter_sleep(0.05, 0.25)
        await target.click(
            position={"x": x - box["x"], "y": y - box["y"]}, delay=random.randint(30, 110), timeout=timeout
        )
    else:
        await target.click(delay=random.randint(30, 110), timeout=timeout)
    await jitter_sleep(0.1, 0.4)


async def human_type(
    target: Locator,
    text: str,
    *,
    min_delay_ms: float = 45,
    max_delay_ms: float = 140,
    clear: bool = True,
    click: bool = True,
) -> None:
    """Focus ``target`` and type ``text`` one character at a time with a random per-key delay.

    With ``clear`` the existing value is selected and deleted first.
    """
    page = target.page
    if click:
        await human_click(target)
    else:
        await target.focus()
    if clear:
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Backspace")
    for i, ch in enumerate(text):
        await page.keyboard.type(ch)
        d = random.uniform(min_delay_ms, max(min_delay_ms, max_delay_ms)) / 1000
        if ch == " " or (i and i % random.randint(5, 9) == 0):
            d *= random.uniform(1.2, 2.2)  # occasional hesitation
        await asyncio.sleep(d)


async def dismiss_cookie_banner(
    page: Page,
    *,
    reject: bool = False,
    selectors: Iterable[str] | None = None,
    timeout: float = 3_000,
) -> bool:
    """Dismiss a OneTrust / Cookiebot banner if one shows up. Returns True when a button was clicked.

    ``reject=True`` prefers rejecting (the gtbv form uses ``#onetrust-reject-all-handler``).
    ``selectors`` overrides the built-in vendor ids (e.g. from ``selectors.yaml``); role-based
    fallbacks (Dutch button names) are tried afterwards.
    """
    css: Sequence[str]
    if selectors is not None:
        css = list(selectors)
    elif reject:
        css = [ONETRUST_REJECT, COOKIEBOT_REJECT]
    else:
        css = [ONETRUST_ACCEPT, COOKIEBOT_ACCEPT]
    names = _REJECT_NAMES if reject else _ACCEPT_NAMES
    candidates: list[Locator] = [page.locator(sel).first for sel in css]
    candidates.append(page.get_by_role("button", name=names).first)
    per = max(timeout / max(len(candidates), 1), 300)
    for loc in candidates:
        try:
            await loc.wait_for(state="visible", timeout=per)
        except Exception:  # noqa: BLE001 - not present
            continue
        try:
            await loc.click(timeout=2_000)
            await jitter_sleep(0.2, 0.5)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False
