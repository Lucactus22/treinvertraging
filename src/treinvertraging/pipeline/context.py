"""Shared helpers for the pipeline steps (sync, assess, claim, reconcile, canary, scheduler).

* ``load_imap_config``: global IMAP settings (web UI -> ``settings_kv``, fall back to ``IMAP_*`` env).
* ``claimant_for``: builds ``ClaimantInfo`` from an account row (decrypts card number and IBAN).
* ``trip_to_dto``: ORM ``Trip`` -> ``TripDTO`` (Europe/Amsterdam). NOTE: the trip *key* is always
  taken from ``Trip.key``, never recomputed from the DTO.
* ``pipeline_lock``: one lock per event loop that serialises whole pipeline runs.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from treinvertraging import crypto
from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.config import get_settings
from treinvertraging.db import session_scope
from treinvertraging.domain import Source, TripDTO
from treinvertraging.models import Account, Event, SettingKV, Trip
from treinvertraging.otp.imap import ImapConfig

log = logging.getLogger(__name__)

# Claim policy / retry constants shared by claim.py and reconcile.py.
MAX_ATTEMPTS = 3


class ClaimantIncomplete(ValueError):
    """The account(s) lack data the claim form needs (card number / IBAN / name / e-mail)."""


# ---------------------------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------------------------


def local_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().TIMEZONE)


def local_today(now: datetime | None = None) -> date:
    now = now or datetime.now(UTC)
    return now.astimezone(local_tz()).date()


def as_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------------------------


def short_error(exc: BaseException, limit: int = 300) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:limit]


def add_event(
    session: Session, kind: str, message: str, *, level: str = "info", trip_key: str | None = None
) -> None:
    session.add(Event(level=level, kind=kind, message=message[:2000], trip_key=trip_key))


def log_event(kind: str, message: str, *, level: str = "info", trip_key: str | None = None) -> None:
    """Write one audit event in its own transaction."""
    with session_scope() as s:
        add_event(s, kind, message, level=level, trip_key=trip_key)


_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def pipeline_lock() -> asyncio.Lock:
    """The lock that serialises full pipeline runs (one per event loop, so tests with fresh loops work)."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock


def find_debug_dir(since: datetime) -> str | None:
    """Newest ``<DATA_DIR>/debug/<bundle>`` directory modified at/after ``since`` (best effort)."""
    root = get_settings().DATA_DIR / "debug"
    try:
        cutoff = as_utc(since).timestamp() - 1
        candidates = [p for p in root.iterdir() if p.is_dir() and p.stat().st_mtime >= cutoff]
    except OSError:
        return None
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def debug_dir_of(obj: object) -> str | None:
    """Debug bundle dir carried by an adapter exception (``debug_dir``) or a ``SubmitResult`` (screenshot)."""
    d = getattr(obj, "debug_dir", None)
    if d:
        return str(d)
    shot = getattr(obj, "screenshot", None)
    if shot:
        return str(Path(str(shot)).parent)
    return None


# ---------------------------------------------------------------------------------------------
# IMAP settings
# ---------------------------------------------------------------------------------------------


def _imap_from_kv(session: Session) -> ImapConfig | None:
    rows = {
        r.key[len("imap.") :]: (r.value or "").strip()
        for r in session.scalars(select(SettingKV).where(SettingKV.key.like("imap.%")))
    }
    host, user, enc = rows.get("host", ""), rows.get("user", ""), rows.get("password_enc", "")
    if not (host and user and enc):
        return None
    try:
        password = crypto.decrypt(enc)
    except Exception:  # noqa: BLE001 - wrong key / corrupt value
        log.warning("IMAP password in settings cannot be decrypted; ignoring stored IMAP settings")
        return None
    m: dict[str, object] = {"host": host, "user": user, "password": password}
    if rows.get("port"):
        try:
            m["port"] = int(rows["port"])
        except ValueError:
            log.warning("invalid imap.port in settings: %r", rows["port"])
    if rows.get("ssl"):
        m["ssl"] = rows["ssl"].lower() not in {"0", "false", "no"}
    for key in ("folder", "senders", "code_regex"):
        if rows.get(key):
            m[key] = rows[key]
    return ImapConfig.from_mapping(m)


def load_imap_config(session: Session | None = None) -> ImapConfig | None:
    """IMAP settings saved in the web UI (``settings_kv``), else the ``IMAP_*`` environment, else None."""
    if session is None:
        with session_scope() as s:
            return load_imap_config(s)
    try:
        cfg = _imap_from_kv(session)
    except Exception:  # noqa: BLE001 - never let a broken settings row break a pipeline run
        log.warning("reading IMAP settings failed", exc_info=True)
        cfg = None
    if cfg is not None:
        return cfg
    try:
        return ImapConfig.from_env()
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------
# accounts / trips
# ---------------------------------------------------------------------------------------------


def digits(text: str | None) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())


def _decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return crypto.decrypt(token).strip()
    except Exception:  # noqa: BLE001
        log.warning("could not decrypt a stored account secret")
        return ""


def claimant_for(account: Account, all_accounts: Sequence[Account] = ()) -> ClaimantInfo:
    """Build the claimant data for a trip of ``account``.

    Each field comes from the trip's own account when present. Otherwise it is taken from another
    *enabled* account: an NS account usually has no card number (the public form needs the
    OV-chipkaart number), so the card number of the first enabled account that has one is used,
    OV-chipkaart accounts first. IBAN, holder name and e-mail fall back the same way (other
    accounts in id order). Raises ``ClaimantIncomplete`` when card number or IBAN is still missing.
    """
    others = [a for a in all_accounts if a.id != account.id and a.enabled]
    card_pref = sorted(others, key=lambda a: (a.source != Source.OVCHIP.value, a.id or 0))
    plain = sorted(others, key=lambda a: a.id or 0)

    def pick(getter, pool: Sequence[Account]) -> str:
        for acc in (account, *pool):
            value = getter(acc)
            if value:
                return value
        return ""

    card = digits(pick(lambda a: digits(_decrypt(a.card_number_enc)), card_pref))
    iban = pick(lambda a: _decrypt(a.iban_enc).replace(" ", "").upper(), plain)
    holder = pick(lambda a: (a.holder_name or "").strip(), plain)
    email = pick(lambda a: (a.email or "").strip(), plain)
    missing = [n for n, v in (("kaartnummer", card), ("IBAN", iban)) if not v]
    if missing:
        raise ClaimantIncomplete(f"ontbrekende gegevens voor account '{account.label}': {', '.join(missing)}")
    return ClaimantInfo(card_number=card, iban=iban, holder_name=holder, email=email)


def trip_to_dto(row: Trip) -> TripDTO:
    """ORM trip -> DTO (tz Europe/Amsterdam). Use ``row.key`` for the key, not ``dto.key()``."""
    tz = local_tz()
    return TripDTO(
        source=Source(row.source),
        card_id=row.card_id,
        checkin_at=as_utc(row.checkin_at).astimezone(tz),
        checkout_at=as_utc(row.checkout_at).astimezone(tz) if row.checkout_at else None,
        from_station=row.from_station,
        to_station=row.to_station,
        fare_eur=row.fare_eur,
        product=row.product,
        operator=row.operator,
        ns_delay_flag=row.ns_delay_flag,
        ns_claim_ref=row.ns_claim_ref,
        raw=dict(row.raw_json or {}),
    )
