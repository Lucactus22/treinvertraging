# Treinvertraging op Proxmox / Running on Proxmox

## Samenvatting (NL)

- Aanbevolen: een **kleine VM** (Debian 12, 2 vCPU, 2 GB RAM, 10 GB schijf) met Docker CE. Dat is het robuust voor Chromium.
- Alternatief: een **LXC-container** (Debian 12, *unprivileged*, `nesting=1,keyctl=1`) met Docker erin. Werkt, maar Chromium in LXC kan lastiger zijn (zie hieronder).
- Installatie: `git clone`, `cp .env.example .env` (wachtwoorden invullen!), `docker compose up -d --build`.
- Bijwerken: `git pull && docker compose up -d --build`.
- Back-up: de mappen `./data` en `./config` (SQLite-database, browserprofielen, sleutel `secret.key`). Zonder `secret.key` zijn de opgeslagen wachtwoorden onleesbaar.
- Zet poort 8080 (web) en 6080 (noVNC) **nooit** open naar het internet. Gebruik Tailscale of WireGuard voor toegang van buitenaf, en zet `BIND_ADDR` in `.env` op je LAN- of Tailscale-adres.

---

## Overview

The app runs as one Docker container (web UI on `:8080`, noVNC on `:6080`). Resource needs are modest: 2 vCPU, 2 GB RAM, 10 GB disk. Chromium runs headed inside Xvfb, so the container needs a generous `/dev/shm` (`shm_size: 1gb` is set in `docker-compose.yml`).

| Port | Purpose | Auth |
|---|---|---|
| 8080 | Web UI (dashboard, approvals, accounts) | HTTP Basic (`WEB_USERNAME` / `WEB_PASSWORD`) |
| 6080 | noVNC: watch the browser or take over when a bot check or 2FA appears | VNC password (`VNC_PASSWORD`) |

