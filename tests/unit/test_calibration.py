from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.browser_utils import page_server, runtime  # noqa: F401 - fixtures
from tests.unit.pipeline_support import add_account, env, env_migrated  # noqa: F401 - fixtures
from treinvertraging import calibration
from treinvertraging.__main__ import app
from treinvertraging.domain import Source
from treinvertraging.models import Account  # noqa: F401

cli = CliRunner()


def test_steps_per_channel_match_the_plan() -> None:
    names = {ch: [s.name for s in steps] for ch, steps in calibration.CHANNEL_STEPS.items()}
    assert names["ns"] == ["login", "reishistorie", "trip-detail", "geld-terug-form", "mijn-aanvragen"]
    assert names["ovchip"] == ["login", "kaartoverzicht", "reishistorie", "export"]
    assert names["gtbv"] == ["start", "reisgegevens", "gegevens", "controle"]
    # the steps that sit right before a submit button warn loudly
    forms = [s for ch in calibration.CHANNEL_STEPS.values() for s in ch if s.no_submit]
    assert {s.name for s in forms} == {"geld-terug-form", "controle"}
    assert all("NIET VERSTUREN" in s.prompt for s in forms)


def test_start_urls() -> None:
    assert calibration.start_url("ns") == "https://www.ns.nl/mijnns"
    assert calibration.start_url("ovchip").startswith("https://www.ov-chipkaart.nl/")
    assert "gtbv-formulier.ns.nl" in calibration.start_url("gtbv")
    with pytest.raises(ValueError):
        calibration.start_url("nope")


def test_resolve_account_validates_channel_and_source(env):  # noqa: F811
    ns = add_account(Source.NS)
    ov = add_account(Source.OVCHIP)
    assert calibration.resolve_account(ns, "ns")[0] == ns
    assert calibration.resolve_account(ns, "gtbv")[0] == ns
    assert calibration.resolve_account(None, "gtbv") == (None, "gtbv")
    with pytest.raises(ValueError, match="needs"):
        calibration.resolve_account(ov, "ns")
    with pytest.raises(ValueError, match="--account"):
        calibration.resolve_account(None, "ns")
    with pytest.raises(LookupError):
        calibration.resolve_account(999, "ns")


