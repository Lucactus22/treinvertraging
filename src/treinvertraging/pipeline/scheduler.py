"""APScheduler wiring (runs in the same asyncio loop as the web app, see ``__main__.serve``).

Jobs
    ``daily_run``  sync -> assess -> claim -> reconcile at a random minute inside ``RUN_WINDOW``,
                   re-randomised every day (a one-shot job that schedules its successor for tomorrow).
                   After a restart the job is only re-planned for today if it has not run today yet.
    ``canary``     login + 1-day trip list, also at a random time in the window, daily.
    ``archiver``   NS API arrivals archive every 10 minutes; only registered when ``NS_API_KEY`` is set.
    ``monthly``    on the 3rd at 04:17: RDT import of the previous month for the stations of the
                   household's trips, plus a refresh of the station list.
    ``watchdog``   daily 09:20: expire what is too late, alert on deadlines within 14 days.
    ``digest``     Monday 08:05: weekly summary notification.

Job state lives in ``DATA_DIR/jobs.db`` (SQLAlchemy job store); jobs are re-registered with
``replace_existing`` on every start, so a changed configuration takes effect after a restart.
The job functions are module level (``job_*``) because a persistent store stores them by reference.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from treinvertraging import notify as notify_mod
from treinvertraging.config import get_settings
from treinvertraging.db import session_scope
from treinvertraging.models import SettingKV
from treinvertraging.pipeline import assess as assess_mod
from treinvertraging.pipeline import canary as canary_mod
from treinvertraging.pipeline import claim as claim_mod
from treinvertraging.pipeline import reconcile as reconcile_mod
from treinvertraging.pipeline import sync as sync_mod
from treinvertraging.pipeline.context import add_event, local_tz, pipeline_lock, short_error

log = logging.getLogger(__name__)

DEFAULT_WINDOW = (time(7, 0), time(22, 0))
MISFIRE_GRACE_S = 4 * 3600
ARCHIVER_INTERVAL_MIN = 10
LAST_RUN_PREFIX = "scheduler.last_run."

JOB_LABELS = {
    "daily_run": "Dagelijkse run (sync, beoordelen, indienen, controleren)",
    "canary": "Canary-controle",
    "archiver": "Aankomsten archiveren (NS API)",
    "monthly": "Maandelijkse RDT-import en stationslijst",
    "watchdog": "Deadline-bewaking",
    "digest": "Wekelijks overzicht",
}
RANDOM_JOBS = {"daily_run": "treinvertraging.pipeline.scheduler:job_daily_run",
               "canary": "treinvertraging.pipeline.scheduler:job_canary"}  # fmt: skip


# ---------------------------------------------------------------------------------------------
# random slot within the run window
# ---------------------------------------------------------------------------------------------


def parse_window(text: str | None) -> tuple[time, time]:
    """``"07:00-22:00"`` -> (07:00, 22:00); invalid input falls back to the default window."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", text or "")
    if m:
        try:
            start, end = time(int(m[1]), int(m[2])), time(int(m[3]), int(m[4]))
            if start < end:
                return start, end
        except ValueError:
            pass
    log.warning("invalid RUN_WINDOW %r; using 07:00-22:00", text)
    return DEFAULT_WINDOW


def random_slot(
    day: date,
    window: tuple[time, time],
    *,
    not_before: datetime | None = None,
    rng: random.Random | None = None,
    tz: ZoneInfo | None = None,
) -> datetime | None:
    """A random whole minute on ``day`` inside ``window`` (and after ``not_before``); None if none left."""
    rng = rng or random
    tz = tz or local_tz()
    start = datetime.combine(day, window[0], tzinfo=tz)
    end = datetime.combine(day, window[1], tzinfo=tz)
    if not_before is not None:
        start = max(start, not_before.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1))
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 0:
        return None
    return start + timedelta(minutes=rng.randint(0, minutes))


# ---------------------------------------------------------------------------------------------
# pipeline entry points (also used by services.py and the CLI)
# ---------------------------------------------------------------------------------------------


async def _step(name: str, fn: Callable[[], Any]) -> str:
    try:
        res = await fn()
        return res.summary() if hasattr(res, "summary") else str(res)
    except Exception as exc:  # noqa: BLE001 - a failing step must not stop the next one
        log.exception("pipeline step %s failed", name)
        with session_scope() as s:
            add_event(s, "pipeline.step_failed", f"{name}: {short_error(exc)}", level="error")
        await notify_mod.notify(f"Stap '{name}' mislukt", short_error(exc), "error")
        return f"{name} FAILED: {short_error(exc)}"