Neither port should ever be reachable from the internet. See [Remote access](#remote-access-tailscale).

## Option A: VM (recommended)

1. In Proxmox, create a VM: Debian 12 (netinst/cloud image), 2 vCPU, 2 GB RAM (4 GB is comfortable), 10-20 GB disk, VirtIO NIC. Enable the QEMU guest agent.
2. Install Docker CE inside the VM ([official docs](https://docs.docker.com/engine/install/debian/)):

   ```bash
   apt-get update && apt-get install -y ca-certificates curl git
   install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
   chmod a+r /etc/apt/keyrings/docker.asc
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
     > /etc/apt/sources.list.d/docker.list
   apt-get update
   apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   systemctl enable --now docker
   ```

3. Continue with [Install](#install).

Why a VM: Chromium needs user namespaces, a large shared-memory area and (headed) a working X stack. All of that works out of the box in a full VM with its own kernel, and snapshots/backups through vzdump are simple.

## Option B: LXC container with Docker

Lighter on resources, but nested containers add moving parts.

1. Download the template: *Storage -> CT Templates -> Templates -> debian-12-standard*.
2. Create the CT: **unprivileged**, 2 vCPU, 2048 MB RAM, 512 MB swap, 10 GB disk. Under *Options -> Features* enable **Nesting** and **keyctl** (equivalent to `features: nesting=1,keyctl=1` in `/etc/pve/lxc/<id>.conf`).
3. Start the CT and install Docker with the same commands as in Option A.
4. Continue with [Install](#install).

Notes for Chromium in LXC:

- Docker in an unprivileged LXC needs `nesting=1,keyctl=1`; without them containers fail to start with `permission denied` errors from runc or AppArmor/sysctl.
- `/dev/shm` inside the LXC is usually fine, but the Docker container gets its own 64 MB `/dev/shm` unless told otherwise. That is why compose sets `shm_size: 1gb`. If Chromium crashes with "Aw, Snap!" or tab crashes, check this first (`docker exec treinvertraging df -h /dev/shm`).
- Newer Proxmox/kernel/AppArmor combinations occasionally block Chromium's sandbox inside nested containers. If the browser will not start and logs mention namespace or sandbox errors, the quickest fix is to run the stack in a VM (Option A) rather than loosening the LXC profile.
- Disk: keep the CT on local-lvm/zfs, not on a network share; SQLite (WAL) does not like NFS/SMB.

## Install

On the VM/CT:

```bash
git clone <your-repo-url> treinvertraging
cd treinvertraging
cp .env.example .env
nano .env          # set WEB_PASSWORD, VNC_PASSWORD, PUBLIC_URL, optional NS_API_KEY / APPRISE_URLS
docker compose up -d --build
docker compose logs -f
```

Checks:

- `docker compose ps` shows the container as `healthy` after ~30-60 seconds.
- `http://<server-ip>:8080/` asks for the Basic-auth credentials from `.env`.
- `http://<server-ip>:6080/vnc.html` shows the noVNC page; connect with `VNC_PASSWORD` (only the first 8 characters count). If `VNC_PASSWORD` is empty, a random one is generated on first start and printed once in `docker compose logs`.

First start: `./data` and `./config` are created next to `docker-compose.yml`. `./config` is seeded from `config.example/` when empty. The entrypoint changes ownership of both folders to the container's `app` user (uid/gid 1000), so don't be surprised when they appear owned by that uid on the host.

Optional `.env` setting for the compose file: `BIND_ADDR` (default `0.0.0.0`). Set it to the server's LAN IP or Tailscale IP to publish ports 8080/6080 on that address only.

## Update

```bash
cd treinvertraging
git pull
docker compose up -d --build
```

Database migrations run automatically on container start. Data and config live in `./data` and `./config` and survive rebuilds. Old images can be removed with `docker image prune`.

## Backups

Everything stateful is in two folders next to `docker-compose.yml`:

| Path | Contents |
|---|---|
| `./data` | `app.db` (SQLite), `secret.key` (encrypts stored passwords/IBAN: **back it up together with app.db**), `profiles/` (browser sessions), `debug/`, `rdt-cache/`, `calibration/` |
| `./config` | `settings.yaml`, `rules.yaml`, `selectors.yaml` |

Options:

- **Proxmox:** schedule vzdump (*Datacenter -> Backup*) of the VM/CT. Use *Snapshot* mode; for a consistent SQLite copy, either stop the container briefly or rely on WAL recovery (it is crash-safe).
- **Proxmox Backup Server:** same job, with a PBS storage target.
- **File level:** `docker compose stop && tar czf backup-$(date +%F).tgz data config && docker compose start`.

`data/calibration/` and `data/debug/` contain personal data (names, card numbers, IBAN). Do not put them in git or share them unredacted. The backup contains secrets too: store it somewhere private and encrypted.

Restore: put `data/` and `config/` back, then `docker compose up -d`.

## Remote access (Tailscale)

Do **not** forward 8080 or 6080 on your router and do not expose them through a reverse proxy to the public internet. The noVNC session gives full control of a browser that is logged in to your NS account.

Use a private overlay network instead:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale ip -4          # e.g. 100.101.102.103
```

Then either leave `BIND_ADDR=0.0.0.0` behind your firewall, or set `BIND_ADDR=100.101.102.103` (Tailscale IP) or the LAN IP in `.env` and run `docker compose up -d`. Reach the UI at `http://<tailscale-ip>:8080` and noVNC at `http://<tailscale-ip>:6080/vnc.html`. Set `PUBLIC_URL` in `.env` to that address so notification links work from your phone. WireGuard works the same way.

Note: Docker publishes ports by editing iptables directly, bypassing ufw/firewalld rules. Binding to a specific address with `BIND_ADDR` is more reliable than a host firewall rule.

## Troubleshooting

- `docker compose logs -f` shows all processes (Xvfb, x11vnc, noVNC, app) with their output.
- Container "unhealthy": the app on :8080 is not up yet or crashed; look at the `app` lines in the logs.
- Chromium crashes: check `/dev/shm` size (see LXC notes) and free RAM.
- Forgot the generated VNC password: set `VNC_PASSWORD` in `.env` and run `docker compose up -d` (the password file is rewritten on every start).
