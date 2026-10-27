# Operations

Day-to-day running of the bot: what the claim states mean, what happens on failures, where the evidence is, how to fix a broken selector without rebuilding, backups and logs.

Run CLI commands inside the container **as the `app` user** (otherwise files under `/data` become root-owned):

```bash
docker exec -u app -it treinvertraging treinvertraging --help
```

## The daily run

The scheduler (same process as the web UI) plans these jobs; **Run** in the web UI shows the next times and lets you start a step by hand.

| Job | When | What |
|---|---|---|
| `daily_run` | random minute inside `RUN_WINDOW` (default 07:00-22:00), re-randomised daily | `sync` -> `assess` -> `claim` -> `reconcile` |
| `canary` | daily, random time in the window | read-only login + 1-day trip list; alerts with a debug bundle when a site changed |
| `archiver` | every 10 min, only with `NS_API_KEY` | stores actual arrival times of your frequent destination stations |
| `monthly` | 3rd of the month 04:17 | RDT delay data import + station list refresh |
| `watchdog` | daily 09:20 | expires late claims, warns about deadlines |
| `digest` | Monday 08:05 | weekly summary notification |

A pipeline run takes a process-wide lock, so a manual run never overlaps the daily run. After a restart, `daily_run` is only re-planned for today if it has not run today yet.

## Claim state machine

```
 detected ──assess──► ineligible ◄── (re-assessed later when delay data / check-out arrive)
    │
    └────────────────► eligible ──deadline passed──► expired
                          │
        CLAIM_MODE=approve│            CLAIM_MODE=auto (or approve after N good claims)
                          ▼                          │
                 awaiting_approval                   │
                  │            │                     │
              [approve]     [reject]                 │
                  ▼            ▼                     │
              approved      rejected                 │
                  │                                  │
                  └──────────────┬───────────────────┘
                                 ▼
                            submitting      (committed BEFORE the submit click)
          ┌─────────────┬────────┼───────────────┬──────────────────────┐
   transient failure    │   site confirmed       │            clicked final submit,
   (attempts < 3)       │        │          InvalidInput /     no confirmation seen
          │             │        ▼          all channels            │
          ▼             │    submitted      blocked / changed       ▼
      approved ◄────────┘        │               │            (stays submitting)
   (next run retries)            ▼               ▼                  │
                             confirmed ◄── seen in "mijn aanvragen" / confirmation mail
                              │      │            needs_human ◄─────┘ reconcile, after 24 h
                              ▼      ▼                 │          without confirmation
                            paid   rejected (by NS)    └──[retry]──► approved
```

| State | Meaning |
|---|---|
| `detected` | trip found, not assessed yet |
| `ineligible` | does not meet the rules (or no delay data / check-out yet; re-assessed until the deadline) |
| `eligible` | qualifies; waiting for the claim policy |
| `awaiting_approval` | `approve` mode: waiting for your tap in **Claims** |
| `approved` | will be submitted by the next claim run (also the target of a retry) |
| `submitting` | the submit is under way or its outcome is unknown; **reconcile** resolves it |
| `submitted` | the form showed its confirmation page |
| `confirmed` | found in Mijn NS "Mijn aanvragen" or in the NS confirmation mail |
| `paid` / `rejected` | NS shows the outcome |
| `needs_human` | automation gave up; see `last_error`, the notification and the debug bundle |
| `expired` | the 3-month deadline passed before it could be submitted |

### Retry semantics

- The state is set to `submitting` and `attempts` is incremented **before** the submit button is clicked, in its own transaction. A claim in `submitting` is never picked up by a claim run again.
- Channels are tried in `SUBMIT_CHANNELS` order (default `gtbv,mijnns`). The next channel is tried only after `BlockedByBotProtection` / `LayoutChanged` **and** only when the attempt provably did not click the final button.
- Transient problems (login required, takeover not completed in time, a form that refused before the final click) put the claim back to `approved`; after 3 attempts it becomes `needs_human`.
- `InvalidInput` (the form rejects your card number, IBAN, ...) and "all channels blocked/changed" go straight to `needs_human`: fix the data or selectors, then press **Retry** on the claim (resets `attempts`). Retry is also available for `rejected` and `expired` claims.
- At most 5 claims are submitted per run, with a random pause between them.
- **Never double-submit.** As soon as an attempt clicks the final submit button, the audit log gets an event `claim.final_submit_clicked`. Such a claim is never rolled back to `approved` automatically. If neither Mijn NS nor the mailbox shows it after 24 hours, `reconcile` moves it to `needs_human` and notifies you: *check in Mijn NS / your mail whether the claim arrived*. It keeps watching it: if it shows up later it becomes `confirmed`. Only press **Retry** after you checked that NS really does not have it.
- Reconcile of a `submitting` claim **without** a final-submit event (e.g. the container was killed mid-way): found remotely -> `confirmed`; not found after 24 hours -> back to `approved` (attempts stay counted, max 3, then `needs_human`). If no status source is reachable, nothing is changed and you are warned.
- The deadline watchdog expires `eligible`, `awaiting_approval` and `needs_human` claims past their deadline; `approved` ones are left alone (you want them tried). It sends a loud notification for every unsubmitted claim whose deadline is within 14 days.

## Takeover and one-time codes

When a site shows a bot check or asks for something the app cannot do, a notification ("Hulp nodig") says what and links to `http://<server>:6080/vnc.html`. Open it, do the step in the live browser, press **Klaar** in the web UI. Without Klaar the task times out after 30 minutes and the claim is retried later.

