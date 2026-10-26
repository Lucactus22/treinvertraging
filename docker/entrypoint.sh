#!/bin/bash
# Container entrypoint: prepare volumes, VNC password, config seed, DB migrations, then supervisord.
# Starts as root so bind-mounted ./data and ./config can be chowned to `app`; supervisord drops
# privileges per program (user=app). If started as a non-root user, the chown steps are skipped
# and the mounted directories must already be writable by that user.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
CONFIG_DIR="${CONFIG_DIR:-/config}"
export DATA_DIR CONFIG_DIR

if [ "$(id -u)" = "0" ]; then
    RUN_AS="app"
    as_app() { setpriv --reuid=app --regid=app --init-groups "$@"; }
else
    RUN_AS="$(id -un)"
    as_app() { "$@"; }
fi

mkdir -p "$DATA_DIR" "$CONFIG_DIR"
if [ "$(id -u)" = "0" ]; then
    chown app:app "$DATA_DIR" "$CONFIG_DIR"
fi

# Data subfolders.
for d in profiles debug rdt-cache calibration; do
    mkdir -p "$DATA_DIR/$d"
done
if [ "$(id -u)" = "0" ]; then
    chown -R app:app "$DATA_DIR"
fi

# Seed /config from config.example when empty (never overwrite existing files).
if [ -z "$(ls -A "$CONFIG_DIR" 2>/dev/null)" ]; then
    echo "[entrypoint] /config is empty, seeding from /app/config.example"
    cp -a /app/config.example/. "$CONFIG_DIR"/
    [ "$(id -u)" = "0" ] && chown -R app:app "$CONFIG_DIR"
fi

# VNC password for x11vnc (noVNC takeover). Generate one if not supplied and print it once.
PASSFILE="$DATA_DIR/vnc.pass"
if [ -z "${VNC_PASSWORD:-}" ]; then
    if [ -f "$DATA_DIR/vnc.generated" ]; then
        echo "[entrypoint] VNC_PASSWORD not set; reusing previously generated password (see $DATA_DIR/vnc.generated)"
        VNC_PASSWORD="$(cat "$DATA_DIR/vnc.generated")"
    else
        VNC_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
        ( umask 077; printf '%s' "$VNC_PASSWORD" > "$DATA_DIR/vnc.generated" )
        echo "=================================================================="
        echo "[entrypoint] VNC_PASSWORD was not set. Generated one (shown once):"
        echo "[entrypoint]   $VNC_PASSWORD"
        echo "[entrypoint] Set VNC_PASSWORD in .env to choose your own."
        echo "=================================================================="
    fi
fi
# x11vnc only uses the first 8 characters of a VNC password.
if [ "${#VNC_PASSWORD}" -gt 8 ]; then
    echo "[entrypoint] note: VNC passwords are truncated to 8 characters by the VNC protocol"
fi
rm -f "$PASSFILE"
x11vnc -storepasswd "$VNC_PASSWORD" "$PASSFILE" >/dev/null 2>&1
chmod 600 "$PASSFILE"
[ -f "$DATA_DIR/vnc.generated" ] && chown "$RUN_AS" "$DATA_DIR/vnc.generated" 2>/dev/null || true
chown "$RUN_AS" "$PASSFILE" 2>/dev/null || true
unset VNC_PASSWORD

# X sockets dir (stale lock from an unclean stop would block Xvfb).
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Database migrations (alembic upgrade head via the app's own helper).
echo "[entrypoint] running database migrations"
as_app python -c "from treinvertraging.db import init_db; init_db()"

echo "[entrypoint] starting supervisord"
exec /usr/bin/supervisord -n -c /etc/supervisor/treinvertraging.conf
