"""HTTP routes of the web UI (server-rendered Jinja2 + htmx)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import mimetypes
import re
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from treinvertraging import crypto, services
from treinvertraging.config import get_settings
from treinvertraging.db import get_session_factory
from treinvertraging.domain import Source
from treinvertraging.models import Account, Event
from treinvertraging.web import logbuffer, queries

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
QUICK_WAIT_SECONDS = 1.0
OTP_PROVIDERS = ("manual", "imap")
CALIBRATION_CHANNELS = {"ns": "Mijn NS", "ovchip": "OV-chipkaart", "gtbv": "Publiek claimformulier"}
RUN_ACTIONS: dict[str, tuple[str, str]] = {
    "sync": ("run_sync", "Synchroniseren"),
    "assess": ("run_assess", "Beoordelen"),
    "claim": ("run_claim", "Claims indienen"),
    "canary": ("run_canary", "Canary-controle"),
}

# ---------------------------------------------------------------------------
# Auth, DB session, rendering helpers
# ---------------------------------------------------------------------------

_basic = HTTPBasic(auto_error=False)


def require_auth(request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """HTTP Basic auth plus a same-origin check on state-changing requests."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if origin and origin != "null" and host and urlsplit(origin).netloc != host:
            raise HTTPException(status_code=403, detail="Cross-origin request geweigerd")
        if origin == "null":
            raise HTTPException(status_code=403, detail="Cross-origin request geweigerd")
    settings = get_settings()
    if not settings.WEB_PASSWORD:  # only reachable with ALLOW_NO_AUTH=1 (checked in create_app)
        return
    ok = False
    if credentials is not None:
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.WEB_USERNAME.encode())
        pass_ok = secrets.compare_digest(credentials.password.encode(), settings.WEB_PASSWORD.encode())
        ok = user_ok and pass_ok
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Inloggen vereist",
            headers={"WWW-Authenticate": 'Basic realm="Treinvertraging", charset="UTF-8"'},
        )


def get_db():
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


router = APIRouter(dependencies=[Depends(require_auth)])


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    level = request.query_params.get("lvl", "")
    msg = request.query_params.get("msg", "")
    flash = {"level": level if level in ("info", "success", "warning", "error") else "info", "msg": msg}
    context.setdefault("flash", flash if msg else None)
    return request.app.state.templates.TemplateResponse(request, name, context, status_code=status_code)


def respond(request: Request, level: str, msg: str, to: str) -> Response:
    """htmx requests get a flash fragment; plain form posts get a redirect with ?msg=."""
    if request.headers.get("hx-request"):
        return request.app.state.templates.TemplateResponse(
            request, "partials/flash.html", {"flash": {"level": level, "msg": msg}}
        )
    return RedirectResponse(f"{to}?{urlencode({'lvl': level, 'msg': msg})}", status_code=303)


NOT_IMPLEMENTED_MSG = "Deze functie is nog niet beschikbaar (de pipeline is nog niet gekoppeld)."


def _short(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:300]


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def launch(
    request: Request, key: str, label: str, factory: Callable[[], Awaitable[Any]]
) -> tuple[str, str]:
    """Run a service call as a background task, waiting briefly so quick results/errors show up.

    Returns ``(flash_level, message)``. Prevents starting the same job twice at the same time.
    """
    jobs: dict[str, asyncio.Task] = request.app.state.jobs
    running = jobs.get(key)
    if running is not None and not running.done():
        return "warning", f"{label} loopt al."
    task = asyncio.ensure_future(factory())
    jobs[key] = task

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            log.info("%s klaar: %s", label, t.result())
        elif isinstance(exc, NotImplementedError):
            log.info("%s: niet beschikbaar (%s)", label, exc)
        else:
            log.error("%s mislukt: %s", label, _short(exc))

    task.add_done_callback(_done)
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=QUICK_WAIT_SECONDS)
    except TimeoutError:
        return "info", f"{label} is gestart op de achtergrond. Volg de voortgang in het logboek."
    except NotImplementedError:
        return "warning", NOT_IMPLEMENTED_MSG
    except Exception as exc:
        return "error", f"{label} mislukt: {_short(exc)}"
    return "success", str(result) if result else f"{label} klaar."


# ---------------------------------------------------------------------------
# Optional integrations owned by WP-D (imported lazily)
# ---------------------------------------------------------------------------


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return default


