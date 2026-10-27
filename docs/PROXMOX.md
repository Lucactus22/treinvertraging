# Treinvertraging op Proxmox / Running on Proxmox

## Samenvatting (NL)

- Aanbevolen: een **kleine VM** (Debian 12, 2 vCPU, 2 GB RAM, 10 GB schijf) met Docker CE. Dat is het robuust voor Chromium.
- Alternatief: een **LXC-container** (Debian 12, *unprivileged*, `nesting=1,keyctl=1`) met Docker erin. Werkt, maar Chromium in LXC kan lastiger zijn (zie hieronder).
- Installatie met **Portainer** (aanbevolen): *Stacks -> Add stack -> Repository*, zie [Portainer](#portainer-stack-from-github--ghcr). Er is geen token nodig (publieke repo).
- Installatie zonder Portainer: `git clone`, `cp .env.example .env` (wachtwoorden invullen!), `docker compose up -d --build`.
- Bijwerken: Portainer: *Pull and redeploy* (of GitOps-updates aan). Zonder Portainer: `git pull && docker compose up -d --build`.
- Back-up: de volumes `treinvertraging-data` en `treinvertraging-config` (SQLite-database, browserprofielen, sleutel `secret.key`). Zonder `secret.key` zijn de opgeslagen wachtwoorden onleesbaar.
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

3. Continue with [Portainer](#portainer-stack-from-github--ghcr) or [Install](#install-plain-docker-compose).

Why a VM: Chromium needs user namespaces, a large shared-memory area and (headed) a working X stack. All of that works out of the box in a full VM with its own kernel, and snapshots/backups through vzdump are simple.

## Option B: LXC container with Docker

Lighter on resources, but nested containers add moving parts.

1. Download the template: *Storage -> CT Templates -> Templates -> debian-12-standard*.
2. Create the CT: **unprivileged**, 2 vCPU, 2048 MB RAM, 512 MB swap, 10 GB disk. Under *Options -> Features* enable **Nesting** and **keyctl** (equivalent to `features: nesting=1,keyctl=1` in `/etc/pve/lxc/<id>.conf`).
3. Start the CT and install Docker with the same commands as in Option A.
4. Continue with [Portainer](#portainer-stack-from-github--ghcr) or [Install](#install-plain-docker-compose).

Notes for Chromium in LXC:

- Docker in an unprivileged LXC needs `nesting=1,keyctl=1`; without them containers fail to start with `permission denied` errors from runc or AppArmor/sysctl.
- `/dev/shm` inside the LXC is usually fine, but the Docker container gets its own 64 MB `/dev/shm` unless told otherwise. That is why compose sets `shm_size: 1gb`. If Chromium crashes with "Aw, Snap!" or tab crashes, check this first (`docker exec treinvertraging df -h /dev/shm`).
- Newer Proxmox/kernel/AppArmor combinations occasionally block Chromium's sandbox inside nested containers. If the browser will not start and logs mention namespace or sandbox errors, the quickest fix is to run the stack in a VM (Option A) rather than loosening the LXC profile.
- Disk: keep the CT on local-lvm/zfs, not on a network share; SQLite (WAL) does not like NFS/SMB.

## Portainer (stack from GitHub + GHCR)

If you run Portainer on the Proxmox VM/CT you do not need a shell: deploy the stack straight from the public GitHub repository. A GitHub Actions workflow (`.github/workflows/docker.yml`) builds the image on every push to `main` and publishes it as `ghcr.io/lucactus22/treinvertraging:latest` (plus `:sha-<commit>` and `:<tag>` for `v*` tags). The compose file pulls that image, so the server never has to build anything.

1. **Stacks -> Add stack**, name it `treinvertraging`.
2. Build method **Repository**:
   - Repository URL: `https://github.com/lucactus22/treinvertraging`
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
   - Authentication: **off**. The repository is public, so no token is needed.
3. **Environment variables** (use *Advanced mode* to paste them as `NAME=value` lines):

   | Variable | Value |
   |---|---|
   | `WEB_PASSWORD` | **required**, password of the web UI (the stack will not deploy without it) |
   | `VNC_PASSWORD` | password for noVNC (only the first 8 characters count); empty = generated once and printed in the container log |
   | `PUBLIC_URL` | `http://<server-ip>:8080`, used in notification links |
   | `APPRISE_URLS` | e.g. `tgram://BOT_TOKEN/CHAT_ID` or `ntfys://my-topic` (comma separated) |
   | `CLAIM_MODE` | `approve` (default; every claim needs your tap in the UI). `dry_run` to only rehearse, `auto` later |
   | `NS_API_KEY` | optional, key from the NS API portal for the arrivals archive |
   | `BIND_ADDR` | optional, LAN or Tailscale IP to publish ports 8080/6080 on (default `0.0.0.0`) |
   | `TAG` | optional, image tag (`latest`, `v0.1.0`, `sha-abc1234`); pin one to avoid surprise updates |

   Everything else in `.env.example` (`WEB_USERNAME`, `RUN_WINDOW`, `IMAP_*`, ...) can be added the same way. Do not commit a `.env` file: the repository is public.
4. **Deploy the stack.** The compose file needs Docker Compose >= 2.24 (the optional `env_file`); current Portainer releases bundle that.
5. Open `http://<server-ip>:8080` (user `admin`, your `WEB_PASSWORD`) and `http://<server-ip>:6080/vnc.html`.

**GHCR package visibility.** The first time the workflow pushes, GitHub creates the package as *private*. Make it public once so the server can pull it without credentials: GitHub -> your profile -> **Packages** -> `treinvertraging` -> **Package settings** -> *Danger zone* -> **Change visibility** -> Public. (If you prefer a private package, add a registry in Portainer: *Registries -> Add registry -> GitHub/Custom* with a personal access token that has `read:packages`.)

**Updates.** In the stack, switch on **GitOps updates** (polling, e.g. every 5 minutes, with *Re-pull image* enabled) or press **Pull and redeploy** with *Re-pull image* ticked when you want a new version. Pushing to `main` publishes a new `:latest`; pinning `TAG` to a version gives you control instead. Database migrations run on container start.

**Data and config** live in the named volumes `treinvertraging-data` (`/data`) and `treinvertraging-config` (`/config`); see [Backups](#backups). To keep them in a host directory instead (easier to back up and to edit selectors), edit the stack in Portainer's web editor and replace the volume lines with

```yaml
    volumes:
      - /srv/treinvertraging/data:/data
      - /srv/treinvertraging/config:/config
```

and delete the `volumes:` block at the bottom of the file (the container fixes the ownership of both directories on start). Note that editing the compose file in the web editor detaches it from the repository; with plain `docker compose` you can do the same with a `docker-compose.override.yml`.

## Install (plain docker compose)

On the VM/CT:

```bash
git clone https://github.com/lucactus22/treinvertraging.git treinvertraging
cd treinvertraging
cp .env.example .env
nano .env          # set WEB_PASSWORD, VNC_PASSWORD, PUBLIC_URL, optional NS_API_KEY / APPRISE_URLS
docker compose up -d --build     # builds locally; `docker compose pull && docker compose up -d` uses the GHCR image
docker compose logs -f
```

Checks:

- `docker compose ps` shows the container as `healthy` after ~30-60 seconds.
- `http://<server-ip>:8080/` asks for the Basic-auth credentials from `.env`.
- `http://<server-ip>:6080/vnc.html` shows the noVNC page; connect with `VNC_PASSWORD` (only the first 8 characters count). If `VNC_PASSWORD` is empty, a random one is generated on first start and printed once in `docker compose logs`.

First start: the named volumes `treinvertraging-data` and `treinvertraging-config` are created (see the Portainer section for host directories). `/config` is seeded per file from `config.example/` (`rules.yaml`, `settings.yaml`; existing files are never overwritten) and `/config/selectors/` gets reference copies `ns.yaml.example`, `ovchip.yaml.example`, `gtbv.yaml.example`. The entrypoint changes ownership of both folders to the container's `app` user (uid/gid 1000).

Optional `.env` setting for the compose file: `BIND_ADDR` (default `0.0.0.0`). Set it to the server's LAN IP or Tailscale IP to publish ports 8080/6080 on that address only.

## Update

```bash
cd treinvertraging
git pull
docker compose up -d --build
```

Database migrations run automatically on container start. Data and config live in the volumes and survive rebuilds. Old images can be removed with `docker image prune`.

## Backups

Everything stateful is in two volumes (named volumes by default, or the host directories you chose):

| Volume / path | Contents |
|---|---|
| `treinvertraging-data` (`/data`) | `app.db` (SQLite), `secret.key` (encrypts stored passwords/IBAN: **back it up together with app.db**), `profiles/` (browser sessions), `debug/`, `rdt-cache/`, `calibration/` |
| `treinvertraging-config` (`/config`) | `settings.yaml`, `rules.yaml`, `selectors/<site>.yaml` |

Options:

- **Proxmox:** schedule vzdump (*Datacenter -> Backup*) of the VM/CT. Use *Snapshot* mode; for a consistent SQLite copy, either stop the container briefly or rely on WAL recovery (it is crash-safe).
- **Proxmox Backup Server:** same job, with a PBS storage target.
- **File level (named volumes):**

  ```bash
  docker stop treinvertraging
  docker run --rm -v treinvertraging-data:/data -v treinvertraging-config:/config -v "$PWD":/backup alpine \
    tar czf /backup/treinvertraging-$(date +%F).tgz /data /config
  docker start treinvertraging
  ```

  With host directories: `tar czf backup.tgz data config` while the container is stopped.

`data/calibration/` and `data/debug/` contain personal data (names, card numbers, IBAN). Do not put them in git or share them unredacted. The backup contains secrets too: store it somewhere private and encrypted.

Restore: extract the archive into the volumes (or host directories) while the container is stopped, then start it again. `docs/OPERATIONS.md` has the details.

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
- Forgot the generated VNC password: set `VNC_PASSWORD` (in `.env` or the Portainer stack) and redeploy (the password file is rewritten on every start).
