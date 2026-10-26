"""Shared helpers for tests that need a real (headless) Chromium and a local web server.

Reuse from other WPs (no conftest change needed)::

    from tests.browser_utils import page_server, runtime  # noqa: F401  (pytest fixtures)

    async def test_x(page_server, runtime):
        page_server.add_json("/api/trips", {"trips": []})
        async with runtime.session("acc1") as s:
            await s.page.goto(page_server.url("/fetch_demo.html"))

* ``LocalServer``: ``http.server`` in a thread, serving a static directory (default
  ``tests/fixtures/pages``) plus programmable routes (``add_json``, ``add_route``) with an optional
  per-route ``delay``. Every request is logged in ``server.requests``.
* ``make_runtime``: a headless ``BrowserRuntime`` with profiles under a temporary ``data_dir``.
* The default ``/xas/`` route (POST) mimics Mendix: 300 ms delay, JSON ``{"n": <counter>}``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from treinvertraging.browser.runtime import BrowserRuntime

PAGES_DIR = Path(__file__).parent / "fixtures" / "pages"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".yaml": "text/plain; charset=utf-8",
}

# route handler: (method, path, body bytes) -> (status, content_type, body bytes)
RouteHandler = Callable[[str, str, bytes], tuple[int, str, bytes]]


@dataclass
class RecordedRequest:
    method: str
    path: str
    body: bytes
    ts: float


@dataclass
class _Route:
    handler: RouteHandler
    delay: float = 0.0


class LocalServer:
    def __init__(self, directory: Path = PAGES_DIR) -> None:
        self.directory = directory
        self.requests: list[RecordedRequest] = []
        self._routes: dict[tuple[str, str], _Route] = {}
        self._counter = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                outer.requests.append(RecordedRequest(method, self.path, body, time.monotonic()))
                route = outer._routes.get((method, path))
                if route is not None:
                    if route.delay:
                        time.sleep(route.delay)
                    status, ctype, payload = route.handler(method, self.path, body)
                elif method == "GET":
                    status, ctype, payload = outer._static(path)
                else:
                    status, ctype, payload = 404, "text/plain", b"not found"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

        # defaults used by fetch_demo.html
        self.add_json("/api/trips", {"trips": [{"id": 1, "from": "UT", "to": "ASD"}]})
        self.add_route("POST", "/xas/", self._xas, delay=0.3)
        self.add_json("/api/login", {"ok": True}, method="POST")

    # -- lifecycle -------------------------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str = "/") -> str:
        return self.base_url + path

    def start(self) -> LocalServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    # -- routes ----------------------------------------------------------------------------------

    def add_route(self, method: str, path: str, handler: RouteHandler, *, delay: float = 0.0) -> None:
        self._routes[(method.upper(), path)] = _Route(handler, delay)

    def add_json(
        self, path: str, data: Any, *, method: str = "GET", delay: float = 0.0, status: int = 200
    ) -> None:
        payload = json.dumps(data).encode()
        self.add_route(method, path, lambda *_: (status, "application/json", payload), delay=delay)

    def requests_to(self, path_prefix: str, method: str | None = None) -> list[RecordedRequest]:
        return [
            r
            for r in self.requests
            if r.path.startswith(path_prefix) and (method is None or r.method == method)
        ]

    def _xas(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        with self._lock:
            self._counter += 1
            n = self._counter
        return 200, "application/json", json.dumps({"n": n}).encode()

    def _static(self, path: str) -> tuple[int, str, bytes]:
        rel = path.lstrip("/") or "index.html"
        f = (self.directory / rel).resolve()
        if self.directory.resolve() not in f.parents or not f.is_file():
            return 404, "text/plain", b"not found"
        return 200, _CONTENT_TYPES.get(f.suffix, "application/octet-stream"), f.read_bytes()


def make_runtime(data_dir: Path, *, headless: bool = True) -> BrowserRuntime:
    """Headless runtime with profiles in ``data_dir/profiles`` (call ``await rt.stop()`` after)."""
    return BrowserRuntime(headless=headless, data_dir=data_dir)


@pytest.fixture
def page_server() -> Iterator[LocalServer]:
    server = LocalServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest_asyncio.fixture
async def runtime(data_dir: Path) -> AsyncIterator[BrowserRuntime]:
    """Headless ``BrowserRuntime`` using the isolated ``data_dir`` fixture from ``tests/conftest.py``."""
    rt = make_runtime(data_dir)
    await rt.start()
    try:
        yield rt
    finally:
        await rt.stop()
