"""Command line interface (Typer). Most commands are stubs wired up by later work packages."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Annotated

import typer

from treinvertraging import __version__
from treinvertraging.domain import Source

app = typer.Typer(help="Self-hosted 'geld terug bij vertraging' bot.", no_args_is_help=True)
account_app = typer.Typer(help="Manage NS / OV-chipkaart accounts.", no_args_is_help=True)
app.add_typer(account_app, name="account")


def _not_implemented() -> None:
    typer.echo("not implemented yet")


@app.callback()
def _main(
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.", is_eager=True)] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


def _run(coro_factory: Callable[[], Awaitable[str]], *, init: bool = True) -> str:
    """Run one pipeline coroutine in a fresh event loop; always stops the browser runtime afterwards."""
    from treinvertraging.browser.runtime import shutdown_runtime
    from treinvertraging.db import init_db

    if init:
        init_db()

    async def main() -> str:
        try:
            return await coro_factory()
        finally:
            await shutdown_runtime()

    try:
        return asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary: show a one-line error, exit 1
        typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc


async def serve_async(
    host: str = "0.0.0.0",  # noqa: S104 - the container is reached over the LAN
    port: int = 8080,
    *,
    stop: asyncio.Event | None = None,
    persistent_jobs: bool = True,
    install_signals: bool = True,
) -> None:
    """Run uvicorn (FastAPI app) and the APScheduler in THIS event loop until stopped.

    One loop is required: the takeover and manual-OTP registries hold futures/events that belong to the
    loop they were created in, and the web handlers resolve them from the same loop the scheduled jobs
    wait on. ``stop`` (tests) ends the server like SIGTERM does.

    SIGINT/SIGTERM are handled here instead of by uvicorn, because uvicorn re-raises the signal with the
    default action after its shutdown, which would kill the process before the scheduler and the browser
    runtime are stopped. A second signal forces uvicorn to exit immediately.
    """
    import contextlib

    import uvicorn

    from treinvertraging.browser.runtime import shutdown_runtime
    from treinvertraging.db import init_db
    from treinvertraging.pipeline.scheduler import PipelineScheduler
    from treinvertraging.web.app import create_app

    init_db()
    app = create_app()
    scheduler = PipelineScheduler(persistent=persistent_jobs).start()
    app.state.scheduler = scheduler

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[override]
            yield

    server = _Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    app.state.server = server
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []

    def _on_signal() -> None:
        if server.should_exit:
            server.force_exit = True
        server.should_exit = True

    if install_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):  # not the main thread / Windows
                pass
    watcher: asyncio.Task | None = None
    if stop is not None:

        async def _watch() -> None:
            await stop.wait()
            server.should_exit = True

        watcher = asyncio.create_task(_watch())
    try:
        await server.serve()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        if watcher is not None:
            watcher.cancel()
        scheduler.shutdown()
        await shutdown_runtime()


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "0.0.0.0",  # noqa: S104
    port: Annotated[int, typer.Option(help="Port of the web UI.")] = 8080,
) -> None:
    """Run the web UI and the scheduler (one process, one event loop)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve_async(host, port))


@app.command()
def sync() -> None:
    """Fetch trips from all enabled accounts."""
    from treinvertraging import services

    typer.echo(_run(services.run_sync))


@app.command()
def assess() -> None:
    """Assess delays and eligibility of detected trips."""
    from treinvertraging import services

    typer.echo(_run(services.run_assess))


@app.command()
def claim(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Fill the forms but stop before the final submit button.")
    ] = False,
) -> None:
    """Apply the claim policy and submit eligible claims."""
    from treinvertraging import services

    typer.echo(_run(lambda: services.run_claim(dry_run=dry_run)))


@app.command()
def reconcile() -> None:
    """Compare submitted claims with Mijn NS / confirmation mails."""
    from treinvertraging import services

    typer.echo(_run(services.run_reconcile))


@app.command()
def canary() -> None:
    """Read-only smoke run (login + list trips). Exit code 1 when it fails."""
    from treinvertraging.pipeline import canary as canary_mod

    outcome: list[bool] = []

    async def go() -> str:
        res = await canary_mod.run_canary()
        outcome.append(res.ok)
        return res.summary()

    typer.echo(_run(go))
    if not all(outcome):
        raise typer.Exit(1)


@app.command()
def calibrate(
    channel: Annotated[str | None, typer.Option(help="Channel to calibrate, e.g. gtbv.")] = None,
    redact: Annotated[bool, typer.Option(help="Mask personal data in the output.")] = False,
) -> None:
    """Record real pages and API calls to lock in selectors and endpoints."""
    _not_implemented()


@app.command()
def login(account_id: Annotated[int | None, typer.Argument(help="Account id.")] = None) -> None:
    """Log in interactively (stores the persistent browser session)."""
    _not_implemented()


@account_app.command("add")
def account_add(
    source: Annotated[Source, typer.Option(help="ns or ovchip.")],
    label: Annotated[str, typer.Option(help="Display name.")],
    username: Annotated[str, typer.Option(help="Login e-mail / username.")] = "",
    holder_name: Annotated[str, typer.Option(help="Account holder name for payouts.")] = "",
    email: Annotated[str, typer.Option(help="Contact e-mail for claims.")] = "",
    password: Annotated[str | None, typer.Option(prompt=True, hide_input=True)] = None,
    card_number: Annotated[str | None, typer.Option(help="16-digit OV-chipkaart number.")] = None,
    iban: Annotated[str | None, typer.Option(help="IBAN for payouts.")] = None,
    otp_provider: Annotated[str, typer.Option(help="manual or imap.")] = "manual",
) -> None:
    """Add an account (secrets are stored encrypted)."""
    from treinvertraging import crypto
    from treinvertraging.db import init_db, session_scope
    from treinvertraging.models import Account

    init_db()
    with session_scope() as session:
        acc = Account(
            source=source.value,
            label=label,
            username=username,
            password_enc=crypto.encrypt(password) if password else None,
            card_number_enc=crypto.encrypt(card_number) if card_number else None,
            iban_enc=crypto.encrypt(iban) if iban else None,
            holder_name=holder_name,
            email=email,
            otp_provider=otp_provider,
            enabled=True,
        )
        session.add(acc)
        session.flush()
        typer.echo(f"added account {acc.id} ({acc.source}, {acc.label})")


@account_app.command("list")
def account_list() -> None:
    """List accounts (never prints secrets)."""
    from sqlalchemy import select

    from treinvertraging.db import init_db, session_scope
    from treinvertraging.models import Account

    init_db()
    with session_scope() as session:
        rows = session.scalars(select(Account).order_by(Account.id)).all()
        if not rows:
            typer.echo("no accounts")
            return
        for a in rows:
            state = "enabled" if a.enabled else "disabled"
            typer.echo(f"{a.id}\t{a.source}\t{a.label}\t{a.username}\t{state}")


if __name__ == "__main__":
    app()