async def run_daily_pipeline() -> str:
    """sync -> assess -> claim -> reconcile, one after the other, never concurrently with other runs."""
    async with pipeline_lock():
        parts = [
            await _step("sync", sync_mod.run_sync),
            await _step("assess", assess_mod.run_assess),
            await _step("claim", claim_mod.run_claim),
            await _step("reconcile", reconcile_mod.run_reconcile),
        ]
    return " | ".join(parts)


async def run_monthly() -> str:
    """Import last month's RDT file for the household's stations and refresh the station list."""
    from treinvertraging.delaydata.archiver import frequent_destinations
    from treinvertraging.stations import get_station_index, reset_station_index_cache

    today = datetime.now(local_tz()).date()
    first_this = today.replace(day=1)
    prev = first_this - timedelta(days=1)
    stations = set(frequent_destinations(50))
    imported, skipped = await assess_mod.ensure_rdt([(prev.year, prev.month)], stations, today=today)
    notes = [f"rdt imported {imported or '-'}, skipped {skipped or '-'}"]
    try:
        await get_station_index().refresh(get_settings().DATA_DIR)
        reset_station_index_cache()
        notes.append("stations refreshed")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"stations refresh failed: {short_error(exc)}")
    message = "monthly: " + "; ".join(notes)
    with session_scope() as s:
        add_event(s, "monthly.done", message)
    return message


async def run_archiver() -> str:
    from treinvertraging.delaydata import archiver

    if not get_settings().NS_API_KEY:
        return "archiver: NS_API_KEY not set"
    res = await archiver.archive_once()
    if res.errors:
        log.warning("archiver errors: %s", res.errors)
    return f"archiver: stored {sum(res.stored.values())} arrival(s) at {len(res.stored)} station(s)"


async def run_digest() -> str:
    body_since = datetime.now(UTC) - timedelta(days=7)
    with session_scope() as s:
        body = notify_mod.build_digest(s, since=body_since)
    await notify_mod.notify("Treinvertraging weekoverzicht", body, "info")
    return "digest sent"


# ---------------------------------------------------------------------------------------------
# job functions (module level so a persistent job store can reference them)
# ---------------------------------------------------------------------------------------------


def _mark_ran(job_id: str) -> None:
    today = datetime.now(local_tz()).date().isoformat()
    with session_scope() as s:
        row = s.get(SettingKV, LAST_RUN_PREFIX + job_id)
        if row is None:
            s.add(SettingKV(key=LAST_RUN_PREFIX + job_id, value=today))
        else:
            row.value = today


async def _random_job(job_id: str, fn: Callable[[], Any]) -> None:
    _mark_ran(job_id)
    try:
        result = await fn()
        log.info("job %s finished: %s", job_id, result)
    except Exception:  # noqa: BLE001
        log.exception("job %s crashed", job_id)
    finally:
        sched = get_scheduler()
        if sched is not None:
            sched.plan_random_job(job_id, tomorrow=True)


async def job_daily_run() -> None:
    await _random_job("daily_run", run_daily_pipeline)


async def job_canary() -> None:
    await _random_job("canary", canary_mod.run_canary)


async def job_archiver() -> None:
    try:
        await run_archiver()
    except Exception:  # noqa: BLE001
        log.exception("archiver job failed")


async def job_monthly() -> None:
    try:
        await run_monthly()
    except Exception as exc:  # noqa: BLE001
        log.exception("monthly job failed")
        with session_scope() as s:
            add_event(s, "monthly.failed", short_error(exc), level="error")


async def job_watchdog() -> None:
    try:
        await claim_mod.run_deadline_watchdog()
    except Exception:  # noqa: BLE001
        log.exception("watchdog job failed")


async def job_digest() -> None:
    try:
        await run_digest()
    except Exception:  # noqa: BLE001
        log.exception("digest job failed")


# ---------------------------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------------------------