async def active_takeovers() -> list[dict[str, Any]]:
    try:
        from treinvertraging.browser import takeover

        items = await _maybe_await(takeover.active_takeovers())
    except Exception:  # ImportError, AttributeError or runtime problems: show nothing
        return []
    return [
        {
            "id": _field(i, "id", "takeover_id"),
            "who": _field(i, "account_label", "account", "label", default=""),
            "reason": _field(i, "reason", default=""),
            "since": _field(i, "since", "started_at", "created_at"),
        }
        for i in (items or [])
    ]


async def pending_prompts() -> list[dict[str, Any]]:
    try:
        from treinvertraging.otp import manual

        items = await _maybe_await(manual.pending_prompts())
    except Exception:
        return []
    return [
        {
            "id": _field(i, "id", "prompt_id"),
            "who": _field(i, "account_label", "account", "label", default=""),
            "hint": _field(i, "message", "hint", "reason", default=""),
            "since": _field(i, "since", "requested_at", "created_at"),
        }
        for i in (items or [])
    ]


def novnc_url() -> str:
    parts = urlsplit(get_settings().PUBLIC_URL)
    host = parts.hostname or "localhost"
    return f"{parts.scheme or 'http'}://{host}:6080/vnc.html"


# ---------------------------------------------------------------------------
# Dashboard and banners
# ---------------------------------------------------------------------------


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    stats = queries.dashboard_stats(db)
    try:
        runs: list[dict] | None = await services.next_runs()
    except NotImplementedError:
        runs = None
    except Exception as exc:
        log.warning("next_runs failed: %s", _short(exc))
        runs = None
    return render(
        request,
        "dashboard.html",
        stats=stats,
        runs=runs,
        claim_mode=get_settings().CLAIM_MODE,
        states=queries.STATE_LABELS,
    )


@router.get("/partials/banners")
async def banners(request: Request) -> Response:
    return render(
        request,
        "partials/banners.html",
        takeovers=await active_takeovers(),
        prompts=await pending_prompts(),
        novnc=novnc_url(),
    )


@router.post("/takeover/{takeover_id}/done")
async def takeover_done(request: Request, takeover_id: str) -> Response:
    items = await active_takeovers()
    match = next((i for i in items if str(i["id"]) == takeover_id), None)
    if match is None:
        return respond(request, "warning", "Deze overname is niet (meer) actief.", "/")
    try:
        from treinvertraging.browser import takeover

        await _maybe_await(takeover.complete_takeover(match["id"]))
    except Exception as exc:
        return respond(request, "error", f"Afronden mislukt: {_short(exc)}", "/")
    return respond(request, "success", "Overname afgerond, de bot gaat verder.", "/")


# ---------------------------------------------------------------------------
# OTP prompt
# ---------------------------------------------------------------------------


@router.get("/otp")
async def otp_page(request: Request) -> Response:
    return render(request, "otp.html", prompts=await pending_prompts(), poll_banners=False)


@router.post("/otp/{prompt_id}")
async def otp_submit(request: Request, prompt_id: str, code: str = Form("")) -> Response:
    code = code.strip()
    if not code:
        return respond(request, "error", "Vul een code in.", "/otp")
    items = await pending_prompts()
    match = next((i for i in items if str(i["id"]) == prompt_id), None)
    if match is None:
        return respond(request, "warning", "Deze code-aanvraag is niet (meer) actief.", "/otp")
    try:
        from treinvertraging.otp import manual

        await _maybe_await(manual.submit_code(match["id"], code))
    except Exception as exc:
        return respond(request, "error", f"Code versturen mislukt: {_short(exc)}", "/otp")
    return respond(request, "success", "Code verstuurd.", "/otp")


# ---------------------------------------------------------------------------
# Trips and claims
# ---------------------------------------------------------------------------


@router.get("/trips")
def trips_page(request: Request, filter: str = "all", db: Session = Depends(get_db)) -> Response:
    if filter not in queries.TRIP_FILTERS:
        filter = "all"
    return render(
        request,
        "trips.html",
        rows=queries.list_trips(db, filter),
        filter=filter,
        filters=queries.TRIP_FILTERS,
        states=queries.STATE_LABELS,
    )


