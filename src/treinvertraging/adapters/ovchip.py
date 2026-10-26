"""OV-chipkaart adapter (WP-I): trips from ``www.ov-chipkaart.nl`` (TripSource only).

Flow
----
``ensure_logged_in``
    ``GET {base}/api/auth/session`` (NextAuth) through the browser context. A JSON object with a
    ``user`` means we are logged in. Otherwise ``/nl/mijn-ov-chip`` is opened, which sends us through
    NextAuth to the WSO2 login (``login.ov-chipkaart.nl/authenticationendpoint/login.do``). Username and
    password are typed with label based locators (id hints as fallback), the optional OTP step goes
    through the account's :class:`~treinvertraging.otp.base.OtpProvider`, and the Cookiebot banner is
    dismissed. A blocked page (HTTP 403/429, a near-empty shell, a challenge text) or a login that does
    not get anywhere asks for a human takeover and then re-checks once.

``list_trips``
    Select the card (last four digits of ``account.card_number_enc``), open "Reishistorie", pick the
    period and then walk a strategy ladder:

    0. API replay (``history.api.url``, only when configured),
    1. CSV export through ``page.expect_download()`` (preferred),
    2. JSON captured by the recorder while the history page loaded,
    3. the DOM table.

    Every rung produces :class:`TxRow` objects (one per transaction line, language neutral), which are
    paired into :class:`~treinvertraging.domain.TripDTO` by :func:`rows_to_trips`.

All site specific strings live in ``selectors/ovchip.yaml`` (see ``treinvertraging.selectors``), none of
them are hard-coded here. The parsing half of this module (CSV/JSON/table -> rows -> trips) is pure and
unit tested without a browser.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from treinvertraging import selectors
from treinvertraging.browser.human import dismiss_cookie_banner, human_click, human_type, jitter_sleep
from treinvertraging.browser.runtime import BrowserRuntime, BrowserSession, get_runtime
from treinvertraging.domain import Source, TripDTO
from treinvertraging.errors import (
    BlockedByBotProtection,
    InvalidInput,
    LayoutChanged,
    LoginRequired,
    TreinvertragingError,
)
from treinvertraging.otp.base import OtpProvider, OtpTimeout, get_otp_provider
from treinvertraging.stations import StationIndex, get_station_index, normalize

log = logging.getLogger(__name__)

SITE = "ovchip"
AMS = ZoneInfo("Europe/Amsterdam")
DEFAULT_BASE_URL = "https://www.ov-chipkaart.nl"


class OvchipNavigationError(TreinvertragingError):
    """The site could not be reached at all (DNS, refused connection, ...), as opposed to being blocked."""


# =============================================================================================
# selector helpers
# =============================================================================================


def _sel(key: str, default: Any = None) -> Any:
    return selectors.get(SITE, key, default)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _norm_key(text: str) -> str:
    """ "Check-in" -> "checkin", "Kaartnummer " -> "kaartnummer" (lower-case letters/digits only)."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _column_aliases(section: str) -> dict[str, list[str]]:
    cols = _sel(f"{section}.columns", {}) or {}
    return {canon: [_norm_key(a) for a in names] for canon, names in cols.items()}


# =============================================================================================
# parsing: values
# =============================================================================================

_MONTHS_NL = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2, "mrt": 3, "maart": 3, "apr": 4, "april": 4,
    "mei": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7, "aug": 8, "augustus": 8, "sep": 9, "sept": 9,
    "september": 9, "okt": 10, "oktober": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_RE_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_RE_DMY = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)")
_RE_D_MON_Y = re.compile(r"(?<!\d)(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})(?!\d)")
_RE_TIME = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?::(\d{2}))?(?!\d)")


def parse_date(text: str | None) -> date | None:
    """dd-mm-yyyy (``/``, ``.``, 2-digit year too), yyyy-mm-dd or "6 okt 2026"; weekday prefix ignored."""
    if not text:
        return None
    text = str(text)
    if m := _RE_ISO_DATE.search(text):
        y, mo, d = (int(g) for g in m.groups())
    elif m := _RE_DMY.search(text):
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
    elif (m := _RE_D_MON_Y.search(text)) and m[2].casefold() in _MONTHS_NL:
        d, mo, y = int(m[1]), _MONTHS_NL[m[2].casefold()], int(m[3])
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_time(text: str | None) -> time | None:
    """HH:MM, H.MM or HH:MM:SS. A date inside the text is ignored."""
    if not text:
        return None
    cleaned = _RE_ISO_DATE.sub(" ", str(text))
    cleaned = _RE_DMY.sub(" ", cleaned)
    m = _RE_TIME.search(cleaned)
    if not m:
        return None
    h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    return time(h, mi, s)


def _parse_iso_datetime(text: str) -> datetime | None:
    if "T" not in text or not _RE_ISO_DATE.search(text):
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(AMS) if dt.tzinfo else dt.replace(tzinfo=AMS)


