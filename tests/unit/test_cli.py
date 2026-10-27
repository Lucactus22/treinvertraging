from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from treinvertraging.__main__ import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("serve", "sync", "assess", "claim", "canary", "calibrate", "login", "account"):
        assert cmd in result.output


def test_account_help() -> None:
    result = runner.invoke(app, ["account", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output and "list" in result.output


def test_account_add_and_list(data_dir: Path) -> None:
    result = runner.invoke(
        app, ["account", "add", "--source", "ns", "--label", "Ik", "--password", "pw", "--iban", "NL00TEST"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["account", "list"])
    assert result.exit_code == 0
    assert "Ik" in result.output and "pw" not in result.output
