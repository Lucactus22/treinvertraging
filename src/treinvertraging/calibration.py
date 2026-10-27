"""Calibration walkthrough (milestone M6): record the real pages and API calls of NS / OV-chipkaart.

``run_calibration`` opens a *headed* browser session for an account (``gtbv`` uses the shared
ephemeral profile), navigates to the channel's start URL and then walks through a fixed list of steps.
For every step a human navigates there in noVNC and confirms (CLI: press Enter; web UI: a takeover with
a "Klaar" button, see ``services.start_calibration``). After each confirmation this module writes

``<DATA_DIR>/calibration/<channel>/<NN-step>/``
    ``page.html``, ``screenshot.png``, ``aria.yml`` (accessibility snapshot), ``recorder.json`` (JSON
    traffic since the previous step) and ``meta.json``

and finally ``<DATA_DIR>/calibration/<channel>/calibration-summary.json`` with the candidate JSON endpoints
(url, method, status, top-level keys) and the visible headings / labels / buttons of every step.

The raw tree contains PERSONAL DATA. ``redact_tree`` makes a shareable copy: text files go through
``browser.debug.redact``; screenshots, traces and anything it cannot read as text are NOT copied.
Nothing here ever clicks a submit button: the human does all the navigating.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Page

from treinvertraging.browser.debug import redact
from treinvertraging.browser.runtime import BrowserRuntime, BrowserSession, get_runtime
from treinvertraging.browser.takeover import DEFAULT_TIMEOUT_S, request_takeover
from treinvertraging.config import get_settings

log = logging.getLogger(__name__)

CHANNELS = ("ns", "ovchip", "gtbv")
SUMMARY_NAME = "calibration-summary.json"

# Text files that are redacted and copied; everything else (screenshots, traces, ...) is skipped.
TEXT_SUFFIXES = {".html", ".htm", ".json", ".yml", ".yaml", ".txt", ".md", ".csv"}


@dataclass(frozen=True)
class Step:
    name: str  # directory suffix, e.g. "reishistorie"
    instruction: str  # Dutch, shown to the human
    no_submit: bool = False

    @property
    def prompt(self) -> str:
        warn = " LET OP: NIET VERSTUREN." if self.no_submit else ""
        return f"{self.instruction}{warn}"


CHANNEL_STEPS: dict[str, tuple[Step, ...]] = {
    "ns": (
        Step(
            "login", "log in bij Mijn NS (de inlogpagina of, als je al bent ingelogd, het Mijn NS-overzicht)"
        ),
        Step(
            "reishistorie", "open de reishistorie ('Reishistorie' / 'Mijn reizen') met de lijst van je reizen"
        ),
        Step("trip-detail", "open het detail van één reis, liefst een reis met vertraging"),
        Step(
            "geld-terug-form",
            "open het formulier 'Geld terug bij vertraging' van die reis (het voorgevulde formulier)",
            no_submit=True,
        ),
        Step("mijn-aanvragen", "open de pagina met je ingediende aanvragen ('Mijn aanvragen' / status)"),
    ),
    "ovchip": (
        Step("login", "log in bij ov-chipkaart.nl (de inlogpagina of, al ingelogd, 'Mijn OV-chip')"),
        Step("kaartoverzicht", "open het overzicht van je OV-chipkaart(en)"),
        Step("reishistorie", "open de reishistorie van de kaart met een periode waarin je hebt gereisd"),
        Step("export", "open de export-/declaratiepagina (CSV/PDF-download) van de reishistorie"),
    ),
    "gtbv": (
        Step("start", "open het publieke formulier: stap 'Vervoerbewijs', kaartnummer ingevuld"),
        Step("reisgegevens", "ga naar stap 'Reisgegevens' en vul een echte vertraagde reis in"),
        Step("gegevens", "ga naar de stap(pen) met je gegevens (IBAN, naam, e-mail)"),
        Step(
            "controle",
            "ga naar de laatste controle-/samenvattingspagina vlak voor het versturen",
            no_submit=True,
        ),
    ),
}

# confirm(step, index, total, page): returns when the human is on the right page.
Confirm = Callable[[Step, int, int, Page], Awaitable[None]]


class CalibrationError(RuntimeError):
    pass


@dataclass
class CalibrationResult:
    channel: str
    out_dir: Path
    steps: list[str] = field(default_factory=list)
    summary_path: Path | None = None
    candidate_endpoints: int = 0

    def summary(self) -> str:
        return (
            f"Kalibratie '{self.channel}' klaar: {len(self.steps)} stap(pen), "
            f"{self.candidate_endpoints} JSON-endpoint(s) gevonden. Uitvoer: {self.out_dir}"
        )


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def calibration_root() -> Path:
    return get_settings().DATA_DIR / "calibration"


def start_url(channel: str) -> str:
    """Where the browser starts: the login entry of the site / the public form."""
    from treinvertraging import selectors

    if channel == "ns":
        from treinvertraging.adapters.ns import DEFAULT_BASE_URL

        return DEFAULT_BASE_URL + str(selectors.get("ns", "login.start_path", "/mijnns"))
    if channel == "ovchip":
        from treinvertraging.adapters.ovchip import DEFAULT_BASE_URL

        return DEFAULT_BASE_URL + str(selectors.get("ovchip", "base.home_path", "/nl/mijn-ov-chip"))
    if channel == "gtbv":
        return str(selectors.get("gtbv", "url", ""))
    raise ValueError(f"unknown channel {channel!r}; choose one of {', '.join(CHANNELS)}")


_HARVEST_JS = """
() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = (e) => (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120);
  const uniq = (a) => [...new Set(a.filter(Boolean))].slice(0, 80);
  const q = (sel) => [...document.querySelectorAll(sel)].filter(vis);
  return {
    headings: uniq(q('h1,h2,h3,h4,[role=heading]').map(txt)),
    labels: uniq(
      q('label').map(txt).concat(
        q('input,select,textarea').map((e) => e.getAttribute('aria-label') || e.getAttribute('placeholder'))
      )
    ),
    buttons: uniq(
      q('button,[role=button],input[type=submit],input[type=button]').map(
        (e) => txt(e) || e.value || e.getAttribute('aria-label')
      )
    ),
    links: uniq(q('a[href]').map(txt)).slice(0, 40),
  };
}
"""


async def _harvest(page: Page) -> dict[str, list[str]]:
    try:
        data = await page.evaluate(_HARVEST_JS)
        return {k: [str(x) for x in v] for k, v in data.items()}
    except Exception as exc:  # noqa: BLE001 - e.g. mid-navigation; the HTML dump is still there
        log.warning("could not harvest headings/labels: %s", type(exc).__name__)
        return {"headings": [], "labels": [], "buttons": [], "links": []}


def describe_endpoint(entry: Any) -> dict[str, Any]:
    """``RecordedResponse`` -> {url, method, status, shape, top_level_keys, size}."""
    shape, keys, items = "other", [], None
    try:
        body = json.loads(entry.response_body)
    except (ValueError, TypeError):
        body = None
        shape = "invalid-json" if not entry.truncated else "truncated"
    if isinstance(body, dict):
        shape, keys = "object", sorted(str(k) for k in body)[:40]
    elif isinstance(body, list):
        shape, items = "array", len(body)
        if body and isinstance(body[0], dict):
            keys = sorted(str(k) for k in body[0])[:40]
    out: dict[str, Any] = {
        "url": entry.url,
        "method": entry.method,
        "status": entry.status,
        "shape": shape,
        "top_level_keys": keys,
        "size": len(entry.response_body),
    }
    if items is not None:
        out["items"] = items
    return out


async def _capture_step(session: BrowserSession, page: Page, step_dir: Path, step: Step) -> dict[str, Any]:
    """Write all artefacts of one step. Each artefact is best effort: one failure never aborts the run."""
    if step_dir.exists():
        shutil.rmtree(step_dir)
    step_dir.mkdir(parents=True)
    meta: dict[str, Any] = {"step": step.name, "ts": datetime.now(UTC).isoformat(), "url": page.url}
    try:
        meta["title"] = await asyncio.wait_for(page.title(), 5)
    except Exception:  # noqa: BLE001
        meta["title"] = ""
    try:
        (step_dir / "page.html").write_text(await asyncio.wait_for(page.content(), 15), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        meta["html_error"] = type(exc).__name__
    try:
        await page.screenshot(path=str(step_dir / "screenshot.png"), full_page=True, timeout=15_000)
    except Exception:  # noqa: BLE001
        try:
            await page.screenshot(path=str(step_dir / "screenshot.png"), timeout=5_000)
        except Exception as exc:  # noqa: BLE001
            meta["screenshot_error"] = type(exc).__name__
    try:
        aria = await page.locator("body").aria_snapshot(timeout=10_000)
        (step_dir / "aria.yml").write_text(aria, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        meta["aria_error"] = type(exc).__name__
    await session.recorder.flush()
    entries = list(session.recorder.entries)
    session.recorder.dump(step_dir)
    session.recorder.clear()  # the next step's recorder.json only holds its own traffic
    page_info = await _harvest(page)
    (step_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        **meta,
        "endpoints": [describe_endpoint(e) for e in entries],
        **page_info,
    }


def _endpoint_key(ep: dict[str, Any]) -> tuple[str, str]:
    parts = urlsplit(ep["url"])
    return ep["method"].upper(), f"{parts.netloc}{parts.path}"


def build_summary(channel: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-step data: the same endpoint (method + host + path) is listed once."""
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for step in steps:
        for ep in step["endpoints"]:
            if ep["status"] >= 400 and not ep["top_level_keys"]:
                continue
            c = candidates.setdefault(
                _endpoint_key(ep),
                {
                    "method": ep["method"],
                    "url": ep["url"],
                    "status": ep["status"],
                    "shape": ep["shape"],
                    "top_level_keys": ep["top_level_keys"],
                    "largest_body": ep["size"],
                    "seen_in_steps": [],
                },
            )
            if step["step"] not in c["seen_in_steps"]:
                c["seen_in_steps"].append(step["step"])
            if ep["size"] > c["largest_body"]:
                c.update(url=ep["url"], shape=ep["shape"], top_level_keys=ep["top_level_keys"],
                         status=ep["status"], largest_body=ep["size"])  # fmt: skip
    return {
        "channel": channel,
        "generated_at": datetime.now(UTC).isoformat(),
        "note": "Contains personal data. Share only the output of `treinvertraging calibrate --redact`.",
        "candidate_json_endpoints": sorted(
            candidates.values(), key=lambda c: (-len(c["top_level_keys"]), -c["largest_body"])
        ),
        "steps": [
            {
                "step": s["step"],
                "url": s["url"],
                "title": s.get("title", ""),
                "headings": s["headings"],
                "labels": s["labels"],
                "buttons": s["buttons"],
                "links": s["links"],
                "json_endpoints": s["endpoints"],
            }
            for s in steps
        ],
    }