def parse_amount(text: str | None) -> Decimal | None:
    """ "€ 3,45", "3,45", "-3,45", "1.234,56", "3.45" -> Decimal (sign kept). Empty or unparsable -> None."""
    if text is None:
        return None
    s = str(text).replace("\xa0", " ").replace("€", "").replace("EUR", "").replace("eur", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(" ", "")
    if s.endswith("-"):  # "3,45-"
        neg, s = True, s[:-1]
    if not s or not re.search(r"\d", s):
        return None
    s = s.replace("+", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if neg else value


def parse_datetime(date_text: str | None, time_text: str | None) -> datetime | None:
    """Combine a date column and a time column (or a full timestamp in either) into an Amsterdam datetime."""
    for candidate in (time_text, date_text):
        if candidate and (dt := _parse_iso_datetime(str(candidate))):
            return dt
    d = parse_date(date_text) or parse_date(time_text)
    t = parse_time(time_text)
    if d is None or t is None:
        return None
    return datetime.combine(d, t, tzinfo=AMS)


def decode_csv_bytes(data: bytes) -> str:
    """UTF-8 (with or without BOM), falling back to Windows-1252 which older exports use."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# =============================================================================================
# parsing: rows
# =============================================================================================


@dataclass
class TxRow:
    """One transaction line of the export, independent of the source (CSV, JSON or DOM)."""

    index: int
    checkin_dt: datetime | None = None
    checkout_dt: datetime | None = None
    from_name: str = ""
    to_name: str = ""
    amount: Decimal | None = None
    transaction: str = ""
    klass: str = ""
    product: str = ""
    remarks: str = ""
    name: str = ""
    card_last4: str | None = None
    operator_col: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(x for x in (self.transaction, self.product, self.remarks, self.operator_col) if x)


def _last4(text: str | None) -> str | None:
    digits = re.sub(r"\D", "", text or "")
    return digits[-4:] if len(digits) >= 4 else None


def _row_from_mapping(index: int, m: dict[str, str], raw: dict[str, Any]) -> TxRow:
    date_text = m.get("date", "")
    checkin = parse_datetime(date_text, m.get("checkin_time"))
    checkout = parse_datetime(date_text, m.get("checkout_time"))
    if checkin and checkout and checkout < checkin:
        # Same export row, check-out earlier on the clock: the trip crossed midnight.
        checkout += timedelta(days=1)
    return TxRow(
        index=index,
        checkin_dt=checkin,
        checkout_dt=checkout,
        from_name=(m.get("from") or "").strip(),
        to_name=(m.get("to") or "").strip(),
        amount=parse_amount(m.get("amount")),
        transaction=(m.get("transaction") or "").strip(),
        klass=(m.get("class") or "").strip(),
        product=(m.get("product") or "").strip(),
        remarks=(m.get("remarks") or "").strip(),
        name=(m.get("name") or "").strip(),
        card_last4=_last4(m.get("card")),
        operator_col=(m.get("operator") or "").strip(),
        raw=raw,
    )


def _map_header(header: Sequence[str], aliases: dict[str, list[str]]) -> dict[str, int]:
    """canonical field -> column index (first matching column wins)."""
    found: dict[str, int] = {}
    for idx, h in enumerate(header):
        key = _norm_key(h)
        if not key:
            continue
        for canon, names in aliases.items():
            if canon not in found and key in names:
                found[canon] = idx
                break
    return found


def _is_header(mapping: dict[str, int]) -> bool:
    return len(mapping) >= 3 and "date" in mapping and bool({"from", "to", "checkin_time"} & set(mapping))


def rows_from_table(table: Sequence[Sequence[str]]) -> list[TxRow]:
    """Header row + data rows (CSV or DOM table) -> rows. Leading junk lines before the header are skipped.

    Raises :class:`LayoutChanged` when no row looks like a header.
    """
    aliases = _column_aliases("csv")
    header_at, mapping = None, {}
    for i, line in enumerate(table):
        mapping = _map_header(line, aliases)
        if _is_header(mapping):
            header_at = i
            break
    if header_at is None:
        raise LayoutChanged("export header not recognised (expected columns like Datum;Check-in;Vertrek;...)")
    header = [str(h).strip() for h in table[header_at]]
    rows: list[TxRow] = []
    for line in table[header_at + 1 :]:
        if not any(str(c).strip() for c in line):
            continue
        cells = [str(c).strip() for c in line]
        cells += [""] * (len(header) - len(cells))
        values = {canon: cells[idx] for canon, idx in mapping.items() if idx < len(cells)}
        raw = {h: cells[i] for i, h in enumerate(header) if h}
        rows.append(_row_from_mapping(len(rows), values, raw))
    return rows


def parse_csv(data: bytes | str) -> list[TxRow]:
    """Parse the Dutch CSV export (``;`` separated, BOM tolerant, delimiter sniffed)."""
    text = decode_csv_bytes(data) if isinstance(data, bytes) else data.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:5])
    delim = max(";\t,", key=lambda d: sample.count(d)) if any(d in sample for d in ";\t,") else ";"
    table = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    return rows_from_table(table)


def _flatten(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}{k}.")
    elif isinstance(obj, list) and obj and all(not isinstance(x, (dict, list)) for x in obj):
        yield prefix.rstrip("."), ", ".join(str(x) for x in obj)
    else:
        yield prefix.rstrip("."), obj


def _json_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)) and value > 10**11:  # epoch milliseconds
        return datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(AMS).isoformat()
    return str(value)


def _find_record_lists(data: Any) -> Iterator[list[dict]]:
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            yield data
        for x in data:
            yield from _find_record_lists(x)
    elif isinstance(data, dict):
        for v in data.values():
            yield from _find_record_lists(v)


def rows_from_json(data: Any) -> list[TxRow]:
    """Tolerant mapper for JSON transaction lists; returns ``[]`` when nothing looks like a trip list.

    Nested keys are flattened and matched against ``json.columns`` (normalised key equals or ends with an
    alias). Unknown shapes are logged by the caller and the original record is kept in ``TxRow.raw``.
    """
    aliases = _column_aliases("json")
    best: list[TxRow] = []
    for records in _find_record_lists(data):
        rows: list[TxRow] = []
        for rec in records:
            flat = {_norm_key(k): v for k, v in _flatten(rec)}
            values: dict[str, str] = {}
            for canon, names in aliases.items():
                for alias in names:
                    key = next((k for k in flat if k == alias), None) or next(
                        (k for k in flat if k.endswith(alias)), None
                    )
                    if key is not None and _json_scalar(flat[key]) != "":
                        values[canon] = _json_scalar(flat[key])
                        break
            if not ({"checkin_time", "checkout_time", "date"} & set(values)):
                continue
            rows.append(_row_from_mapping(len(rows), values, rec))
        if len(rows) > len(best):
            best = rows
    return best


# =============================================================================================
# parsing: pairing check-in / check-out into trips
# =============================================================================================


def _tx_cfg(name: str, default: str) -> re.Pattern[str]:
    return _rx(_sel(f"csv.transaction.{name}", default))


def classify(row: TxRow) -> str:
    """ "full" (check-in and check-out in one row), "checkin", "checkout" or "other" (top-ups, ...)."""
    has_in = bool(row.checkin_dt and row.from_name)
    has_out = bool(row.checkout_dt and row.to_name)
    if has_in and has_out:
        return "full"
    if has_in:
        return "checkin"
    if has_out:
        return "checkout"
    # Single-station layouts: the check-out row carries its station in the "Vertrek" column.
    if row.checkout_dt and row.from_name and _tx_cfg("checkout_regex", "check-?uit").search(row.transaction):
        row.to_name, row.from_name = row.from_name, ""
        return "checkout"
    return "other"


@dataclass
class PairedTrip:
    checkin: TxRow
    checkout: TxRow | None  # None: missing check-out
    full_row: bool  # both halves came from a single export row


def pair_rows(rows: Sequence[TxRow], *, max_trip: timedelta | None = None) -> list[PairedTrip]:
    """Pair check-in and check-out transactions of the same card into trips.

    * Rows that already hold both halves (``Check-in``/``Vertrek`` and ``Check-uit``/``Bestemming``) are
      trips by themselves.
    * Otherwise a check-in is held until the next check-out of the same card within ``max_trip``; a
      check-in followed by another check-in (or by nothing, or by a too distant check-out) is a trip
      with a missing check-out.
    * A check-out without check-in ("incheck vergeten") cannot be claimed and is dropped.
    * Top-ups and other non-travel lines (no stations) are ignored.
    """
    if max_trip is None:
        max_trip = timedelta(hours=float(_sel("csv.max_trip_hours", 14)))
    by_card: dict[str, list[tuple[datetime, int, str, TxRow]]] = {}
    for row in rows:
        kind = classify(row)
        if kind == "other":
            continue
        dt = row.checkout_dt if kind == "checkout" else row.checkin_dt
        assert dt is not None
        # On equal timestamps the check-out sorts first so that "check-uit 09:00" + "check-in 09:00" at
        # a transfer pairs the check-out with the previous check-in.
        by_card.setdefault(row.card_last4 or "", []).append((dt, 0 if kind == "checkout" else 1, kind, row))

    trips: list[PairedTrip] = []
    for items in by_card.values():
        items.sort(key=lambda t: (t[0], t[1], t[3].index))
        pending: TxRow | None = None
        for dt, _order, kind, row in items:
            if kind == "full":
                if pending is not None:
                    trips.append(PairedTrip(pending, None, False))
                    pending = None
                trips.append(PairedTrip(row, row, True))
            elif kind == "checkin":
                if pending is not None:
                    trips.append(PairedTrip(pending, None, False))
                pending = row
            else:  # checkout
                if (
                    pending is not None
                    and pending.checkin_dt is not None
                    and timedelta(0) <= dt - pending.checkin_dt <= max_trip
                ):
                    trips.append(PairedTrip(pending, row, False))
                    pending = None
                else:
                    if pending is not None:
                        trips.append(PairedTrip(pending, None, False))
                        pending = None
                    log.info("ovchip: check-out without matching check-in at %s dropped", dt.isoformat())
        if pending is not None:
            trips.append(PairedTrip(pending, None, False))
    return sorted(trips, key=lambda p: p.checkin.checkin_dt or datetime.min.replace(tzinfo=AMS))


def infer_operator(*rows: TxRow) -> str | None:
    """Operator from an explicit column, else from transaction/product/remarks text (``None`` = unknown)."""
    for row in rows:
        if row.operator_col:
            return _canonical_operator(row.operator_col)
    text = " ".join(r.text for r in rows)
    for name, pattern in (_sel("csv.operators", {}) or {}).items():
        if _rx(pattern).search(text):
            return str(name)
    return None


def _canonical_operator(text: str) -> str:
    for name, pattern in (_sel("csv.operators", {}) or {}).items():
        if _rx(pattern).search(text):
            return str(name)
    return text.strip()


def _resolve_station(idx: StationIndex, name: str | None, *, strict: bool) -> str | None:
    """Station code for a name. ``strict`` (non-NS operators, bus-stop style "Plaats, Halte" names):
    only accept a match on an official station name, never a prefix or fuzzy match."""
    if not name:
        return None
    code = idx.code_for(name)
    if code is None:
        return None
    if strict or "," in name:
        st = idx.get(code)
        official = {normalize(n) for n in (st.name_short, st.name_medium, st.name_long) if n} if st else set()
        if normalize(name) not in official:
            return None
    return code


def mask_card(last4: str | None, fallback: str) -> str:
    return f"**** **** **** {last4}" if last4 else fallback


def _jsonable_row(row: TxRow) -> dict[str, Any]:
    return {
        k: v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)
        for k, v in row.raw.items()
    }


def rows_to_trips(
    rows: Sequence[TxRow],
    *,
    stations: StationIndex | None = None,
    account_last4: str | None = None,
    fallback_card_id: str = "ovchip",
    since: date | None = None,
    source_format: str = "csv",
) -> list[TripDTO]:
    """Pair ``rows`` into trips and convert them to :class:`TripDTO`.

    ``account_last4``: when set, rows of other cards in the same export are dropped. Station names are
    mapped to codes through the station index (the name as shown on the site stays in ``raw``); an
    unresolvable name is kept verbatim as ``from_station`` so nothing is silently lost.
    """
    idx = stations or get_station_index()
    if account_last4:
        kept = [r for r in rows if r.card_last4 in (None, account_last4)]
        if len(kept) != len(rows):
            log.info("ovchip: dropped %d rows of other cards", len(rows) - len(kept))
        rows = kept
    out: dict[str, TripDTO] = {}
    for p in pair_rows(rows):
        ci, co = p.checkin, p.checkout
        assert ci.checkin_dt is not None
        if since is not None and ci.checkin_dt.date() < since:
            continue
        from_name = ci.from_name
        to_name = (co.to_name if co else "") or None
        card = account_last4 or ci.card_last4
        operator = infer_operator(ci, co) if co else infer_operator(ci)
        amount = co.amount if co is not None else None
        raw = {
            "format": source_format,
            "from_name": from_name,
            "to_name": to_name,
            "card_last4": card,
            "incomplete": co is None,
            "transactions": [
                t
                for t in ((ci.transaction, co.transaction) if co and co is not ci else (ci.transaction,))
                if t
            ],
            "class": (co or ci).klass or None,
            "rows": [_jsonable_row(ci)] if co is None or co is ci else [_jsonable_row(ci), _jsonable_row(co)],
        }
        strict = operator not in (None, "NS")
        from_code = _resolve_station(idx, from_name, strict=strict)
        to_code = _resolve_station(idx, to_name, strict=strict) if to_name else None
        if from_code is None:
            raw["from_station_unresolved"] = True
        if to_name and to_code is None:
            raw["to_station_unresolved"] = True
        trip = TripDTO(
            source=Source.OVCHIP,
            card_id=mask_card(card, fallback_card_id),
            checkin_at=ci.checkin_dt,
            checkout_at=co.checkout_dt if co else None,
            from_station=from_code or from_name,
            to_station=(to_code or to_name) if to_name else None,
            fare_eur=abs(amount) if amount is not None else None,
            product=((co.product if co else "") or ci.product) or None,
            operator=operator,
            ns_delay_flag=None,
            ns_claim_ref=None,
            raw=raw,
        )
        out.setdefault(trip.key(), trip)
    return sorted(out.values(), key=lambda t: t.checkin_at)


def trips_from_csv(
    data: bytes | str,
    *,
    stations: StationIndex | None = None,
    account_last4: str | None = None,
    since: date | None = None,
    fallback_card_id: str = "ovchip",
) -> list[TripDTO]:
    """Convenience: parse a CSV export straight to trips."""
    return rows_to_trips(
        parse_csv(data),
        stations=stations,
        account_last4=account_last4,
        since=since,
        fallback_card_id=fallback_card_id,
    )


# =============================================================================================
# playwright helpers
# =============================================================================================


async def _visible(loc: Locator) -> bool:
    try:
        return await loc.first.is_visible()
    except PlaywrightError:
        return False


async def _first_visible(
    cands: Sequence[Locator], timeout_ms: float = 0, poll_ms: float = 150
) -> Locator | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while True:
        for loc in cands:
            if await _visible(loc):
                return loc.first
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(poll_ms / 1000)


def _field_locators(page: Page, spec: dict[str, Any]) -> list[Locator]:
    """Label first, then placeholder, then id hints (see selectors ``login.username`` etc.)."""
    fields = page.locator("input, textarea, select")
    out: list[Locator] = [page.get_by_label(_rx(x)).and_(fields) for x in spec.get("labels", [])]
    out += [page.get_by_placeholder(_rx(x)) for x in spec.get("placeholders", [])]
    out += [page.locator(css) for css in spec.get("id_hints", [])]
    return out


def _named_locators(
    page: Page, names: Sequence[str], roles: Sequence[str] = ("button", "link")
) -> list[Locator]:
    return [page.get_by_role(role, name=_rx(n)) for n in names for role in roles]  # type: ignore[arg-type]


@dataclass
class Credentials:
    username: str
    password: str


def _read_secret(account: Any, plain_attr: str, enc_attr: str) -> str | None:
    value = getattr(account, plain_attr, None)
    if value:
        return str(value)
    token = getattr(account, enc_attr, None)
    if token:
        from treinvertraging.crypto import decrypt

        return decrypt(token)
    return None


# =============================================================================================
# the adapter
# =============================================================================================


class OvchipAdapter:
    """``TripSource`` for ov-chipkaart.nl. One browser session per public call."""

    source = Source.OVCHIP

    def __init__(
        self,
        account: Any,
        *,
        runtime: BrowserRuntime | None = None,
        otp: OtpProvider | None = None,
        base_url: str = DEFAULT_BASE_URL,
        stations: StationIndex | None = None,
    ) -> None:
        self.account = account
        self.base_url = base_url.rstrip("/")
        self._runtime = runtime
        self._otp = otp
        self._stations = stations
        self._account_key = str(getattr(account, "id", account))
        self.cookie_timeout_ms: float = 3000.0  # lowered once a banner was clicked away
        self._login_submitted_at: datetime | None = None
        t = _sel("timing", {}) or {}
        self.type_delay_ms: tuple[float, float] = tuple(t.get("type_delay_ms", (45, 140)))  # type: ignore[assignment]
        self.pause_s: tuple[float, float] = tuple(t.get("pause_s", (0.4, 1.2)))  # type: ignore[assignment]
        self.settle_timeout_s: float = float(t.get("settle_timeout_s", 25))
        self.block_check_after_s: float = float(t.get("block_check_after_s", 3))
        self.nav_timeout_ms: float = float(t.get("nav_timeout_ms", 30_000))
        self.download_timeout_ms: float = float(t.get("download_timeout_ms", 45_000))
        self.otp_timeout_s: float = float(t.get("otp_timeout_s", 180))
        self.takeover_timeout_s: float = float(t.get("takeover_timeout_s", 1800))
        self.cookie_timeout_ms = float(t.get("cookie_timeout_ms", self.cookie_timeout_ms))

    # -- account ----------------------------------------------------------------------------

    def _credentials(self) -> Credentials:
        username = str(getattr(self.account, "username", "") or "")
        password = _read_secret(self.account, "password", "password_enc")
        if not username or not password:
            raise InvalidInput("OV-chipkaart account has no username/password configured")
        return Credentials(username, password)

    @property
    def card_last4(self) -> str | None:
        number = _read_secret(self.account, "card_number", "card_number_enc")
        return _last4(number)

    def _otp_provider(self) -> OtpProvider:
        if self._otp is None:
            self._otp = get_otp_provider(self.account)
        return self._otp

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

    # -- session ----------------------------------------------------------------------------

    @asynccontextmanager
    async def _open(self) -> Any:
        rt = self._runtime or get_runtime()
        async with rt.session(self._account_key) as s:
            yield s

    async def _pause(self, lo: float = 0.0, hi: float = 0.0) -> None:
        a, b = self.pause_s
        if b > 0:
            await jitter_sleep(a + lo, b + hi)

    async def _session_ok(self, s: BrowserSession) -> bool:
        """NextAuth ``/api/auth/session``: a JSON object with a ``user`` key means logged in."""
        url = self._url(_sel("base.session_path", "/api/auth/session"))
        try:
            resp = await s.context.request.get(url, timeout=10_000, fail_on_status_code=False)
            if resp.status != 200:
                return False
            data = await resp.json()
        except Exception:  # noqa: BLE001 - network error, HTML instead of JSON, ...
            return False
        return isinstance(data, dict) and bool(data.get("user"))

    async def _dismiss_cookies(self, page: Page) -> None:
        css = _sel("cookies.selectors", None)
        try:
            clicked = await dismiss_cookie_banner(page, selectors=css, timeout=self.cookie_timeout_ms)
        except PlaywrightError:
            clicked = False
        # After the first successful dismissal the consent cookie is stored: do not wait again.
        if clicked:
            self.cookie_timeout_ms = min(self.cookie_timeout_ms, 600.0)

    async def _goto(self, page: Page, url: str) -> int | None:
        """Navigate; returns the HTTP status or ``None``. Connection resets/timeouts count as blocking."""
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        except PlaywrightError as exc:
            msg = str(exc)
            if re.search(
                r"ERR_(NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|CONNECTION_REFUSED|ADDRESS_UNREACHABLE)", msg
            ):
                raise OvchipNavigationError(f"cannot reach {url}: {msg.splitlines()[0]}") from exc
            # Timeouts, resets, HTTP/2 protocol errors, empty responses: typical of a WAF dropping us.
            return -1
        return resp.status if resp is not None else None

    async def _blocked_reason(self, page: Page, status: int | None, *, settle_s: float = 0.0) -> str | None:
        """Why this page looks like a bot block (``None`` = looks fine). ``settle_s``: wait for slow SSR."""
        if status == -1:
            return "navigation failed (connection reset or timeout)"
        if status in (403, 429):
            return f"HTTP {status}"
        text_rx = _rx(_sel("block.text_regex", "access denied")) if _sel("block.text_regex") else None
        html_rx = _rx(_sel("block.html_regex")) if _sel("block.html_regex") else None
        min_html = int(_sel("block.min_html_bytes", 1500))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle_s
        while True:
            try:
                html = await page.content()
                text = await page.evaluate("document.body ? document.body.innerText : ''")
            except PlaywrightError:
                html, text = "", ""
            small = len(html.encode("utf-8")) < min_html
            if text_rx and text_rx.search(text[:3000]):
                return "challenge/blocked page text"
            if small and html_rx and html_rx.search(html):
                return "bot-protection script served instead of the page"
            if not (small and not text.strip()):
                return None
            if loop.time() >= deadline:
                return f"page is nearly empty ({len(html)} bytes)"
            await asyncio.sleep(0.4)

    # -- login state machine -------------------------------------------------------------------

    def _user_locs(self, page: Page) -> list[Locator]:
        return _field_locators(page, _sel("login.username", {}) or {})

    def _pass_locs(self, page: Page) -> list[Locator]:
        return _field_locators(page, _sel("login.password", {}) or {})

    def _otp_locs(self, page: Page) -> list[Locator]:
        return _field_locators(page, _sel("login.otp", {}) or {})

    async def _any_visible(self, locs: Sequence[Locator]) -> bool:
        return await _first_visible(locs, 0) is not None

    async def _wait_state(
        self, s: BrowserSession, *, status: int | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        """("login" | "otp" | "logged_in" | "entry" | "blocked" | "unknown", detail)."""
        page = s.page
        if status in (403, 429, -1):
            return "blocked", (await self._blocked_reason(page, status)) or f"HTTP {status}"
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        limit = self.settle_timeout_s if timeout is None else timeout
        last_check = -10.0
        while True:
            elapsed = loop.time() - t0
            if await self._any_visible(self._pass_locs(page)) or await self._any_visible(
                self._user_locs(page)
            ):
                return "login", ""
            if await self._any_visible(self._otp_locs(page)):
                return "otp", ""
            if elapsed - last_check >= 1.0:
                last_check = elapsed
                if await self._session_ok(s):
                    return "logged_in", ""
            if elapsed >= 1.0:
                entry = _named_locators(page, _sel("login.entry_button_names", []) or [])
                if await _first_visible(entry, 0) is not None:
                    return "entry", ""
            if elapsed >= self.block_check_after_s and (reason := await self._blocked_reason(page, status)):
                return "blocked", reason
            if elapsed >= limit:
                return "unknown", f"unrecognised page {page.url!r}"
            await asyncio.sleep(0.25)

    async def _login_error(self, page: Page) -> str | None:
        rx = _sel("login.error_regex")
        if not rx:
            return None
        pattern = _rx(rx)
        for css in _sel("login.error_css", []) or []:
            try:
                loc = page.locator(css).first
                if await loc.is_visible():
                    text = (await loc.inner_text()).strip()
                    if text and pattern.search(text):
                        return text
            except PlaywrightError:
                continue
        return None

    async def _tick(self, page: Page, label_patterns: Sequence[str]) -> None:
        for pat in label_patterns:
            loc = page.get_by_label(_rx(pat)).and_(page.locator("input[type=checkbox]")).first
            try:
                if await loc.is_visible():
                    await loc.check(timeout=2000)
                    return
            except PlaywrightError:
                continue

    async def _click_submit(
        self, page: Page, names: Sequence[str], css: Sequence[str], fallback: Locator | None
    ) -> None:
        btn = await _first_visible(_named_locators(page, names), 800)
        if btn is None:
            btn = await _first_visible([page.locator(c) for c in css], 500)
        if btn is not None:
            await human_click(btn)
        elif fallback is not None:
            await fallback.press("Enter")
        else:
            raise LayoutChanged("no submit button found on the login page")

    async def _submit_credentials(self, page: Page) -> None:
        creds = self._credentials()
        tmin, tmax = self.type_delay_ms
        user = await _first_visible(self._user_locs(page), 0)
        if user is not None:
            await human_type(user, creds.username, min_delay_ms=tmin, max_delay_ms=tmax)
            await self._pause()
        pw = await _first_visible(self._pass_locs(page), 0)
        if pw is None and user is not None:  # identifier-first flow: username, "volgende", then password
            nxt = await _first_visible(_named_locators(page, _sel("login.next_names", []) or []), 800)
            if nxt is not None:
                await human_click(nxt)
            pw = await _first_visible(self._pass_locs(page), 8000)
        if pw is None:
            raise LayoutChanged("password field not found on the login page")
        await human_type(pw, creds.password, min_delay_ms=tmin, max_delay_ms=tmax)
        await self._pause()
        await self._tick(page, _sel("login.remember_labels", []) or [])
        self._login_submitted_at = datetime.now(UTC)
        await page.evaluate("window.__ovMarker = true")
        await self._click_submit(page, _sel("login.submit_names", []), _sel("login.submit_css", []), pw)
        await self._await_change(page)

    async def _await_change(self, page: Page, timeout: float = 20.0) -> None:
        """After a submit: wait for a navigation, an error, an OTP prompt or the form disappearing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                marker = await page.evaluate("window.__ovMarker === true")
            except PlaywrightError:
                return  # execution context destroyed: navigation in progress
            if not marker or await self._login_error(page):
                return
            if await self._any_visible(self._otp_locs(page)):
                return
            if not await self._any_visible(self._pass_locs(page)):
                return
            await asyncio.sleep(0.2)

    async def _submit_otp(self, s: BrowserSession) -> str | None:
        """Fetch a code from the OtpProvider and enter it. Returns an error text when no code arrived."""
        page = s.page
        since = (self._login_submitted_at or datetime.now(UTC)) - timedelta(seconds=15)
        try:
            code = await self._otp_provider().get_code(self.account, since, self.otp_timeout_s)
        except OtpTimeout:
            return "no verification code received in time"
        field_ = await _first_visible(self._otp_locs(page), 3000)
        if field_ is None:
            raise LayoutChanged("verification code field disappeared")
        tmin, tmax = self.type_delay_ms
        await human_type(field_, code, min_delay_ms=tmin, max_delay_ms=tmax)
        await self._pause()
        await self._tick(page, _sel("login.otp.remember_labels", []) or [])
        await page.evaluate("window.__ovMarker = true")
        await self._click_submit(
            page, _sel("login.otp.submit_names", []), _sel("login.submit_css", []), field_
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while loop.time() < deadline:  # wait until the OTP page is gone (or errors)
            try:
                gone = not await page.evaluate("window.__ovMarker === true")
            except PlaywrightError:
                return None
            if gone or not await self._any_visible(self._otp_locs(page)):
                return None
            if await self._login_error(page):
                return "verification code rejected"
            await asyncio.sleep(0.2)
        return None

    async def _login_pass(self, s: BrowserSession) -> tuple[str, str] | None:
        """One attempt at getting logged in. ``None`` = logged in; else ("blocked"|"stuck", reason)."""
        page = s.page
        if await self._session_ok(s):
            return None
        status = await self._goto(page, self._url(_sel("base.home_path", "/nl/mijn-ov-chip")))
        await self._dismiss_cookies(page)
        state, detail = await self._wait_state(s, status=status)
        logins = otps = 0
        for _ in range(8):
            if state == "logged_in":
                return None
            if state == "blocked":
                return "blocked", detail
            if state == "unknown":
                return "stuck", detail
            if state == "entry":
                entry = await _first_visible(
                    _named_locators(page, _sel("login.entry_button_names", [])), 1000
                )
                if entry is not None:
                    await human_click(entry)
                state, detail = await self._wait_state(s)
                await self._dismiss_cookies(page)
                continue
            if state == "login":
                if logins >= 2:
                    return "stuck", "login form keeps coming back after two attempts"
                logins += 1
                await self._dismiss_cookies(page)
                await self._submit_credentials(page)
                state, detail = await self._wait_state(s)
                if state == "login" and (err := await self._login_error(page)):
                    # Never retry: repeated wrong passwords lock the account.
                    raise InvalidInput(f"OV-chipkaart rejected the credentials: {err}")
                continue
            if state == "otp":
                if otps >= 2:
                    return "stuck", "verification code was not accepted"
                otps += 1
                err = await self._submit_otp(s)
                if err:
                    return "stuck", err
                state, detail = await self._wait_state(s)
                continue
        return "stuck", "login did not settle"

    async def _ensure(self, s: BrowserSession) -> None:
        async with s.debug_bundle("ovchip-login") as bundle:
            for attempt in range(2):
                outcome = await self._login_pass(s)
                if outcome is None:
                    return
                kind, reason = outcome
                bundle.keep(f"{kind}: {reason}")
                if attempt == 0:
                    log.warning("ovchip login needs a human (%s): %s", kind, reason)
                    await s.takeover(f"OV-chipkaart inloggen: {reason}", timeout=self.takeover_timeout_s)
                    if await self._session_ok(s):
                        return
                    continue
                if kind == "blocked":
                    raise BlockedByBotProtection(f"ov-chipkaart.nl blocked automated access: {reason}")
                raise LoginRequired(f"could not log in to ov-chipkaart.nl: {reason}")

    async def ensure_logged_in(self) -> None:
        async with self._open() as s:
            await self._ensure(s)

    # -- trips -----------------------------------------------------------------------------------

    async def list_trips(self, since: date) -> list[TripDTO]:
        async with self._open() as s:
            await self._ensure(s)
            async with s.debug_bundle("ovchip-list-trips") as bundle:
                rows, fmt = await self._collect_rows(s, since, bundle)
                trips = rows_to_trips(
                    rows,
                    stations=self._stations,
                    account_last4=self.card_last4,
                    fallback_card_id=f"ovchip-account-{self._account_key}",
                    since=since,
                    source_format=fmt,
                )
                bundle.note("rows", len(rows))
                bundle.note("trips", len(trips))
                bundle.note("format", fmt)
                return trips

    async def _navigate(self, s: BrowserSession, path: str) -> None:
        """goto + block check; one human takeover, then BlockedByBotProtection."""
        page = s.page
        for attempt in range(2):
            status = await self._goto(page, self._url(path))
            await self._dismiss_cookies(page)
            reason = await self._blocked_reason(page, status, settle_s=self.block_check_after_s)
            if reason is None:
                return
            if attempt == 0:
                await s.takeover(f"OV-chipkaart: {reason}", timeout=self.takeover_timeout_s)
                continue
            raise BlockedByBotProtection(f"ov-chipkaart.nl blocked {path}: {reason}")

    async def _settle(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightError:
            pass

    async def _collect_rows(self, s: BrowserSession, since: date, bundle: Any) -> tuple[list[TxRow], str]:
        page = s.page
        await self._navigate(s, _sel("base.home_path", "/nl/mijn-ov-chip"))
        await self._select_card(page, self.card_last4, bundle)
        s.recorder.clear()
        await self._open_history(s)
        await self._choose_period(page, since)
        await self._settle(page)

        ladder: list[tuple[str, Callable[[], Any]]] = [
            ("api", lambda: self._rung_api(s, since)),
            ("csv", lambda: self._rung_csv(page)),
            ("json", lambda: self._rung_json(s)),
            ("dom", lambda: self._rung_dom(page)),
        ]
        errors: list[str] = []
        for name, rung in ladder:
            try:
                rows = await rung()
            except (LayoutChanged, PlaywrightError, ValueError, TimeoutError) as exc:
                log.warning("ovchip: %s rung failed: %s", name, exc)
                errors.append(f"{name}: {exc}")
                continue
            if rows is not None:
                bundle.note("rung", name)
                return rows, name
        bundle.keep("no strategy produced trips")
        raise LayoutChanged(
            "could not read the OV-chipkaart travel history: " + "; ".join(errors or ["no export found"])
        )

    async def _select_card(self, page: Page, last4: str | None, bundle: Any) -> None:
        if not last4:
            return
        card_rx = re.compile(rf"(?<!\d)(?:\d{{12}})?{last4}(?!\d)")
        spec_labels = _sel("card.select_labels", []) or []
        fields = page.locator("select")
        select = await _first_visible([page.get_by_label(_rx(x)).and_(fields) for x in spec_labels], 1500)
        if select is not None:
            options = await select.locator("option").all()
            for i, opt in enumerate(options):
                if card_rx.search(await opt.inner_text()):
                    await select.select_option(index=i)
                    await self._settle(page)
                    return
        else:
            for role in _sel("card.item_roles", []) or []:
                item = await _first_visible([page.get_by_role(role, name=card_rx)], 0)
                if item is not None:
                    await human_click(item)
                    await self._settle(page)
                    return
            item = await _first_visible([page.get_by_text(card_rx)], 0)
            if item is not None:
                await human_click(item)
                await self._settle(page)
                return
        log.warning("ovchip: card ending in %s not found in the card picker; using what is shown", last4)
        bundle.note("card_picker", f"card ending {last4} not found")

    async def _open_history(self, s: BrowserSession) -> None:
        page = s.page
        link = await _first_visible(
            _named_locators(page, _sel("history.link_names", []) or [], ("link", "tab", "button")), 3000
        )
        if link is not None:
            await human_click(link)
            await self._dismiss_cookies(page)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightError:
                pass
            return
        direct = _sel("base.history_path", "")
        if direct:
            await self._navigate(s, direct)
            return
        raise LayoutChanged("no 'Reishistorie' link found (set history.link_names or base.history_path)")

    async def _choose_period(self, page: Page, since: date) -> None:
        spec = _sel("history.period", {}) or {}
        today = datetime.now(AMS).date()
        changed = False
        from_loc = await _first_visible(_field_locators(page, {"labels": spec.get("from_labels", [])}), 800)
        if from_loc is not None:
            to_loc = await _first_visible(_field_locators(page, {"labels": spec.get("to_labels", [])}), 500)
            for loc, value in ((from_loc, since), (to_loc, today)):
                if loc is None:
                    continue
                is_date_input = (await loc.get_attribute("type") or "").lower() == "date"
                text = (
                    value.isoformat()
                    if is_date_input
                    else value.strftime(spec.get("date_format", "%d-%m-%Y"))
                )
                await loc.fill(text)
            changed = True
        else:
            select = await _first_visible(
                [
                    page.get_by_label(_rx(x)).and_(page.locator("select"))
                    for x in spec.get("select_labels", [])
                ],
                500,
            )
            if select is not None:
                texts = [await o.inner_text() for o in await select.locator("option").all()]
                for pattern in spec.get("option_regexes", []):
                    hit = next((i for i, t in enumerate(texts) if _rx(pattern).search(t)), None)
                    if hit is not None:
                        await select.select_option(index=hit)
                        changed = True
                        break
        if changed:
            apply_btn = await _first_visible(_named_locators(page, spec.get("apply_names", [])), 500)
            if apply_btn is not None:
                await human_click(apply_btn)
            await self._settle(page)

    # -- strategy ladder -------------------------------------------------------------------------

    async def _rung_api(self, s: BrowserSession, since: date) -> list[TxRow] | None:
        template = _sel("history.api.url", "")
        if not template:
            return None
        url = self._url(
            template.format(
                **{
                    "from": since.isoformat(),
                    "to": datetime.now(AMS).date().isoformat(),
                    "card": self.card_last4 or "",
                }
            )
        )
        resp = await s.context.request.get(url, timeout=20_000, fail_on_status_code=False)
        if resp.status != 200:
            raise ValueError(f"API replay {url} returned HTTP {resp.status}")
        rows = rows_from_json(await resp.json())
        return rows or None

    async def _rung_csv(self, page: Page) -> list[TxRow] | None:
        spec = _sel("history.export", {}) or {}
        csv_ctl = _named_locators(page, spec.get("csv_names", ["csv"]), ("button", "link", "menuitem"))
        ctl = await _first_visible(csv_ctl, 1500)
        if ctl is None and spec.get("open_names"):
            opener = await _first_visible(_named_locators(page, spec["open_names"]), 500)
            if opener is not None:
                await human_click(opener)
                ctl = await _first_visible(csv_ctl, 2500)
        if ctl is None:
            return None
        async with page.expect_download(timeout=self.download_timeout_ms) as info:
            await human_click(ctl)
        download = await info.value
        path = await download.path()
        data = Path(path).read_bytes()
        return parse_csv(data)

    async def _rung_json(self, s: BrowserSession) -> list[TxRow] | None:
        rec = s.recorder
        regex = _sel("history.api.regex", "")
        if not regex:
            return None
        try:
            await rec.wait_for(regex, timeout=3.0, new_only=False)
        except TimeoutError:
            pass
        await rec.flush()
        best: list[TxRow] = []
        for entry in reversed(rec.find(regex)):
            try:
                rows = rows_from_json(entry.json())
            except ValueError:
                continue
            if len(rows) > len(best):
                best = rows
        if not best:
            log.info("ovchip: no usable JSON among %d recorded responses", len(rec.find(regex)))
        return best or None

    async def _rung_dom(self, page: Page) -> list[TxRow] | None:
        css = _sel("history.dom.table", "table")
        tables = await page.evaluate(
            """(css) => [...document.querySelectorAll(css)].map(t => {
                const rows = [...t.querySelectorAll('tr')].map(tr =>
                    [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim()));
                return rows;
            })""",
            css,
        )
        for table in tables:
            try:
                return rows_from_table(table)
            except LayoutChanged:
                continue
        return None
