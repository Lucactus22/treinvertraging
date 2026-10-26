"""Manual OTP: the web UI shows pending prompts, the user types the code."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from treinvertraging.browser.takeover import default_notify
from treinvertraging.otp.base import OtpTimeout

log = logging.getLogger(__name__)

Notifier = Callable[[str, str], Awaitable[Any]]


@dataclass(eq=False)
class PendingPrompt:
    id: str
    account: str
    created: datetime
    since: datetime
    future: asyncio.Future[str] = field(repr=False)
    loop: asyncio.AbstractEventLoop = field(repr=False)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "account": self.account, "created": self.created.isoformat()}


_pending: dict[str, PendingPrompt] = {}


def pending_prompts() -> list[PendingPrompt]:
    """Prompts waiting for a code, oldest first."""
    return sorted((p for p in _pending.values() if not p.future.done()), key=lambda p: p.created)


def submit_code(prompt_id: str, code: str) -> bool:
    """Deliver ``code`` (whitespace and dashes stripped). False when the prompt is unknown/closed/empty."""
    cleaned = re.sub(r"[\s\-]", "", code or "")
    p = _pending.get(prompt_id)
    if p is None or not cleaned or p.future.done():
        return False

    def _set() -> None:
        if not p.future.done():
            p.future.set_result(cleaned)

    try:
        p.loop.call_soon_threadsafe(_set)
    except RuntimeError:  # loop closed
        return False
    return True


def clear_prompts() -> None:
    """For tests."""
    _pending.clear()


class ManualOtpProvider:
    """``get_code`` registers a prompt and waits for :func:`submit_code`."""

    def __init__(self, notifier: Notifier | None = None) -> None:
        self.notifier = notifier

    async def _notify(self, account: Any) -> None:
        title = f"Verificatiecode nodig: {account}"
        body = "Voer de code in via de webinterface (OTP-pagina)."
        try:
            await (self.notifier or default_notify)(title, body)
        except Exception:  # noqa: BLE001 - notifications are best-effort
            log.info("OTP prompt notification failed", exc_info=True)

    async def get_code(self, account: Any, since: datetime, timeout: float) -> str:
        label = getattr(account, "label", None) or getattr(account, "id", None) or account
        loop = asyncio.get_running_loop()
        p = PendingPrompt(
            id=uuid.uuid4().hex[:12],
            account=str(label),
            created=datetime.now(UTC),
            since=since,
            future=loop.create_future(),
            loop=loop,
        )
        _pending[p.id] = p
        try:
            await self._notify(p.account)
            return await asyncio.wait_for(asyncio.shield(p.future), timeout)
        except TimeoutError:
            raise OtpTimeout(f"no code entered for {p.account} within {timeout:.0f}s") from None
        finally:
            _pending.pop(p.id, None)
            if not p.future.done():
                p.future.cancel()


_manual: ManualOtpProvider | None = None


def get_manual_provider() -> ManualOtpProvider:
    global _manual
    if _manual is None:
        _manual = ManualOtpProvider()
    return _manual
