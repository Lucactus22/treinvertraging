"""IMAP-based OTP provider (and generic mail reader for NS confirmation mails).

All imaplib work is synchronous and runs in ``asyncio.to_thread``. The pure parsing helpers
(:func:`parse_message`, :func:`extract_code`) are what the tests exercise with ``.eml`` fixtures.

Configuration (``ImapConfig``) comes from the constructor, :meth:`ImapConfig.from_mapping` or
:meth:`ImapConfig.from_env` (``IMAP_HOST``, ``IMAP_PORT``, ``IMAP_USER``, ``IMAP_PASSWORD``,
``IMAP_FOLDER``, ``IMAP_SENDERS`` comma separated, ``IMAP_CODE_REGEX``, ``IMAP_SSL``).
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from typing import Any

from treinvertraging.otp.base import OtpTimeout, ensure_aware

log = logging.getLogger(__name__)

DEFAULT_CODE_REGEX = r"\b\d{6}\b"
DEFAULT_SENDERS = ("ns.nl", "ov-chipkaart.nl")
# Mail clocks and ours differ a bit; accept messages up to this much before ``since``.
CLOCK_SLACK = timedelta(seconds=90)


@dataclass(frozen=True)
class ImapConfig:
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 993
    ssl: bool = True
    folder: str = "INBOX"
    senders: tuple[str, ...] = DEFAULT_SENDERS  # substrings of the From address, case-insensitive
    code_regex: str = DEFAULT_CODE_REGEX  # first capture group is used when the regex has one
    poll_interval: float = 5.0
    imap_timeout: float = 30.0

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any]) -> ImapConfig:
        senders = m.get("senders", DEFAULT_SENDERS)
        if isinstance(senders, str):
            senders = [s.strip() for s in re.split(r"[,\s]+", senders) if s.strip()]
        return cls(
            host=str(m["host"]),
            user=str(m["user"]),
            password=str(m["password"]),
            port=int(m.get("port", 993)),
            ssl=bool(m.get("ssl", True)),
            folder=str(m.get("folder", "INBOX")),
            senders=tuple(senders),
            code_regex=str(m.get("code_regex", DEFAULT_CODE_REGEX)),
            poll_interval=float(m.get("poll_interval", 5.0)),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, prefix: str = "IMAP_") -> ImapConfig:
        e = os.environ if env is None else env
        try:
            host, user, password = e[f"{prefix}HOST"], e[f"{prefix}USER"], e[f"{prefix}PASSWORD"]
        except KeyError as ex:
            raise ValueError(f"IMAP not configured: missing {ex.args[0]}") from None
        m: dict[str, Any] = {"host": host, "user": user, "password": password}
        for key, name in (
            ("port", "PORT"),
            ("folder", "FOLDER"),
            ("senders", "SENDERS"),
            ("code_regex", "CODE_REGEX"),
        ):
            if e.get(f"{prefix}{name}"):
                m[key] = e[f"{prefix}{name}"]
        if e.get(f"{prefix}SSL"):
            m["ssl"] = e[f"{prefix}SSL"].strip().lower() not in {"0", "false", "no"}
        return cls.from_mapping(m)


@dataclass
class MailMessage:
    uid: str
    sender: str  # bare address, lower-case
    subject: str
    date: datetime | None  # aware UTC
    text: str  # plain text body (HTML converted)


# --------------------------------------------------------------------------------------------
# parsing (pure)
# --------------------------------------------------------------------------------------------

_TAG_BLOCKS = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    html = _TAG_BLOCKS.sub(" ", html)
    html = re.sub(r"<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</h\d\s*>", "\n", html, flags=re.I)
    return re.sub(r"[ \t\r\f\v]+", " ", unescape(_TAGS.sub(" ", html))).strip()


def message_text(msg: EmailMessage) -> str:
    """Plain text of a message: ``text/plain`` parts, else the HTML parts converted to text."""
    plain: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        (plain if ctype == "text/plain" else html).append(str(content))
    if plain:
        return "\n".join(plain).strip()
    return html_to_text("\n".join(html))


def parse_message(raw: bytes | EmailMessage, uid: str = "") -> MailMessage:
    msg = raw if isinstance(raw, EmailMessage) else email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    date: datetime | None = None
    if msg["Date"]:
        try:
            date = ensure_aware(parsedate_to_datetime(str(msg["Date"]))).astimezone(UTC)
        except (TypeError, ValueError):
            date = None
    return MailMessage(
        uid=uid,
        sender=parseaddr(str(msg["From"] or ""))[1].lower(),
        subject=str(msg["Subject"] or ""),
        date=date,
        text=message_text(msg),
    )


def sender_matches(sender: str, filters: tuple[str, ...] | list[str] | None) -> bool:
    if not filters:
        return True
    s = sender.lower()
    return any(f.lower() in s for f in filters)


def extract_code(mail: MailMessage, code_regex: str = DEFAULT_CODE_REGEX) -> str | None:
    """First regex match in the subject, then in the body (group 1 if the regex has a group)."""
    rx = re.compile(code_regex)
    for text in (mail.subject, mail.text):
        m = rx.search(text)
        if m:
            return m.group(1) if rx.groups else m.group(0)
    return None


def find_code(
    mails: list[MailMessage],
    *,
    since: datetime,
    senders: tuple[str, ...] | list[str] | None = DEFAULT_SENDERS,
    code_regex: str = DEFAULT_CODE_REGEX,
) -> str | None:
    """Code from the newest matching mail that is not older than ``since`` (minus clock slack)."""
    threshold = ensure_aware(since) - CLOCK_SLACK
    for mail in sorted(mails, key=lambda m: m.date or datetime.min.replace(tzinfo=UTC), reverse=True):
        if not sender_matches(mail.sender, senders):
            continue
        if mail.date is None or mail.date < threshold:
            continue
        code = extract_code(mail, code_regex)
        if code:
            return code
    return None


# --------------------------------------------------------------------------------------------
# IMAP access (sync, run in a thread)
# --------------------------------------------------------------------------------------------


def _connect(cfg: ImapConfig) -> imaplib.IMAP4:
    cls = imaplib.IMAP4_SSL if cfg.ssl else imaplib.IMAP4
    conn = cls(cfg.host, cfg.port, timeout=cfg.imap_timeout)
    conn.login(cfg.user, cfg.password)
    return conn


def fetch_messages_sync(cfg: ImapConfig, since: datetime, *, limit: int = 30) -> list[MailMessage]:
    """Newest ``limit`` messages received on/after the day of ``since`` (read-only, no flag changes)."""
    since = ensure_aware(since).astimezone(UTC)
    day = (since - timedelta(days=1)).strftime("%d-%b-%Y")  # IMAP SINCE has day granularity
    conn = _connect(cfg)
    try:
        typ, _ = conn.select(cfg.folder, readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot select folder {cfg.folder!r}")
        typ, data = conn.search(None, "SINCE", day)
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-limit:]
        out: list[MailMessage] = []
        for mid in reversed(ids):
            typ, parts = conn.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK":
                continue
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    out.append(
                        parse_message(part[1], uid=mid.decode() if isinstance(mid, bytes) else str(mid))
                    )
                    break
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


class ImapOtpProvider:
    """Polls the mailbox for a fresh mail from the configured senders and extracts the code."""

    def __init__(self, config: ImapConfig) -> None:
        self.config = config

    async def list_messages(
        self,
        since: datetime,
        *,
        senders: tuple[str, ...] | list[str] | None = None,
        subject_regex: str | None = None,
        limit: int = 30,
    ) -> list[MailMessage]:
        """Mails since ``since`` (newest first) filtered by sender and subject; used for NS
        confirmation mails by the reconcile step."""
        mails = await asyncio.to_thread(fetch_messages_sync, self.config, since, limit=limit)
        threshold = ensure_aware(since) - CLOCK_SLACK
        sel = self.config.senders if senders is None else senders
        rx = re.compile(subject_regex, re.I) if subject_regex else None
        return [
            m
            for m in mails
            if sender_matches(m.sender, sel)
            and (m.date is None or m.date >= threshold)
            and (rx is None or rx.search(m.subject))
        ]

    async def get_code(self, account: Any, since: datetime, timeout: float) -> str:
        cfg = self.config
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                mails = await asyncio.to_thread(fetch_messages_sync, cfg, since)
                code = find_code(mails, since=since, senders=cfg.senders, code_regex=cfg.code_regex)
                if code:
                    return code
            except (imaplib.IMAP4.error, OSError) as exc:
                # transient (network, throttling): keep polling until the deadline
                log.warning("IMAP poll failed: %s", type(exc).__name__)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise OtpTimeout(f"no verification mail from {list(cfg.senders)} within {timeout:.0f}s")
            await asyncio.sleep(min(cfg.poll_interval, remaining))
