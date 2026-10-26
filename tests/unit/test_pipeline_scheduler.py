from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from tests.unit.pipeline_support import (
    TZ,
    add_account,
    env,  # noqa: F401 - fixture
    events,
    set_mode,
)
from treinvertraging import db, services
from treinvertraging.delaydata import rdt
from treinvertraging.domain import ClaimState
from treinvertraging.models import Claim, SettingKV
from treinvertraging.pipeline import scheduler as sch
from treinvertraging.pipeline.scheduler import PipelineScheduler, parse_window, random_slot


def test_parse_window():
    assert parse_window("07:00-22:00") == (time(7, 0), time(22, 0))
    assert parse_window(" 6:30 - 9:15 ") == (time(6, 30), time(9, 15))
    assert parse_window("garbage") == (time(7, 0), time(22, 0))
    assert parse_window("22:00-07:00") == (time(7, 0), time(22, 0))  # start must be before end


def test_random_slot_stays_inside_window_and_after_not_before():
    window = (time(7, 0), time(22, 0))
    rng = random.Random(1)
    day = date(2026, 10, 27)
    slots = [random_slot(day, window, rng=rng, tz=TZ) for _ in range(300)]
    assert all(
        datetime.combine(day, time(7, 0), TZ) <= s <= datetime.combine(day, time(22, 0), TZ) for s in slots
    )
    assert len({s.minute for s in slots}) > 20  # really random, not on the hour
    assert all(s.second == 0 for s in slots)
    now = datetime(2026, 10, 27, 15, 30, 40, tzinfo=TZ)
    for _ in range(100):
        s = random_slot(day, window, not_before=now, rng=rng, tz=TZ)
        assert now < s <= datetime.combine(day, time(22, 0), TZ)
    late = datetime(2026, 10, 27, 22, 30, tzinfo=TZ)
    assert random_slot(day, window, not_before=late, rng=rng, tz=TZ) is None


async def test_scheduler_registers_jobs_and_next_runs(env):  # noqa: F811
    sched = PipelineScheduler(persistent=False).start()
    try:
        runs = await services.next_runs()
        names = [r["job"] for r in runs]
        assert len(runs) == 5  # no archiver without NS_API_KEY
        assert {r["id"] for r in runs} == {"daily_run", "canary", "monthly", "watchdog", "digest"}
        assert all(r["next_run"] is not None for r in runs)
        assert [r["next_run"] for r in runs] == sorted(r["next_run"] for r in runs)
        assert any("Dagelijkse run" in n for n in names)
        now = datetime.now(TZ)
        for jid in ("daily_run", "canary"):
            when = next(r["next_run"] for r in runs if r["id"] == jid)
            assert when > now
            local = when.astimezone(TZ)
            assert time(7, 0) <= local.time() <= time(22, 0)
    finally:
        sched.shutdown()
    assert await services.next_runs() == []


async def test_archiver_only_with_api_key(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "approve", NS_API_KEY="secret")
    sched = PipelineScheduler(persistent=False).start()
    try:
        job = sched.scheduler.get_job("archiver")
        assert job is not None
        assert job.trigger.interval == timedelta(minutes=10)
    finally:
        sched.shutdown()


async def test_daily_run_is_rerandomised_for_tomorrow_and_not_replanned_after_running(env):  # noqa: F811
    sched = PipelineScheduler(persistent=False, rng=random.Random(7)).start()
    try:
        today = datetime.now(TZ).date()
        when = sched.plan_random_job("daily_run", tomorrow=True)
        assert when.date() == today + timedelta(days=1)
        # same day, already ran: ensure/plan goes to tomorrow
        sch._mark_ran("daily_run")
        sched.scheduler.remove_job("daily_run")
        when2 = sched.ensure_random_job("daily_run")
        assert when2.date() == today + timedelta(days=1)
        # an existing future plan is kept
        assert sched.ensure_random_job("daily_run") == when2
        # different days give different minutes (re-randomised)
        times = {sched.plan_random_job("daily_run", tomorrow=True).time() for _ in range(10)}
        assert len(times) > 1
    finally:
        sched.shutdown()


