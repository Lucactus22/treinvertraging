# Treinvertraging

**NL** — Een zelf gehoste versie van wat trein-vertraging.nl doet, voor één huishouden. Het draait als Docker-container op je (Proxmox-)server, leest je reizen uit **Mijn NS** en van de **OV-chipkaart**-site, rekent uit welke reizen recht geven op NS' *geld terug bij vertraging*, **dient de claims zelf in** via het publieke NS-claimformulier en meldt het je. Geen abonnementskosten en geen derde partij die je wachtwoorden bewaart.

**EN** — A self-hosted take on trein-vertraging.nl for one household. It runs as a Docker container on your home server, reads your trips from Mijn NS and the OV-chipkaart site, works out which trips qualify for NS delay compensation, submits the claims through NS's public claim form and notifies you.

> **Status:** gebouwd tegen fixtures en een beste gok van de paginastructuur. De delen achter de login (reishistorie, claimformulier) moeten één keer gekalibreerd worden op jouw server: zie [`docs/CALIBRATION.md`](docs/CALIBRATION.md). Tot die tijd: `CLAIM_MODE=approve` of `dry_run`.

## What it does

1. **Sync** — reads the trips of the last ~95 days from Mijn NS and ov-chipkaart.nl (headed Chromium in a virtual display, persistent browser profile per account).
2. **Assess** — checks the delay independently (Rijden de Treinen open data and a small archive of NS API arrivals) and applies the rules in `config/rules.yaml` (30 min = 50 %, 60 min = 100 %, minimum payout, 3 month deadline, ...).
3. **Claim** — according to `CLAIM_MODE` (see below) submits the claim through the public form `gtbv-formulier.ns.nl` (no NS login needed) or, as fallback, the pre-filled form in Mijn NS. Human takeover (noVNC) is the last resort, never a silent failure.
4. **Reconcile** — confirms claims in "Mijn aanvragen" / the NS confirmation mail, tracks paid and rejected claims, never submits twice.
5. **Notify** — Apprise (Telegram, ntfy, e-mail, ...): approvals needed, submitted, paid, deadline warnings, failures with a debug bundle.

## Architecture

```
┌──────────────────────── docker container "treinvertraging" ────────────────────────┐
│ supervisord                                                                         │
│  ├─ Xvfb :99 + x11vnc + noVNC (:6080)   ← watch / take over the browser              │
│  └─ treinvertraging serve (:8080)                                                    │
│       ├─ FastAPI + Jinja2 + htmx dashboard (HTTP Basic)                              │
│       ├─ APScheduler: daily sync → assess → claim → reconcile, canary, archiver,     │
│       │               monthly RDT import, deadline watchdog, weekly digest           │
│       ├─ Browser runtime (Playwright, persistent profile per account)                │
│       ├─ Adapters: ns (Mijn NS) · ovchip (ov-chipkaart.nl) · gtbv (public form)      │
│       ├─ Delay data: RDT importer · NS API archiver · matcher · rules engine         │
│       └─ Notifier (Apprise)                                                          │
│ /data   app.db · profiles/ · debug/ · calibration/ · rdt-cache/ · secret.key        │
│ /config settings.yaml · rules.yaml · selectors/<site>.yaml                           │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Python 3.12, FastAPI, SQLAlchemy 2 + SQLite (WAL), Alembic, APScheduler 3, Playwright (Chromium), Apprise. Passwords, card number and IBAN are encrypted at rest (Fernet, key in `/data/secret.key`).

## Quick start with Portainer

The image is built by GitHub Actions and published as `ghcr.io/lucactus22/treinvertraging`.

1. Portainer -> **Stacks -> Add stack -> Repository**
   - URL `https://github.com/lucactus22/treinvertraging`, reference `refs/heads/main`, compose path `docker-compose.yml`, no authentication (public repo).
