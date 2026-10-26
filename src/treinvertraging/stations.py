"""Station codes and names (WP-B).

Data: the Rijden de Treinen station list (CC BY 4.0, https://opendata.rijdendetreinen.nl/public/stations/).
Columns: ``id,code,uic,name_short,name_medium,name_long,slug,country,type,geo_lat,geo_lng``.

``StationIndex.code_for(name)`` maps free text ("Utrecht Centraal", "Utrecht C.", "utrecht cs",
"'s-Hertogenbosch", "Den Bosch") to a station code ("UT"). Resolution order:

1. exact match on any official name (name_short / name_medium / name_long) or a user alias;
2. match after normalisation (case, accents, punctuation, "C."/"CS" -> "centraal", "station");
3. the input is itself a station code written in capitals ("UT", "ASD");
4. the input is a word-prefix of station names ("Utrecht", "Eindhoven"): the highest-ranked
   station type wins if that is unambiguous (megastation > knooppunt > intercity > ...);
5. rapidfuzz fuzzy match above ``fuzzy_threshold`` (0-100) and clearly better than the runner-up
   with a different code (otherwise ``None``: no answer beats a wrong claim).

The cache lives in ``DATA_DIR/rdt-cache/stations.csv`` (see ``refresh``). A copy of the
2023-09 list is bundled as ``defaults/stations.csv`` so the app works offline on first start.
"""

from __future__ import annotations

import csv
import io
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

from treinvertraging.errors import TreinvertragingError

STATIONS_INDEX_URL = "https://opendata.rijdendetreinen.nl/public/stations/"
# Fallback when the directory listing cannot be parsed (latest known list at time of writing).
FALLBACK_STATIONS_URL = STATIONS_INDEX_URL + "stations-2023-09.csv"
CACHE_SUBDIR = "rdt-cache"
CACHE_FILENAME = "stations.csv"
DEFAULT_FUZZY_THRESHOLD = 88.0
_AMBIGUITY_MARGIN = 2.0
_HTTP_TIMEOUT = 30.0

# Lower is better; unknown types rank last.
_TYPE_RANK = {
    "megastation": 0,
    "knooppuntIntercitystation": 1,
    "knooppuntSneltreinstation": 2,
    "intercitystation": 3,
    "sneltreinstation": 4,
    "knooppuntStoptreinstation": 5,
    "stoptreinstation": 6,
}


class StationsError(TreinvertragingError):
    """The station list could not be downloaded or parsed."""


@dataclass(frozen=True, slots=True)
class Station:
    code: str
    name_short: str
    name_medium: str
    name_long: str
    country: str = "NL"
    type: str = ""
    uic: str = ""
    lat: float | None = None
    lng: float | None = None


# --------------------------------------------------------------------------- normalisation

_ABBREVIATIONS = {"cs": "centraal", "ctr": "centraal", "cent": "centraal", "centr": "centraal"}
_NOISE = {"station", "stn", "ns"}


