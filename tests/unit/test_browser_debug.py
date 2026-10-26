# ruff: noqa: F811  (pytest fixtures imported from tests.browser_utils)
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.browser_utils import LocalServer, page_server, runtime  # noqa: F401
from treinvertraging.browser.debug import debug_bundle, prune_bundles, redact
from treinvertraging.browser.runtime import BrowserRuntime


def test_redact_iban() -> None:
    assert redact("IBAN NL91ABNA0417164300 ok") == "IBAN [IBAN] ok"
    assert redact("nl91 abna 0417 1643 00") == "[IBAN]"
    assert redact("DE89 3704 0044 0532 0130 00") == "[IBAN]"


def test_redact_card_numbers() -> None:
    assert redact("kaart 3528123412341234!") == "kaart [CARD]!"
    assert redact("3528 1234 1234 1234") == "[CARD]"
    assert redact("3528-1234-1234-1234") == "[CARD]"


def test_redact_email_and_otp() -> None:
    assert redact("mail jan.devries+ns@example.co.uk nu") == "mail [EMAIL] nu"
    assert redact("Je code is 482913.") == "Je code is [OTP]."
    assert redact("code: 482913") == "code: [OTP]"


def test_redact_leaves_other_numbers_alone() -> None:
    s = "ts 1761472805123 amount 12.50 postcode 3511 AB id 1234567 time 10:00:05 color #123456"
    assert redact(s) == s


def test_redact_passwords() -> None:
    out = redact('{"user": "x", "password": "hunter2", "csrfToken": "tok"}')
    assert "hunter2" not in out and "tok" not in out
    assert redact("email=a%40b.nl&password=hunter2&x=1") == "email=a%40b.nl&password=[REDACTED]&x=1"


def test_redact_empty() -> None:
    assert redact("") == ""


def test_prune_bundles(tmp_path: Path) -> None:
    old = tmp_path / "20200101-000000-old"
    new = tmp_path / "20261026-000000-new"
    old.mkdir()
    new.mkdir()
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    (tmp_path / "stray.txt").write_text("x")
    assert prune_bundles(tmp_path) == 1
    assert not old.exists() and new.exists() and (tmp_path / "stray.txt").exists()
    assert prune_bundles(tmp_path / "missing") == 0


async def test_bundle_written_on_exception_and_redacted(
    page_server: LocalServer, runtime: BrowserRuntime, tmp_path: Path
) -> None:
    page_server.add_json("/api/trips", {"owner": "jan@example.com", "iban": "NL91ABNA0417164300"})
    out = tmp_path / "dbg"
    async with runtime.session("d1") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Laad reizen").click()
        await s.recorder.wait_for(r"/api/trips", timeout=5)
        await s.page.get_by_role("button", name="Inloggen").click()
        await s.recorder.wait_for(r"/api/login", timeout=5)
        await s.page.locator("#out").evaluate("e => e.textContent += ' kaart 3528123412341234'")
        with pytest.raises(ValueError, match="kapot") as ei:
            async with s.debug_bundle("Test Action!", debug_dir=out):
                raise ValueError("kapot voor jan@example.com")
    bundle: Path = ei.value.debug_dir
    assert bundle.parent == out and bundle.name.endswith("-test-action")
    names = {p.name for p in bundle.iterdir()}
    assert {"screenshot.png", "page.html", "recorder.json", "meta.json", "trace.zip", "aria.yml"} <= names
    meta = json.loads((bundle / "meta.json").read_text())
    assert meta["action"] == "Test Action!" and "ValueError" in meta["error"]
    assert "jan@example.com" not in meta["error"]
    everything = "".join(
        (bundle / n).read_text() for n in ("page.html", "recorder.json", "meta.json", "aria.yml")
    )
    for secret in ("jan@example.com", "NL91ABNA0417164300", "3528123412341234", "hunter2"):
        assert secret not in everything
    assert "[IBAN]" in everything and "[EMAIL]" in everything
    assert (bundle / "screenshot.png").stat().st_size > 100
    assert (bundle / "trace.zip").stat().st_size > 100


async def test_bundle_not_written_on_success_unless_kept(
    page_server: LocalServer, runtime: BrowserRuntime, tmp_path: Path
) -> None:
    out = tmp_path / "dbg2"
    async with runtime.session("d2") as s:
        await s.page.goto(page_server.url("/plain.html"))
        async with debug_bundle(s.page, "quiet", debug_dir=out):
            pass
        assert not out.exists() or list(out.iterdir()) == []
        async with debug_bundle(s.page, "kept", debug_dir=out) as b:
            b.keep("submission evidence")
            b.note("reference", "GTBV-1")
        assert b.path is not None and b.path.parent == out
        meta = json.loads((b.path / "meta.json").read_text())
        assert meta["notes"] == {"keep_reason": "submission evidence", "reference": "GTBV-1"}
        assert meta["title"] == "Plain"
        # tracing was properly stopped: a second bundle can trace again
        async with debug_bundle(s.page, "again", debug_dir=out) as b2:
            b2.keep()
        assert (b2.path / "trace.zip").exists()


async def test_bundle_uses_registered_recorder_without_explicit_arg(
    page_server: LocalServer, runtime: BrowserRuntime, tmp_path: Path
) -> None:
    async with runtime.session("d3") as s:
        await s.page.goto(page_server.url("/fetch_demo.html"))
        await s.page.get_by_role("button", name="Laad reizen").click()
        await s.recorder.wait_for(r"/api/trips", timeout=5)
        async with debug_bundle(s.page, "auto-rec", debug_dir=tmp_path) as b:
            b.keep()
        assert "/api/trips" in (b.path / "recorder.json").read_text()


async def test_bundle_prunes_old_on_entry(
    page_server: LocalServer, runtime: BrowserRuntime, tmp_path: Path
) -> None:
    old = tmp_path / "2020-old"
    old.mkdir()
    t = time.time() - 31 * 86400
    os.utime(old, (t, t))
    async with runtime.session("d4") as s:
        await s.page.goto(page_server.url("/plain.html"))
        async with debug_bundle(s.page, "x", debug_dir=tmp_path):
            pass
    assert not old.exists()