async def test_job_function_reschedules_itself_for_the_next_day(env, monkeypatch):  # noqa: F811
    called = []

    async def fake_pipeline() -> str:
        called.append(1)
        return "ok"

    monkeypatch.setattr(sch, "run_daily_pipeline", fake_pipeline)
    sched = PipelineScheduler(persistent=False).start()
    try:
        sched.scheduler.remove_job("daily_run")
        await sch.job_daily_run()
        assert called == [1]
        job = sched.scheduler.get_job("daily_run")
        assert job.next_run_time.astimezone(TZ).date() == datetime.now(TZ).date() + timedelta(days=1)
        with db.session_scope() as s:
            assert (
                s.get(SettingKV, sch.LAST_RUN_PREFIX + "daily_run").value
                == datetime.now(TZ).date().isoformat()
            )
    finally:
        sched.shutdown()


async def test_persistent_job_store_survives_restart(env):  # noqa: F811
    first = PipelineScheduler(persistent=True).start()
    planned = first.scheduler.get_job("daily_run").next_run_time
    first.shutdown()
    assert (env.data_dir / "jobs.db").exists()
    second = PipelineScheduler(persistent=True).start()
    try:
        assert second.scheduler.get_job("daily_run").next_run_time == planned  # kept, not re-rolled
        assert len(second.next_runs()) == 5
    finally:
        second.shutdown()


async def test_run_daily_pipeline_with_fake_adapters_submits_exactly_once(env, monkeypatch):  # noqa: F811
    set_mode(monkeypatch, "auto", TV_FAKE_ADAPTERS="1")
    add_account()
    imports = []

    async def rdt_stub(year, month, stations, *a, **k):
        imports.append((year, month))

    monkeypatch.setattr(rdt, "import_month", rdt_stub)
    out1 = await sch.run_daily_pipeline()
    out2 = await sch.run_daily_pipeline()
    assert "FAILED" not in out1 + out2
    lines = (env.data_dir / "fake-submissions.jsonl").read_text().splitlines()
    assert len(lines) == 1  # one delayed fake trip, submitted once over two full runs
    with db.session_scope() as s:
        states = sorted(c.state for c in s.query(Claim))
    assert states == [ClaimState.INELIGIBLE.value, ClaimState.SUBMITTED.value]


async def test_failing_step_does_not_stop_the_next_one(env, monkeypatch):  # noqa: F811
    seen = []

    async def boom():
        raise RuntimeError("sync exploded")

    class R:
        def summary(self):
            return "fine"

    async def ok():
        seen.append(1)
        return R()

    monkeypatch.setattr(sch.sync_mod, "run_sync", boom)
    monkeypatch.setattr(sch.assess_mod, "run_assess", ok)
    monkeypatch.setattr(sch.claim_mod, "run_claim", ok)
    monkeypatch.setattr(sch.reconcile_mod, "run_reconcile", ok)
    out = await sch.run_daily_pipeline()
    assert "sync FAILED" in out and seen == [1, 1, 1]
    assert any(e.kind == "pipeline.step_failed" for e in events("pipeline."))
    assert "Stap 'sync' mislukt" in env.titles()


async def test_monthly_job_imports_previous_month_for_trip_stations_and_refreshes_stations(env, monkeypatch):  # noqa: F811
    from tests.unit.pipeline_support import make_trip, seed_claim

    acc = add_account()
    seed_claim(ClaimState.ELIGIBLE, account_id=acc, trip=make_trip(to="RTD"))
    imports = []

    async def rdt_stub(year, month, stations, *a, **k):
        imports.append((year, month, set(stations)))

    refreshed = []

    async def refresh(self, data_dir=None, **kw):
        refreshed.append(data_dir)

    monkeypatch.setattr(rdt, "import_month", rdt_stub)
    from treinvertraging.stations import StationIndex

    monkeypatch.setattr(StationIndex, "refresh", refresh)
    out = await sch.run_monthly()
    prev = datetime.now(TZ).date().replace(day=1) - timedelta(days=1)
    assert imports == [(prev.year, prev.month, {"RTD"})]
    assert refreshed and "stations refreshed" in out
