from __future__ import annotations

from tests.unit.pipeline_support import (
    add_account,
    env,  # noqa: F401 - fixture
    events,
)
from treinvertraging import db
from treinvertraging.adapters.fake import FakeTripSource
from treinvertraging.errors import BlockedByBotProtection
from treinvertraging.pipeline import canary
from treinvertraging.web import queries


async def test_canary_ok_writes_event_and_does_not_notify(env):  # noqa: F811
    acc = add_account()
    src = FakeTripSource([])
    res = await canary.run_canary(trip_source_factory=lambda a: src)
    assert res.ok and res.checked == 1
    assert src.login_calls == 1 and len(src.list_calls) == 1
    (ev,) = events("canary")
    assert ev.kind == "canary.ok" and ev.level == "info"
    assert env.sent == []
    assert acc > 0


async def test_canary_failure_notifies_with_debug_dir_and_shows_on_dashboard(env):  # noqa: F811
    add_account(label="Ik")
    bundle = env.data_dir / "debug" / "20261026-ns-login"
    bundle.mkdir(parents=True)
    (bundle / "screenshot.png").write_bytes(b"png")
    exc = BlockedByBotProtection("akamai challenge")
    exc.debug_dir = str(bundle)
    res = await canary.run_canary(trip_source_factory=lambda a: FakeTripSource(login_error=exc))
    assert not res.ok and res.debug_dir == str(bundle)
    (ev,) = events("canary")
    assert ev.kind == "canary.failed" and ev.level == "error"
    assert "Ik" in ev.message and str(bundle) in ev.message
    (note,) = env.sent
    assert (
        note["level"] == "error" and str(bundle) in note["body"] and note["attach"].endswith("screenshot.png")
    )
    with db.session_scope() as s:  # what the dashboard reads
        stats = queries.dashboard_stats(s)
        assert stats.canary.kind == "canary.failed" and stats.canary.level == "error"


async def test_canary_failure_picks_up_newest_debug_bundle_when_exception_has_none(env):  # noqa: F811
    add_account()
    src = FakeTripSource()

    async def boom(since):
        bundle = env.data_dir / "debug" / "20261026-list"
        bundle.mkdir(parents=True)
        raise RuntimeError("layout")

    src.list_trips = boom
    res = await canary.run_canary(trip_source_factory=lambda a: src)
    assert not res.ok and res.debug_dir and res.debug_dir.endswith("20261026-list")


async def test_canary_reports_every_failing_account_in_one_event(env):  # noqa: F811
    add_account(label="a")
    add_account(label="b")
    add_account(label="off", enabled=False)
    res = await canary.run_canary(
        trip_source_factory=lambda a: FakeTripSource(list_error=RuntimeError(a.label))
    )
    assert sorted(res.errors) == ["a", "b"] and res.checked == 2
    assert len(events("canary")) == 1


async def test_canary_without_accounts_is_ok(env):  # noqa: F811
    res = await canary.run_canary()
    assert res.ok and res.checked == 0
