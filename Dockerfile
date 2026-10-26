# syntax=docker/dockerfile:1
# Base image version must match playwright==1.63.0 in pyproject.toml.
# It already ships Chromium under /ms-playwright; never run `playwright install` here.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Amsterdam \
    DISPLAY=:99 \
    DATA_DIR=/data \
    CONFIG_DIR=/config \
    ALEMBIC_DIR=/app/alembic \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify supervisor tzdata curl ca-certificates python3-venv \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. Fixed uid/gid 1000 (the base image's default `ubuntu` user is removed
# if it holds that id). The entrypoint starts as root only to fix volume ownership; supervisord
# then runs every program as `app`.
RUN (userdel -r ubuntu 2>/dev/null || true) \
    && (getent group 1000 >/dev/null && groupdel "$(getent group 1000 | cut -d: -f1)" || true) \
    && groupadd -g 1000 app \
    && useradd -m -u 1000 -g 1000 -s /bin/bash app \
    && mkdir -p /data /config /app /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && chown app:app /data /config /app

WORKDIR /app

# Install the project into an isolated venv (the base image's Debian-packaged Python libs can't be
# upgraded by pip). Dependencies are resolved from pyproject.toml.
RUN python -m venv /opt/venv
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY config.example ./config.example
# Optional: behind a TLS-intercepting proxy, build with
#   docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt .
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install .

COPY docker/supervisord.conf /etc/supervisor/treinvertraging.conf
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data", "/config"]
EXPOSE 8080 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
