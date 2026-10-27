# Calibration (milestone M6)

The adapters were written without access to your accounts. The parts behind a login (Mijn NS travel history, the pre-filled claim form, "Mijn aanvragen", the OV-chipkaart history) and the later steps of the public claim form are built on best-guess structure. **Calibration** records the real pages once, on your own server, so a follow-up Claude session can lock in the selectors, the JSON endpoints and real test fixtures.

You do the clicking, the app does the recording. **Nothing is ever submitted** during calibration.

> **Privacy first.** The raw output in `/data/calibration/` contains your name, card number, IBAN and trips. The repository is public: never commit it. Share only the redacted copy (step 5), and never share screenshots or `trace.zip`.

## 0. Prerequisites

- The stack is running ([`PROXMOX.md`](PROXMOX.md)) and `HEADLESS=false` (default). Calibration needs the visible browser.
- noVNC works: `http://<server-ip>:6080/vnc.html` (password = `VNC_PASSWORD`).
- Accounts are added under **Accounts** in the web UI (`http://<server-ip>:8080`): one NS account (e-mail + password), optionally an OV-chipkaart account, IBAN, holder name and your 16-digit OV-chipkaart number (the public claim form needs the card number).
- Pick a **real delayed trip** you have not claimed yet, for the NS and gtbv steps. Without one, the claim-form steps cannot be recorded.
- Do not run calibration while the daily run is busy (the browser profile of an account is used by one process at a time).

## 1. Log in once

Starting a session primes the persistent browser profile, so later runs rarely need to log in again.

- Web UI: **Accounts -> Test login** on the account, or
- CLI: `docker exec -u app -it treinvertraging treinvertraging login --account <id>`

The app tries the automatic login. If NS or OV-chipkaart shows a bot check or a verification code, you get a notification ("Hulp nodig") with the noVNC link: open it, finish the login by hand, then press **Klaar** in the web UI. Keep "ingelogd blijven" ticked.

> Always use `docker exec -u app`. Without `-u app` the command runs as root and the files it creates under `/data` (browser profile!) become unreadable for the app.

## 2. Record each channel

Run once per channel. The browser opens in noVNC at the start page; for every step the app tells you where to go, you navigate there by hand in noVNC and confirm.

**From the web UI:** Accounts -> **Kalibreren** (pick the channel). Every step is a takeover: a banner and a notification say, for example, *"Kalibratie stap 2/5 (reishistorie): ga naar ..., klik Klaar"*. Navigate in noVNC, then press **Klaar** in the web UI.

**From the CLI** (press Enter instead of Klaar):

```bash
docker exec -u app -it treinvertraging treinvertraging calibrate --account <id> --channel ns
docker exec -u app -it treinvertraging treinvertraging calibrate --account <id> --channel ovchip
docker exec -u app -it treinvertraging treinvertraging calibrate --account <id> --channel gtbv
```

| Channel | Steps (in order) | What to open |
|---|---|---|
| `ns` | `login`, `reishistorie`, `trip-detail`, `geld-terug-form`, `mijn-aanvragen` | Mijn NS after login; the travel history list; the detail of one (delayed) trip; the pre-filled *Geld terug bij vertraging* form of that trip (**do not submit**); the page with your submitted claims |
| `ovchip` | `login`, `kaartoverzicht`, `reishistorie`, `export` | Mijn OV-chip; the card overview; the travel history for a period with trips; the export / declaration download page (you may download a CSV too) |
| `gtbv` | `start`, `reisgegevens`, `gegevens`, `controle` | The public form, step "Vervoerbewijs" with your card number; then fill in a **real delayed trip** step by step: trip details, your details (IBAN, name, e-mail), and stop on the final check page (**do not click the submit button**) |

Tips:

- Let each page finish loading (and expand lists, click "meer reizen") before you confirm: the app records what is on screen and the JSON requests that happened since the previous step.
- Open the trip detail and the claim form via the normal links, so the underlying API calls are captured.
- If you want to claim that trip anyway, you may submit it by hand in noVNC **after** the `gtbv` run has finished.
- If a step goes wrong, run the channel again: it overwrites the step directories.

Output per step, in `/data/calibration/<channel>/<NN-step>/`:

| File | Content |
|---|---|
| `page.html` | HTML of the page as you see it |
| `screenshot.png` | full-page screenshot (personal data!) |
| `aria.yml` | accessibility snapshot: roles, labels, button names (the basis for label-based locators) |
| `recorder.json` | every JSON response since the previous step: URL, method, status, request and response body |
| `meta.json` | URL, title, time |

