# Treinvertraging: self-hosted "geld terug bij vertraging" bot

A self-hosted version of what trein-vertraging.nl does, for one household. It runs as a
Docker container on a Proxmox home server. It reads your train trips from **Mijn NS**
and the **OV-chipkaart** site, works out which trips qualify for NS delay compensation,
**submits the claims automatically** through NS's public claim form
(`gtbv-formulier.ns.nl`, no login needed), and notifies you.

No fees and no third party holding your passwords. It costs the electricity of the box
it runs on.

---

## 0. Ground truth and constraints

These findings come from recon on 2026-10-26 and shape the whole design.

| Fact | Consequence |
|---|---|
| Mijn NS login runs through `www.ns.nl/mijnns` → `loginapi.ns.nl/oauth/authorize` (client `mijnns-code-production`, SAML hop) → `login.ns.nl/login`, a form with `email`, `password`, a `remember` checkbox and a hidden `csrfToken`. | Login can be automated, but see the next row. |
| Both `login.ns.nl` and `loginapi.ns.nl` post to obfuscated paths (`/vtnf/...`, `/__1Jo/...`). These are **Akamai Bot Manager** sensor endpoints. | Headless Chromium is likely to be flagged. Run **headed Chromium inside Xvfb**, keep a **persistent profile**, log in rarely, keep request volume low, and keep a human-takeover fallback. |
| OV-chipkaart: `www.ov-chipkaart.nl` (Next.js + NextAuth) → `login.ov-chipkaart.nl` (**WSO2 Identity Server** OIDC, `BasicAuthenticator`). From a datacenter IP with headless Chromium the login page came back as a 583-byte empty shell, which looks like WAF or bot blocking. | Same mitigations. It will probably behave better from a residential IP (your home). Calibration (milestone M6) must verify this. |
| Rijden de Treinen publishes a CC BY 4.0 archive of every NL train service with per-stop delays: `https://opendata.rijdendetreinen.nl/public/services/services-YYYY-MM.csv.gz`, refreshed monthly. Columns: `Service:RDT-ID, Service:Date, Service:Type, Service:Company, Service:Train number, Service:Completely cancelled, Service:Partly cancelled, Service:Maximum delay, Stop:RDT-ID, Stop:Station code, Stop:Station name, Stop:Arrival time, Stop:Arrival delay, Stop:Arrival cancelled, Stop:Departure time, Stop:Departure delay, Stop:Departure cancelled, Stop:Platform change, Stop:Planned platform, Stop:Actual platform`. | Delays can be verified independently of NS's own flagging. |
| The NS API portal (`gateway.apiportal.ns.nl/reisinformatie-api/...`) is free with a subscription key and has realtime arrivals and departures, but not history. | The app keeps its own recent-arrivals archive for the stations you travel to, which covers the weeks before RDT publishes the month. |
| NS rule (domestic, per public sources): ≥30 min late at the final destination gives 50% of the single fare, ≥60 min gives 100%. Claims must be filed within **3 months**. Excluded: amounts under €2.20, announced engineering works, not checked in, force majeure. Subscriptions are refunded differently. | Rules live in `config/rules.yaml`, not in code, so they can be corrected. **Verify against ns.nl during M6.** |
| **NS's public claim form** `https://gtbv-formulier.ns.nl/ns/klantenservice-geld-terug-bij-vertraging` (it redirects to `/`) needs **no login**. It is a **Mendix** app: the client talks JSON-RPC to `POST /xas/`, and loading the page creates a `Claim` object with `ClaimStatus="Concept"`. Step 1 is "Vervoerbewijs": a radio "Waarmee heb je gereisd?" with the options `OV-chipkaart / NS-Business Card`, `Eenmalige chipkaart`, `E-ticket of mobiel ticket`, `Bankpas / Creditcard` and `OV-pas`. Choosing OV-chipkaart shows "Je OV-chipkaartnummer / NS-Business Card" as 4 groups of 4 digits (prefilled `3528`), with the error "Vul een OV-chipkaartnummer in.", and a button **"Naar reisgegevens"**. The page loads **reCAPTCHA Enterprise** (invisible, score-based, key `6LfxEUstAAAAAOkBikYzFCTasulEe8H3cahWlxwO`). Mendix input `name` attributes carry generated suffixes (`..._erh_11`), so **they are not stable**. The steps after the card number were *not* explored, to avoid sending data to NS; calibration covers them. | This is the **primary submission channel**: no Akamai-protected login is needed to file a claim. Drive it with **UI automation via labels and roles** (Mendix ids and names change per deploy). Replaying the `/xas/` API is *not* attempted, because of the reCAPTCHA token and Mendix state. A good reCAPTCHA score needs headed Chromium, a persistent profile and a residential IP, and takeover is the fallback when a challenge appears. |
| When Mijn NS is logged in with a linked card, it shows travel history and offers a **pre-filled "geld terug" form** for eligible trips. | Mijn NS is the primary **trip source**, and its prefilled form is the **fallback submission channel**. The OV-chip adapter is the second trip source, and it also provides the card number the public form needs. |