def normalize(name: str) -> str:
    """Lower-case, strip accents and punctuation, expand "C."/"CS", drop "station"."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)  # also turns apostrophes/hyphens/dots into spaces
    tokens = [t for t in text.split() if t not in _NOISE]
    if len(tokens) > 1:
        if tokens[-1] == "c":
            tokens[-1] = "centraal"
        tokens[-1] = _ABBREVIATIONS.get(tokens[-1], tokens[-1])
    return " ".join(tokens)


# --------------------------------------------------------------------------- index


class StationIndex:
    def __init__(
        self,
        stations: Iterable[Station] = (),
        *,
        fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self.fuzzy_threshold = min(float(fuzzy_threshold), 100.0)
        self._by_code: dict[str, Station] = {}
        self._exact: dict[str, str] = {}
        self._norm: dict[str, str] = {}  # normalised name -> code (first/best wins)
        self._norm_names: list[str] = []
        self._norm_codes: list[str] = []
        for st in stations:
            self._add(st)
        for alias, code in (aliases or {}).items():
            self.add_alias(alias, code)

    # -- construction

    def _add(self, st: Station) -> None:
        code = st.code.strip().upper()
        if not code or code in self._by_code:
            return
        self._by_code[code] = st
        for name in {st.name_short, st.name_medium, st.name_long}:
            if not name:
                continue
            self._exact.setdefault(name.strip().casefold(), code)
            norm = normalize(name)
            if norm and norm not in self._norm:
                self._norm[norm] = code
                self._norm_names.append(norm)
                self._norm_codes.append(code)

    def add_alias(self, alias: str, code: str) -> None:
        """Register an extra name for a known code (e.g. user-specific spellings)."""
        code = code.strip().upper()
        if code not in self._by_code:
            raise KeyError(f"unknown station code {code!r}")
        self._exact[alias.strip().casefold()] = code
        norm = normalize(alias)
        if norm and norm not in self._norm:
            self._norm[norm] = code
            self._norm_names.append(norm)
            self._norm_codes.append(code)

    @classmethod
    def from_csv_text(cls, text: str, **kwargs) -> StationIndex:
        reader = csv.DictReader(io.StringIO(text))
        fields = set(reader.fieldnames or [])
        if not {"code", "name_long"} <= fields:
            raise StationsError(f"unexpected station CSV columns: {sorted(fields)}")
        stations: list[Station] = []
        for row in reader:
            code = (row.get("code") or "").strip()
            long_name = (row.get("name_long") or "").strip()
            if not code or not long_name:
                continue
            stations.append(
                Station(
                    code=code.upper(),
                    name_short=(row.get("name_short") or "").strip(),
                    name_medium=(row.get("name_medium") or "").strip(),
                    name_long=long_name,
                    country=(row.get("country") or "NL").strip(),
                    type=(row.get("type") or "").strip(),
                    uic=(row.get("uic") or "").strip(),
                    lat=_to_float(row.get("geo_lat")),
                    lng=_to_float(row.get("geo_lng")),
                )
            )
        if not stations:
            raise StationsError("station CSV contains no stations")
        return cls(stations, **kwargs)

    @classmethod
    def from_csv(cls, path: Path | str, **kwargs) -> StationIndex:
        return cls.from_csv_text(Path(path).read_text(encoding="utf-8-sig"), **kwargs)

    @classmethod
    def bundled(cls, **kwargs) -> StationIndex:
        text = (resources.files("treinvertraging") / "defaults" / "stations.csv").read_text(
            encoding="utf-8-sig"
        )
        return cls.from_csv_text(text, **kwargs)

    @classmethod
    def load(cls, data_dir: Path | str | None = None, **kwargs) -> StationIndex:
        """Load the cached list from ``DATA_DIR/rdt-cache``; fall back to the bundled list."""
        path = cache_path(data_dir)
        if path.is_file():
            try:
                return cls.from_csv(path, **kwargs)
            except (StationsError, OSError, UnicodeDecodeError):
                pass  # corrupt cache: use the bundled list until the next refresh()
        return cls.bundled(**kwargs)

    # -- lookups

    def __len__(self) -> int:
        return len(self._by_code)

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and code.strip().upper() in self._by_code

    def get(self, code: str) -> Station | None:
        return self._by_code.get(code.strip().upper())

    def codes(self) -> list[str]:
        return list(self._by_code)

    def name_for(self, code: str, kind: str = "long") -> str | None:
        """Official name for a code (``kind``: "long" (default), "medium" or "short")."""
        st = self.get(code)
        if st is None:
            return None
        return {"long": st.name_long, "medium": st.name_medium, "short": st.name_short}[kind] or st.name_long

    def code_for(self, name: str | None) -> str | None:
        if not name or not name.strip():
            return None
        raw = name.strip()
        code = self._exact.get(raw.casefold())
        if code:
            return code
        norm = normalize(raw)
        if not norm:
            return None
        code = self._norm.get(norm)
        if code:
            return code
        if raw.isupper() and raw in self._by_code:
            return raw
        code = self._prefix_match(norm)
        if code:
            return code
        return self._fuzzy_match(norm)

    # -- internals

    def _rank(self, code: str) -> tuple[int, int]:
        st = self._by_code[code]
        return (0 if st.country == "NL" else 1, _TYPE_RANK.get(st.type, 9))

    def _prefix_match(self, norm: str) -> str | None:
        if len(norm) < 4:
            return None
        prefix = norm + " "
        codes = {c for n, c in zip(self._norm_names, self._norm_codes, strict=True) if n.startswith(prefix)}
        if not codes:
            return None
        if len(codes) == 1:
            return next(iter(codes))
        ranked = sorted(codes, key=self._rank)
        if self._rank(ranked[0]) < self._rank(ranked[1]):
            return ranked[0]
        return None

    def _fuzzy_match(self, norm: str) -> str | None:
        if not self._norm_names:
            return None
        hits = process.extract(
            norm, self._norm_names, scorer=fuzz.WRatio, score_cutoff=self.fuzzy_threshold, limit=8
        )
        if not hits:
            return None
        best_score = hits[0][1]
        best_code = self._norm_codes[hits[0][2]]
        for _, score, idx in hits[1:]:
            other = self._norm_codes[idx]
            if other != best_code and best_score - score < _AMBIGUITY_MARGIN:
                return None
        return best_code

    # -- network

    async def refresh(
        self,
        data_dir: Path | str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> Path:
        """Download the newest station list into the cache and reload this index from it.

        Returns the cache path. Raises ``StationsError`` on network/parse problems; the existing
        cache and the in-memory index are left untouched in that case.
        """
        path = await download_stations(data_dir, client=client)
        fresh = StationIndex.from_csv(path, fuzzy_threshold=self.fuzzy_threshold)
        self.__dict__.update({k: v for k, v in fresh.__dict__.items() if k != "fuzzy_threshold"})
        return path


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- cache + download


def cache_path(data_dir: Path | str | None = None) -> Path:
    if data_dir is None:
        from treinvertraging.config import get_settings

        data_dir = get_settings().DATA_DIR
    return Path(data_dir) / CACHE_SUBDIR / CACHE_FILENAME


def latest_station_csv_url(index_html: str, base_url: str = STATIONS_INDEX_URL) -> str | None:
    """Pick the newest international list (``stations-YYYY-MM.csv``, not the ``-nl`` ones)."""
    names = re.findall(r'href="(stations-(\d{4}-\d{2})\.csv)"', index_html)
    if not names:
        return None
    return base_url + max(names, key=lambda n: n[1])[0]


async def download_stations(
    data_dir: Path | str | None = None, *, client: httpx.AsyncClient | None = None
) -> Path:
    """Fetch the newest list, validate it parses, then atomically replace the cache file."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    try:
        urls: list[str] = []
        try:
            listing = await http.get(STATIONS_INDEX_URL)
            listing.raise_for_status()
            found = latest_station_csv_url(listing.text)
            if found:
                urls.append(found)
        except httpx.HTTPError:
            pass
        if FALLBACK_STATIONS_URL not in urls:
            urls.append(FALLBACK_STATIONS_URL)

        last_error: Exception | None = None
        for url in urls:
            try:
                resp = await http.get(url)
                resp.raise_for_status()
                text = resp.content.decode("utf-8-sig")
                index = StationIndex.from_csv_text(text)
                if len(index) < 100:
                    raise StationsError(f"{url}: only {len(index)} stations, refusing to use it")
            except (httpx.HTTPError, StationsError, UnicodeDecodeError) as exc:
                last_error = exc
                continue
            target = cache_path(data_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False
            ) as tmp:
                tmp.write(text)
            Path(tmp.name).replace(target)
            return target
        raise StationsError(f"could not download station list: {last_error}")
    finally:
        if own:
            await http.aclose()


# --------------------------------------------------------------------------- shared instance


@lru_cache(maxsize=1)
def get_station_index() -> StationIndex:
    """Process-wide index (cache file if present, else bundled list)."""
    return StationIndex.load()


def reset_station_index_cache() -> None:
    get_station_index.cache_clear()
