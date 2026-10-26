"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates

from treinvertraging.config import get_settings
from treinvertraging.web import logbuffer, queries, routes

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    tz = ZoneInfo(get_settings().TIMEZONE)

    def local(value: datetime | None, fmt: str = "%d-%m-%Y %H:%M") -> str:
        if value is None:
            return "-"
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(tz).strftime(fmt)

    def eur(value: Decimal | float | int | None) -> str:
        if value is None:
            return "-"
        return f"€ {Decimal(value):.2f}".replace(".", ",")

    templates.env.filters["local"] = local
    templates.env.filters["eur"] = eur
    templates.env.globals["state_label"] = lambda s: queries.STATE_LABELS.get(str(s), str(s))
    templates.env.globals["app_name"] = "Treinvertraging"
    return templates


def create_app() -> FastAPI:
    """Build the app. Refuses to start without ``WEB_PASSWORD`` unless ``ALLOW_NO_AUTH=1``."""
    settings = get_settings()
    if not settings.WEB_PASSWORD:
        if os.environ.get("ALLOW_NO_AUTH") != "1":
            raise RuntimeError(
                "WEB_PASSWORD is niet ingesteld. Stel WEB_PASSWORD in, of zet ALLOW_NO_AUTH=1 "
                "om bewust zonder authenticatie te starten (alleen op een vertrouwd netwerk)."
            )
        log.warning("ALLOW_NO_AUTH=1: de webinterface draait ZONDER authenticatie")

    app = FastAPI(title="Treinvertraging", docs_url=None, redoc_url=None, openapi_url=None)
    logbuffer.install()
    app.state.templates = _make_templates()
    app.state.jobs = {}  # key -> asyncio.Task for "run now" style background jobs

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.include_router(routes.router)
    return app