> **Honest caveat:** the build agents have **no access to your accounts**. The parts behind
> login (the travel-history pages and the claim form) are built against best-guess structure
> plus recorded fixtures. Milestone **M6, Calibration** is where you run the built-in
> recorder once on your own server. It captures the real pages and API calls, and a
> follow-up Claude session then locks in the selectors and endpoints. The plan is designed
> around that step, not around hoping the first guess is right.

---

## 1. Making "fully automatic" robust

1. **Strategy ladder per action.** Every adapter action (`login`, `list_trips`,
   `list_claimable`, `submit_claim`, `list_submitted_claims`) is implemented as an ordered
   list of strategies:
   1. **API replay**: call the site's own JSON endpoints, which the recorder discovers, using
      the cookies and tokens of the logged-in browser context (`context.request`). This is
      fast and structured, and it survives visual redesigns.
   2. **UI automation** with *semantic* locators: `get_by_role` and `get_by_label` with Dutch
      text, never brittle CSS. All locators live in `config/selectors.yaml`, a mounted volume,
      so they can be fixed without a rebuild.
   3. **Human takeover**: pause, notify you with a link to the live browser (noVNC on port
      6080), let you click through, then resume. This is the last rung, never a silent failure.
2. **Network sniffing over DOM scraping.** While pages load, a recorder captures every
   JSON XHR or fetch response. Trip data is parsed from that JSON wherever possible.
3. **Look like a person.** Headed Chromium in Xvfb, a persistent profile per account,
   `nl-NL` locale, `Europe/Amsterdam` timezone, realistic viewport, and jittered waits.
   It runs once a day at a random time between 07:00 and 22:00, plus a light session
   keep-alive. With "remember me" checked it rarely has to log in again.
4. **Automatic 2FA and verification codes.** An `OtpProvider` interface with:
   (a) **IMAP**, which reads the latest NS or OV verification mail from a mailbox using an
   app password and extracts the code with a regex; (b) **manual**, which prompts in the
   web UI and in the notification and waits for you to enter the code.
5. **Never double-submit.** Each trip has a deterministic key
   `sha256(source|card_id|checkin_ts|from_station)`, and claims follow a strict state
   machine. The claim is set to `submitting` before the click and confirmed afterwards by
   **re-reading "mijn aanvragen"** in Mijn NS, or by the **confirmation e-mail** NS sends
   after the public form, which the IMAP reader picks up. If a run crashes mid-submit, the
   next run reconciles instead of retrying blindly.
5b. **Two submission channels.** The primary channel is the public form
   (`gtbv-formulier.ns.nl`), which needs no login and so is not exposed to the NS login's
   bot detection. The secondary channel is the prefilled form inside Mijn NS. When one
   fails with `BlockedByBotProtection` or `LayoutChanged`, the other is tried before falling
   back to human takeover.
6. **Evidence and debug bundles.** Every submission and every failure saves a screenshot,
   page HTML, captured JSON and a trace (`playwright trace`) to
   `/data/debug/<timestamp>-<action>/`. That bundle is everything a later Claude session
   needs to fix a broken selector.
7. **Daily canary.** A read-only smoke run (login + list trips). If the site changed, you
   are alerted with the debug bundle *before* a claim deadline is missed.
8. **Approval period.** The `CLAIM_MODE` setting can be `dry_run`, `approve` or `auto`.
   Start with `approve`: each claim needs a one-tap approval in the UI. After N successful
   claims, switch to `auto`.
9. **Deadline watchdog.** Any eligible trip less than 14 days from its 3-month deadline
   that is not yet `confirmed` triggers a loud alert.
10. **Independent verification.** The delay is computed from RDT and NS API data, not
    just trusted from NS's own flag. It catches trips that NS did not flag, and records
    evidence if NS rejects a claim.

---

## 2. Architecture