def _current_page(session: BrowserSession) -> Page:
    """The tab the human is looking at: the newest open page of the context."""
    pages = [p for p in session.context.pages if not p.is_closed()]
    return pages[-1] if pages else session.page


# ---------------------------------------------------------------------------------------------
# walkthrough
# ---------------------------------------------------------------------------------------------


def _session_for(rt: BrowserRuntime, channel: str, account_id: int | None):
    if channel == "gtbv":
        return rt.ephemeral_session()
    assert account_id is not None
    return rt.session(account_id)


def resolve_account(account_id: int | None, channel: str) -> tuple[int | None, str]:
    """Validate the account for ``channel``; returns ``(account_id, label)``."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; choose one of {', '.join(CHANNELS)}")
    if account_id is None:
        if channel == "gtbv":
            return None, "gtbv"
        raise ValueError(f"channel {channel} needs --account <id>")
    from treinvertraging.db import session_scope
    from treinvertraging.models import Account

    with session_scope() as s:
        acc = s.get(Account, account_id)
        if acc is None:
            raise LookupError(f"account {account_id} not found")
        source, label = acc.source, acc.label
    if channel in ("ns", "ovchip") and source != channel:
        raise ValueError(
            f"account {account_id} is a '{source}' account; channel '{channel}' needs a '{channel}' account"
        )
    return account_id, label


async def run_calibration(
    account_id: int | None,
    channel: str,
    *,
    confirm: Confirm,
    runtime: BrowserRuntime | None = None,
    out_dir: Path | None = None,
    steps: Iterable[Step] | None = None,
    require_headed: bool = True,
) -> CalibrationResult:
    """Run the walkthrough (see module docstring). ``confirm`` blocks until the human is on the page."""
    account_id, _label = resolve_account(account_id, channel)
    rt = runtime or get_runtime()
    if require_headed and rt.headless:
        raise CalibrationError("calibratie vereist een zichtbare browser: zet HEADLESS=false (noVNC)")
    step_list = list(steps) if steps is not None else list(CHANNEL_STEPS[channel])
    out = out_dir or (calibration_root() / channel)
    out.mkdir(parents=True, exist_ok=True)
    result = CalibrationResult(channel=channel, out_dir=out)
    captured: list[dict[str, Any]] = []
    async with _session_for(rt, channel, account_id) as session:
        session.page.set_default_timeout(30_000)
        url = start_url(channel)
        try:
            await session.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:  # noqa: BLE001 - blocked/offline: the human can still navigate by hand
            log.warning("calibration: could not open %s (%s); continue by hand", url, type(exc).__name__)
        for i, step in enumerate(step_list, start=1):
            page = _current_page(session)
            await confirm(step, i, len(step_list), page)
            page = _current_page(session)
            dirname = f"{i:02d}-{step.name}"
            captured.append(await _capture_step(session, page, out / dirname, step))
            result.steps.append(dirname)
            log.info("calibration: captured %s/%s", channel, dirname)
    summary = build_summary(channel, captured)
    result.summary_path = out / SUMMARY_NAME
    result.summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    result.candidate_endpoints = len(summary["candidate_json_endpoints"])
    return result


def takeover_confirm(label: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> Confirm:
    """Confirm through the web UI's takeover mechanism: banner + notification + "Klaar" button."""

    async def confirm(step: Step, index: int, total: int, page: Page) -> None:
        reason = (
            f"Kalibratie stap {index}/{total} ({step.name}): ga naar {step.prompt}. Klik daarna op Klaar."
        )
        await request_takeover(label, page, reason, timeout=timeout)

    return confirm


