"""Network recorder: captures JSON responses and tracks in-flight requests.

Attach one :class:`Recorder` to a ``BrowserContext`` (all pages) or a single ``Page``.

* JSON responses (content type contains ``json``) end up in a bounded ring
  (:attr:`Recorder.entries`) with request body, response body (truncated to 2 MB), status, ...
* Every request is tracked until it finishes or fails, so :meth:`Recorder.wait_idle` can wait for
  "no pending request matching ``url_regex`` and none started/finished for ``quiet_ms``". This is
  what the Mendix form needs (no pending ``POST /xas/``).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import weakref
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Request, Response

MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_RING_SIZE = 500
# A request that never reports completion (page closed mid-flight) must not block wait_idle forever.
PENDING_EXPIRY_S = 120.0

_registry: weakref.WeakKeyDictionary[Any, Recorder] = weakref.WeakKeyDictionary()


@dataclass
class RecordedResponse:
    url: str
    method: str
    status: int
    content_type: str
    request_body: str | None
    response_body: str
    truncated: bool = False
    resource_type: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def json(self) -> Any:
        """Parse the response body as JSON (raises ``ValueError`` when it is not valid JSON)."""
        return json.loads(self.response_body)

    def request_json(self) -> Any:
        if not self.request_body:
            raise ValueError("no request body")
        return json.loads(self.request_body)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        try:
            d["response_body"] = self.json()
        except ValueError:
            pass
        return d


@dataclass
class _Pending:
    url: str
    method: str
    started: float


class Recorder:
    def __init__(self, *, max_entries: int = DEFAULT_RING_SIZE, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        self.entries: deque[RecordedResponse] = deque(maxlen=max_entries)
        self.max_body_bytes = max_body_bytes
        self._pending: dict[Request, _Pending] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._activity: deque[tuple[float, str]] = deque(maxlen=2000)  # (monotonic ts, url)
        self._total = 0  # monotonically increasing count of recorded entries (for wait_for)
        self._target: Page | BrowserContext | None = None

    # -- attach / detach -------------------------------------------------------------------------

    def attach(self, target: Page | BrowserContext) -> Recorder:
        if self._target is not None:
            raise RuntimeError("Recorder is already attached")
        self._target = target
        target.on("request", self._on_request)
        target.on("requestfinished", self._on_request_done)
        target.on("requestfailed", self._on_request_done)
        target.on("response", self._on_response)
        try:
            _registry[target] = self
        except TypeError:  # not weak-referenceable; debug_bundle then needs an explicit recorder
            pass
        return self

    def detach(self) -> None:
        t = self._target
        if t is None:
            return
        for ev, cb in (
            ("request", self._on_request),
            ("requestfinished", self._on_request_done),
            ("requestfailed", self._on_request_done),
            ("response", self._on_response),
        ):
            try:
                t.remove_listener(ev, cb)
            except Exception:  # noqa: BLE001 - target may already be closed
                pass
        self._target = None

    # -- event handlers (sync; body capture is scheduled as a task) ------------------------------

    def _on_request(self, request: Request) -> None:
        now = time.monotonic()
        self._pending[request] = _Pending(request.url, request.method, now)
        self._activity.append((now, request.url))

    def _on_request_done(self, request: Request) -> None:
        self._pending.pop(request, None)
        self._activity.append((time.monotonic(), request.url))

    def _on_response(self, response: Response) -> None:
        task = asyncio.ensure_future(self._capture(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, response: Response) -> None:
        try:
            headers = await response.all_headers()
            ctype = headers.get("content-type", "")
            if "json" not in ctype.lower():
                return
            request = response.request
            try:
                req_body: str | None = request.post_data
            except Exception:  # noqa: BLE001 - binary post data
                req_body = None
            try:
                raw = await response.body()
            except Exception:  # noqa: BLE001 - redirect / navigated away / evicted
                return
            truncated = len(raw) > self.max_body_bytes
            body = raw[: self.max_body_bytes].decode("utf-8", errors="replace")
            self.entries.append(
                RecordedResponse(
                    url=response.url,
                    method=request.method,
                    status=response.status,
                    content_type=ctype,
                    request_body=req_body,
                    response_body=body,
                    truncated=truncated,
                    resource_type=request.resource_type,
                )
            )
            self._total += 1
        except Exception:  # noqa: BLE001 - never let the recorder break the page flow
            return

    # -- queries ---------------------------------------------------------------------------------

    async def flush(self) -> None:
        """Wait until all response bodies seen so far have been captured."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def find(self, url_regex: str, *, method: str | None = None, status: int | None = None) -> list[RecordedResponse]:
        """Recorded responses whose URL matches ``url_regex`` (``re.search``), oldest first."""
        rx = re.compile(url_regex)
        return [
            e
            for e in self.entries
            if rx.search(e.url)
            and (method is None or e.method.upper() == method.upper())
            and (status is None or e.status == status)
        ]

    def latest(self, url_regex: str, *, method: str | None = None) -> RecordedResponse | None:
        found = self.find(url_regex, method=method)
        return found[-1] if found else None

    async def wait_for(
        self, url_regex: str, *, timeout: float = 15.0, method: str | None = None, new_only: bool = True
    ) -> RecordedResponse:
        """Wait for a (new) JSON response matching ``url_regex``; ``TimeoutError`` when none arrives."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        start_total = self._total if new_only else -1
        seen_before = {id(e) for e in self.entries} if new_only else set()
        while True:
            for e in self.find(url_regex, method=method):
                if id(e) not in seen_before or not new_only:
                    return e
            if loop.time() >= deadline:
                raise TimeoutError(f"no response matching {url_regex!r} within {timeout}s (seen {self._total - start_total})")
            await asyncio.sleep(0.025)

    def pending(self, url_regex: str | None = None) -> list[tuple[str, str]]:
        """In-flight requests (method, url), optionally filtered by ``url_regex``."""
        now = time.monotonic()
        rx = re.compile(url_regex) if url_regex else None
        out = []
        for p in self._pending.values():
            if now - p.started > PENDING_EXPIRY_S:
                continue
            if rx is None or rx.search(p.url):
                out.append((p.method, p.url))
        return out

    def _last_matching_activity(self, rx: re.Pattern[str]) -> float | None:
        for ts, url in reversed(self._activity):
            if rx.search(url):
                return ts
        return None

    async def wait_idle(self, url_regex: str = r"/xas/", *, quiet_ms: int = 500, timeout: float = 15.0) -> None:
        """Wait until no request matching ``url_regex`` is in flight and none started/finished in
        the last ``quiet_ms`` milliseconds. Raises ``TimeoutError`` after ``timeout`` seconds.

        Call it right after the action that triggers the requests; if no matching request is ever
        issued it simply returns after ``quiet_ms``.
        """
        rx = re.compile(url_regex)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        quiet = quiet_ms / 1000
        call_time = time.monotonic()
        while True:
            now = time.monotonic()
            live = [p for p in self._pending.values() if rx.search(p.url) and now - p.started <= PENDING_EXPIRY_S]
            if not live:
                last = max(call_time, self._last_matching_activity(rx) or 0.0)
                if now - last >= quiet:
                    await self.flush()
                    return
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"requests matching {url_regex!r} still pending after {timeout}s: "
                    f"{[(p.method, p.url) for p in live][:5]}"
                )
            await asyncio.sleep(0.02)

    def clear(self) -> None:
        self.entries.clear()

    # -- persistence -----------------------------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.entries], indent=2, ensure_ascii=False, default=str)

    def dump(self, directory: str | Path) -> Path:
        """Write ``recorder.json`` into ``directory`` (created if needed) and return its path."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "recorder.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def get_recorder(target: Page | BrowserContext) -> Recorder | None:
    """The recorder attached to ``target`` (or to the page's context), if any."""
    for t in (target, getattr(target, "context", None)):
        if t is None:
            continue
        try:
            rec = _registry.get(t)
        except TypeError:
            rec = None
        if rec is not None:
            return rec
    return None