```
┌──────────────────────── docker container "treinvertraging" ────────────────────────┐
│ supervisord                                                                         │
│  ├─ Xvfb :99  +  x11vnc  +  noVNC (websockify :6080)   ← watch / take over browser   │
│  └─ app (python -m treinvertraging serve)                                            │
│       ├─ FastAPI + Jinja2 + htmx dashboard  (:8080)                                  │
│       ├─ APScheduler jobs: sync · assess · claim · reconcile · canary · archive     │
│       ├─ Browser runtime (Playwright, persistent contexts in /data/profiles)        │
│       ├─ Adapters: ns (Mijn NS) · ovchip (ov-chipkaart.nl) · gtbv (public form)     │
│       ├─ Delay data: RDT importer · NS API arrivals archiver · matcher              │
│       ├─ Rules engine (config/rules.yaml)                                           │
│       └─ Notifier (Apprise → Telegram / ntfy / e-mail)                              │
│ /data  (volume): app.db (SQLite WAL) · profiles/ · debug/ · rdt-cache/ · secret.key │
│ /config(volume): settings.yaml · rules.yaml · selectors.yaml                         │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

**Stack:** Python 3.12, Playwright for Python (Chromium), FastAPI, Jinja2 + htmx (no JS
build step), SQLAlchemy 2 with SQLite (WAL), Alembic, APScheduler 3, pydantic-settings,
`cryptography` (Fernet), Apprise, httpx, Typer (CLI), pytest, pytest-asyncio and ruff.
The base image is `mcr.microsoft.com/playwright/python:v1.<pinned>-noble`, with Xvfb,
x11vnc, novnc, websockify and supervisor added via apt.

### Repository layout

```
treinvertraging/
├─ pyproject.toml
├─ Dockerfile
├─ docker-compose.yml
├─ docker/supervisord.conf
├─ docker/entrypoint.sh
├─ config.example/{settings.yaml,rules.yaml,selectors.yaml}
├─ .env.example
├─ src/treinvertraging/
│  ├─ __init__.py
│  ├─ __main__.py                 # Typer CLI
│  ├─ config.py                   # pydantic-settings + YAML loading
│  ├─ db.py                       # engine, session, Base
│  ├─ models.py                   # ORM models (see §3)
│  ├─ crypto.py                   # Fernet secret box
│  ├─ domain.py                   # pydantic DTOs + enums shared by all modules
│  ├─ rules.py                    # compensation rules engine (pure)
│  ├─ stations.py                 # station code/name normalisation
│  ├─ delaydata/{rdt.py,nsapi.py,archiver.py,matcher.py}
│  ├─ browser/{runtime.py,recorder.py,human.py,debug.py,takeover.py}
│  ├─ otp/{base.py,imap.py,manual.py}
│  ├─ adapters/{base.py,ns.py,ovchip.py,gtbv.py}
│  ├─ pipeline/{sync.py,assess.py,claim.py,reconcile.py,canary.py,scheduler.py}
│  ├─ notify.py
│  └─ web/{app.py,routes.py,templates/*.html,static/}
├─ alembic/ ...
├─ tests/{unit,fixtures,integration}/
└─ docs/{PROXMOX.md,CALIBRATION.md,OPERATIONS.md}
```

---

## 3. Shared contracts (frozen before agents fan out)

Agent A writes these first (`domain.py`, `models.py`, `adapters/base.py`). All other agents
code against them and do not change them without updating this file.

```python
# domain.py
class Source(StrEnum): NS = "ns"; OVCHIP = "ovchip"
class ClaimState(StrEnum):
    DETECTED="detected"          # trip found, not yet assessed
    INELIGIBLE="ineligible"
    ELIGIBLE="eligible"          # meets rules, waiting for policy (dry_run/approve/auto)
    AWAITING_APPROVAL="awaiting_approval"
    APPROVED="approved"
    SUBMITTING="submitting"      # set BEFORE clicking submit
    SUBMITTED="submitted"        # site confirmed receipt
    CONFIRMED="confirmed"        # seen in "mijn aanvragen"
    PAID="paid"; REJECTED="rejected"
    NEEDS_HUMAN="needs_human"    # ladder exhausted
    EXPIRED="expired"

class TripDTO(BaseModel):
    source: Source
    card_id: str                 # masked card number or NS account id
    checkin_at: datetime         # tz-aware Europe/Amsterdam
    checkout_at: datetime | None
    from_station: str            # canonical station code (e.g. "UT")
    to_station: str | None
    fare_eur: Decimal | None     # amount actually charged
    product: str | None          # e.g. "Dal Voordeel", "Vol tarief"
    operator: str | None         # "NS", "Arriva", ...
    ns_delay_flag: bool | None   # NS itself marks it as delayed/claimable
    ns_claim_ref: str | None     # opaque handle to open NS's prefilled form
    raw: dict                    # original JSON/row for debugging
    def key(self) -> str: ...    # sha256(source|card_id|checkin_at.isoformat()|from_station)

class DelayAssessment(BaseModel):
    trip_key: str
    arrival_delay_min: int | None
    cancelled: bool
    evidence: list[str]          # e.g. ["rdt:2026-09 train 3045 UT→ASD +34", "ns_flag"]
    eligible: bool
    percent: int                 # 0/50/100
    expected_eur: Decimal
    reason: str
    deadline: date

class SubmitResult(BaseModel):
    ok: bool; reference: str | None; screenshot: str | None; message: str
    channel: str                    # "gtbv" | "mijnns"
    reached_final_submit: bool      # True once the final submit button was clicked
    dry_run: bool = False
```

```python
# adapters/base.py
class ClaimantInfo(BaseModel):          # from the account row, decrypted at use
    card_number: str                    # 16 digits, "3528..."
    iban: str; holder_name: str; email: str
    ticket_type: Literal["ov_chipkaart"] = "ov_chipkaart"

class TripSource(Protocol):             # ns.py, ovchip.py
    source: Source
    async def ensure_logged_in(self) -> None: ...
    async def list_trips(self, since: date) -> list[TripDTO]: ...

class ClaimSubmitter(Protocol):         # gtbv.py (primary), ns.py (fallback)
    channel: str                        # "gtbv" | "mijnns"
    async def submit_claim(self, trip: TripDTO, who: ClaimantInfo, *, dry_run: bool) -> SubmitResult: ...

class ClaimStatusReader(Protocol):      # ns.py ("mijn aanvragen"), otp/imap.py (confirmation mails)
    async def list_submitted_claims(self, since: date) -> list[dict]: ...
```
`dry_run=True` must fill every step and stop **before** the final submit button,
saving a debug bundle. This is how the first runs and the calibration exercise the real
form safely.

**ORM tables:** `accounts` (id, source, label, username, password_enc, card_number_enc,
iban_enc, holder_name, email, otp_provider, enabled), `trips` (key PK, account_id, fields from TripDTO,
raw_json, first_seen, last_seen), `assessments` (trip_key FK, fields above, assessed_at),
`claims` (id, trip_key UNIQUE, state, channel, expected_eur, paid_eur, reference, attempts,
last_error, debug_dir, timestamps), `events` (audit log: ts, level, kind, message,
trip_key?), `arrivals` (station, train_number, planned_at, actual_at, delay_min,
cancelled, source) with a UNIQUE(station, train_number, planned_at) constraint,
`settings_kv`.

---

## 4. Work breakdown: agents and milestones

Each work package (WP) is sized for **one Sonnet agent**. WPs in the same wave run in
parallel. Every agent must leave `ruff check` and `pytest` green for its own tests, and
must not edit files owned by another WP (the shared contracts in §3 are owned by WP-A).

### Wave 1: foundation (sequential, one agent)

**WP-A: skeleton and contracts**
- `pyproject.toml` (deps from §2, `[project.scripts] treinvertraging = "treinvertraging.__main__:app"`), ruff config, pytest config.
- `config.py`: settings loaded from env plus `/config/settings.yaml`. Keys: `DATA_DIR`,
  `CONFIG_DIR`, `CLAIM_MODE` (dry_run|approve|auto, default `approve`), `AUTO_AFTER_N_APPROVED`,
  `TIMEZONE`, `NS_API_KEY`, `APPRISE_URLS`, `WEB_USERNAME`, `WEB_PASSWORD`, `PUBLIC_URL`
  (used in notification links), `HEADLESS` (default false), `RUN_WINDOW` ("07:00-22:00").
- `domain.py`, `models.py`, `db.py`, initial Alembic migration, `crypto.py` (Fernet key auto-
  generated at `/data/secret.key` with mode 0600 on first start).
- `adapters/base.py` (Protocol only), `__main__.py` with a Typer app and stub subcommands:
  `serve`, `sync`, `assess`, `claim`, `canary`, `calibrate`, `login`, `account add|list`.
- Unit tests for `TripDTO.key()` stability and crypto roundtrip.
- **Done when:** `pip install -e . && treinvertraging --help` works and the tests pass.

### Wave 2: parallel (five agents)

**WP-B: rules engine and stations** (`rules.py`, `stations.py`, `config.example/rules.yaml`)
- `rules.yaml` schema: thresholds per operator or product (`ns_domestic: [{min:30,pct:50},{min:60,pct:100}]`),
  `min_payout_eur: 2.20`, `deadline_months: 3`, exclusions (`operators_excluded`, `require_checkout`),
  and a `subscription_products` section (percent of a monthly amount, configurable).
  Add a comment marking the numbers *to verify on ns.nl*.
- `assess(trip, delay_min, cancelled, ns_flag) -> DelayAssessment` as a pure function. If
  `ns_flag` is true and there is no independent data, trust NS. A cancelled final leg
  counts as ≥60 minutes unless the rules say otherwise.
- `stations.py`: load the RDT stations dataset (`https://opendata.rijdendetreinen.nl/public/stations/` →
  pick the current CSV), cache it in `/data/rdt-cache`, and map free-text names ("Utrecht Centraal",
  "Utrecht C.") to codes with fuzzy matching (`rapidfuzz`).
- Table-driven tests: about 25 cases covering the thresholds, edge minutes 29/30/59/60, under-€2.20, expired deadline, excluded operator and missing checkout.

**WP-C: delay data** (`delaydata/*`)
- `rdt.py`: download `services-YYYY-MM.csv.gz` (and the yearly file as a fallback), using
  ETag or Last-Modified caching. Stream-parse with `csv` and gzip, keeping only stations in a
  given set, and upsert into `arrivals` (`source="rdt"`).
- `nsapi.py`: an httpx client for `GET /reisinformatie-api/api/v2/arrivals?station=XX`
  with the `Ocp-Apim-Subscription-Key` header. `archiver.py` is a job that polls the arrivals
  for the user's **frequent destination stations** (the top N `to_station`s from `trips`,
  plus config) every 10 minutes and stores the actual times. A light polling budget keeps
  it under the free-tier limits.
- `matcher.py`: given a trip (from, to, checkin, checkout), find the arrival at `to_station`
  within [checkin + 5 min, checkout + 5 min] whose planned time best fits. If checkout
  exists, the delay is roughly `checkout - planned arrival - walking slack`, cross-checked
  with the actual arrival delay. Return `(delay_min, cancelled, evidence[])`.
  Document the heuristic, and use RDT's `Arrival cancelled` to detect cancellations.
- Tests with a small gzipped CSV fixture (handcraft about 50 rows in the real column format above).

**WP-D: browser runtime** (`browser/*`, `otp/*`)
- `runtime.py`: `BrowserRuntime` keeps one Playwright instance and one
  `launch_persistent_context(user_data_dir=/data/profiles/<account_id>, headless=settings.HEADLESS,
  locale="nl-NL", timezone_id="Europe/Amsterdam", viewport 1366x768, args=["--disable-blink-features=AutomationControlled"])`
  per account. An async context manager `session(account)` serialises access with a
  per-account lock.
- `recorder.py`: attach to a page or context and capture every response whose content type
  contains `json` (url, method, status, request body, response body, truncated to 2 MB)
  into an in-memory ring, with `find(url_regex)` and `dump(dir)`.
- `human.py`: helpers for jittered sleeps, human-like `type()` with a per-character delay,
  scroll-into-view and click, and cookie-banner dismissal (OneTrust `#onetrust-accept-btn-handler`
  on ns.nl, Cookiebot on ov-chipkaart.nl, using role-based fallbacks).
- `debug.py`: `async with debug_bundle(page, action) as b:`. On exception, or when
  `b.keep()` is called, it writes screenshot, HTML, recorder JSON and a Playwright trace
  zip to `/data/debug/...`, and prunes bundles older than 30 days.
- `takeover.py`: `await request_takeover(account, page, reason, timeout=30min)` sets an
  in-memory flag shown in the UI, notifies with a `PUBLIC_URL:6080/vnc.html` link and
  waits until the user clicks "Klaar" in the UI (or the timeout passes).
- `otp/imap.py`: polls IMAP (imaplib in a thread) for messages newer than T0 from
  configurable senders and extracts the code with a configurable regex (default `\b\d{6}\b`).
  `otp/manual.py` waits for a code typed into the UI form.
- Tests: recorder and debug against a local `aiohttp` or `http.server` test page; IMAP
  parsing against stored `.eml` fixtures.

**WP-E: notifications and web UI** (`notify.py`, `web/*`)
- `notify.py`: Apprise wrapper with `notify(title, body, level, attach=None)`, and a daily
  digest (new eligible trips, € expected, € paid, failures).
- FastAPI app with HTTP Basic auth (`WEB_USERNAME` / `WEB_PASSWORD`) and server-rendered
  pages using htmx:
  - **Dashboard**: totals (€ claimed, paid, pending), next run time, canary status and a
    takeover banner when one is active.
  - **Trips**: a filterable table (eligible, claimed, ineligible) showing the delay evidence.
  - **Claims**: state, approve and reject buttons (for `awaiting_approval`), a retry
    button, and links to debug bundles (screenshots served from `/data/debug`).
  - **Accounts**: add or edit an NS or OV account (credentials stored encrypted), IBAN and
    holder name, OTP provider settings, and a "Test login" button.
  - **Run**: buttons for sync, assess, claim and canary now, plus a live log tail (SSE).
  - **OTP prompt** page and **"Klaar" (takeover done)** button.
  - `GET /healthz` for Docker.
- It uses only the `models.py` and `domain.py` contracts, and calls pipeline functions
  through a thin `services` facade that WP-G implements (stub it with `NotImplementedError`).
- Tests: FastAPI TestClient covering auth required, account create (with the password
  stored encrypted) and approve transitions.

**WP-F: Docker and deployment** (`Dockerfile`, `docker-compose.yml`, `docker/*`, `docs/PROXMOX.md`)
- Dockerfile FROM `mcr.microsoft.com/playwright/python:v1.<X>-noble` (pin the X that
  matches the `playwright` version in `pyproject.toml`). Apt packages: `xvfb x11vnc novnc websockify supervisor tzdata`.
  Non-root user `app`, `TZ=Europe/Amsterdam`, `DISPLAY=:99`. A `HEALTHCHECK` curls `/healthz`.
- `supervisord.conf` runs these programs: xvfb (`Xvfb :99 -screen 0 1366x768x24`), x11vnc
  (`-display :99 -forever -shared -rfbauth /data/vnc.pass`), novnc (`websockify --web /usr/share/novnc 6080 localhost:5900`),
  and app (`treinvertraging serve`).
- `entrypoint.sh`: creates `/data` subfolders, generates the VNC password from `VNC_PASSWORD`,
  seeds `/config` from `config.example` when it is empty, runs `alembic upgrade head`, and
  execs supervisord.
- `docker-compose.yml`: one service with `restart: unless-stopped`, `shm_size: 1gb`,
  `init: true`, volumes `./data:/data` and `./config:/config`, ports `8080` and `6080`
  bound to the LAN only, `env_file: .env`.
- `docs/PROXMOX.md` covers two options:
  (a) **an LXC container with Docker** (Debian 12 template, `nesting=1,keyctl=1`, 2 vCPU,
  2 GB RAM, 10 GB disk);
  (b) **a small VM** (Debian 12, Docker CE), which is more robust for Chromium.
  It also covers install via `git clone`, `cp .env.example .env`, `docker compose up -d --build`,
  backups (`/data` with Proxmox Backup or vzdump), and *not* exposing the ports to the
  internet (use Tailscale or WireGuard for remote access).
- **Done when:** `docker compose build` succeeds, the container starts, `/healthz` returns
  200, and noVNC shows the Xvfb desktop.

### Wave 3: parallel (four agents; needs waves 1 and 2 merged)

**WP-G: pipeline and scheduler** (`pipeline/*`, `services` facade)
- `sync.py`: for each enabled account, `adapter.list_trips(since=today-95d)` and upsert
  trips (keeping `raw_json`). Each new trip gets a `claims` row in state `detected`.
- `assess.py`: for `detected` trips, refresh delay data for the needed months and
  stations, run the matcher and `rules.assess`, store the assessment, and transition to
  `eligible` or `ineligible`. Deduplicate across sources: if an OV-chip trip and an NS trip
  share card, check-in (±2 min) and station, keep the NS one as canonical and link the
  other as corroboration.
- `claim.py`: applies the policy. `dry_run` only marks and logs. `approve` moves claims to
  `awaiting_approval` and notifies with an approve link. `auto` (or after
  `AUTO_AFTER_N_APPROVED` successes) submits through the configured channels in order
  (`SUBMIT_CHANNELS`, default `["gtbv","mijnns"]`): set `submitting` and `channel`, commit,
  submit, then record the result. Try the next channel only on `BlockedByBotProtection` or
  `LayoutChanged`, and only if the first attempt **provably did not reach the final submit**
  (a flag in `SubmitResult`). Limit to 5 submissions per run with jitter between them.
- `reconcile.py`: reads every `ClaimStatusReader` (Mijn NS "mijn aanvragen" and the IMAP
  confirmation mails from NS). A `submitting` claim found remotely becomes
  `confirmed`; one not found after 24 hours goes back to `approved` (attempts+1, max 3, then
  `needs_human`). It also updates `paid` and `rejected` if NS shows a status.
- `canary.py`: login plus a 1-day `list_trips`, storing the result and alerting on failure
  with the debug bundle.
- `scheduler.py`: APScheduler with SQLite job store. Jobs: daily `sync → assess → claim →
  reconcile` at a random minute inside `RUN_WINDOW`, `canary` daily, `archiver` every
  10 minutes, a monthly RDT import on the 3rd, the deadline watchdog daily, and the digest
  weekly. It also implements the WP-E `services` facade.
- Tests: the full pipeline with a **FakeAdapter** (in `tests/fakes.py`) and a temporary
  SQLite DB, covering idempotency (run twice, one submission), crash between `submitting` and
  submit (reconcile handles it), the approve flow and the deadline alert.

**WP-H: NS adapter** (`adapters/ns.py`, NS section of `config/selectors.yaml`)
- `ensure_logged_in`: open `https://www.ns.nl/mijnns`. If redirected to `login.ns.nl/login`,
  dismiss cookies, fill `#email` and `#password` with human typing, check `#remember` and
  submit. Handle a verification-code screen through the account's OTP provider. Detect
  Akamai or blocked states (no form, a challenge page, or HTTP 403), and on detection or
  after 2 failed attempts call `request_takeover`. Logged-in is detected by a URL on
  `www.ns.nl/mijnns` and a recorder hit on a JSON profile or dashboard endpoint.
- `list_trips` strategy ladder:
  1. API replay: use a recorded endpoint pattern from `selectors.yaml`
     (`ns.api.travel_history`, initially empty and filled in during calibration) via
     `context.request.get` (cookies included).
  2. Navigate to the travel history ("Reishistorie" / "Mijn reizen") through role-based
     links, let the recorder capture the JSON, then parse it with a **tolerant mapper**
     that searches for keys like `checkIn*`, `checkOut*`, `fare|price|amount`, station names
     and delay or claim markers. Unknown shapes are logged and kept in `raw`.
  3. DOM fallback: parse list items using the locators in `selectors.yaml`.
- `submit_claim` (**fallback channel `mijnns`**): open the trip's prefilled "Geld terug
  bij vertraging" form (via `ns_claim_ref`, or by navigating from the trip). Fill IBAN and holder only if empty,
  tick the required confirmation checkbox(es) by label, and capture the screenshot before
  submitting. Submit, then wait for a confirmation text or reference number (regex in
  selectors). If `CLAIM_MODE=dry_run` or `--no-submit` is set, stop **before** clicking.
- `list_submitted_claims`: the "Mijn aanvragen" or status overview, using the same ladder.
- `calibrate()`: a headed walkthrough. It opens the pages one by one, and for each step asks
  the user (in the UI or CLI) to navigate there manually in noVNC, then saves HTML,
  screenshot, accessibility tree (`page.accessibility.snapshot()`) and the full recorder
  dump to `/data/calibration/ns/<step>/`. The steps are login, reishistorie, trip detail,
  geld-terug form (**do not submit**) and mijn aanvragen. It finally writes
  `calibration-summary.json` listing the candidate JSON endpoints.
- Tests: parse fixtures in `tests/fixtures/ns/*.json` and `*.html`, which the agent writes
  as plausible guesses and marks as `SYNTHETIC`. Login flow runs against a local fake login
  page with the same field ids as the real `login.ns.nl/login` (`email`, `password`,
  `remember`, `csrfToken`).

**WP-I: OV-chipkaart adapter** (`adapters/ovchip.py`)
- `ensure_logged_in`: `https://www.ov-chipkaart.nl/nl/mijn-ov-chip` → NextAuth →
  `login.ov-chipkaart.nl/authenticationendpoint/login.do` (WSO2). Fill the username and
  password fields (WSO2 defaults are `#usernameUserInput` or `#username`, and `#password`;
  use label-based locators with these as hints), handle OTP, and fall back to takeover.
  Session validity is checked with `GET https://www.ov-chipkaart.nl/api/auth/session` (NextAuth
  returns JSON with a `user` when logged in).
- `list_trips`: navigate to the card's travel history ("Reishistorie"), select the period,
  and prefer the **CSV export / declaration download** (`page.expect_download()`), parsing
  the Dutch CSV (`;`-separated, columns like Datum, Check-in, Vertrek, Check-uit,
  Bestemming, Bedrag, Transactie, Klasse, Product). Pair check-in and check-out rows into
  trips. The fallback is the recorder JSON, then the DOM.
- `calibrate()`: same as NS, with the steps login, card overview, reishistorie and export.
- Tests: a CSV parser with a synthetic fixture, check-in/check-out pairing (including a
  missing check-out and transfers between operators) and station-name normalisation via
  `stations.py`.

**WP-K: public claim form submitter** (`adapters/gtbv.py`, `gtbv` section of `selectors.yaml`)
- Implements `ClaimSubmitter` with `channel="gtbv"` against
  `https://gtbv-formulier.ns.nl/ns/klantenservice-geld-terug-bij-vertraging`.
- It is a Mendix SPA: wait for the Mendix client to settle after every action (no pending
  `POST /xas/` requests, which the recorder tracks, and no `.mx-progress` overlay) instead of
  using fixed sleeps. **Never** use `name` or `id` attributes, because they carry generated
  suffixes. Use only `get_by_label`, `get_by_role` and `get_by_text`, with the strings in
  `selectors.yaml`.
- Known step 1 (verified): dismiss OneTrust (`#onetrust-reject-all-handler`, role fallback
  "Weigeren"), check the radio labelled `OV-chipkaart / NS-Business Card`, and type the
  16-digit card number into the 4 grouped inputs under "Je OV-chipkaartnummer / NS-Business
  Card". The first group is prefilled with `3528`, so clear each group or type the remaining
  12 digits after it, and verify the combined value. Click `Naar reisgegevens`. Detect the
  validation errors (`Vul een OV-chipkaartnummer in.` and any text containing "ongeldig")
  and raise `InvalidInput`, which is **not** retried.
- Later steps (unverified, so implement them generically and finalise during calibration):
  the trip details (date, check-in station and time, destination, planned and actual
  arrival or delay, possibly the train number), the payout details (IBAN, holder name),
  contact details (e-mail), a confirmation checkbox, and the final submit. Build a small
  **step engine**: `steps:` in `selectors.yaml` is an ordered list of
  `{expect_heading, fields: [{label, value_from, kind: text|date|time|select|autocomplete|radio|checkbox}], next_button}`.
  `value_from` is a dotted path into `{trip, who, assessment}`. Station fields are probably
  autocompletes: type the station name from `stations.py`, then pick the option whose text
  matches. When a heading is not in the config, raise `LayoutChanged` and keep the debug bundle.
- reCAPTCHA Enterprise: do nothing special in code. If a visible challenge iframe
  (`iframe[title*="recaptcha challenge"]`) appears, call `request_takeover`.
- Success means the confirmation page (text matching `selectors.yaml: gtbv.success_regex`,
  default `(bedankt|ontvangen|aanvraag is verstuurd)`). Extract the reference number if one
  is present. Set `SubmitResult.reached_final_submit=True` right before clicking the final
  button.
- `dry_run=True` runs every step and stops before the final button, keeping the bundle.
- `calibrate --channel gtbv`: runs with the user's real card and a real eligible trip in
  `dry_run` mode, and dumps each step (screenshot, accessibility tree, recorder) so the
  `steps:` config can be completed.
- Tests: a local **fake Mendix-like page** (plain HTML and JS served by `http.server`, with
  the same labels, grouped card inputs, a 300 ms async "xas" delay and multiple steps) that
  covers the happy path, an invalid card, `LayoutChanged` on an unknown heading, the
  dry-run stop, and the challenge iframe triggering takeover.

### Wave 4: integration (one agent, Opus or Sonnet)

**WP-J: integrate, end-to-end test and docs**
- Merge everything, resolve interface drift, and wire `__main__.py` commands to real
  implementations.
- An end-to-end test in Docker: start the container with `CLAIM_MODE=dry_run` and the
  `FakeAdapter` enabled (`TV_FAKE_ADAPTERS=1`), run `sync`, `assess` and `claim`, and assert
  the DB states and the UI pages.
- `docs/CALIBRATION.md` (step by step for the user), `docs/OPERATIONS.md` (logs, debug
  bundles, how to fix `selectors.yaml`, backup and restore) and `README.md` (quick start).

### M6: calibration (you plus a later Claude session, on your server)

1. `docker compose up -d`, open `http://<server>:8080`, and add the NS account (and the OV
   account), IBAN and holder.
2. Open `http://<server>:6080/vnc.html`, run **Accounts → Kalibreren** and follow the
   prompts. For the public form, run `calibrate --channel gtbv` with a real delayed trip.
   It stops before submitting, and you may then submit that one claim by hand in noVNC. You log in once yourself (this also stores the persistent session).
3. The result is `/data/calibration/` (it contains personal data, so don't put it in git).
   Start a Claude session on this repo, point it at a **redacted** copy (the
   `treinvertraging calibrate --redact` command masks names, card numbers, IBAN and e-mail),
   and let it fill in `selectors.yaml` (`ns.api.travel_history`, `ns.api.claims`, the `gtbv.steps` and form labels)
   and real fixtures.
4. Run with `CLAIM_MODE=approve` for the first 3 to 5 claims, check them in Mijn NS, then
   switch to `auto`.

---

## 5. Agent rules (paste into every Sonnet agent prompt)

- Python 3.12, type hints everywhere, `async` for anything touching Playwright or the network.
- Do not invent fields in the §3 contracts. If you need one, put it in `raw` or note it under "Contract change requests" in your final report.
- Never hard-code CSS selectors for NS or OV pages in code. Use `config/selectors.yaml` keys with role and label-based defaults.
- Never log passwords, IBANs or OTP codes, and redact them in debug bundles (`debug.py` provides `redact()`).
- Every network-facing function has a timeout and a clear exception type (`LoginRequired`, `BlockedByBotProtection`, `LayoutChanged`, `InvalidInput`, `TakeoverTimeout`), all defined in `treinvertraging/errors.py` (owned by WP-A).
- Tests must not touch the real internet. Use fixtures and local servers.
- Finish with: files changed, how to run your tests, known gaps.
- Dev environment: use the shared virtualenv at `.venv` (`.venv/bin/pip`, `.venv/bin/pytest`). Pin `playwright==1.63.0`; its Chromium is preinstalled under `/opt/pw-browsers`, so **never run `playwright install`**. The Docker image is `mcr.microsoft.com/playwright/python:v1.63.0-noble`.
- Only touch files owned by your WP. If you need a new dependency, add it to `pyproject.toml` in a single line and mention it in your report. Do not reformat other people's files.

---

## 6. Definition of done (before calibration)

- [ ] `docker compose up -d --build` works on a clean Debian 12 VM or LXC.
- [ ] The UI is reachable with basic auth, accounts can be added, and secrets are encrypted at rest.
- [ ] The full pipeline runs end to end with fake adapters, and idempotency is proven by tests.
- [ ] The RDT import and matcher compute delays for fixture trips correctly.
- [ ] noVNC takeover and the OTP prompt work against local test pages.
- [ ] `ruff check` is clean and `pytest` is green.
- [ ] The calibration recorder produces a redacted bundle.

## 7. Out of scope (for now)

OVpay, NS Business, other operators (Arriva, Keolis, ...) and the "uitchecken vergeten"
refunds. The adapter interface leaves room to add them later. The subscription-advice
feature is also out of scope.
