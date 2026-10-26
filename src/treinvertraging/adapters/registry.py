"""Factory for the adapters used by the pipeline.

The real adapters are imported lazily (they pull in Playwright). With ``TV_FAKE_ADAPTERS=1`` the
fakes from ``adapters/fake.py`` are returned instead (see that module for their configuration).

* ``trip_source_for(account)``: ``NsAdapter`` / ``OvchipAdapter`` for the account's source.
* ``submitters(channels, account=None)``: submitters in the order of ``channels`` (``"gtbv"`` ->
  ``GtbvSubmitter``; ``"mijnns"`` -> ``NsAdapter`` of ``account`` when that is an NS account, else
  of the first enabled NS account). Channels that cannot be built are skipped (logged).
* ``status_readers(accounts)``: one ``NsAdapter`` per enabled NS account ("mijn aanvragen").

``imap`` (an ``ImapConfig``) is only needed for accounts with ``otp_provider == "imap"``; when
omitted it is loaded from the settings / environment. Without any IMAP config such an account falls
back to the manual (web UI) OTP provider.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

from treinvertraging.adapters.base import ClaimStatusReader, ClaimSubmitter, TripSource
from treinvertraging.config import get_settings
from treinvertraging.domain import Source

log = logging.getLogger(__name__)

__all__ = ["fake_adapters_enabled", "status_readers", "submitters", "trip_source_for"]


def fake_adapters_enabled() -> bool:
    return os.environ.get("TV_FAKE_ADAPTERS", "").strip().lower() in {"1", "true", "yes", "on"}


def _otp_for(account: Any, imap: Any) -> Any:
    from treinvertraging.otp.base import get_otp_provider

    if (getattr(account, "otp_provider", None) or "manual").lower() == "imap":
        if imap is None:
            from treinvertraging.pipeline.context import load_imap_config

            imap = load_imap_config()
        if imap is None:
            log.warning("account %s wants IMAP OTP but IMAP is not configured; using manual OTP", account.id)
            from treinvertraging.otp.manual import get_manual_provider

            return get_manual_provider()
    return get_otp_provider(account, imap=imap)


def _first_ns_account() -> Any | None:
    from treinvertraging.db import session_scope
    from treinvertraging.models import Account

    with session_scope() as s:
        return s.scalars(
            select(Account)
            .where(Account.source == Source.NS.value, Account.enabled.is_(True))
            .order_by(Account.id)
        ).first()


# ---------------------------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------------------------


def _fake_trip_source(account: Any) -> TripSource:
    from treinvertraging.adapters import fake

    source = Source(account.source)
    path = os.environ.get("TV_FAKE_TRIPS_FILE") or str(get_settings().DATA_DIR / "fake_trips.json")
    if Path(path).is_file():
        return fake.FakeTripSource(
            fake.trips_from_json(Path(path).read_text(encoding="utf-8"), source), source=source
        )
    return fake.FakeTripSource(source=source)


def _fake_submitters(channels: Iterable[str]) -> list[ClaimSubmitter]:
    from treinvertraging.adapters import fake

    failures = fake.failures_from_env(os.environ.get("TV_FAKE_FAIL"))
    log_path = Path(
        os.environ.get("TV_FAKE_SUBMISSIONS_FILE") or str(get_settings().DATA_DIR / "fake-submissions.jsonl")
    )
    return [fake.FakeSubmitter(ch, always=failures.get(ch), log_path=log_path) for ch in channels]


def _fake_status_readers() -> list[ClaimStatusReader]:
    from treinvertraging.adapters import fake

    path = os.environ.get("TV_FAKE_STATUS_FILE")
    records = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else []
    return [fake.FakeStatusReader(records)]


# ---------------------------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------------------------


def trip_source_for(account: Any, *, imap: Any = None) -> TripSource:
    if fake_adapters_enabled():
        return _fake_trip_source(account)
    source = Source(account.source)
    otp = _otp_for(account, imap)
    if source is Source.NS:
        from treinvertraging.adapters.ns import NsAdapter

        return NsAdapter(account, otp=otp)
    from treinvertraging.adapters.ovchip import OvchipAdapter

    return OvchipAdapter(account, otp=otp)


def submitters(channels: Sequence[str], *, account: Any = None, imap: Any = None) -> list[ClaimSubmitter]:
    """Submitters for ``channels`` in that order (unknown or unbuildable channels are skipped)."""
    channels = [c.strip().lower() for c in channels if c and c.strip()]
    if fake_adapters_enabled():
        return _fake_submitters(channels)
    out: list[ClaimSubmitter] = []
    for channel in channels:
        if channel == "gtbv":
            from treinvertraging.adapters.gtbv import GtbvSubmitter

            out.append(GtbvSubmitter())
        elif channel == "mijnns":
            ns_account = account if account is not None and account.source == Source.NS.value else None
            ns_account = ns_account or _first_ns_account()
            if ns_account is None:
                log.info("channel mijnns skipped: no enabled NS account")
                continue
            from treinvertraging.adapters.ns import NsAdapter

            out.append(NsAdapter(ns_account, otp=_otp_for(ns_account, imap)))
        else:
            log.warning("unknown submit channel %r ignored", channel)
    return out


def status_readers(accounts: Iterable[Any], *, imap: Any = None) -> list[ClaimStatusReader]:
    """Readers for "mijn aanvragen": one per enabled NS account."""
    if fake_adapters_enabled():
        return _fake_status_readers()
    out: list[ClaimStatusReader] = []
    for account in accounts:
        if account.source == Source.NS.value and account.enabled:
            from treinvertraging.adapters.ns import NsAdapter

            out.append(NsAdapter(account, otp=_otp_for(account, imap)))
    return out