async def test_run_calibration_writes_artifacts_and_summary(env, runtime, page_server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(calibration, "start_url", lambda channel: page_server.url("/fetch_demo.html"))
    asked: list[str] = []

    async def confirm(step, index, total, page):
        asked.append(f"{index}/{total} {step.name}")
        if index == 1:
            await page.get_by_role("button", name="Laad reizen").click()
            await page.wait_for_function("document.getElementById('out').textContent.length > 0")
        if index == 2:
            await page.goto(page_server.url("/plain.html"))

    steps = [calibration.Step("een", "doe iets"), calibration.Step("twee", "doe nog iets", no_submit=True)]
    result = await calibration.run_calibration(
        None, "gtbv", confirm=confirm, runtime=runtime, steps=steps, require_headed=False
    )
    assert asked == ["1/2 een", "2/2 twee"]
    root = env.data_dir / "calibration" / "gtbv"
    assert result.out_dir == root and result.steps == ["01-een", "02-twee"]
    for d in ("01-een", "02-twee"):
        files = {p.name for p in (root / d).iterdir()}
        assert {"page.html", "screenshot.png", "aria.yml", "recorder.json", "meta.json"} <= files
    # recorder.json of step 2 only holds step 2's traffic (cleared after step 1)
    first = json.loads((root / "01-een" / "recorder.json").read_text())
    second = json.loads((root / "02-twee" / "recorder.json").read_text())
    assert any(e["url"].endswith("/api/trips") for e in first) and second == []

    summary = json.loads((root / "calibration-summary.json").read_text())
    (ep,) = [e for e in summary["candidate_json_endpoints"] if e["url"].endswith("/api/trips")]
    assert ep["method"] == "GET" and ep["status"] == 200 and ep["top_level_keys"] == ["trips"]
    assert ep["seen_in_steps"] == ["een"]
    s1, s2 = summary["steps"]
    assert "Fetch demo" in s1["headings"] and "Laad reizen" in s1["buttons"]
    assert s1["json_endpoints"][0]["top_level_keys"] == ["trips"]
    assert s2["url"].endswith("/plain.html")
    assert result.candidate_endpoints == 1 and "1 JSON-endpoint" in result.summary()


async def test_run_calibration_refuses_headless_runtime(env, runtime):  # noqa: F811
    async def confirm(*_a):
        raise AssertionError("must not be asked")

    with pytest.raises(calibration.CalibrationError, match="zichtbare browser"):
        await calibration.run_calibration(None, "gtbv", confirm=confirm, runtime=runtime)


async def test_service_start_calibration_validates_before_starting_a_browser(env):  # noqa: F811
    from treinvertraging import services

    ov = add_account(Source.OVCHIP)
    with pytest.raises(ValueError):
        await services.start_calibration(ov, "bogus")
    with pytest.raises(ValueError):
        await services.start_calibration(ov, "ns")
    with pytest.raises(LookupError):
        await services.start_calibration(404, "gtbv")


async def test_service_start_calibration_uses_takeovers(env, monkeypatch):  # noqa: F811
    """Each step becomes a takeover with the agreed reason text; "Klaar" resumes the walkthrough."""
    import asyncio

    from treinvertraging import services
    from treinvertraging.browser import takeover

    acc = add_account(Source.NS)
    reasons: list[str] = []

    async def fake_request(account, page, reason, *, timeout):
        reasons.append(reason)

    monkeypatch.setattr(calibration, "request_takeover", fake_request)
    seen: dict = {}

    async def fake_run(account_id, channel, *, confirm, **kw):
        seen["args"] = (account_id, channel)
        for i, step in enumerate(calibration.CHANNEL_STEPS[channel], start=1):
            await confirm(step, i, len(calibration.CHANNEL_STEPS[channel]), None)
        return calibration.CalibrationResult(channel, Path("/x"), steps=["01-login"])

    monkeypatch.setattr(calibration, "run_calibration", fake_run)
    out = await asyncio.wait_for(services.start_calibration(acc, "ns"), 5)
    assert seen["args"] == (acc, "ns") and "Kalibratie 'ns' klaar" in out
    assert len(reasons) == 5
    assert reasons[0].startswith("Kalibratie stap 1/5 (login): ga naar ")
    assert reasons[3].count("NIET VERSTUREN") == 1 and reasons[3].endswith("Klik daarna op Klaar.")
    assert takeover.active_takeovers() == []


# ---- redaction ----------------------------------------------------------------------------------


def _tree(root: Path) -> None:
    step = root / "ns" / "01-login"
    step.mkdir(parents=True)
    (step / "page.html").write_text(
        "<p>Jan de Vries jan@example.com NL91 ABNA 0417 1643 00 kaart 3528 0000 0000 0001</p>"
    )
    (step / "recorder.json").write_text(
        '[{"response_body": {"iban": "NL91ABNA0417164300", "password": "hunter2"}}]'
    )
    (step / "aria.yml").write_text("- heading 'Welkom jan@example.com'")
    (step / "screenshot.png").write_bytes(b"\x89PNG secret")
    (step / "trace.zip").write_bytes(b"PK secret")
    (step / "download.pdf").write_bytes(b"%PDF secret")
    (root / "ns" / "calibration-summary.json").write_text('{"labels": ["jan@example.com"]}')


def test_redact_tree_masks_text_and_skips_screenshots_and_traces(tmp_path: Path) -> None:
    src, dst = tmp_path / "cal", tmp_path / "out"
    _tree(src)
    skipped = calibration.redact_tree(src, dst, extra_terms=["Jan de Vries", "Vries"])
    assert sorted(skipped) == [
        "ns/01-login/download.pdf",
        "ns/01-login/screenshot.png",
        "ns/01-login/trace.zip",
    ]
    html = (dst / "ns/01-login/page.html").read_text()
    for secret in ("jan@example.com", "NL91", "3528", "Jan de Vries"):
        assert secret not in html
    assert "[EMAIL]" in html and "[IBAN]" in html and "[CARD]" in html and "[NAME]" in html
    js = (dst / "ns/01-login/recorder.json").read_text()
    assert "hunter2" not in js and "0417164300" not in js
    assert "jan@example.com" not in (dst / "ns/01-login/aria.yml").read_text()
    assert "jan@example.com" not in (dst / "ns/calibration-summary.json").read_text()
    # nothing binary was copied
    assert not list(dst.rglob("*.png")) and not list(dst.rglob("*.zip")) and not list(dst.rglob("*.pdf"))
    readme = (dst / "README.md").read_text()
    assert "screenshot.png" in readme and "trace.zip" in readme and "persoonsgegevens" in readme
    # the source is untouched
    assert "jan@example.com" in (src / "ns/01-login/page.html").read_text()


def test_redact_tree_refuses_output_inside_source_and_missing_source(tmp_path: Path) -> None:
    _tree(tmp_path / "cal")
    with pytest.raises(ValueError):
        calibration.redact_tree(tmp_path / "cal", tmp_path / "cal" / "out")
    with pytest.raises(FileNotFoundError):
        calibration.redact_tree(tmp_path / "missing", tmp_path / "out")


def test_cli_redact_only(env, tmp_path: Path) -> None:  # noqa: F811
    _tree(env.data_dir / "calibration")
    out = tmp_path / "redacted"
    res = cli.invoke(app, ["calibrate", "--channel", "ns", "--redact", str(out)])
    assert res.exit_code == 0, res.output
    assert "NOT copied" in res.output and (out / "README.md").is_file()
    assert (out / "01-login" / "page.html").is_file()  # --channel narrows the copy to that channel


def test_cli_argument_validation(env) -> None:  # noqa: F811
    assert cli.invoke(app, ["calibrate"]).exit_code == 2
    assert cli.invoke(app, ["calibrate", "--channel", "bogus"]).exit_code == 2
    assert cli.invoke(app, ["calibrate", "--channel", "ns"]).exit_code == 2  # needs --account
    assert cli.invoke(app, ["calibrate", "--account", "1"]).exit_code == 2  # needs --channel
    assert cli.invoke(app, ["login"]).exit_code == 2


def test_cli_login_runs_ensure_logged_in_with_fakes(env_migrated, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setenv("TV_FAKE_ADAPTERS", "1")
    acc = add_account(Source.NS, label="Mijn NS")
    res = cli.invoke(app, ["login", "--account", str(acc)])
    assert res.exit_code == 0, res.output
    assert "Ingelogd bij 'Mijn NS'." in res.output
    res = cli.invoke(app, ["login", "999"])
    assert res.exit_code == 1 and "LookupError" in res.output