@router.get("/claims")
def claims_page(request: Request, db: Session = Depends(get_db)) -> Response:
    claims = queries.list_claims(db)
    items = [
        {
            "claim": c,
            "assessment": queries.latest_assessment(c.trip),
            "files": queries.debug_files(c.debug_dir),
        }
        for c in claims
    ]
    return render(request, "claims.html", items=items, states=queries.STATE_LABELS)


async def _claim_action(request: Request, claim_id: int, fn_name: str, ok_msg: str) -> Response:
    fn = getattr(services, fn_name)
    try:
        await fn(claim_id)
    except NotImplementedError:
        return respond(request, "warning", NOT_IMPLEMENTED_MSG, "/claims")
    except services.ClaimNotFound:
        return respond(request, "error", f"Claim {claim_id} bestaat niet.", "/claims")
    except services.InvalidTransition as exc:
        return respond(request, "warning", f"Niet mogelijk: {exc}", "/claims")
    except Exception as exc:
        log.exception("%s(%s) failed", fn_name, claim_id)
        return respond(request, "error", f"Mislukt: {_short(exc)}", "/claims")
    return respond(request, "success", ok_msg, "/claims")


@router.post("/claims/{claim_id}/approve")
async def claim_approve(request: Request, claim_id: int) -> Response:
    return await _claim_action(request, claim_id, "approve_claim", f"Claim {claim_id} goedgekeurd.")


@router.post("/claims/{claim_id}/reject")
async def claim_reject(request: Request, claim_id: int) -> Response:
    return await _claim_action(request, claim_id, "reject_claim", f"Claim {claim_id} afgewezen.")


@router.post("/claims/{claim_id}/retry")
async def claim_retry(request: Request, claim_id: int) -> Response:
    return await _claim_action(request, claim_id, "retry_claim", f"Claim {claim_id} opnieuw ingepland.")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")