def console_confirm() -> Confirm:
    """Confirm on the command line: print the instruction and wait for Enter."""

    async def confirm(step: Step, index: int, total: int, page: Page) -> None:
        print(f"\n[{index}/{total}] Stap '{step.name}': ga in de browser (noVNC) naar: {step.prompt}")
        try:
            await asyncio.to_thread(input, "    Druk op Enter als je daar bent... ")
        except EOFError as exc:
            raise CalibrationError("geen invoer beschikbaar (stdin gesloten); kalibratie afgebroken") from exc

    return confirm


# ---------------------------------------------------------------------------------------------
# redacted copy
# ---------------------------------------------------------------------------------------------

README_TEXT = """\
# Geredigeerde kalibratie-uitvoer

Deze map is gemaakt met `treinvertraging calibrate --redact`. HTML-, JSON- en YAML-bestanden zijn
door de redactiefilter gehaald (IBAN, kaartnummers, e-mailadressen, 6-cijferige codes,
wachtwoordvelden{names}). Controleer de bestanden toch even zelf voordat je ze deelt: namen en
adressen in vrije tekst worden niet altijd herkend.

## WAARSCHUWING: screenshots en traces zijn NIET meegekopieerd

Screenshots (`screenshot.png`) en Playwright-traces (`trace.zip`) bevatten persoonsgegevens
(naam, reizen, kaartnummer, IBAN) die niet betrouwbaar te redigeren zijn. Ze staan hier bewust niet
in. Deel ze alleen als je ze eerst handmatig hebt onleesbaar gemaakt.

## Overgeslagen bestanden ({count})

{skipped}
"""