Verification codes: with `otp_provider=imap` (settings: IMAP block under Accounts or `IMAP_*` variables) the code is read from the mailbox; with `manual` the **OTP** page in the UI asks for it.

## Debug bundles

Every failure (and every dry run / submit) leaves a directory `/data/debug/<YYYYMMDD-HHMMSS>-<action>/`:

| File | Content |
|---|---|
| `screenshot.png` | full-page screenshot |
| `page.html` | page HTML (redacted) |
| `aria.yml` | accessibility snapshot (redacted) |
| `recorder.json` | captured JSON traffic (redacted) |
| `trace.zip` | Playwright trace (**not** redacted; keep private) |
| `meta.json` | action, URL, title, error |

The **Claims** page links the bundle of a claim (screenshots are served from `/debug/...`). Bundles older than 30 days are pruned automatically. Open a trace with `npx playwright show-trace trace.zip` or at <https://trace.playwright.dev> (the file stays in your browser).

Never put bundles in git or share them unredacted; redacted text files are fine to give a Claude session. The same applies to `/data/calibration/` ([`CALIBRATION.md`](CALIBRATION.md)).

## Fixing selectors without a rebuild

All site-specific labels, URLs, regexes and form steps live in YAML, not in code. The image contains defaults (`src/treinvertraging/defaults/selectors/{ns,ovchip,gtbv}.yaml`); on every start the container also writes reference copies to `/config/selectors/<site>.yaml.example`.

To override a few keys:

1. Create `/config/selectors/<site>.yaml` (`ns`, `ovchip` or `gtbv`) containing **only the keys that differ**. It is deep-merged over the defaults: nested mappings merge, but **lists (such as `gtbv` `steps:`) and scalars are replaced as a whole**, so copy the complete list when you change one step.

   ```yaml
   # /config/selectors/ns.yaml
   api:
     travel_history: /api/reishistorie?from={since}
   login:
     logged_in_text: "Welkom"
   ```

2. Edit it where the volume lives (`docker volume inspect treinvertraging-config`, a host directory, or `docker exec -u app -it treinvertraging sh`). The file is re-read when it changes (modification time); no restart is needed, and the next run uses it.
3. Test: **Accounts -> Test login**, `treinvertraging canary`, or `treinvertraging claim --dry-run`. Look at the new debug bundle.
4. Once it works, give the same change to a Claude session so it lands in the defaults (and the fixtures/tests) and you can delete your override. `rules.yaml` (thresholds, minimum payout, deadline) works the same way in `/config/rules.yaml`; `/config/settings.yaml` holds settings, but **environment variables win** over it (with the provided compose file every setting is an environment variable).

An invalid YAML file makes the affected adapter fail with a YAML parse error; the canary reports it.

## Backups

State is in two volumes (named volumes `treinvertraging-data` / `treinvertraging-config` by default):

| Path | Contents | Notes |
|---|---|---|
| `/data/app.db` (+ `-wal`, `-shm`) | trips, claims, events, accounts (secrets encrypted) | |
| `/data/secret.key` | Fernet key for stored passwords/IBAN | **without it the database secrets are unreadable**; back up together with `app.db` |
| `/data/profiles/` | browser sessions (cookies) | losing it only means logging in again |
| `/data/debug/`, `/data/calibration/` | evidence, personal data | optional, never in git |
| `/config/` | `settings.yaml`, `rules.yaml`, `selectors/*.yaml` | |

Consistent file backup:

```bash
docker stop treinvertraging
docker run --rm -v treinvertraging-data:/data -v treinvertraging-config:/config -v "$PWD":/backup alpine \
  tar czf /backup/treinvertraging-$(date +%F).tgz /data /config
docker start treinvertraging
```

Restore into empty volumes with the container stopped:

```bash
docker run --rm -v treinvertraging-data:/data -v treinvertraging-config:/config -v "$PWD":/backup alpine \
  tar xzf /backup/treinvertraging-YYYY-MM-DD.tgz -C /
docker start treinvertraging
```

(The entrypoint fixes ownership on start.) Proxmox-level backups (vzdump / PBS) of the whole VM/CT work too; see [`PROXMOX.md`](PROXMOX.md). Backups contain secrets: store them encrypted and private.

## Logs and inspection

- **Container log:** `docker logs -f treinvertraging` (Portainer: *Containers -> treinvertraging -> Logs*). Shows Xvfb, x11vnc, noVNC and the app.
- **Live log tail:** web UI -> **Run**.
- **Audit log:** table `events` (`ts`, `level`, `kind`, `message`, `trip_key`); every state change is there (`claim.submitting`, `claim.final_submit_clicked`, `claim.submitted`, `reconcile.*`, `watchdog.deadline`, `canary.*`, ...):

  ```bash
  docker exec -u app treinvertraging python -c "
  import sqlite3
  for r in sqlite3.connect('/data/app.db').execute(
      \"select ts, level, kind, message from events order by id desc limit 30\"): print(*r, sep=' | ')"
  ```

- **Health:** `GET /healthz` (no auth); the container is `healthy` when it answers.
- **Fake mode for experiments:** `TV_FAKE_ADAPTERS=1` swaps all adapters for fakes (no network, nothing submitted for real); see `src/treinvertraging/adapters/fake.py` for `TV_FAKE_*` options.

## Upgrading

Portainer: *Pull and redeploy* with *Re-pull image* (or GitOps polling). Plain compose: `docker compose pull && docker compose up -d` (or `git pull && docker compose up -d --build`). Migrations run on start; a downgrade after a migration is not supported, so back up first. Pin `TAG` to a version or `sha-...` to stay on a known image.
