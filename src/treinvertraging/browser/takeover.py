"""Human takeover: pause automation, notify, wait until the user clicks "Klaar" in the web UI.

The registry is in-memory (single process). The web UI lists :func:`active_takeovers` and calls
:func:`complete_takeover` from its "Klaar" button.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from treinvertraging.errors import TakeoverTimeout

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], Awaitable[Any]]

DEFAULT_TIMEOUT_S = 30 * 60
NOVNC_PORT = 6080


@dataclass(eq=False)
class Takeover:
    id: str
    account: str
    reason: str
    created: datetime
    url: str
    event: asyncio.Event = field(repr=False)
    loop: asyncio.AbstractEventLoop = field(repr=False)
    completed_at: datetime | None = None
    timed_out: bool = False

    @property
    def done(self) -> bool:
        return self.event.is_set()

    def view(self) -> dict[str, Any]:
        """JSON-friendly representation for the UI."""
        return {
            "id": self.id,
            "account": self.account,
            "reason": self.reason,
            "created": self.created.isoformat(),
            "url": self.url,
        }


_active: dict[str, Takeover] = {}
_default_notifier: Notifier | None = None


def set_default_notifier(notifier: Notifier | None) -> None:
    """Override how takeovers notify (``None`` restores lazy use of ``treinvertraging.notify``)."""
    global _default_notifier
    _default_notifier = notifier


def novnc_url(public_url: str | None = None) -> str:
    """Link to the noVNC viewer: same host as ``PUBLIC_URL`` but on port 6080."""
    if public_url is None:
        from treinvertraging.config import get_settings

        public_url = get_settings().PUBLIC_URL
    parts = urlsplit(public_url if "//" in public_url else f"//{public_url}")
    scheme = parts.scheme or "http"
    host = parts.hostname or "localhost"
    if ":" in host:  # ipv6
        host = f"[{host}]"
    return f"{scheme}://{host}:{NOVNC_PORT}/vnc.html"


async def default_notify(title: str, body: str) -> None:
    """Notify through ``treinvertraging.notify.notify`` if that module exists; else just log."""
    try:
        from treinvertraging import notify as notify_mod

        fn = notify_mod.notify
    except (ImportError, AttributeError):
        log.info("takeover notification (no notifier available): %s - %s", title, body)
        return
    try:
        try:
            res = await fn(title, body, "warning") if inspect.iscoroutinefunction(fn) else await asyncio.to_thread(fn, title, body, "warning")
        except TypeError:
            res = await fn(title, body) if inspect.iscoroutinefunction(fn) else await asyncio.to_thread(fn, title, body)
        if inspect.isawaitable(res):
            await res
    except Exception:  # noqa: BLE001
        log.warning("takeover notification failed", exc_info=True)


async def _send(notifier: Notifier | None, title: str, body: str) -> None:
    fn = notifier or _default_notifier or default_notify
    try:
        await fn(title, body)
    except Exception:  # noqa: BLE001 - never block a takeover on a broken notifier
        log.warning("takeover notifier failed", exc_info=True)


async def start_takeover(
    account: Any,
    page: Any = None,
    reason: str = "",
    *,
    notifier: Notifier | None = None,
    public_url: str | None = None,
) -> Takeover:
    """Register a takeover and notify, without waiting. Pair with :func:`wait_takeover`."""
    url = novnc_url(public_url)
    t = Takeover(
        id=uuid.uuid4().hex[:12],
        account=str(account),
        reason=reason,
        created=datetime.now(UTC),
        url=url,
        event=asyncio.Event(),
        loop=asyncio.get_running_loop(),
    )
    _active[t.id] = t
    if page is not None:
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
    await _send(
        notifier,
        f"Hulp nodig: {t.account}",
        f"{reason or 'Handmatige actie vereist'}\nOpen de browser: {url}\nKlik daarna op 'Klaar' in de webinterface.",
    )
    return t


async def wait_takeover(t: Takeover, timeout: float = DEFAULT_TIMEOUT_S) -> Takeover:
    """Wait for completion; unregisters the takeover. Raises ``TakeoverTimeout`` on timeout."""
    try:
        await asyncio.wait_for(t.event.wait(), timeout)
    except TimeoutError:
        t.timed_out = True
        raise TakeoverTimeout(f"takeover for {t.account} not completed within {timeout:.0f}s: {t.reason}") from None
    finally:
        _active.pop(t.id, None)
    return t


async def request_takeover(
    account: Any,
    page: Any = None,
    reason: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    notifier: Notifier | None = None,
    public_url: str | None = None,
) -> Takeover:
    """Block until the user completes the takeover ("Klaar"); ``TakeoverTimeout`` otherwise.

    Also cleans up when the awaiting task is cancelled.
    """
    t = await start_takeover(account, page, reason, notifier=notifier, public_url=public_url)
    try:
        return await wait_takeover(t, timeout)
    finally:
        _active.pop(t.id, None)


def complete_takeover(takeover_id: str) -> bool:
    """Mark a takeover as done (thread-safe). Returns False when the id is unknown."""
    t = _active.get(takeover_id)
    if t is None:
        return False
    t.completed_at = datetime.now(UTC)
    try:
        t.loop.call_soon_threadsafe(t.event.set)
    except RuntimeError:  # loop closed
        t.event.set()
    return True


def active_takeovers() -> list[Takeover]:
    """Pending takeovers, oldest first."""
    return sorted((t for t in _active.values() if not t.done), key=lambda t: t.created)


def get_takeover(takeover_id: str) -> Takeover | None:
    return _active.get(takeover_id)


def clear_takeovers() -> None:
    """For tests."""
    _active.clear()
