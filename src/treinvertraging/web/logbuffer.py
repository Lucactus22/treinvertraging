"""In-memory ring buffer for log records, used by the live log tail (SSE)."""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

_CAPACITY = 500


@dataclass(frozen=True)
class LogLine:
    seq: int
    ts: str
    level: str
    logger: str
    message: str

    def format(self) -> str:
        return f"{self.ts} {self.level:<7} {self.logger}: {self.message}"


class RingBufferHandler(logging.Handler):
    """Keeps the last ``capacity`` records; ``lines_after(seq)`` returns newer ones."""

    def __init__(self, capacity: int = _CAPACITY) -> None:
        super().__init__(level=logging.INFO)
        self._buf: deque[LogLine] = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._lock_buf = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[0]:
                msg += f" [{record.exc_info[0].__name__}]"
        except Exception:
            return
        line = LogLine(
            seq=next(self._seq),
            ts=datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            level=record.levelname,
            logger=record.name,
            message=msg,
        )
        with self._lock_buf:
            self._buf.append(line)

    def lines_after(self, seq: int = 0) -> list[LogLine]:
        with self._lock_buf:
            return [ln for ln in self._buf if ln.seq > seq]

    def clear(self) -> None:
        with self._lock_buf:
            self._buf.clear()


_handler: RingBufferHandler | None = None


def get_handler() -> RingBufferHandler:
    """The process-wide handler (created on first use)."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler()
    return _handler


def install(logger: logging.Logger | None = None) -> RingBufferHandler:
    """Attach the handler to the root logger (idempotent) and make sure INFO is emitted."""
    handler = get_handler()
    root = logger or logging.getLogger()
    if handler not in root.handlers:
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler


async def stream(handler: RingBufferHandler, *, backlog: int = 100, poll: float = 1.0):
    """Async generator yielding formatted log lines: the last ``backlog`` first, then new ones."""
    existing = handler.lines_after(0)[-backlog:]
    last = 0
    for ln in existing:
        last = ln.seq
        yield ln.format()
    while True:
        await asyncio.sleep(poll)
        for ln in handler.lines_after(last):
            last = ln.seq
            yield ln.format()
