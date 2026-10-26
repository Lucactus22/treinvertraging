"""Browser runtime: one Playwright instance, one persistent Chromium profile per account.

Usage::

    rt = get_runtime()                       # process-wide singleton, started lazily
    async with rt.session(account.id) as s:  # serialised per account
        await s.page.goto(...)
        async with s.debug_bundle("list-trips") as b: ...
    async with rt.ephemeral_session() as s:  # public claim form, profile "gtbv"
        ...
    await shutdown_runtime()

A persistent profile can only be opened by one Chromium at a time, so ``session`` takes a
per-profile ``asyncio.Lock`` and closes the context when the block ends (cookies and storage persist
in ``<DATA_DIR>/profiles/<account_id>``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from treinvertraging.browser.debug import Bundle, debug_bundle
from treinvertraging.browser.recorder import Recorder
from treinvertraging.browser.takeover import Notifier, Takeover, request_takeover
from treinvertraging.config import get_settings

log = logging.getLogger(__name__)

EPHEMERAL_PROFILE = "gtbv"
VIEWPORT = {"width": 1366, "height": 768}
CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]
_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


@dataclass
class BrowserSession:
    account_id: str
    context: BrowserContext
    page: Page
    recorder: Recorder
    profile_dir: Path
    runtime: BrowserRuntime = field(repr=False)

    async def new_page(self) -> Page:
        """Open an extra tab (covered by the same recorder)."""
        return await self.context.new_page()

    def debug_bundle(self, action: str, **kw: Any) -> contextlib.AbstractAsyncContextManager[Bundle]:
        """``async with s.debug_bundle("x") as b`` bound to this session's page and recorder."""
        return debug_bundle(self.page, action, recorder=self.recorder, **kw)

    async def takeover(self, reason: str, *, timeout: float | None = None, notifier: Notifier | None = None) -> Takeover:
        """Ask a human to continue in the live browser; see :func:`takeover.request_takeover`."""
        kw: dict[str, Any] = {"notifier": notifier}
        if timeout is not None:
            kw["timeout"] = timeout
        return await request_takeover(self.account_id, self.page, reason, **kw)


class BrowserRuntime:
    def __init__(
        self,
        *,
        headless: bool | None = None,
        data_dir: Path | None = None,
        slow_mo: float | None = None,
        launch_options: dict[str, Any] | None = None,
    ) -> None:
        self._headless = headless
        self._data_dir = data_dir
        self._slow_mo = slow_mo
        self._launch_options = launch_options or {}
        self._pw: Playwright | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._open: dict[str, BrowserContext] = {}
        self._start_lock = asyncio.Lock()

    # -- configuration ---------------------------------------------------------------------------

    @property
    def headless(self) -> bool:
        return get_settings().HEADLESS if self._headless is None else self._headless

    @property
    def profiles_dir(self) -> Path:
        return (self._data_dir or get_settings().DATA_DIR) / "profiles"

    def profile_dir(self, account_id: int | str) -> Path:
        return self.profiles_dir / str(account_id)

    @property
    def started(self) -> bool:
        return self._pw is not None

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> BrowserRuntime:
        async with self._start_lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
        return self

    async def stop(self) -> None:
        async with self._start_lock:
            for key, ctx in list(self._open.items()):
                with contextlib.suppress(Exception):
                    await ctx.close()
                self._open.pop(key, None)
            if self._pw is not None:
                with contextlib.suppress(Exception):
                    await self._pw.stop()
                self._pw = None

    async def __aenter__(self) -> BrowserRuntime:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- sessions --------------------------------------------------------------------------------

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def is_busy(self, account_id: int | str) -> bool:
        lock = self._locks.get(str(account_id))
        return bool(lock and lock.locked())

    @staticmethod
    def _clear_stale_locks(profile: Path) -> None:
        # Left behind after a container kill; we hold the per-profile lock so nobody else uses it.
        for name in _SINGLETON_FILES:
            with contextlib.suppress(OSError):
                (profile / name).unlink()

    @asynccontextmanager
    async def session(
        self, account_id: int | str, *, lock_timeout: float | None = None
    ) -> AsyncIterator[BrowserSession]:
        """Exclusive persistent-context session for ``account_id`` (queues behind other users)."""
        if self._pw is None:
            await self.start()
        assert self._pw is not None
        key = str(account_id)
        lock = self._lock_for(key)
        try:
            await asyncio.wait_for(lock.acquire(), lock_timeout)
        except TimeoutError:
            raise TimeoutError(f"browser profile {key!r} is busy") from None
        context: BrowserContext | None = None
        recorder = Recorder()
        try:
            profile = self.profile_dir(key)
            profile.mkdir(parents=True, exist_ok=True)
            self._clear_stale_locks(profile)
            opts: dict[str, Any] = {
                "headless": self.headless,
                "locale": "nl-NL",
                "timezone_id": get_settings().TIMEZONE,
                "viewport": dict(VIEWPORT),
                "args": list(CHROMIUM_ARGS),
                "ignore_default_args": ["--enable-automation"],
            }
            if self._slow_mo:
                opts["slow_mo"] = self._slow_mo
            opts.update(self._launch_options)
            context = await self._pw.chromium.launch_persistent_context(str(profile), **opts)
            self._open[key] = context
            recorder.attach(context)
            page = context.pages[0] if context.pages else await context.new_page()
            yield BrowserSession(
                account_id=key, context=context, page=page, recorder=recorder, profile_dir=profile, runtime=self
            )
        finally:
            recorder.detach()
            if context is not None:
                self._open.pop(key, None)
                with contextlib.suppress(Exception):
                    await context.close()
            lock.release()

    def ephemeral_session(self, **kw: Any) -> contextlib.AbstractAsyncContextManager[BrowserSession]:
        """Session for the public form. Uses the persistent profile ``gtbv`` (keeps reCAPTCHA reputation)."""
        return self.session(EPHEMERAL_PROFILE, **kw)


_runtime: BrowserRuntime | None = None


def get_runtime() -> BrowserRuntime:
    """Process-wide runtime (not started until first use)."""
    global _runtime
    if _runtime is None:
        _runtime = BrowserRuntime()
    return _runtime


def set_runtime(runtime: BrowserRuntime | None) -> None:
    """For tests / app wiring: replace the process-wide runtime."""
    global _runtime
    _runtime = runtime


async def shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.stop()
        _runtime = None
