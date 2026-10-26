"""Mijn NS adapter (WP-H): trip source, status reader and *fallback* claim channel ("mijnns").

Every locator, label, URL and regex lives in ``defaults/selectors/ns.yaml`` (override in
``CONFIG_DIR/selectors/ns.yaml``); this module only holds the control flow and the tolerant
parsers. Nothing here knows the real post-login page structure: it is calibrated in milestone M6.

Strategy ladder (PLAN.md section 1):

* ``ensure_logged_in``  open ``/mijnns`` -> login form (human typing) -> optional verification code
  (OTP provider) -> bot protection detected => human takeover, then one more attempt.
* ``list_trips`` / ``list_submitted_claims``  1. API replay (``api.travel_history`` / ``api.claims``)
  2. navigate by link names and parse the JSON captured by the recorder with a tolerant mapper
  3. DOM fallback (``dom.*`` / ``claims_dom.*``).
* ``submit_claim``  prefilled form (``ns_claim_ref``) or open from the trip, fill IBAN/holder only when
  empty, tick confirmations, screenshot, stop before the final click on ``dry_run``.

Secrets (password, IBAN, OTP codes) are never logged; error texts are passed through ``redact``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from treinvertraging import crypto, models, selectors
from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.browser.debug import Bundle, default_debug_dir, redact
from treinvertraging.browser.human import dismiss_cookie_banner, human_click, human_type, jitter_sleep
from treinvertraging.browser.runtime import BrowserRuntime, BrowserSession, get_runtime
from treinvertraging.domain import Source, SubmitResult, TripDTO
from treinvertraging.errors import (
    BlockedByBotProtection,
    InvalidInput,
    LayoutChanged,
    LoginRequired,
)
from treinvertraging.otp.base import OtpProvider, OtpTimeout, get_otp_provider
from treinvertraging.stations import get_station_index

log = logging.getLogger(__name__)

SITE = "ns"
AMS = ZoneInfo("Europe/Amsterdam")
DEFAULT_BASE_URL = "https://www.ns.nl"

# net errors that look like a WAF dropping the connection rather than an outage
_BLOCK_NET_ERRORS = re.compile(
    r"ERR_HTTP2_PROTOCOL_ERROR|ERR_CONNECTION_RESET|ERR_EMPTY_RESPONSE|ERR_ACCESS_DENIED|ERR_SSL_PROTOCOL", re.I
)


def _cfg(key: str, default: Any = None) -> Any:
    return selectors.get(SITE, key, default)


# =================================================================================================
# pure helpers: IBAN, dates, money, card numbers
# =================================================================================================


def normalize_iban(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def valid_iban(value: str) -> bool:
    """ISO 13616 shape + mod-97 checksum."""
    n = normalize_iban(value)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", n):
        return False
    rearranged = n[4:] + n[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "maa": 3, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_RE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
_RE_NL_NUM = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:\D+(\d{1,2})[:.](\d{2}))?")
_RE_NL_TXT = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})(?:\D+(\d{1,2})[:.](\d{2}))?")
_RE_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?$")


def _to_ams(dt: datetime) -> datetime:
    return dt.replace(tzinfo=AMS) if dt.tzinfo is None else dt.astimezone(AMS)


def _stamp(value: Any) -> tuple[str, Any] | None:
    """Classify a scalar as ``("dt", datetime)``, ``("date", date)`` or ``("time", time)``."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, datetime):
            return ("dt", _to_ams(value))
        if isinstance(value, int | float):
            if value > 1e11:
                return ("dt", datetime.fromtimestamp(value / 1000, AMS))
            if value > 1e8:
                return ("dt", datetime.fromtimestamp(value, AMS))
            return None
        if not isinstance(value, str):
            return None
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return _stamp(int(s)) if len(s) >= 9 else None
        m = _RE_TIME.match(s)
        if m:
            return ("time", time(int(m[1]), int(m[2])))
        if _RE_ISO.match(s):
            if len(s) <= 10:
                return ("date", date.fromisoformat(s))
            return ("dt", _to_ams(datetime.fromisoformat(s)))
        m = _RE_NL_NUM.search(s)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
            if m[4] is None:
                return ("date", date(y, mo, d))
            return ("dt", datetime(y, mo, d, int(m[4]), int(m[5]), tzinfo=AMS))
        m = _RE_NL_TXT.search(s)
        if m and m[2].lower() in _MONTHS:
            d, mo, y = int(m[1]), _MONTHS[m[2].lower()], int(m[3])
            if m[4] is None:
                return ("date", date(y, mo, d))
            return ("dt", datetime(y, mo, d, int(m[4]), int(m[5]), tzinfo=AMS))
    except (ValueError, OverflowError, OSError):
        return None
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Tolerant parser: ISO (with/without offset), epoch s/ms, ``12-10-2026 08:15``, ``12 okt 2026 08:15``.

    Naive values are Europe/Amsterdam local time; the result is always in Europe/Amsterdam.
    Date-only and time-only values return ``None``.
    """
    st = _stamp(value)
    return st[1] if st and st[0] == "dt" else None


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _tokens(key: Any) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", str(key))]


def _money_from_str(s: str) -> Decimal | None:
    t = re.sub(r"[^\d,.\-]", "", s)
    if not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in t:
        t = t.replace(",", ".") if t.count(",") == 1 else t.replace(",", "")
    elif "." in t and re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_eur(value: Any, *, key: str = "", unit: str = "auto") -> Decimal | None:
    """Money from JSON/text -> euros (absolute value, 2 decimals).

    ``unit``: ``cents`` / ``euros`` force the interpretation of plain numbers; ``auto`` uses the
    heuristic: a key mentioning "cent" means cents; a *whole* number >= 100 without "eur" in the key
    is cents (540 -> 5.40); floats and strings with a decimal separator or a currency sign are euros.
    """
    nk = _norm(key)
    amount: Decimal | None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            nk2 = _norm(k)
            if "cent" in nk2:
                amt = parse_eur(v, key=k, unit="cents")
                if amt is not None:
                    return amt
        for k, v in value.items():
            if _norm(k) in {"amount", "value", "total", "bedrag", "price", "prijs", "fare"}:
                amt = parse_eur(v, key=key + k, unit=unit)
                if amt is not None:
                    return amt
        return None
    if isinstance(value, str):
        has_currency = "€" in value or "eur" in value.lower()
        amount = _money_from_str(value)
        if amount is None:
            return None
        is_plain_int = amount == amount.to_integral_value() and not re.search(r"[.,]\d", value)
        if is_plain_int and not has_currency:
            return parse_eur(int(amount), key=key, unit=unit)
        scale = Decimal(1)
    elif isinstance(value, int | float):
        amount = Decimal(str(value))
        if unit == "cents" or "cent" in nk:
            scale = Decimal(100)
        elif unit == "euros" or isinstance(value, float) or "eur" in nk:
            scale = Decimal(1)
        else:
            scale = Decimal(100) if abs(value) >= 100 else Decimal(1)
    else:
        return None
    return abs(amount / scale).quantize(Decimal("0.01"))


def mask_card(value: str) -> str | None:
    """``3528 0123 4567 8901`` -> ``3528********8901`` (already masked values are kept)."""
    s = re.sub(r"[\s-]", "", value or "")
    if len(s) < 8 or not re.fullmatch(r"[0-9*xX•]+", s):
        return None
    if re.search(r"[*xX•]", s):
        return s
    return s[:4] + "*" * (len(s) - 8) + s[-4:]


# =================================================================================================
# tolerant JSON mapper
# =================================================================================================

_IN_PREFIX = ("checkin", "checkedin", "departure", "vertrek", "starttime", "start", "begin")
_OUT_PREFIX = ("checkout", "checkuit", "checkedout", "arrival", "aankomst", "endtime", "end", "eind")
_FROM_NAMES = {
    "from", "origin", "van", "fromstation", "originstation", "vertrekstation", "vertrekplaats",
    "fromname", "originname", "departurestation",
}  # fmt: skip
_TO_NAMES = {
    "to", "destination", "naar", "tostation", "destinationstation", "bestemming", "aankomststation",
    "toname", "destinationname", "aankomstplaats", "arrivalstation",
}  # fmt: skip
_STATION_HINTS = ("station", "name", "naam", "location", "locatie", "plaats", "stop", "code", "uic")
_NAME_KEYS = (
    "name", "longname", "mediumname", "shortname", "naam", "stationname", "displayname", "label",
    "long", "medium", "short",
)  # fmt: skip
_CODE_KEYS = ("code", "stationcode", "uiccode", "uic")
_DATE_KEYS = {"date", "datum", "traveldate", "reisdatum", "day", "dag", "journeydate"}
_FARE_HINTS = ("fare", "price", "amount", "bedrag", "tarief", "prijs", "cost", "kosten", "charged", "afgeschreven")
_FARE_EXCLUDE = ("discount", "korting", "delay", "compensat", "refund", "vergoeding", "claim", "vat", "btw")
_DELAY_HINTS = (
    "delay", "vertraag", "vertraging", "compensat", "claimable", "eligible", "geldterug", "refund",
    "terugbetal", "restitutie",
)  # fmt: skip
_CLAIM_CONTEXT = ("claim", "geldterug", "compensat", "refund", "terugbetal", "restitut")
_REF_TOKENS = {"id", "ref", "reference", "url", "link", "href", "nummer", "number", "token", "uri"}
_PRODUCT_KEYS = {"product", "productname", "productnaam", "kortingsvorm", "tariff", "tarief", "producttype"}
_OPERATOR_KEYS = {"operator", "carrier", "vervoerder", "company", "maatschappij", "provider", "operatorname"}
_CLAIM_REF_KEYS = {
    "reference", "referentie", "referencenumber", "referentienummer", "claimid", "claimnumber",
    "claimreference", "aanvraagnummer", "aanvraagid", "kenmerk", "dossiernummer",
}  # fmt: skip
_STATUS_KEYS = {"status", "state", "claimstatus", "aanvraagstatus", "statusomschrijving", "statustext"}


def _is_station_key(nk: str) -> bool:
    return any(h in nk for h in _STATION_HINTS)


def _name_of(value: Any) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        return s if s and _stamp(s) is None else None
    if isinstance(value, dict):
        norm = {_norm(k): v for k, v in value.items()}
        for want in (*_NAME_KEYS, *_CODE_KEYS):
            v = norm.get(want)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _record_date(rec: dict[str, Any]) -> date | None:
    for k, v in rec.items():
        if _norm(k) in _DATE_KEYS:
            st = _stamp(v)
            if st and st[0] in ("date", "dt"):
                return st[1].date() if st[0] == "dt" else st[1]
    return None


def _point(
    rec: dict[str, Any], prefixes: tuple[str, ...], station_names: set[str], rec_date: date | None
) -> tuple[datetime | None, str | None]:
    """Resolve (timestamp, station name) of the check-in or check-out side of ``rec``."""
    full: list[datetime] = []
    dates: list[date] = []
    times: list[time] = []
    station: str | None = None

    def collect(value: Any) -> None:
        st = _stamp(value)
        if st is None:
            return
        {"dt": full, "date": dates, "time": times}[st[0]].append(st[1])

    for k, v in rec.items():
        nk = _norm(k)
        if not any(nk.startswith(p) for p in prefixes):
            continue
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if _is_station_key(_norm(k2)):
                    station = station or _name_of(v2)
                else:
                    collect(v2)
        elif _is_station_key(nk):
            station = station or _name_of(v)
        else:
            collect(v)
    if station is None:
        for k, v in rec.items():
            if _norm(k) in station_names:
                station = _name_of(v)
                if station:
                    break
    if full:
        return full[0], station
    day = dates[0] if dates else rec_date
    if day and times:
        return datetime.combine(day, times[0], tzinfo=AMS), station
    return None, station


def _truthy(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "no", "nee", "none", "null", "0", "n", "geen", "-"}
    if isinstance(value, list | tuple):
        return bool(value)
    if isinstance(value, dict):
        inner = [_truthy(v) for v in value.values()]
        return any(x for x in inner if x is not None) if value else False
    return None


def _delay_flag(rec: dict[str, Any]) -> bool | None:
    flags = [
        _truthy(v) for k, v in rec.items() if any(h in _norm(k) for h in _DELAY_HINTS) and _truthy(v) is not None
    ]
    if not flags:
        return None
    return any(flags)


def _claim_ref(rec: dict[str, Any]) -> str | None:
    for k, v in rec.items():
        toks = _tokens(k)
        joined = "".join(toks)
        if not any(c in joined for c in _CLAIM_CONTEXT):
            continue
        if not (set(toks) & _REF_TOKENS) or any(t in ("status", "state") for t in toks):
            continue
        if isinstance(v, dict):
            norm = {_norm(a): b for a, b in v.items()}
            for want in ("href", "url", "link", "id", "ref", "reference"):
                if isinstance(norm.get(want), str | int) and not isinstance(norm.get(want), bool):
                    v = norm[want]
                    break
        if isinstance(v, bool) or not isinstance(v, str | int):
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _find_fare(rec: dict[str, Any], unit: str) -> Decimal | None:
    for k, v in rec.items():
        nk = _norm(k)
        if any(h in nk for h in _FARE_HINTS) and not any(x in nk for x in _FARE_EXCLUDE):
            amt = parse_eur(v, key=str(k), unit=unit)
            if amt is not None:
                return amt
    return None


def _first_str(rec: dict[str, Any], keys: set[str]) -> str | None:
    for k, v in rec.items():
        if _norm(k) in keys:
            name = _name_of(v) if isinstance(v, dict) else (v.strip() if isinstance(v, str) else None)
            if name:
                return name
    return None


def _find_card(rec: dict[str, Any]) -> str | None:
    for k, v in rec.items():
        nk = _norm(k)
        if any(h in nk for h in ("card", "kaart", "mediumid", "pasnummer", "pasnr")) and isinstance(v, str | int):
            masked = mask_card(str(v))
            if masked:
                return masked
    return None


class _Mapping:
    """Settings for the record -> TripDTO conversion (also lets tests call the mapper directly)."""

    def __init__(
        self,
        *,
        default_card_id: str,
        fare_unit: str = "auto",
        card_from_data: bool = True,
        default_operator: str | None = None,
        strategy: str = "json",
        index: Any = None,
    ) -> None:
        self.default_card_id = default_card_id
        self.fare_unit = fare_unit
        self.card_from_data = card_from_data
        self.default_operator = default_operator or None
        self.strategy = strategy
        self.index = index if index is not None else get_station_index()

    def code(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.index.code_for(name) or name


def _trip_from_record(rec: dict[str, Any], m: _Mapping) -> TripDTO | None:
    rd = _record_date(rec)
    checkin, from_name = _point(rec, _IN_PREFIX, _FROM_NAMES, rd)
    checkout, to_name = _point(rec, _OUT_PREFIX, _TO_NAMES, rd)
    if checkin is None or not from_name:
        return None
    if checkout is not None and checkout < checkin:
        bumped = checkout + timedelta(days=1)
        checkout = bumped if bumped - checkin < timedelta(hours=12) else None
    fare = _find_fare(rec, m.fare_unit)
    flag = _delay_flag(rec)
    ref = _claim_ref(rec)
    if flag is None and ref:
        flag = True
    if to_name is None and fare is None and checkout is None and flag is None:
        return None  # a bare timestamp + station is not enough to call it a trip
    card = (_find_card(rec) if m.card_from_data else None) or m.default_card_id
    return TripDTO(
        source=Source.NS,
        card_id=card,
        checkin_at=checkin,
        checkout_at=checkout,
        from_station=m.code(from_name) or from_name,
        to_station=m.code(to_name),
        fare_eur=fare,
        product=_first_str(rec, _PRODUCT_KEYS),
        operator=_first_str(rec, _OPERATOR_KEYS) or m.default_operator,
        ns_delay_flag=flag,
        ns_claim_ref=ref,
        raw={"strategy": m.strategy, "from_name": from_name, "to_name": to_name, "record": rec},
    )


def _walk(node: Any, visit: Any, depth: int = 0) -> None:
    """Call ``visit(dict) -> bool`` on every dict; a True result stops the descent below it."""
    if depth > 14:
        return
    if isinstance(node, dict):
        if visit(node):
            return
        for v in node.values():
            _walk(v, visit, depth + 1)
    elif isinstance(node, list | tuple):
        for v in node:
            _walk(v, visit, depth + 1)


def map_trips(data: Any, mapping: _Mapping) -> list[TripDTO]:
    """Find trip-like records anywhere in ``data`` (any JSON shape) and convert them."""
    out: dict[str, TripDTO] = {}

    def visit(d: dict[str, Any]) -> bool:
        trip = _trip_from_record(d, mapping)
        if trip is None:
            return False
        out.setdefault(trip.key(), trip)
        return True

    _walk(data, visit)
    return list(out.values())


def _claim_from_record(rec: dict[str, Any], m: _Mapping) -> dict[str, Any] | None:
    status = _first_str(rec, _STATUS_KEYS)
    reference = None
    for k, v in rec.items():
        if _norm(k) in _CLAIM_REF_KEYS and isinstance(v, str | int) and not isinstance(v, bool) and str(v).strip():
            reference = str(v).strip()
            break
    if not status or not reference:
        return None
    rd = _record_date(rec)
    checkin, from_name = _point(rec, _IN_PREFIX, _FROM_NAMES, rd)
    _, to_name = _point(rec, _OUT_PREFIX, _TO_NAMES, rd)
    when: date | None = checkin.date() if checkin else rd
    if when is None:
        for k, v in rec.items():
            nk = _norm(k)
            if any(h in nk for h in ("date", "datum", "created", "ingediend", "submitted", "aangemaakt")):
                st = _stamp(v)
                if st and st[0] in ("dt", "date"):
                    when = st[1].date() if st[0] == "dt" else st[1]
                    break
    amount = _find_fare(rec, m.fare_unit)
    return {
        "date": when.isoformat() if when else None,
        "from": m.code(from_name),
        "to": m.code(to_name),
        "status": status,
        "reference": reference,
        "amount": str(amount) if amount is not None else None,
        "checkin_at": checkin.isoformat() if checkin else None,
        "raw": {"strategy": m.strategy, "from_name": from_name, "to_name": to_name, "record": rec},
    }


def map_claims(data: Any, mapping: _Mapping) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def visit(d: dict[str, Any]) -> bool:
        claim = _claim_from_record(d, mapping)
        if claim is None:
            return False
        out.setdefault(claim["reference"], claim)
        return True

    _walk(data, visit)
    return list(out.values())


# =================================================================================================
# locator helpers
# =================================================================================================


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _locators(scope: Page | Locator, spec: Any) -> list[Locator]:
    """Candidate locators for an element spec (``css`` -> ``label`` -> ``role``/``name`` -> ``text``)."""
    if not spec:
        return []
    if isinstance(spec, str):
        spec = {"css": spec}
    out: list[Locator] = []
    if spec.get("css"):
        out.append(scope.locator(spec["css"]).first)
    if spec.get("label"):
        out.append(scope.get_by_label(_rx(spec["label"])).first)
    if spec.get("role"):
        name = _rx(spec["name"]) if spec.get("name") else None
        out.append(scope.get_by_role(spec["role"], name=name).first)
    if spec.get("text"):
        out.append(scope.get_by_text(_rx(spec["text"])).first)
    return out


async def _find(scope: Page | Locator, spec: Any, *, timeout: float = 0.0) -> Locator | None:
    """First visible locator of ``spec``, polling up to ``timeout`` seconds."""
    cands = _locators(scope, spec)
    if not cands:
        return None
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        for loc in cands:
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except PlaywrightError:
                continue
        if loop.time() >= end:
            return None
        await asyncio.sleep(0.2)


async def _read_field(item: Locator, spec: str) -> str | None:
    """``"css"`` -> inner text, ``"css@attr"`` -> attribute value; ``None`` when absent/empty."""
    if not spec:
        return None
    attr = None
    m = re.search(r"@([\w:-]+)$", spec)
    if m:
        attr, spec = m[1], spec[: m.start()]
    loc = item.locator(spec).first
    try:
        if await loc.count() == 0:
            return None
        text = await loc.get_attribute(attr) if attr else await loc.inner_text()
    except PlaywrightError:
        return None
    text = (text or "").strip()
    return text or None


def _path_only(url: str) -> str:
    p = urlsplit(url)
    return f"{p.netloc}{p.path}"


# =================================================================================================
# the adapter
# =================================================================================================


class NsAdapter:
    """Trip source + status reader + fallback claim submitter for Mijn NS (channel ``mijnns``)."""

    source = Source.NS
    channel = "mijnns"

    def __init__(
        self,
        account: models.Account,
        *,
        runtime: BrowserRuntime | None = None,
        otp: OtpProvider | None = None,
        base_url: str = DEFAULT_BASE_URL,
        takeover_timeout: float | None = None,
    ) -> None:
        self.account = account
        self.base_url = base_url.rstrip("/")
        self._runtime = runtime
        self._otp = otp
        self._takeover_timeout = takeover_timeout
        self._cookie_hosts: set[str] = set()
        self.default_card_id = f"ns-{account.id}"

    # ---------------------------------------------------------------------------------------------
    # plumbing
    # ---------------------------------------------------------------------------------------------

    def _c(self, key: str, default: Any = None) -> Any:
        return _cfg(key, default)

    def _ms(self, key: str, default_s: float) -> float:
        return float(self._c(f"timeouts.{key}", default_s)) * 1000

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[BrowserSession]:
        rt = self._runtime or get_runtime()
        self._cookie_hosts = set()
        async with rt.session(self.account.id) as s:
            s.page.set_default_timeout(self._ms("action_s", 20))
            yield s

    def _abs(self, path_or_url: str) -> str:
        return urljoin(self.base_url + "/", path_or_url)

    def _url_matches(self, key: str, url: str) -> bool:
        pattern = self._c(key, "")
        if not pattern:
            return False
        return re.search(pattern, _clean_url(url)) is not None

    def _password(self) -> str:
        if not self.account.password_enc:
            raise LoginRequired("no password stored for this Mijn NS account")
        return crypto.decrypt(self.account.password_enc)

    def _mapping(self, strategy: str) -> _Mapping:
        return _Mapping(
            default_card_id=self.default_card_id,
            fare_unit=str(self._c("mapping.fare_unit", "auto")),
            card_from_data=bool(self._c("mapping.card_from_data", True)),
            default_operator=self._c("mapping.default_operator", "") or None,
            strategy=strategy,
        )

    async def _goto(self, page: Page, url: str) -> int | None:
        """Navigate; blocked status codes / WAF-style network errors raise ``BlockedByBotProtection``."""
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self._ms("nav_s", 30))
        except PlaywrightError as e:
            msg = str(e).splitlines()[0][:200]
            if _BLOCK_NET_ERRORS.search(msg):
                raise BlockedByBotProtection(f"connection dropped while opening {_path_only(url)}: {msg}") from e
            raise LayoutChanged(f"could not open {_path_only(url)}: {msg}") from e
        status = resp.status if resp is not None else None
        if status in (self._c("login.blocked.status_codes", [403, 429]) or []):
            raise BlockedByBotProtection(f"HTTP {status} for {_path_only(url)}")
        await self._maybe_cookies(page)
        return status

    async def _maybe_cookies(self, page: Page) -> None:
        host = urlsplit(page.url).netloc
        if not host or host in self._cookie_hosts:
            return
        self._cookie_hosts.add(host)
        with contextlib.suppress(PlaywrightError):
            await dismiss_cookie_banner(
                page,
                selectors=list(self._c("cookie.selectors", []) or []),
                timeout=float(self._c("cookie.timeout_ms", 3000)),
            )

    async def _type(self, loc: Locator, text: str) -> None:
        await human_type(
            loc,
            text,
            min_delay_ms=float(self._c("typing.min_delay_ms", 45)),
            max_delay_ms=float(self._c("typing.max_delay_ms", 140)),
        )

    async def _settle_network(self, s: BrowserSession) -> None:
        try:
            await s.recorder.wait_idle(
                r".",
                quiet_ms=int(self._c("nav.quiet_ms", 800)),
                timeout=float(self._c("nav.idle_timeout_s", 15)),
            )
        except TimeoutError:
            log.debug("network did not go quiet on %s", _path_only(s.page.url))

    # ---------------------------------------------------------------------------------------------
    # login
    # ---------------------------------------------------------------------------------------------

    async def ensure_logged_in(self) -> None:
        async with self._session() as s, s.debug_bundle("ns-login"):
            await self._ensure(s)

    async def _ensure(self, s: BrowserSession) -> None:
        """Log in (or confirm the persisted session); takeover when blocked or failing twice."""
        max_attempts = max(1, int(self._c("login.max_attempts", 2)))
        reason = ""
        for attempt in range(1, max_attempts + 1):
            try:
                await self._login_attempt(s)
                return
            except BlockedByBotProtection as e:
                reason = f"bot protection: {e}"
                break
            except (LayoutChanged, PlaywrightError, TimeoutError) as e:
                reason = f"login failed ({type(e).__name__}): {str(e).splitlines()[0][:200] if str(e) else ''}"
                log.warning("Mijn NS login attempt %d/%d failed: %s", attempt, max_attempts, reason)
                if attempt < max_attempts:
                    await jitter_sleep(1.0, 2.5)
        log.warning("Mijn NS login needs a human: %s", reason)
        timeout = self._takeover_timeout or float(self._c("login.takeover_timeout_s", 1800))
        await s.takeover(f"Mijn NS inloggen lukt niet automatisch ({reason})", timeout=timeout)
        # re-check after the human is done: the login may be complete, or the form may be usable now
        try:
            await self._login_attempt(s)
        except (LayoutChanged, PlaywrightError, TimeoutError) as e:
            raise BlockedByBotProtection(f"still not logged in after takeover: {reason}") from e

    async def _looks_blocked(self, page: Page) -> bool:
        for sel in self._c("login.blocked.selectors", []) or []:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return True
            except PlaywrightError:
                continue
        pattern = self._c("login.blocked.text_regex", "")
        if not pattern:
            return False
        try:
            text = await page.locator("body").inner_text(timeout=1500)
        except PlaywrightError:
            return False
        return re.search(pattern, text, re.IGNORECASE) is not None

    async def _detect_state(self, s: BrowserSession) -> str:
        """``blocked | login | otp | logged_in | no_form | other | navigating``"""
        page = s.page
        try:
            if await self._looks_blocked(page):
                return "blocked"
            url = page.url
            on_login = self._url_matches("login.login_url_regex", url)
            if await _find(page, self._c("login.email")):
                return "login"
            if (on_login or not self._url_matches("login.logged_in_url_regex", url)) and await _find(
                page, self._c("login.otp.input")
            ):
                if not await _find(page, self._c("login.password")):
                    return "otp"
            if on_login:
                return "no_form"
            if self._url_matches("login.logged_in_url_regex", url):
                text = self._c("login.logged_in_text", "")
                if text and not await _find(page, {"text": text}):
                    return "other"
                api = self._c("login.logged_in_api_regex", "")
                if api:
                    ok = s.recorder.find(api, status=200)
                    bad = s.recorder.find(api, status=401) + s.recorder.find(api, status=403)
                    if bad and not ok:
                        return "other"
                return "logged_in"
            return "other"
        except PlaywrightError:
            return "navigating"  # page changed under our feet; poll again

    async def _settle(self, s: BrowserSession) -> str:
        """Poll until the page is in a stable recognisable state (JS redirects need a moment)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self._c("login.settle_s", 15))
        prev, stable = "", 0
        while True:
            st = await self._detect_state(s)
            if st == "blocked":
                return st
            stable = stable + 1 if st == prev else 1
            prev = st
            if st in ("login", "otp", "logged_in") and stable >= 2:
                return st
            if loop.time() >= deadline:
                return st
            await asyncio.sleep(0.3)

    async def _login_attempt(self, s: BrowserSession) -> None:
        page = s.page
        await self._goto(page, self._abs(self._c("login.start_path", "/mijnns")))
        state = await self._settle(s)
        if state == "logged_in":
            return
        if state in ("blocked", "no_form"):
            raise BlockedByBotProtection(
                "challenge/block page" if state == "blocked" else "no login form on the login page"
            )
        if state not in ("login", "otp"):
            raise LayoutChanged(f"unexpected page after opening Mijn NS: {_path_only(page.url)} ({state})")
        await self._maybe_cookies(page)
        if state == "login":
            since = await self._submit_credentials(s)
            state = await self._after_login_submit(s, "login")
        else:
            since = datetime.now(UTC) - timedelta(seconds=5)
        if state == "otp":
            await self._submit_otp(s, since)
            state = await self._after_login_submit(s, "otp")
        if state != "logged_in":
            raise LayoutChanged(f"login did not finish (state {state})")

    async def _submit_credentials(self, s: BrowserSession) -> datetime:
        page = s.page
        email = await _find(page, self._c("login.email"), timeout=5)
        pw = await _find(page, self._c("login.password"), timeout=5)
        if email is None or pw is None:
            raise LayoutChanged("login form: email/password field not found")
        await self._type(email, self.account.username)
        await jitter_sleep(0.2, 0.6)
        await self._type(pw, self._password())
        remember = await _find(page, self._c("login.remember"))
        if remember is not None and not await remember.is_checked():
            await human_click(remember)
        submit = await _find(page, self._c("login.submit"), timeout=2)
        since = datetime.now(UTC) - timedelta(seconds=5)
        if submit is not None:
            await human_click(submit)
        else:
            await pw.press("Enter")
        return since

    async def _after_login_submit(self, s: BrowserSession, phase: str) -> str:
        """Wait for the result of a submit: ``logged_in``/``otp``; raise on rejection or block."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self._c("login.post_submit_s", 30))
        err_rx = self._c("login.error_text_regex" if phase == "login" else "login.otp.error_text_regex", "")
        stable, prev = 0, ""
        await asyncio.sleep(0.3)
        while True:
            st = await self._detect_state(s)
            stable = stable + 1 if st == prev else 1
            prev = st
            if st == "blocked":
                raise BlockedByBotProtection("block page after submitting the login")
            if st == "logged_in" and stable >= 2:
                return st
            if st == "otp" and phase == "login" and stable >= 2:
                return st
            if st in ("login", "otp") and err_rx and await self._error_visible(s.page, err_rx):
                raise LoginRequired(
                    "Mijn NS rejected the credentials" if phase == "login" else "Mijn NS rejected the verification code"
                )
            if loop.time() >= deadline:
                raise LoginRequired(f"no result after submitting the {phase} form (still {st})")
            await asyncio.sleep(0.3)

    async def _error_visible(self, page: Page, pattern: str) -> bool:
        try:
            text = await page.locator("body").inner_text(timeout=1500)
        except PlaywrightError:
            return False
        return re.search(pattern, text, re.IGNORECASE) is not None

    async def _submit_otp(self, s: BrowserSession, since: datetime) -> None:
        provider = self._otp or get_otp_provider(self.account)
        timeout = float(self._c("login.otp.timeout_s", 180))
        try:
            code = await provider.get_code(self.account, since, timeout)
        except OtpTimeout as e:
            raise LoginRequired("no verification code received in time") from e
        page = s.page
        box = await _find(page, self._c("login.otp.input"), timeout=5)
        if box is None:
            raise LayoutChanged("verification code field vanished")
        await self._type(box, code)
        submit = await _find(page, self._c("login.otp.submit"), timeout=2)
        if submit is not None:
            await human_click(submit)
        else:
            await box.press("Enter")

    # ---------------------------------------------------------------------------------------------
    # navigation + capture
    # ---------------------------------------------------------------------------------------------

    async def _open_section(self, s: BrowserSession, which: str) -> None:
        """Open "travel_history" / "claims": direct URL, else the first matching link by name."""
        page = s.page
        s.recorder.clear()
        for path in self._c(f"nav.{which}_urls", []) or []:
            status = await self._goto(page, self._abs(path))
            if status is None or status < 400:
                await self._settle_network(s)
                return
        names: list[str] = list(self._c(f"nav.{which}_links", []) or [])
        if not self._url_matches("login.logged_in_url_regex", page.url):
            await self._goto(page, self._abs(self._c("login.start_path", "/mijnns")))
        for attempt in (0, 1):
            for name in names:
                for role in ("link", "button", "menuitem", "tab"):
                    loc = page.get_by_role(role, name=_rx(name)).first  # type: ignore[arg-type]
                    try:
                        if await loc.count() and await loc.is_visible():
                            await human_click(loc)
                            await page.wait_for_load_state("domcontentloaded")
                            await self._maybe_cookies(page)
                            await self._settle_network(s)
                            return
                    except PlaywrightError:
                        continue
            if attempt == 0:
                menu = await _find(page, self._c("nav.menu_button"))
                if menu is None:
                    break
                await human_click(menu)
                await asyncio.sleep(0.5)
        raise LayoutChanged(f"no link found for {which.replace('_', ' ')} (tried {names})")

    async def _load_more(self, s: BrowserSession) -> None:
        for _ in range(int(self._c("nav.max_load_more", 5))):
            btn = await _find(s.page, self._c("nav.load_more"))
            if btn is None:
                return
            await human_click(btn)
            await self._settle_network(s)

    async def _api_json(self, s: BrowserSession, template: str, since: date) -> Any | None:
        today = datetime.now(AMS).date()
        url = (
            template.replace("{since}", since.isoformat())
            .replace("{today}", today.isoformat())
            .replace("{since_ms}", str(int(datetime.combine(since, time(), AMS).timestamp() * 1000)))
        )
        try:
            resp = await s.context.request.get(
                self._abs(url), headers={"Accept": "application/json"}, timeout=self._ms("api_s", 30)
            )
        except PlaywrightError as e:
            log.warning("API replay failed for %s: %s", _path_only(url), str(e).splitlines()[0][:160])
            return None
        if resp.status != 200:
            log.info("API replay %s -> HTTP %s, falling back to the UI", _path_only(url), resp.status)
            return None
        try:
            return await resp.json()
        except (ValueError, PlaywrightError):
            log.info("API replay %s did not return JSON", _path_only(url))
            return None

    def _recorded_json(self, s: BrowserSession) -> Iterable[tuple[str, Any]]:
        ignore = self._c("api.ignore_url_regex", "")
        only = self._c("api.json_url_regex", "")
        for e in list(s.recorder.entries):
            if e.status != 200 or e.truncated:
                continue
            if ignore and re.search(ignore, e.url, re.IGNORECASE):
                continue
            if only and not re.search(only, e.url, re.IGNORECASE):
                continue
            try:
                yield e.url, e.json()
            except ValueError:
                continue

    # ---------------------------------------------------------------------------------------------
    # list_trips
    # ---------------------------------------------------------------------------------------------

    async def list_trips(self, since: date) -> list[TripDTO]:
        async with self._session() as s, s.debug_bundle("ns-list-trips") as b:
            await self._ensure(s)
            trips, parsed = await self._collect_trips(s, since, b)
            if not parsed:
                if not await self._empty_state(s.page):
                    b.keep("no trips could be parsed")
                    raise LayoutChanged("no trips could be parsed from the travel history (see debug bundle)")
                log.info("Mijn NS travel history is empty")
            return [t for t in trips if t.checkin_at.astimezone(AMS).date() >= since]

    async def _empty_state(self, page: Page) -> bool:
        pattern = self._c("nav.empty_text_regex", "")
        return bool(pattern) and await self._error_visible(page, pattern)

    async def _collect_trips(self, s: BrowserSession, since: date, b: Bundle) -> tuple[list[TripDTO], int]:
        """Run the ladder; returns (trips, number parsed before the ``since`` filter)."""
        template = self._c("api.travel_history", "")
        if template:  # strategy 1: API replay
            data = await self._api_json(s, template, since)
            if data is not None:
                trips = map_trips(data, self._mapping("api"))
                if trips:
                    return trips, len(trips)
                log.warning("api.travel_history returned JSON but no trips could be mapped")
        # strategy 2: navigate + recorder JSON
        await self._open_section(s, "travel_history")
        await self._load_more(s)
        trips = self._trips_from_recorder(s)
        if trips:
            return trips, len(trips)
        # strategy 3: DOM
        trips = await self._trips_from_dom(s.page)
        if not trips:
            b.note("recorded_json_urls", [_path_only(e.url) for e in s.recorder.entries])
        return trips, len(trips)

    def _trips_from_recorder(self, s: BrowserSession) -> list[TripDTO]:
        found: dict[str, TripDTO] = {}
        mapping = self._mapping("recorder")
        for url, data in self._recorded_json(s):
            trips = map_trips(data, mapping)
            if trips:
                log.info("trips found in recorded JSON %s (%d)", _path_only(url), len(trips))
                for t in trips:
                    found.setdefault(t.key(), t)
            else:
                log.debug("recorded JSON %s has no recognisable trips", _path_only(url))
        return list(found.values())

    async def _trips_from_dom(self, page: Page) -> list[TripDTO]:
        item_css = self._c("dom.item", "")
        if not item_css:
            return []
        fields: dict[str, str] = self._c("dom.fields", {}) or {}
        rows = page.locator(item_css)
        count = await rows.count()
        mapping = self._mapping("dom")
        found: dict[str, TripDTO] = {}
        for i in range(count):
            item = rows.nth(i)
            rec: dict[str, Any] = {}
            keymap = {
                "date": "date", "checkin": "checkInTime", "checkin_time": "checkInClock",
                "checkout": "checkOutTime", "checkout_time": "checkOutClock", "from": "from", "to": "to",
                "fare": "fare", "product": "product", "operator": "operator",
            }  # fmt: skip
            for name, key in keymap.items():
                value = await _read_field(item, fields.get(name, ""))
                if value:
                    rec[key] = value
            if fields.get("delay"):
                rec["delayed"] = await item.locator(fields["delay"]).count() > 0
            ref = await _read_field(item, fields.get("claim_link", ""))
            if ref:
                rec["claimUrl"] = ref
            trip = _trip_from_record(rec, mapping)
            if trip is not None:
                found.setdefault(trip.key(), trip)
        return list(found.values())

    # ---------------------------------------------------------------------------------------------
    # list_submitted_claims
    # ---------------------------------------------------------------------------------------------

    async def list_submitted_claims(self, since: date) -> list[dict]:
        """``[{date, from, to, status, reference, amount, checkin_at, raw}]`` from "Mijn aanvragen".

        ``date`` is the trip date (ISO ``YYYY-MM-DD``), ``from``/``to`` are station codes (or the raw
        name when unknown), ``amount`` a decimal string in euros.
        """
        async with self._session() as s, s.debug_bundle("ns-list-claims") as b:
            await self._ensure(s)
            claims = await self._collect_claims(s, since)
            if not claims and not await self._empty_state(s.page):
                b.keep("no claims could be parsed")
                log.warning("no claims could be parsed from the claims overview (see debug bundle)")
            out = []
            for c in claims:
                if c["date"] and date.fromisoformat(c["date"]) < since:
                    continue
                out.append(c)
            return out

    async def _collect_claims(self, s: BrowserSession, since: date) -> list[dict[str, Any]]:
        mapping = self._mapping("api")
        template = self._c("api.claims", "")
        if template:
            data = await self._api_json(s, template, since)
            if data is not None:
                claims = map_claims(data, mapping)
                if claims:
                    return claims
        await self._open_section(s, "claims")
        await self._load_more(s)
        mapping = self._mapping("recorder")
        found: dict[str, dict[str, Any]] = {}
        for url, data in self._recorded_json(s):
            for c in map_claims(data, mapping):
                log.info("claims found in recorded JSON %s", _path_only(url))
                found.setdefault(c["reference"], c)
        if found:
            return list(found.values())
        return await self._claims_from_dom(s.page)

    async def _claims_from_dom(self, page: Page) -> list[dict[str, Any]]:
        item_css = self._c("claims_dom.item", "")
        if not item_css:
            return []
        fields: dict[str, str] = self._c("claims_dom.fields", {}) or {}
        rows = page.locator(item_css)
        mapping = self._mapping("dom")
        out: list[dict[str, Any]] = []
        for i in range(await rows.count()):
            item = rows.nth(i)
            vals = {n: await _read_field(item, fields.get(n, "")) for n in ("date", "from", "to", "status", "reference", "amount")}
            if not vals["status"] and not vals["reference"]:
                continue
            st = _stamp(vals["date"]) if vals["date"] else None
            when = (st[1].date() if st[0] == "dt" else st[1]) if st and st[0] in ("dt", "date") else None
            amount = parse_eur(vals["amount"]) if vals["amount"] else None
            out.append(
                {
                    "date": when.isoformat() if when else None,
                    "from": mapping.code(vals["from"]),
                    "to": mapping.code(vals["to"]),
                    "status": vals["status"] or "",
                    "reference": vals["reference"],
                    "amount": str(amount) if amount is not None else None,
                    "checkin_at": None,
                    "raw": {"strategy": "dom", "from_name": vals["from"], "to_name": vals["to"], "record": vals},
                }
            )
        return out

    # ---------------------------------------------------------------------------------------------
    # submit_claim
    # ---------------------------------------------------------------------------------------------

    async def submit_claim(self, trip: TripDTO, who: ClaimantInfo, *, dry_run: bool) -> SubmitResult:
        iban = normalize_iban(who.iban)
        if not valid_iban(iban):
            raise InvalidInput("IBAN is not valid (checksum)")
        if not who.holder_name.strip():
            raise InvalidInput("account holder name is empty")
        async with self._session() as s, s.debug_bundle("ns-submit-claim") as b:
            await self._ensure(s)
            await self._open_claim_form(s, trip)
            return await self._run_claim_form(s, b, trip, who, iban, dry_run)

    def _claim_url(self, trip: TripDTO) -> str | None:
        ref = (trip.ns_claim_ref or "").strip()
        if not ref:
            return None
        if ref.startswith(("http://", "https://", "/")):
            url = ref
        else:
            tpl = self._c("claim.url_template", "")
            if not tpl or "{ref}" not in tpl:
                return None
            url = tpl.replace("{ref}", quote(ref, safe=""))
        full = self._abs(url)
        host = urlsplit(full).hostname or ""
        base_host = urlsplit(self.base_url).hostname or ""
        suffixes = [x.lstrip(".") for x in self._c("claim.allowed_host_suffixes", []) or []]
        if host != base_host and not any(host == x or host.endswith("." + x) for x in suffixes):
            raise LayoutChanged(f"claim URL points outside the allowed hosts: {host}")
        return full

    async def _open_claim_form(self, s: BrowserSession, trip: TripDTO) -> None:
        page = s.page
        url = self._claim_url(trip)
        if url:
            status = await self._goto(page, url)
            if status is not None and status >= 400:
                raise LayoutChanged(f"claim form URL returned HTTP {status}")
            if self._url_matches("login.login_url_regex", page.url):
                raise LoginRequired("redirected to the login page while opening the claim form")
        else:
            await self._open_claim_via_ui(s, trip)
        heading = self._c("claim.expect_heading", "")
        if heading:
            h = await _find(page, {"role": "heading", "name": heading}, timeout=float(self._c("claim.heading_timeout_s", 15)))
            if h is None:
                raise LayoutChanged(f"claim form heading {heading!r} not found on {_path_only(page.url)}")

    async def _open_claim_via_ui(self, s: BrowserSession, trip: TripDTO) -> None:
        """No usable ``ns_claim_ref``: find the trip in the travel history and click its claim button."""
        page = s.page
        item_css = self._c("dom.item", "")
        if not item_css:
            raise LayoutChanged("trip has no ns_claim_ref and dom.item is not configured to find it in the UI")
        await self._open_section(s, "travel_history")
        await self._load_more(s)
        local = trip.checkin_at.astimezone(AMS)
        rows = page.locator(item_css).filter(has_text=local.strftime("%H:%M"))
        names = [n for n in (trip.raw.get("from_name"), trip.from_station) if n]
        row = None
        for i in range(await rows.count()):
            cand = rows.nth(i)
            text = (await cand.inner_text()).lower()
            if any(str(n).lower() in text for n in names):
                row = cand
                break
        if row is None:
            raise LayoutChanged("trip not found in the Mijn NS travel history")
        btn = await _find(row, self._c("claim.trip_button")) or await _find(row, self._c("claim.trip_link"))
        if btn is None:
            raise LayoutChanged("no claim button on the trip row (not claimable?)")
        await human_click(btn)
        await page.wait_for_load_state("domcontentloaded")
        await self._maybe_cookies(page)
        await self._settle_network(s)

    async def _visible_error(self, page: Page) -> str | None:
        sel = self._c("claim.error_selector", "")
        pattern = self._c("claim.error_regex", "")
        if not sel or not pattern:
            return None
        loc = page.locator(sel)
        try:
            for i in range(min(await loc.count(), 10)):
                el = loc.nth(i)
                if await el.is_visible():
                    text = (await el.inner_text()).strip()
                    if text and re.search(pattern, text, re.IGNORECASE):
                        return redact(text)[:200]
        except PlaywrightError:
            return None
        return None

    async def _fill_text(self, page: Page, spec_key: str, value: str) -> None:
        """Type ``value`` into the field only when it is visible and empty (never overwrite NS data)."""
        loc = await _find(page, self._c(spec_key))
        if loc is None:
            return
        try:
            current = (await loc.input_value()).strip()
        except PlaywrightError:
            return
        if not current:
            await self._type(loc, value)
        else:
            log.info("claim form: %s already filled, leaving it", spec_key)

    async def _tick_checkboxes(self, page: Page, ticked: set[str]) -> None:
        for entry in self._c("claim.checkboxes", []) or []:
            label = entry["label"] if isinstance(entry, dict) else str(entry)
            if label in ticked:
                continue
            cb = page.get_by_role("checkbox", name=_rx(label)).first
            try:
                if not await cb.count() or not await cb.is_visible():
                    continue
                if not await cb.is_checked():
                    try:
                        await cb.check(timeout=3000)
                    except PlaywrightError:  # custom styled box: click its label text
                        await human_click(page.get_by_text(_rx(label)).first)
                if await cb.is_checked():
                    ticked.add(label)
            except PlaywrightError:
                continue

    async def _wait_any(self, page: Page, keys: list[str], timeout: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            for k in keys:
                if await _find(page, self._c(k)) is not None:
                    return
            if loop.time() >= end:
                return
            await asyncio.sleep(0.25)

    def _screenshot_path(self, trip: TripDTO, tag: str) -> Path:
        d = default_debug_dir() / "ns-claims"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{trip.key()[:8]}-{tag}.png"

    async def _run_claim_form(
        self, s: BrowserSession, b: Bundle, trip: TripDTO, who: ClaimantInfo, iban: str, dry_run: bool
    ) -> SubmitResult:
        page = s.page
        ticked: set[str] = set()
        step_timeout = float(self._c("claim.step_timeout_s", 8))
        for _ in range(int(self._c("claim.max_steps", 6))):
            await self._wait_any(
                page, ["claim.iban", "claim.holder", "claim.email", "claim.submit", "claim.next"], step_timeout
            )
            await self._fill_text(page, "claim.iban", iban)
            await self._fill_text(page, "claim.holder", who.holder_name.strip())
            if who.email:
                await self._fill_text(page, "claim.email", who.email.strip())
            await self._tick_checkboxes(page, ticked)
            err = await self._visible_error(page)
            if err:
                raise InvalidInput(f"Mijn NS rejected the form data: {err}")
            submit = await _find(page, self._c("claim.submit"))
            if submit is not None:
                break
            nxt = await _find(page, self._c("claim.next"))
            if nxt is None:
                raise LayoutChanged("claim form: neither a submit nor a next button is visible")
            await human_click(nxt)
            await self._settle_network(s)
            await asyncio.sleep(0.4)
        else:
            raise LayoutChanged("claim form: submit button not reached within claim.max_steps")
        required = [
            e if isinstance(e, str) else e["label"]
            for e in self._c("claim.checkboxes", []) or []
            if isinstance(e, str) or not e.get("optional")
        ]
        missing = [label for label in required if label not in ticked]
        if missing:
            raise LayoutChanged(f"claim form: required confirmation checkbox not found: {missing}")

        shot = self._screenshot_path(trip, "presubmit")
        await page.screenshot(path=str(shot), full_page=True)
        if dry_run:
            b.keep("dry_run: stopped before the final submit")
            return SubmitResult(
                ok=True,
                reference=None,
                screenshot=str(shot),
                message="dry run: form filled, stopped before the final submit",
                channel=self.channel,
                reached_final_submit=False,
                dry_run=True,
            )

        # point of no return: from here a failure must report reached_final_submit=True
        try:
            await human_click(submit)
            return await self._await_confirmation(s, b, trip, shot)
        except InvalidInput:
            raise
        except Exception as e:  # noqa: BLE001 - report instead of raising: the click already happened
            b.keep("error after the final click")
            log.error("Mijn NS claim: error after clicking submit: %s", type(e).__name__)
            return SubmitResult(
                ok=False,
                reference=None,
                screenshot=str(shot),
                message=f"clicked submit but could not confirm ({type(e).__name__}): {redact(str(e))[:200]}",
                channel=self.channel,
                reached_final_submit=True,
            )

    async def _await_confirmation(self, s: BrowserSession, b: Bundle, trip: TripDTO, pre_shot: Path) -> SubmitResult:
        page = s.page
        success = self._c("claim.success_regex", "")
        loop = asyncio.get_running_loop()
        end = loop.time() + float(self._c("claim.success_timeout_s", 30))
        text = ""
        while True:
            try:
                text = await page.locator("body").inner_text(timeout=2000)
            except PlaywrightError:
                text = ""
            if success and re.search(success, text, re.IGNORECASE):
                break
            err = await self._visible_error(page)
            if err:
                raise InvalidInput(f"Mijn NS rejected the claim: {err}")
            if loop.time() >= end:
                b.keep("no confirmation after submit")
                return SubmitResult(
                    ok=False,
                    reference=None,
                    screenshot=str(pre_shot),
                    message="clicked submit but no confirmation text appeared",
                    channel=self.channel,
                    reached_final_submit=True,
                )
            await asyncio.sleep(0.4)
        ref = None
        m = re.search(self._c("claim.reference_regex", ""), text, re.IGNORECASE) if self._c("claim.reference_regex") else None
        if m:
            ref = m.group(1).strip()
        shot = self._screenshot_path(trip, "confirmation")
        with contextlib.suppress(PlaywrightError):
            await page.screenshot(path=str(shot), full_page=True)
        b.keep("claim submitted")
        return SubmitResult(
            ok=True,
            reference=ref,
            screenshot=str(shot if shot.exists() else pre_shot),
            message="claim submitted via Mijn NS",
            channel=self.channel,
            reached_final_submit=True,
        )


def _clean_url(url: str) -> str:
    """``scheme://host/path`` (no query/fragment) so ``redirect_uri=.../mijnns`` cannot fool a match."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"
