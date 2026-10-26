"""Public NS claim form submitter (``gtbv-formulier.ns.nl``, channel ``gtbv``).

The form is a Mendix SPA without login. We drive it with semantic locators only (labels, roles,
visible text; never ``name``/``id``, which carry generated suffixes) and wait for the Mendix client
to settle after every action via ``recorder.wait_idle(r"/xas/")``.

Flow
1. open the form, reject cookies (OneTrust), pick the OV-chipkaart radio, type the 16-digit card
   number into the grouped inputs, click "Naar reisgegevens" (VERIFIED, see ``gtbv.yaml``);
2. a generic *step engine* driven by ``steps:`` in ``gtbv.yaml`` (UNVERIFIED defaults);
3. the final submit button. ``dry_run`` stops right before it.

Error semantics (important for the pipeline's channel fallback):

* exceptions (``InvalidInput``, ``LayoutChanged``, ``BlockedByBotProtection``, ``TakeoverTimeout``)
  are only raised **before** the final submit button was clicked, so the claim provably was not sent;
* once the final button was clicked, problems are returned as ``SubmitResult(ok=False,
  reached_final_submit=True)`` and never raised, so the caller does not retry blindly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from treinvertraging import selectors
from treinvertraging.adapters.base import ClaimantInfo
from treinvertraging.browser.debug import Bundle, redact
from treinvertraging.browser.human import dismiss_cookie_banner, human_click, human_type
from treinvertraging.browser.runtime import BrowserRuntime, BrowserSession, get_runtime
from treinvertraging.domain import DelayAssessment, SubmitResult, TripDTO
from treinvertraging.errors import (
    BlockedByBotProtection,
    InvalidInput,
    LayoutChanged,
)
from treinvertraging.stations import get_station_index

log = logging.getLogger(__name__)

SITE = "gtbv"
CHANNEL = "gtbv"
DEFAULT_URL = "https://gtbv-formulier.ns.nl/ns/klantenservice-geld-terug-bij-vertraging"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")

_DEFAULT_TIMEOUTS = {"load_s": 45.0, "step_s": 25.0, "field_s": 10.0, "success_s": 60.0, "idle_s": 15.0}
_MAX_TAKEOVERS = 3
_POLL_S = 0.25
_ERROR_POLLS = 3  # an error text must be visible in this many consecutive polls
_KINDS = {"text", "date", "time", "select", "autocomplete", "radio", "checkbox"}


# ---------------------------------------------------------------------------------------------
# context for ``value_from``
# ---------------------------------------------------------------------------------------------


def _station_name(code: str | None) -> str | None:
    if not code:
        return None
    return get_station_index().name_for(code) or code


def _spaced(text: str, n: int = 4) -> str:
    return " ".join(text[i : i + n] for i in range(0, len(text), n))


def build_context(
    trip: TripDTO, who: ClaimantInfo, assessment: DelayAssessment | None = None
) -> dict[str, Any]:
    """Mapping that ``value_from`` paths resolve against: trip, who, assessment, derived."""
    cin = trip.checkin_at.astimezone(AMSTERDAM)
    cout = trip.checkout_at.astimezone(AMSTERDAM) if trip.checkout_at else None
    iban = re.sub(r"\s+", "", who.iban).upper()
    card = re.sub(r"\D", "", who.card_number)
    fare = f"{trip.fare_eur:.2f}".replace(".", ",") if trip.fare_eur is not None else None
    derived: dict[str, Any] = {
        "checkin_date_nl": cin.strftime("%d-%m-%Y"),
        "checkin_date_iso": cin.date().isoformat(),
        "checkin_time": cin.strftime("%H:%M"),
        "checkout_date_nl": cout.strftime("%d-%m-%Y") if cout else None,
        "checkout_time": cout.strftime("%H:%M") if cout else None,
        "from_station_code": trip.from_station,
        "to_station_code": trip.to_station,
        "from_station_name": _station_name(trip.from_station),
        "to_station_name": _station_name(trip.to_station),
        "iban_compact": iban,
        "iban_spaced": _spaced(iban),
        "card_number": card,
        "today_nl": datetime.now(AMSTERDAM).strftime("%d-%m-%Y"),
        "delay_minutes": assessment.arrival_delay_min if assessment else None,
        "fare_eur": fare,
        "product": trip.product,
    }
    return {"trip": trip, "who": who, "assessment": assessment, "derived": derived}


def resolve_path(ctx: dict[str, Any], path: str) -> Any:
    """Walk a dotted path through dicts / attributes. Returns ``None`` when anything is missing."""
    node: Any = ctx
    for part in path.split("."):
        if part.startswith("_") or node is None:
            return None
        node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
    return node


# ---------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------


@dataclass
class FieldSpec:
    label: str
    kind: str = "text"
    value_from: str | None = None
    value: Any = None
    has_value: bool = False
    optional: bool = False
    exact: bool = False


@dataclass
class StepSpec:
    name: str
    expect_heading: str
    fields: list[FieldSpec] = field(default_factory=list)
    next_button: str | None = None
    final: bool = False


@dataclass
class Config:
    url: str
    reject_selectors: list[str]
    radio_label: str
    card_group_label: str
    card_prefill: str
    start_next_button: str
    start_error_regex: str
    steps: list[StepSpec]
    final_submit_button: str
    success_regex: str
    reference_regex: str
    error_regex: str
    blocked_regex: str
    challenge_iframe: str
    busy_selector: str
    timeouts: dict[str, float]
    typing_delay_ms: tuple[float, float]
    settle_quiet_ms: int


def _name(spec: str) -> str | re.Pattern[str]:
    """Label/button spec -> Playwright name: plain substring, or a regex with a ``re:`` prefix."""
    return re.compile(spec[3:], re.I) if spec.startswith("re:") else spec


def _parse_step(raw: dict[str, Any], index: int) -> StepSpec:
    fields = []
    for f in raw.get("fields") or []:
        kind = f.get("kind", "text")
        if kind not in _KINDS:
            raise LayoutChanged(f"gtbv.yaml steps[{index}]: unknown field kind {kind!r}")
        fields.append(
            FieldSpec(
                label=f["label"],
                kind=kind,
                value_from=f.get("value_from"),
                value=f.get("value"),
                has_value="value" in f,
                optional=bool(f.get("optional", False)),
                exact=bool(f.get("exact", False)),
            )
        )
    heading = raw.get("expect_heading")
    if not heading:
        raise LayoutChanged(f"gtbv.yaml steps[{index}]: expect_heading is required")
    return StepSpec(
        name=raw.get("name") or f"step{index + 1}",
        expect_heading=heading,
        fields=fields,
        next_button=raw.get("next_button"),
        final=bool(raw.get("final", False)),
    )


def load_config(
    *,
    url: str | None = None,
    timeouts: dict[str, float] | None = None,
    typing_delay_ms: tuple[float, float] | None = None,
    settle_quiet_ms: int | None = None,
) -> Config:
    g = lambda key, default=None: selectors.get(SITE, key, default)  # noqa: E731
    steps = [_parse_step(s, i) for i, s in enumerate(g("steps", []) or [])]
    if not steps:
        raise LayoutChanged("gtbv.yaml: no steps configured")
    delay = typing_delay_ms or tuple(g("timing.typing_delay_ms", [45, 140]))  # type: ignore[arg-type]
    start_texts = [re.escape(t) for t in g("start.error_texts", []) or []]
    start_rx = "|".join([*start_texts, g("start.error_regex", "ongeldig")])
    return Config(
        url=url or g("url", DEFAULT_URL),
        reject_selectors=list(g("cookie.reject_selectors", ["#onetrust-reject-all-handler"])),
        radio_label=g("start.radio_label", "OV-chipkaart / NS-Business Card"),
        card_group_label=g("start.card_group_label", "Je OV-chipkaartnummer / NS-Business Card"),
        card_prefill=str(g("start.card_prefill", "3528")),
        start_next_button=g("start.next_button", "Naar reisgegevens"),
        start_error_regex=start_rx,
        steps=steps,
        final_submit_button=g("final_submit_button", "re:versturen|indienen"),
        success_regex=g("success_regex", r"(bedankt|ontvangen|aanvraag is verstuurd)"),
        reference_regex=g("reference_regex", ""),
        error_regex=g("error_regex", "ongeldig|niet geldig|onjuist|is verplicht"),
        blocked_regex=g("blocked_regex", "access denied|unusual traffic"),
        challenge_iframe=g("challenge_iframe", 'iframe[title*="recaptcha challenge" i]'),
        busy_selector=g("busy_selector", ".mx-progress"),
        timeouts={**_DEFAULT_TIMEOUTS, **(g("timeouts", {}) or {}), **(timeouts or {})},
        typing_delay_ms=(float(delay[0]), float(delay[1])),
        settle_quiet_ms=int(
            settle_quiet_ms if settle_quiet_ms is not None else g("timing.settle_quiet_ms", 500)
        ),
    )


# ---------------------------------------------------------------------------------------------
# the submitter
# ---------------------------------------------------------------------------------------------


class GtbvSubmitter:
    """``ClaimSubmitter`` for the public claim form."""

    channel = CHANNEL

    def __init__(
        self,
        *,
        runtime: BrowserRuntime | None = None,
        url: str | None = None,
        timeouts: dict[str, float] | None = None,
        typing_delay_ms: tuple[float, float] | None = None,
        settle_quiet_ms: int | None = None,
    ) -> None:
        self._runtime = runtime
        self._url = url
        self._timeouts = timeouts
        self._typing = typing_delay_ms
        self._quiet = settle_quiet_ms

    async def submit_claim(
        self,
        trip: TripDTO,
        who: ClaimantInfo,
        *,
        dry_run: bool,
        assessment: DelayAssessment | None = None,
    ) -> SubmitResult:
        card = re.sub(r"[\s-]", "", who.card_number)
        if not re.fullmatch(r"\d{16}", card):
            raise InvalidInput("OV-chipkaartnummer moet uit 16 cijfers bestaan")
        if who.ticket_type != "ov_chipkaart":
            raise InvalidInput(f"ticket_type {who.ticket_type!r} wordt niet ondersteund")
        cfg = load_config(
            url=self._url,
            timeouts=self._timeouts,
            typing_delay_ms=self._typing,
            settle_quiet_ms=self._quiet,
        )
        ctx = build_context(trip, who, assessment)
        rt = self._runtime or get_runtime()
        async with rt.ephemeral_session() as session:
            run = _Run(cfg, session, card, ctx, dry_run=dry_run)
            async with session.debug_bundle("gtbv-submit") as bundle:
                try:
                    result = await run.execute(bundle)
                except PlaywrightError as exc:
                    raise LayoutChanged(f"formulier reageert niet zoals verwacht: {_short(exc)}") from exc
                if result.dry_run or run.reached_final:
                    bundle.keep("dry_run" if result.dry_run else "submitted")
                    path = await bundle.write()
                    result.screenshot = str(path / "screenshot.png")
            return result


def _short(exc: BaseException) -> str:
    return redact(str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__)[:300]


class _Run:
    """State of one submission attempt (one browser page)."""

    def __init__(
        self, cfg: Config, session: BrowserSession, card: str, ctx: dict[str, Any], *, dry_run: bool
    ) -> None:
        self.cfg = cfg
        self.session = session
        self.page: Page = session.page
        self.card = card
        self.ctx = ctx
        self.dry_run = dry_run
        self.reached_final = False
        self._takeovers = 0

    # -- small helpers -------------------------------------------------------------------------

    def _t(self, key: str) -> float:
        return self.cfg.timeouts[key]

    def _ms(self, key: str) -> float:
        return self.cfg.timeouts[key] * 1000

    async def _sleep(self) -> None:
        await asyncio.sleep(_POLL_S)

    async def _type(self, loc: Locator, text: str) -> None:
        lo, hi = self.cfg.typing_delay_ms
        await human_type(loc, text, min_delay_ms=lo, max_delay_ms=hi)

    async def _is_visible(self, loc: Locator) -> bool:
        try:
            return await loc.first.is_visible()
        except PlaywrightError:
            return False

    async def _poll(self, check: Callable[[], Awaitable[Any]], timeout_s: float) -> Any:
        """Call ``check`` until it returns something truthy; ``None`` on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            value = await check()
            if value:
                return value
            if loop.time() >= deadline:
                return None
            await self._sleep()

    # -- settling & challenge --------------------------------------------------------------------

    async def _settle(self) -> None:
        """Wait for Mendix to go quiet (no pending ``POST /xas/``), then for the busy overlay."""
        try:
            await self.session.recorder.wait_idle(
                r"/xas/", quiet_ms=self.cfg.settle_quiet_ms, timeout=self._t("idle_s")
            )
        except TimeoutError:
            log.warning("gtbv: /xas/ requests still pending after %.0fs", self._t("idle_s"))
        if self.cfg.busy_selector:
            try:
                await self.page.locator(self.cfg.busy_selector).first.wait_for(state="hidden", timeout=3_000)
            except PlaywrightError:
                pass
        await self._check_challenge()

    async def _check_challenge(self) -> None:
        """A visible reCAPTCHA challenge needs a human: request takeover and continue afterwards."""
        if not self.cfg.challenge_iframe or self._takeovers >= _MAX_TAKEOVERS:
            return
        if await self._is_visible(self.page.locator(self.cfg.challenge_iframe)):
            self._takeovers += 1
            log.warning("gtbv: reCAPTCHA challenge visible, requesting takeover")
            await self.session.takeover(
                "reCAPTCHA-uitdaging op het NS-claimformulier: los hem op in de browser"
            )
            await self._settle()

    async def _guard_blocked(self) -> None:
        body = await self._body_text()
        if self.cfg.blocked_regex and re.search(self.cfg.blocked_regex, body, re.I):
            raise BlockedByBotProtection("het formulier toont een bot-beveiligingspagina")

    async def _body_text(self) -> str:
        try:
            return await self.page.locator("body").inner_text(timeout=5_000)
        except PlaywrightError:
            return ""

    async def _visible_error(self, regex: str) -> str | None:
        loc = self.page.get_by_text(re.compile(regex, re.I))
        try:
            for i in range(min(await loc.count(), 5)):
                item = loc.nth(i)
                if await item.is_visible():
                    return redact((await item.inner_text()).strip())[:200]
        except PlaywrightError:
            return None
        return None

    # -- steps ---------------------------------------------------------------------------------

    def _heading(self, step: StepSpec) -> Locator:
        return self.page.get_by_role("heading", name=re.compile(step.expect_heading, re.I))

    async def _visible_step(self) -> int | None:
        for i, step in enumerate(self.cfg.steps):
            if await self._is_visible(self._heading(step)):
                return i
        return None

    async def _wait_transition(self, current: int | None) -> int:
        """Wait until a configured step other than ``current`` is on screen; return its index.

        ``InvalidInput`` when the site keeps showing a validation error instead, ``LayoutChanged``
        (no known heading) otherwise.
        """
        regex = self.cfg.start_error_regex if current is None else self.cfg.error_regex
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._t("step_s")
        error_polls = 0
        error_text: str | None = None
        while True:
            await self._check_challenge()
            idx = await self._visible_step()
            if idx is not None and idx != current:
                return idx
            if current is None or idx == current:
                error_text = await self._visible_error(regex)
                error_polls = error_polls + 1 if error_text else 0
                if error_polls >= _ERROR_POLLS:
                    raise InvalidInput(f"het formulier weigert de invoer: {error_text}")
            if loop.time() >= deadline:
                break
            await self._sleep()
        await self._guard_blocked()
        headings = [redact(h.strip()) for h in await self.page.get_by_role("heading").all_inner_texts()]
        raise LayoutChanged(f"onbekende formulierstap, zichtbare koppen: {headings[:6]}")

    # -- execute -------------------------------------------------------------------------------

    async def execute(self, bundle: Bundle) -> SubmitResult:
        cfg, page = self.cfg, self.page
        resp = await page.goto(cfg.url, wait_until="domcontentloaded", timeout=self._ms("load_s"))
        if resp is not None and resp.status in (403, 429):
            raise BlockedByBotProtection(f"formulier gaf HTTP {resp.status}")
        if resp is not None and resp.status >= 400:
            raise LayoutChanged(f"formulier gaf HTTP {resp.status}")
        await dismiss_cookie_banner(page, reject=True, selectors=cfg.reject_selectors, timeout=5_000)
        await self._settle()

        await self._start_step()
        idx = await self._wait_transition(None)
        for _ in range(len(cfg.steps) + 2):
            step = cfg.steps[idx]
            bundle.note(f"step_{step.name}", "seen")
            await self._fill_step(step)
            if step.final:
                return await self._final(step, bundle)
            if not step.next_button:
                raise LayoutChanged(f"stap {step.name!r} heeft geen next_button en is niet final")
            await self._click_button(step.next_button)
            await self._settle()
            idx = await self._wait_transition(idx)
        raise LayoutChanged("de laatste stap (final) werd niet bereikt")

    # -- step 1: Vervoerbewijs (VERIFIED) ----------------------------------------------------------

    async def _start_step(self) -> None:
        cfg, page = self.cfg, self.page
        radio = page.get_by_role("radio", name=_name(cfg.radio_label)).first
        try:
            await radio.wait_for(state="visible", timeout=self._ms("field_s") * 2)
        except PlaywrightError:
            await self._guard_blocked()
            raise LayoutChanged(f"radio {cfg.radio_label!r} niet gevonden") from None
        await self._check_radio(radio, cfg.radio_label)
        await self._settle()
        await self._fill_card()
        await self._settle()
        await self._click_button(cfg.start_next_button)
        await self._settle()

    async def _check_radio(self, radio: Locator, label: str) -> None:
        if await radio.is_checked():
            return
        try:
            await human_click(radio, timeout=5_000)
        except PlaywrightError:
            await self.page.get_by_text(label, exact=True).first.click(timeout=5_000)
        if not await radio.is_checked():
            await radio.check(force=True, timeout=5_000)

    async def _card_group(self) -> Locator:
        label = self.cfg.card_group_label
        rx = re.compile(re.escape(label), re.I)
        by_role = self.page.get_by_role("group", name=rx)
        try:
            if await by_role.count() and await by_role.first.locator("input:visible").count():
                return by_role.first
        except PlaywrightError:
            pass
        # innermost container that holds both the label text and input(s)
        container = (
            self.page.locator("div, fieldset, section, form")
            .filter(has_text=rx)
            .filter(has=self.page.locator("input:visible"))
            .last
        )
        try:
            await container.wait_for(state="visible", timeout=self._ms("field_s"))
        except PlaywrightError:
            raise LayoutChanged(f"kaartnummer-groep {label!r} niet gevonden") from None
        return container

    async def _fill_card(self) -> None:
        group = await self._card_group()
        inputs = group.locator("input:visible")
        n = await inputs.count()
        all_inputs = [inputs.nth(i) for i in range(n)]
        tel = [loc for loc in all_inputs if (await loc.get_attribute("type") or "") == "tel"]
        if len(tel) == 4:
            chosen = tel
        elif n == 4:
            chosen = all_inputs
        elif n == 1:
            chosen = all_inputs
        elif n > 4:
            chosen = all_inputs[-4:]
        else:
            raise LayoutChanged(f"onverwacht aantal kaartnummer-velden: {n}")
        chunks = [self.card] if len(chosen) == 1 else [self.card[i * 4 : i * 4 + 4] for i in range(4)]
        for loc, chunk in zip(chosen, chunks, strict=True):
            current = re.sub(r"\D", "", await loc.input_value())
            if current != chunk:
                await self._type(loc, chunk)
        values = [re.sub(r"\D", "", await loc.input_value()) for loc in chosen]
        got = "".join(values)
        if got != self.card:
            raise LayoutChanged("kaartnummer-velden bevatten na invullen niet het verwachte nummer")

    # -- generic fields ------------------------------------------------------------------------

    async def _click_button(self, spec: str) -> None:
        btn = self.page.get_by_role("button", name=_name(spec)).first
        try:
            await btn.wait_for(state="visible", timeout=self._ms("field_s"))
        except PlaywrightError:
            raise LayoutChanged(f"knop {spec!r} niet gevonden") from None
        await human_click(btn)

    async def _fill_step(self, step: StepSpec) -> None:
        for spec in step.fields:
            if spec.has_value:
                value = spec.value
            else:
                value = resolve_path(self.ctx, spec.value_from) if spec.value_from else None
                if value is None and spec.kind == "checkbox" and not spec.value_from:
                    value = True
            if value is None or value == "":
                if spec.optional:
                    continue
                raise InvalidInput(f"geen waarde voor veld {spec.label!r} ({spec.value_from})")
            try:
                await self._fill_field(spec, value)
            except PlaywrightError as exc:
                if spec.optional and not await self._is_visible(self.page.get_by_label(_name(spec.label))):
                    continue
                raise LayoutChanged(
                    f"veld {spec.label!r} (stap {step.name}) niet te bedienen: {_short(exc)}"
                ) from exc
            await self._settle()

    def _by_label(self, spec: FieldSpec) -> Locator:
        name = _name(spec.label)
        if spec.exact and isinstance(name, str):
            return self.page.get_by_label(name, exact=True).first
        return self.page.get_by_label(name).first

    async def _fill_field(self, spec: FieldSpec, value: Any) -> None:
        kind = spec.kind
        if kind == "checkbox":
            await self._set_checkbox(spec, bool(value))
            return
        if kind == "radio":
            await self._pick_radio(spec, str(value))
            return
        loc = self._by_label(spec)
        await loc.wait_for(state="visible", timeout=self._ms("field_s"))
        text = str(value)
        if kind == "select":
            await self._select(loc, text)
        elif kind == "autocomplete":
            await self._autocomplete(loc, text)
        else:
            await self._type_value(loc, text, kind)

    async def _type_value(self, loc: Locator, text: str, kind: str) -> None:
        itype = (await loc.get_attribute("type") or "").lower()
        if kind == "date" and itype == "date":
            m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", text)
            await loc.fill(f"{m[3]}-{m[2]}-{m[1]}" if m else text)
        elif kind == "time" and itype == "time":
            await loc.fill(text)
        else:
            await self._type(loc, text)
        await self.page.keyboard.press("Tab")  # Mendix commits on blur

    async def _select(self, loc: Locator, text: str) -> None:
        tag = (await loc.evaluate("e => e.tagName")).upper()
        if tag == "SELECT":
            try:
                await loc.select_option(label=text, timeout=5_000)
            except PlaywrightError:
                await loc.select_option(value=text, timeout=5_000)
            return
        await human_click(loc)
        option = self.page.get_by_role("option", name=re.compile(re.escape(text), re.I)).first
        await option.wait_for(state="visible", timeout=self._ms("field_s"))
        await human_click(option)

    async def _autocomplete(self, loc: Locator, text: str) -> None:
        """Type ``text``, wait for a listbox option with a matching name and click it."""
        esc = re.escape(text)
        attempts = [text]
        first_word = text.split()[0] if text.split() else text
        if first_word != text:
            attempts.append(first_word)
        for attempt in attempts:
            await self._type(loc, attempt)
            await self._settle()
            exact = self.page.get_by_role("option", name=re.compile(rf"^\s*{esc}\s*$", re.I))
            partial = self.page.get_by_role("option", name=re.compile(esc, re.I))

            async def find(exact: Locator = exact, partial: Locator = partial) -> Locator | None:
                for cand in (exact, partial):
                    if await self._is_visible(cand):
                        return cand.first
                return None

            option = await self._poll(find, self._t("field_s") / len(attempts))
            if option is not None:
                await human_click(option)
                return
        raise LayoutChanged(f"geen suggestie gevonden voor autocomplete-veld {redact(text)!r}")

    async def _set_checkbox(self, spec: FieldSpec, want: bool) -> None:
        box = self.page.get_by_role("checkbox", name=_name(spec.label)).first
        if not await self._is_visible(box):
            box = self._by_label(spec)
        await box.wait_for(state="attached", timeout=self._ms("field_s"))
        if await box.is_checked() == want:
            return
        try:
            await human_click(box, timeout=5_000)
        except PlaywrightError:
            pass
        if await box.is_checked() != want:
            if await box.is_visible():
                await self.page.get_by_text(spec.label.removeprefix("re:"), exact=False).first.click(
                    timeout=3_000
                )
            if await box.is_checked() != want:
                await box.set_checked(want, force=True, timeout=5_000)

    async def _pick_radio(self, spec: FieldSpec, option: str) -> None:
        scope: Page | Locator = self.page
        group = self.page.get_by_role("group", name=_name(spec.label)).first
        if spec.label and await self._is_visible(group):
            scope = group
        radio = scope.get_by_role("radio", name=_name(option)).first
        await radio.wait_for(state="visible", timeout=self._ms("field_s"))
        await self._check_radio(radio, option)

    # -- final ---------------------------------------------------------------------------------

    async def _final(self, step: StepSpec, bundle: Bundle) -> SubmitResult:
        cfg = self.cfg
        btn = self.page.get_by_role("button", name=_name(cfg.final_submit_button)).first
        try:
            await btn.wait_for(state="visible", timeout=self._ms("field_s"))
        except PlaywrightError:
            raise LayoutChanged(f"knop {cfg.final_submit_button!r} niet gevonden") from None
        if not await btn.is_enabled():
            raise LayoutChanged("de verzendknop is uitgeschakeld (ontbrekende invoer?)")
        if self.dry_run:
            return SubmitResult(
                ok=True,
                reference=None,
                screenshot=None,
                message="dry-run: alle stappen ingevuld, niet verstuurd",
                channel=CHANNEL,
                reached_final_submit=False,
                dry_run=True,
            )
        # From here on nothing is raised: the caller must know the claim may have been sent.
        self.reached_final = True
        try:
            await human_click(btn)
            await self._settle()
            return await self._await_confirmation(step)
        except Exception as exc:  # noqa: BLE001
            log.exception("gtbv: error after the final submit click")
            bundle.note("post_submit_error", _short(exc))
            return SubmitResult(
                ok=False,
                reference=None,
                screenshot=None,
                message=f"fout na het versturen, status onbekend: {type(exc).__name__}: {_short(exc)}",
                channel=CHANNEL,
                reached_final_submit=True,
            )

    async def _await_confirmation(self, step: StepSpec) -> SubmitResult:
        cfg = self.cfg
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._t("success_s")
        error_polls = 0
        error_text: str | None = None
        while True:
            await self._check_challenge()
            body = await self._body_text()
            gone = not await self._is_visible(self._heading(step))
            if gone and re.search(cfg.success_regex, body, re.I):
                ref = None
                if cfg.reference_regex:
                    m = re.search(cfg.reference_regex, body, re.I)
                    ref = m.group(1) if m else None
                return SubmitResult(
                    ok=True,
                    reference=ref,
                    screenshot=None,
                    message="aanvraag verstuurd",
                    channel=CHANNEL,
                    reached_final_submit=True,
                )
            if not gone:
                error_text = await self._visible_error(cfg.error_regex)
                error_polls = error_polls + 1 if error_text else 0
                if error_polls >= _ERROR_POLLS:
                    return SubmitResult(
                        ok=False,
                        reference=None,
                        screenshot=None,
                        message=f"het formulier meldt een fout na versturen: {error_text}",
                        channel=CHANNEL,
                        reached_final_submit=True,
                    )
            if loop.time() >= deadline:
                return SubmitResult(
                    ok=False,
                    reference=None,
                    screenshot=None,
                    message="geen bevestigingspagina gezien na versturen, status onbekend",
                    channel=CHANNEL,
                    reached_final_submit=True,
                )
            await asyncio.sleep(0.3)


__all__ = ["GtbvSubmitter", "build_context", "load_config", "resolve_path"]
