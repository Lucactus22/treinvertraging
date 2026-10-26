from __future__ import annotations

import asyncio

import pytest

from treinvertraging.browser import takeover as tk
from treinvertraging.errors import TakeoverTimeout


@pytest.fixture(autouse=True)
def _clean() -> None:
    tk.clear_takeovers()
    tk.set_default_notifier(None)


async def _wait_active() -> tk.Takeover:
    for _ in range(100):
        if tk.active_takeovers():
            return tk.active_takeovers()[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no takeover registered")


def test_novnc_url_uses_public_host() -> None:
    assert tk.novnc_url("http://nas.lan:8080") == "http://nas.lan:6080/vnc.html"
    assert tk.novnc_url("https://tv.example.com") == "https://tv.example.com:6080/vnc.html"
    assert tk.novnc_url("192.168.1.5:8080") == "http://192.168.1.5:6080/vnc.html"


async def test_complete_flow_notifies_and_unregisters() -> None:
    sent: list[tuple[str, str]] = []

    async def notifier(title: str, body: str) -> None:
        sent.append((title, body))

    task = asyncio.create_task(
        tk.request_takeover(
            "ns-jan", None, "Akamai challenge", timeout=5, notifier=notifier, public_url="http://box:8080"
        )
    )
    t = await _wait_active()
    assert (t.account, t.reason) == ("ns-jan", "Akamai challenge")
    assert t.view()["url"] == "http://box:6080/vnc.html"
    assert "http://box:6080/vnc.html" in sent[0][1]
    assert tk.complete_takeover(t.id)
    done = await task
    assert done.id == t.id and done.completed_at is not None
    assert tk.active_takeovers() == []
    assert not tk.complete_takeover(t.id)  # gone


async def test_timeout_raises_and_unregisters() -> None:
    async def notifier(title: str, body: str) -> None:
        pass

    with pytest.raises(TakeoverTimeout):
        await tk.request_takeover("a", None, "x", timeout=0.1, notifier=notifier)
    assert tk.active_takeovers() == []


async def test_default_notifier_hook_and_failure_tolerated() -> None:
    calls: list[str] = []

    async def bad(title: str, body: str) -> None:
        calls.append(title)
        raise RuntimeError("down")

    tk.set_default_notifier(bad)
    task = asyncio.create_task(tk.request_takeover("a", None, "x", timeout=5))
    t = await _wait_active()
    tk.complete_takeover(t.id)
    await task
    assert calls


async def test_complete_from_other_thread() -> None:
    async def notifier(title: str, body: str) -> None:
        pass

    task = asyncio.create_task(tk.request_takeover("a", None, "x", timeout=5, notifier=notifier))
    t = await _wait_active()
    await asyncio.to_thread(tk.complete_takeover, t.id)
    await asyncio.wait_for(task, 2)


async def test_cancel_unregisters() -> None:
    async def notifier(title: str, body: str) -> None:
        pass

    task = asyncio.create_task(tk.request_takeover("a", None, "x", timeout=5, notifier=notifier))
    await _wait_active()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tk.active_takeovers() == []


async def test_default_notify_without_notify_module_does_not_raise() -> None:
    await tk.default_notify("t", "b")