Finally `/data/calibration/<channel>/calibration-summary.json` lists the **candidate JSON endpoints** (URL, method, status, top-level keys, in which steps they appeared) plus the visible headings, labels, buttons and links of every step.

## 3. Check the result

```bash
docker exec -u app treinvertraging ls -R /data/calibration/ns | head -40
docker exec -u app treinvertraging python -c "import json;d=json.load(open('/data/calibration/ns/calibration-summary.json'));print(json.dumps([ (e['method'],e['url'],e['top_level_keys']) for e in d['candidate_json_endpoints']],indent=1))"
```

The travel history call typically shows up as a `GET` with keys like `trips`/`reizen`/`transactions`.

## 4. Redact

```bash
docker exec -u app treinvertraging treinvertraging calibrate --channel ns --redact /data/calibration-redacted/ns
docker exec -u app treinvertraging treinvertraging calibrate --channel gtbv --redact /data/calibration-redacted/gtbv
# or everything at once:
docker exec -u app treinvertraging treinvertraging calibrate --redact /data/calibration-redacted
```

(`--redact` on its own only converts what was recorded before; together with `--account` and `--channel` it records first and redacts afterwards.)

The redacted copy:

- passes every text file (`.html`, `.json`, `.yml`, `.txt`, `.csv`) through the redaction filter: IBANs, 16-digit card numbers, e-mail addresses, 6-digit codes and password/CSRF fields are masked, and so are the holder names stored in your accounts (`[NAME]`);
- **does not include screenshots, `trace.zip` or any other non-text file.** They contain personal data. A `README.md` in the output directory lists every skipped file and says so.

Skim the redacted files yourself before sharing: names and addresses in free text are not always recognised. Copy the directory out of the container, e.g. `docker cp treinvertraging:/data/calibration-redacted ./calibration-redacted`.

## 5. Let Claude fill in the selectors

Start a Claude Code session in a clone of this repository and give it **only the redacted copy** (not `/data`, not the screenshots):

> I calibrated the treinvertraging bot. The redacted output is in `./calibration-redacted/` (ns, ovchip and gtbv). Read `docs/CALIBRATION.md` and `src/treinvertraging/defaults/selectors/*.yaml`. Fill in the CALIBRATE keys: `api.travel_history` and `api.claims` for ns (from `calibration-summary.json`), the `login.*`/`dom.*`/`claims_dom.*` locators, the `steps:` of `gtbv.yaml` (headings and labels from `aria.yml`/`page.html`), `final_submit_button`, `success_regex` and `reference_regex`. Turn the recorded JSON and HTML into fixtures under `tests/fixtures/` and make the adapter tests pass. Do not guess: where the data does not show something, say so.

Claude updates `src/treinvertraging/defaults/selectors/*.yaml` and the fixtures; you pull the new image. To test **before** a new image exists, put the same keys in `/config/selectors/<site>.yaml` ([`OPERATIONS.md`](OPERATIONS.md#fixing-selectors-without-a-rebuild)).

Things that usually need a real value:

| Key | Where | From |
|---|---|---|
| `api.travel_history`, `api.claims` | `ns.yaml` | endpoint URLs in `calibration-summary.json` (use `{since}` as placeholder for the start date) |
| `mapping.fare_unit` | `ns.yaml` | cents or euros in the real JSON |
| `login.logged_in_text` / `logged_in_api_regex` | `ns.yaml` | text or profile endpoint only seen when logged in |
| `base.history_path`, `csv.*` | `ovchip.yaml` | URL of the history, columns of the CSV export |
| `steps:` (headings, labels, `next_button`), `final_submit_button`, `success_regex`, `reference_regex` | `gtbv.yaml` | `aria.yml` of the gtbv steps |

## 6. Verify with a dry run, then go live

1. Put `CLAIM_MODE=dry_run` and run `docker exec -u app -it treinvertraging treinvertraging sync`, `assess`, `claim --dry-run`. The public form is filled in up to the final page and **stops before the final button**; the debug bundle (screenshot, HTML, trace) is under `/data/debug/`.
2. Check the screenshot and the claim under **Claims** in the UI.
3. Switch to `CLAIM_MODE=approve`. For the first 3 to 5 claims, approve each one and check it in Mijn NS ("Mijn aanvragen") or in the confirmation e-mail.
4. When you trust it, set `CLAIM_MODE=auto` (or leave `approve`: after `AUTO_AFTER_N_APPROVED` successful approved claims it switches by itself).

The daily canary (login + trip list) warns you with a debug bundle if a site changes later. Then repeat the relevant calibration channel.