class PipelineScheduler:
    """Owns the ``AsyncIOScheduler``. Must be started from inside the running event loop."""

    def __init__(self, *, persistent: bool = True, rng: random.Random | None = None) -> None:
        self.persistent = persistent
        self.rng = rng or random.Random()
        self.scheduler: AsyncIOScheduler | None = None

    # -- lifecycle

    def start(self) -> PipelineScheduler:
        settings = get_settings()
        if self.persistent:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            store: Any = SQLAlchemyJobStore(url=f"sqlite:///{settings.DATA_DIR / 'jobs.db'}")
        else:
            store = MemoryJobStore()
        self.scheduler = AsyncIOScheduler(jobstores={"default": store}, timezone=settings.TIMEZONE)
        self.scheduler.start()
        set_scheduler(self)
        self._register_fixed_jobs()
        for job_id in RANDOM_JOBS:
            self.ensure_random_job(job_id)
        log.info("scheduler started with jobs: %s", [j["job"] for j in self.next_runs()])
        return self

    def shutdown(self) -> None:
        if self.scheduler is not None and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if get_scheduler() is self:
            set_scheduler(None)

    # -- jobs

    def _register_fixed_jobs(self) -> None:
        assert self.scheduler is not None
        tz = get_settings().TIMEZONE
        common: dict[str, Any] = {
            "replace_existing": True,
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE_S,
        }
        ref = "treinvertraging.pipeline.scheduler:"
        if get_settings().NS_API_KEY:
            self.scheduler.add_job(
                ref + "job_archiver",
                IntervalTrigger(minutes=ARCHIVER_INTERVAL_MIN, timezone=tz),
                id="archiver",
                name=JOB_LABELS["archiver"],
                **common,
            )
        else:
            self._remove("archiver")
        self.scheduler.add_job(
            ref + "job_monthly",
            CronTrigger(day=3, hour=4, minute=17, timezone=tz),
            id="monthly",
            name=JOB_LABELS["monthly"],
            **common,
        )
        self.scheduler.add_job(
            ref + "job_watchdog",
            CronTrigger(hour=9, minute=20, timezone=tz),
            id="watchdog",
            name=JOB_LABELS["watchdog"],
            **common,
        )
        self.scheduler.add_job(
            ref + "job_digest",
            CronTrigger(day_of_week="mon", hour=8, minute=5, timezone=tz),
            id="digest",
            name=JOB_LABELS["digest"],
            **common,
        )

    def _remove(self, job_id: str) -> None:
        assert self.scheduler is not None
        try:
            self.scheduler.remove_job(job_id)
        except JobLookupError:
            pass

    def _ran_today(self, job_id: str, today: date) -> bool:
        with session_scope() as s:
            row = s.get(SettingKV, LAST_RUN_PREFIX + job_id)
            return bool(row and row.value == today.isoformat())

    def ensure_random_job(self, job_id: str, *, now: datetime | None = None) -> datetime | None:
        """Keep an already planned future run, else plan one (today if still possible, else tomorrow)."""
        assert self.scheduler is not None
        now = now or datetime.now(local_tz())
        job = self.scheduler.get_job(job_id)
        if job is not None and job.next_run_time is not None and job.next_run_time > now:
            return job.next_run_time
        return self.plan_random_job(job_id, tomorrow=False, now=now)

    def plan_random_job(self, job_id: str, *, tomorrow: bool, now: datetime | None = None) -> datetime | None:
        """(Re)plan ``job_id`` at a random minute inside RUN_WINDOW. Returns the chosen time."""
        assert self.scheduler is not None
        tz = local_tz()
        now = (now or datetime.now(tz)).astimezone(tz)
        window = parse_window(get_settings().RUN_WINDOW)
        today = now.date()
        when: datetime | None = None
        if not tomorrow and not self._ran_today(job_id, today):
            when = random_slot(today, window, not_before=now, rng=self.rng, tz=tz)
        if when is None:
            when = random_slot(today + timedelta(days=1), window, rng=self.rng, tz=tz)
        assert when is not None
        self.scheduler.add_job(
            RANDOM_JOBS[job_id],
            DateTrigger(run_date=when, timezone=get_settings().TIMEZONE),
            id=job_id,
            name=JOB_LABELS[job_id],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_S,
        )
        log.info("job %s planned for %s", job_id, when.isoformat())
        return when

    # -- info

    def next_runs(self) -> list[dict[str, Any]]:
        if self.scheduler is None:
            return []
        rows = [
            {"id": j.id, "job": j.name, "next_run": getattr(j, "next_run_time", None)}
            for j in self.scheduler.get_jobs()
        ]
        rows.sort(key=lambda r: (r["next_run"] is None, r["next_run"] or datetime.max.replace(tzinfo=UTC)))
        return rows


_current: PipelineScheduler | None = None


def get_scheduler() -> PipelineScheduler | None:
    return _current


def set_scheduler(scheduler: PipelineScheduler | None) -> None:
    global _current
    _current = scheduler
