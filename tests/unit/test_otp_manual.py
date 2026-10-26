from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from treinvertraging.otp.base import OtpProvider, OtpTimeout
from treinvertraging.otp.manual import ManualOtpProvider, clear_prompts, pending_prompts, submit_code


@pytest.fixture(autouse=True)
def _clean() -> None:
    clear_prompts()


async def _noop(title: str, body: str) -> None:
    return None


async def _wait_pending() -> list:
    for _ in range(100):
        if pending_prompts():
            return pending_prompts()
        await asyncio.sleep(0.01)
    raise AssertionError("no prompt registered")


async def test_prompt_roundtrip() -> None:
    notes: list[tuple[str, str]] = []

    async def notifier(title: str, body: str) -> None:
        notes.append((title, body))

    p = ManualOtpProvider(notifier=notifier)
    assert isinstance(p, OtpProvider)
    task = asyncio.create_task(p.get_code("ns-jan", datetime.now(UTC), timeout=5))
    prompts = await _wait_pending()
    assert prompts[0].account == "ns-jan"
    assert prompts[0].view()["id"] == prompts[0].id
    assert submit_code(prompts[0].id, " 123 456 ")
    assert await task == "123456"
    assert pending_prompts() == []
    assert notes and "ns-jan" in notes[0][0]
    assert "123456" not in notes[0][1]


async def test_submit_unknown_or_empty_code() -> None:
    p = ManualOtpProvider(notifier=_noop)
    task = asyncio.create_task(p.get_code("x", datetime.now(UTC), timeout=5))
    prompts = await _wait_pending()
    assert not submit_code("nope", "123456")
    assert not submit_code(prompts[0].id, "   ")
    assert submit_code(prompts[0].id, "999000")
    assert await task == "999000"
    assert not submit_code(prompts[0].id, "111111")  # already closed


async def test_timeout_cleans_up() -> None:
    p = ManualOtpProvider(notifier=_noop)
    with pytest.raises(OtpTimeout):
        await p.get_code("x", datetime.now(UTC), timeout=0.1)
    assert pending_prompts() == []


async def test_notifier_failure_does_not_break_prompt() -> None:
    async def boom(title: str, body: str) -> None:
        raise RuntimeError("apprise down")

    p = ManualOtpProvider(notifier=boom)
    task = asyncio.create_task(p.get_code("x", datetime.now(UTC), timeout=5))
    prompts = await _wait_pending()
    submit_code(prompts[0].id, "424242")
    assert await task == "424242"


async def test_cancel_removes_prompt() -> None:
    p = ManualOtpProvider(notifier=_noop)
    task = asyncio.create_task(p.get_code("x", datetime.now(UTC), timeout=5))
    await _wait_pending()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending_prompts() == []
