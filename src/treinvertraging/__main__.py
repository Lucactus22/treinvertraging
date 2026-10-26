"""Command line interface (Typer). Most commands are stubs wired up by later work packages."""

from __future__ import annotations

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


@app.command()
def serve() -> None:
    """Run the web UI and the scheduler."""
    _not_implemented()


@app.command()
def sync() -> None:
    """Fetch trips from all enabled accounts."""
    _not_implemented()


@app.command()
def assess() -> None:
    """Assess delays and eligibility of detected trips."""
    _not_implemented()


@app.command()
def claim() -> None:
    """Apply the claim policy and submit eligible claims."""
    _not_implemented()


@app.command()
def canary() -> None:
    """Read-only smoke run (login + list trips)."""
    _not_implemented()


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
