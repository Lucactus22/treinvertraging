"""Debug bundles and redaction.

``async with debug_bundle(page, "gtbv-submit") as b:`` writes, when the block raises or
``b.keep()`` was called, a directory ``<DATA_DIR>/debug/<timestamp>-<action>/`` with

* ``screenshot.png`` (full page), ``page.html``, ``aria.yml`` (accessibility snapshot)
* ``recorder.json`` (captured JSON traffic)
* ``trace.zip`` (Playwright trace; NOTE: traces contain unredacted page content, keep them private)
* ``meta.json`` (action, url, title, error, notes)

Text artifacts are passed through :func:`redact`. Bundles older than 30 days are pruned.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from treinvertraging.browser.recorder import Recorder, get_recorder
from treinvertraging.config import get_settings

RETENTION_DAYS = 30

# --------------------------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------------------------

_IBAN_NL = re.compile(r"\bNL\s?\d{2}\s?[A-Z]{4}\s?\d{4}\s?\d{4}\s?\d{2}\b", re.IGNORECASE)
_IBAN_GENERIC = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_OTP = re.compile(r"(?<![\w#+\-])(?<!\d[.,:/])\d{6}(?!\w|[.,]\d|[:/\-])")
_JSON_SECRET = re.compile(
    r'("(?:password|passwd|wachtwoord|pass|csrfToken|access_token|id_token|refresh_token|secret)"\s*:\s*")[^"]*(")',
    re.IGNORECASE,
)
_FORM_SECRET = re.compile(r"((?:password|passwd|wachtwoord|csrfToken)=)[^&\s\"'<>]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Mask IBANs, 16-digit card numbers, e-mail addresses, 6-digit codes and password fields."""
    if not text:
        return text
    text = _JSON_SECRET.sub(r"\1[REDACTED]\2", text)
    text = _FORM_SECRET.sub(r"\1[REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _IBAN_NL.sub("[IBAN]", text)
    text = _IBAN_GENERIC.sub("[IBAN]", text)
    text = _CARD.sub("[CARD]", text)
    text = _OTP.sub("[OTP]", text)
    return text


# --------------------------------------------------------------------------------------------
# bundles
# --------------------------------------------------------------------------------------------


def default_debug_dir() -> Path:
    return get_settings().DATA_DIR / "debug"


def _slug(action: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", action.lower()).strip("-") or "action"


def prune_bundles(
    debug_dir: Path | None = None, *, max_age_days: int = RETENTION_DAYS, now: float | None = None
) -> int:
    """Delete bundle directories whose mtime is older than ``max_age_days``. Returns the count."""
    root = debug_dir or default_debug_dir()
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max_age_days * 86400
    removed = 0
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


class Bundle:
    def __init__(
        self,
        page: Page | None,
        action: str,
        *,
        recorder: Recorder | None = None,
        debug_dir: Path | None = None,
        trace: bool = True,
    ) -> None:
        self.page = page
        self.action = action
        self.recorder = recorder if recorder is not None else (get_recorder(page) if page is not None else None)
        self.debug_dir = debug_dir or default_debug_dir()
        self.want_trace = trace
        self.path: Path | None = None
        self.notes: dict[str, Any] = {}
        self._keep = False
        self._tracing = False

    def keep(self, reason: str | None = None) -> None:
        """Write the bundle on exit even without an exception."""
        self._keep = True
        if reason:
            self.notes["keep_reason"] = reason

    def note(self, key: str, value: Any) -> None:
        self.notes[key] = value

    @property
    def kept(self) -> bool:
        return self._keep

    async def _start_trace(self) -> None:
        if not (self.want_trace and self.page is not None):
            return
        try:
            await self.page.context.tracing.start(screenshots=True, snapshots=True)
            self._tracing = True
        except Exception:  # noqa: BLE001 - already tracing / context closed
            self._tracing = False

    async def _stop_trace(self, path: Path | None) -> None:
        if not self._tracing or self.page is None:
            return
        self._tracing = False
        try:
            if path is not None:
                await self.page.context.tracing.stop(path=str(path))
            else:
                await self.page.context.tracing.stop()
        except Exception:  # noqa: BLE001
            pass

    def _new_dir(self) -> Path:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        base = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{_slug(self.action)}"
        d = self.debug_dir / base
        n = 1
        while d.exists():
            n += 1
            d = self.debug_dir / f"{base}-{n}"
        d.mkdir()
        return d

    async def write(self, error: BaseException | None = None) -> Path:
        """Write the bundle now (idempotent: a second call returns the same directory)."""
        if self.path is not None:
            return self.path
        d = self._new_dir()
        self.path = d
        page = self.page
        meta: dict[str, Any] = {
            "action": self.action,
            "ts": datetime.now(UTC).isoformat(),
            "error": redact(f"{type(error).__name__}: {error}") if error is not None else None,
            "notes": self.notes,
        }
        if page is not None:
            try:
                meta["url"] = redact(page.url)
                meta["title"] = redact(await asyncio.wait_for(page.title(), 5))
            except Exception:  # noqa: BLE001
                pass
            try:
                await page.screenshot(path=str(d / "screenshot.png"), full_page=True, timeout=10_000)
            except Exception:  # noqa: BLE001
                try:
                    await page.screenshot(path=str(d / "screenshot.png"), timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
            try:
                html = await asyncio.wait_for(page.content(), 10)
                (d / "page.html").write_text(redact(html), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            try:
                aria = await page.locator("body").aria_snapshot(timeout=5_000)
                (d / "aria.yml").write_text(redact(aria), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        if self.recorder is not None:
            try:
                await self.recorder.flush()
                (d / "recorder.json").write_text(redact(self.recorder.to_json()), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        await self._stop_trace(d / "trace.zip")
        (d / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return d


@asynccontextmanager
async def debug_bundle(
    page: Page,
    action: str,
    *,
    recorder: Recorder | None = None,
    debug_dir: Path | None = None,
    trace: bool = True,
    prune: bool = True,
) -> AsyncIterator[Bundle]:
    """Collect debugging evidence around ``action``; see module docstring.

    On an exception the bundle directory is attached to it as ``exc.debug_dir`` (best effort) and
    the exception is re-raised.
    """
    b = Bundle(page, action, recorder=recorder, debug_dir=debug_dir, trace=trace)
    if prune:
        try:
            prune_bundles(b.debug_dir)
        except Exception:  # noqa: BLE001
            pass
    await b._start_trace()
    try:
        yield b
    except asyncio.CancelledError:
        await b._stop_trace(None)
        raise
    except BaseException as exc:
        path = await b.write(exc)
        try:
            exc.debug_dir = path  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        if b.kept:
            await b.write(None)
        else:
            await b._stop_trace(None)