def redact_tree(src: Path, dst: Path, *, extra_terms: Iterable[str] = ()) -> list[str]:
    """Copy ``src`` to ``dst``, redacting text files. Returns the skipped files (relative paths).

    ``extra_terms`` (e.g. the holder names from the accounts table) are replaced by ``[NAME]``.
    Screenshots, traces and every non-text file are skipped, and listed in ``dst/README.md``.
    """
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        raise FileNotFoundError(f"{src} does not exist; run `treinvertraging calibrate` first")
    if dst.resolve() == src.resolve() or src.resolve() in dst.resolve().parents:
        raise ValueError("the output directory must be outside the calibration directory")
    terms = sorted({t.strip() for t in extra_terms if len(t.strip()) >= 3}, key=len, reverse=True)
    term_rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE) if terms else None
    skipped: list[str] = []
    dst.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            skipped.append(str(rel))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped.append(str(rel))
            continue
        text = redact(text)
        if term_rx is not None:
            text = term_rx.sub("[NAME]", text)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (dst / "README.md").write_text(
        README_TEXT.format(
            names=", opgegeven namen" if terms else "",
            count=len(skipped),
            skipped="\n".join(f"- `{p}`" for p in skipped) or "(geen)",
        ),
        encoding="utf-8",
    )
    return skipped


def account_name_terms() -> list[str]:
    """Holder names / login names / labels of the stored accounts, for ``redact_tree(extra_terms=...)``."""
    settings = get_settings()
    if not settings.db_path.exists():
        return []
    from sqlalchemy import select

    from treinvertraging.db import session_scope
    from treinvertraging.models import Account

    terms: list[str] = []
    with session_scope() as s:
        for a in s.scalars(select(Account)):
            name = (a.holder_name or "").strip()
            terms += [name, *[w for w in name.split() if len(w) >= 4]]
    return [t for t in terms if t]
