"""Daily canary: login plus a 1-day ``list_trips`` for every enabled account (read-only).

One ``events`` row is written per run: kind ``canary.ok`` (level info) or ``canary.failed``
(level error). The dashboard shows the newest event whose kind starts with ``canary``. A failure
is also notified, with the path of the newest debug bundle created during the run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from treinvertraging import notify as notify_mod
from treinvertraging.adapters import registry
from treinvertraging.db import session_scope
from treinvertraging.models import Account
from treinvertraging.pipeline.context import add_event, debug_dir_of, find_debug_dir, local_today, short_error
from treinvertraging.pipeline.sync import TripSourceFactory, enabled_accounts

log = logging.getLogger(__name__)

CANARY_TIMEOUT_S = 40 * 60


@dataclass
class CanaryResult:
    ok: bool = True
    checked: int = 0
    trips_seen: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # account label -> error
    debug_dir: str | None = None

    def summary(self) -> str:
        if self.ok:
            return f"canary ok: {self.checked} account(s), {self.trips_seen} trip(s) in the last day"
        return "canary FAILED: " + "; ".join(f"{k}: {v}" for k, v in self.errors.items())


async def _check_one(account: Account, factory: TripSourceFactory, since: date) -> int:
    source = factory(account)
    await source.ensure_logged_in()
    return len(await source.list_trips(since))


async def run_canary(
    *,
    trip_source_factory: TripSourceFactory | None = None,
    today: date | None = None,
    timeout: float = CANARY_TIMEOUT_S,
    notify: bool = True,
) -> CanaryResult:
    today = today or local_today()
    since = today - timedelta(days=1)
    factory = trip_source_factory or registry.trip_source_for
    started = datetime.now(UTC)
    result = CanaryResult()
    for account in enabled_accounts():
        result.checked += 1
        try:
            result.trips_seen += await asyncio.wait_for(_check_one(account, factory, since), timeout)
        except Exception as exc:  # noqa: BLE001 - any failure is exactly what the canary reports
            log.warning("canary failed for %s: %s", account.label, short_error(exc))
            result.ok = False
            result.errors[account.label] = short_error(exc)
            result.debug_dir = debug_dir_of(exc) or result.debug_dir
    if not result.ok:
        result.debug_dir = result.debug_dir or find_debug_dir(started)
    message = result.summary() + (f" (debug: {result.debug_dir})" if result.debug_dir else "")
    with session_scope() as s:
        add_event(s, "canary.ok" if result.ok else "canary.failed", message, level="info" if result.ok else "error")
    if not result.ok and notify:
        shot = None
        if result.debug_dir:
            from pathlib import Path

            pngs = sorted(Path(result.debug_dir).glob("*.png"))
            shot = str(pngs[0]) if pngs else None
        await notify_mod.notify(
            "Canary mislukt: de site is mogelijk veranderd",
            message + "\nBekijk de debug-bundel in de webinterface (Claims) voor details.",
            "error",
            attach=shot,
        )
    return result