2. Environment variables: at least `WEB_PASSWORD`; also `VNC_PASSWORD`, `PUBLIC_URL` (`http://<server-ip>:8080`), `APPRISE_URLS`, `CLAIM_MODE=approve`, optional `NS_API_KEY`.
3. Deploy. Open `http://<server-ip>:8080` (user `admin`) and add your accounts under **Accounts**.
4. If the image cannot be pulled, make the GHCR package public once (GitHub -> Packages -> treinvertraging -> Package settings -> Change visibility).

Details, GitOps updates and host-path volumes: [`docs/PROXMOX.md`](docs/PROXMOX.md#portainer-stack-from-github--ghcr).

## Quick start with plain docker compose

```bash
git clone https://github.com/lucactus22/treinvertraging.git && cd treinvertraging
cp .env.example .env && nano .env     # WEB_PASSWORD, VNC_PASSWORD, PUBLIC_URL, ...
docker compose up -d --build          # or: docker compose pull && docker compose up -d
docker compose logs -f
```

Web UI `:8080`, noVNC `:6080/vnc.html`. **Never expose these ports to the internet**; use a LAN address (`BIND_ADDR`) or Tailscale/WireGuard.

Command line (inside the container, as the `app` user so files stay owned by `app`):

```bash
docker exec -u app -it treinvertraging treinvertraging --help
docker exec -u app -it treinvertraging treinvertraging account list
docker exec -u app -it treinvertraging treinvertraging claim --dry-run
```

## CLAIM_MODE

| Mode | Behaviour |
|---|---|
| `dry_run` | Never submits. Every eligible claim is *rehearsed* once: the form is filled in up to, but not including, the final button, and a debug bundle is kept. |
| `approve` (default) | Eligible claims become `awaiting_approval` and you get a notification. You approve (or reject) in the UI; approved claims are submitted by the next run. After `AUTO_AFTER_N_APPROVED` (default 5) successful approved claims it behaves like `auto`; set it to `0` to never switch. |
| `auto` | Eligible claims are submitted without asking (max 5 per run, with pauses in between). |

Recommended path: `dry_run` while calibrating -> `approve` for the first claims (check them in Mijn NS) -> `auto`.

## Safety and privacy

- **The repository is public.** Never commit `/data`, `/config`, `.env`, calibration dumps, debug bundles, screenshots or traces. They contain your name, card number, IBAN, trips and browser sessions. `.gitignore` and `.dockerignore` exclude them; keep it that way.
- Share calibration output only after `treinvertraging calibrate --redact OUT_DIR`, and remember that screenshots and `trace.zip` are never included in the redacted copy because they hold personal data ([`docs/CALIBRATION.md`](docs/CALIBRATION.md)).
- The web UI uses HTTP Basic over plain HTTP: keep it on a trusted network or behind Tailscale/WireGuard. noVNC gives full control of a browser that is logged in to your NS account.
- A claim whose final submit button was clicked is **never** re-queued automatically; if NS does not confirm it within 24 hours you get a notification to check Mijn NS / your mail ([`docs/OPERATIONS.md`](docs/OPERATIONS.md)).
- The compensation rules in `rules.yaml` are based on public sources: verify them on ns.nl.
- Not affiliated with NS. Use at your own risk; automated access may be against the sites' terms.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q                     # includes browser tests (needs Chromium for Playwright)
TV_E2E_DOCKER=1 .venv/bin/pytest -q tests/e2e   # builds the image and exercises it with fake adapters
```

`TV_FAKE_ADAPTERS=1` swaps the real adapters for fakes (`adapters/fake.py`): useful to try the dashboard without NS accounts. Behind a TLS-intercepting proxy, build with `docker build --secret id=extra_ca,src=/path/ca-bundle.crt .` (the e2e test does this when `TV_EXTRA_CA` is set).

## Docs

- [`docs/PROXMOX.md`](docs/PROXMOX.md): VM/LXC, Portainer, backups, remote access
- [`docs/CALIBRATION.md`](docs/CALIBRATION.md): record the real pages once and lock in the selectors
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md): claim states, retries, watchdog, debug bundles, fixing selectors, backups, logs
- [`PLAN.md`](PLAN.md): the original design