def _valid_iban(iban: str) -> bool:
    if not _IBAN_RE.match(iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def _last4(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return crypto.decrypt(token)[-4:]
    except Exception:
        return "????"


def _account_view(a: Account) -> dict[str, Any]:
    return {
        "acc": a,
        "card_last4": _last4(a.card_number_enc),
        "iban_last4": _last4(a.iban_enc),
        "has_password": bool(a.password_enc),
    }


def _blank_form() -> dict[str, Any]:
    return {
        "source": Source.NS.value,
        "label": "",
        "username": "",
        "holder_name": "",
        "email": "",
        "otp_provider": "manual",
        "enabled": True,
    }


@router.get("/accounts")
def accounts_page(request: Request, db: Session = Depends(get_db)) -> Response:
    imap = queries.kv_get_all(db, "imap.")
    return render(
        request,
        "accounts.html",
        accounts=[_account_view(a) for a in queries.list_accounts(db)],
        channels=CALIBRATION_CHANNELS,
        imap=imap,
        imap_has_password=bool(imap.get("password_enc")),
        errors=[],
    )


@router.get("/accounts/new")
def account_new(request: Request) -> Response:
    return render(
        request, "account_form.html", account=None, form=_blank_form(), errors=[], card_hint=None, iban_hint=None
    )


@router.get("/accounts/{account_id}/edit")
def account_edit(request: Request, account_id: int, db: Session = Depends(get_db)) -> Response:
    acc = db.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "Account niet gevonden")
    form = {
        "source": acc.source,
        "label": acc.label,
        "username": acc.username,
        "holder_name": acc.holder_name,
        "email": acc.email,
        "otp_provider": acc.otp_provider,
        "enabled": acc.enabled,
    }
    return render(
        request,
        "account_form.html",
        account=acc,
        form=form,
        errors=[],
        card_hint=_last4(acc.card_number_enc),
        iban_hint=_last4(acc.iban_enc),
    )


def _apply_account_form(
    acc: Account | None,
    *,
    source: str,
    label: str,
    username: str,
    password: str,
    card_number: str,
    iban: str,
    holder_name: str,
    email: str,
    otp_provider: str,
    enabled: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate; returns (normalised values, errors)."""
    errors: list[str] = []
    label, username, holder_name, email = label.strip(), username.strip(), holder_name.strip(), email.strip()
    if source not in {s.value for s in Source}:
        errors.append("Kies een geldige bron (Mijn NS of OV-chipkaart).")
    if not label:
        errors.append("Geef het account een naam.")
    if otp_provider not in OTP_PROVIDERS:
        errors.append("Onbekende OTP-methode.")
    if acc is None:
        if not username:
            errors.append("Gebruikersnaam (e-mailadres) is verplicht.")
        if not password:
            errors.append("Wachtwoord is verplicht.")
    card = re.sub(r"[\s-]", "", card_number)
    if card and not re.fullmatch(r"\d{16}", card):
        errors.append("Een OV-chipkaartnummer bestaat uit 16 cijfers.")
    iban_norm = re.sub(r"\s", "", iban).upper()
    if iban_norm and not _valid_iban(iban_norm):
        errors.append("Dit IBAN-nummer is niet geldig.")
    if email and "@" not in email:
        errors.append("Dit e-mailadres is niet geldig.")
    if errors:
        return None, errors
    return {
        "source": source,
        "label": label,
        "username": username,
        "password": password,
        "card": card,
        "iban": iban_norm,
        "holder_name": holder_name,
        "email": email,
        "otp_provider": otp_provider,
        "enabled": enabled,
    }, []


def _save_account(db: Session, acc: Account, v: dict[str, Any]) -> None:
    acc.source = v["source"]
    acc.label = v["label"]
    if v["username"] or acc.id is None:
        acc.username = v["username"]
    acc.holder_name = v["holder_name"]
    acc.email = v["email"]
    acc.otp_provider = v["otp_provider"]
    acc.enabled = v["enabled"]
    # Secrets: blank on edit means "keep the stored value".
    if v["password"]:
        acc.password_enc = crypto.encrypt(v["password"])
    if v["card"]:
        acc.card_number_enc = crypto.encrypt(v["card"])
    if v["iban"]:
        acc.iban_enc = crypto.encrypt(v["iban"])


async def _account_post(
    request: Request, db: Session, acc: Account | None, values: dict[str, Any]
) -> Response:
    v, errors = _apply_account_form(acc, **values)
    if v is None:
        form = {k: values[k] for k in ("source", "label", "username", "holder_name", "email", "otp_provider", "enabled")}
        return render(
            request,
            "account_form.html",
            status_code=422,
            account=acc,
            form=form,
            errors=errors,
            card_hint=_last4(acc.card_number_enc) if acc else None,
            iban_hint=_last4(acc.iban_enc) if acc else None,
        )
    creating = acc is None
    if acc is None:
        acc = Account(source=v["source"], label=v["label"])
        db.add(acc)
    _save_account(db, acc, v)
    db.flush()
    db.add(
        Event(
            level="info",
            kind="account.created" if creating else "account.updated",
            message=f"account {acc.id} ({acc.label})",
        )
    )
    db.commit()
    return respond(request, "success", f"Account '{acc.label}' opgeslagen.", "/accounts")


@router.post("/accounts")
async def account_create(
    request: Request,
    source: str = Form(""),
    label: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    card_number: str = Form(""),
    iban: str = Form(""),
    holder_name: str = Form(""),
    email: str = Form(""),
    otp_provider: str = Form("manual"),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    values = dict(
        source=source, label=label, username=username, password=password, card_number=card_number,
        iban=iban, holder_name=holder_name, email=email, otp_provider=otp_provider, enabled=enabled is not None,
    )  # fmt: skip
    return await _account_post(request, db, None, values)


@router.post("/accounts/{account_id}")
async def account_update(
    request: Request,
    account_id: int,
    source: str = Form(""),
    label: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    card_number: str = Form(""),
    iban: str = Form(""),
    holder_name: str = Form(""),
    email: str = Form(""),
    otp_provider: str = Form("manual"),
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    acc = db.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "Account niet gevonden")
    values = dict(
        source=source, label=label, username=username, password=password, card_number=card_number,
        iban=iban, holder_name=holder_name, email=email, otp_provider=otp_provider, enabled=enabled is not None,
    )  # fmt: skip
    return await _account_post(request, db, acc, values)


@router.post("/accounts/{account_id}/test-login")
async def account_test_login(request: Request, account_id: int, db: Session = Depends(get_db)) -> Response:
    if db.get(Account, account_id) is None:
        return respond(request, "error", "Account niet gevonden.", "/accounts")
    level, msg = await launch(
        request, f"test_login:{account_id}", "Test login", lambda: services.test_login(account_id)
    )
    return respond(request, level, msg, "/accounts")


@router.post("/accounts/{account_id}/calibrate")
async def account_calibrate(
    request: Request, account_id: int, channel: str = Form("ns"), db: Session = Depends(get_db)
) -> Response:
    if channel not in CALIBRATION_CHANNELS:
        return respond(request, "error", "Onbekend kanaal.", "/accounts")
    if db.get(Account, account_id) is None:
        return respond(request, "error", "Account niet gevonden.", "/accounts")
    level, msg = await launch(
        request,
        f"calibrate:{account_id}",
        "Kalibratie",
        lambda: services.start_calibration(account_id, channel),
    )
    return respond(request, level, msg, "/accounts")


@router.post("/settings/imap")
async def imap_settings(
    request: Request,
    host: str = Form(""),
    port: str = Form("993"),
    user: str = Form(""),
    password: str = Form(""),
    folder: str = Form("INBOX"),
    senders: str = Form(""),
    code_regex: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    if port.strip() and not port.strip().isdigit():
        return respond(request, "error", "Poort moet een getal zijn.", "/accounts")
    if code_regex.strip():
        try:
            re.compile(code_regex.strip())
        except re.error:
            return respond(request, "error", "Ongeldige reguliere expressie voor de code.", "/accounts")
    for key, value in (
        ("host", host), ("port", port), ("user", user), ("folder", folder),
        ("senders", senders), ("code_regex", code_regex),
    ):  # fmt: skip
        queries.kv_set(db, f"imap.{key}", value.strip())
    if password:
        queries.kv_set(db, "imap.password_enc", crypto.encrypt(password))
    db.commit()
    return respond(request, "success", "IMAP-instellingen opgeslagen.", "/accounts")


# ---------------------------------------------------------------------------
# Run now + live log
# ---------------------------------------------------------------------------


@router.get("/run")
def run_page(request: Request) -> Response:
    jobs: dict[str, asyncio.Task] = request.app.state.jobs
    running = sorted(k for k, t in jobs.items() if not t.done())
    return render(request, "run.html", actions=RUN_ACTIONS, running=running)


@router.post("/run/{action}")
async def run_action(request: Request, action: str) -> Response:
    if action not in RUN_ACTIONS:
        raise HTTPException(404, "Onbekende actie")
    fn_name, label = RUN_ACTIONS[action]
    level, msg = await launch(request, f"run:{action}", label, lambda: getattr(services, fn_name)())
    return respond(request, level, msg, "/run")


@router.get("/run/logs")
def run_logs(lines: int = 200) -> Response:
    handler = logbuffer.get_handler()
    text = "\n".join(ln.format() for ln in handler.lines_after(0)[-max(1, min(lines, 500)) :])
    return PlainTextResponse(text)


@router.get("/run/logs/stream")
async def run_logs_stream(request: Request) -> EventSourceResponse:
    async def events():
        async for line in logbuffer.stream(logbuffer.get_handler()):
            if await request.is_disconnected():
                return
            yield {"data": line}

    return EventSourceResponse(events(), ping=15)


# ---------------------------------------------------------------------------
# Static files and debug bundles
# ---------------------------------------------------------------------------


@router.get("/static/{name}")
def static_file(name: str) -> Response:
    target = (STATIC_DIR / name).resolve()
    if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


_TEXT_ONLY = {".html", ".htm", ".xhtml", ".svg", ".xml", ".js", ".mjs"}


@router.get("/debug/{path:path}")
def debug_file(request: Request, path: str) -> Response:
    target = queries.safe_debug_path(path)
    if target is None:
        raise HTTPException(404, "Niet gevonden")
    if target.is_dir():
        root = queries.debug_root()
        # Bundle directories are named by timestamp: newest first at the root, alphabetical inside.
        entries = sorted(target.iterdir(), key=lambda p: p.name, reverse=target == root)
        rel = "" if target == root else target.relative_to(root).as_posix()
        items = [
            {
                "name": e.name + ("/" if e.is_dir() else ""),
                "url": f"/debug/{(rel + '/' if rel else '') + e.name}",
                "image": e.suffix.lower() in queries.IMAGE_SUFFIXES,
            }
            for e in entries
        ]
        return render(request, "debug_dir.html", rel=rel, items=items)
    # Captured pages come from third-party sites: never let the browser execute them here.
    media_type = "text/plain; charset=utf-8" if target.suffix.lower() in _TEXT_ONLY else None
    if media_type is None:
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        },
    )
