"""OTP provider interface and factory."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


class OtpTimeout(TimeoutError):
    """No verification code arrived in time."""


@runtime_checkable
class OtpProvider(Protocol):
    async def get_code(self, account: Any, since: datetime, timeout: float) -> str:
        """Return the verification code issued after ``since`` or raise :class:`OtpTimeout`.

        ``account`` is the ``models.Account`` row (or any object/label identifying it).
        """
        ...


def ensure_aware(dt: datetime) -> datetime:
    """Naive datetimes are assumed to be UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def get_otp_provider(account: Any, *, imap: Any = None) -> OtpProvider:
    """Pick the provider named by ``account.otp_provider`` (``"manual"`` default, or ``"imap"``).

    ``imap`` may be an :class:`~treinvertraging.otp.imap.ImapConfig`; otherwise it is read from the
    ``IMAP_*`` environment variables.
    """
    name = (getattr(account, "otp_provider", None) or "manual").lower()
    if name == "imap":
        from treinvertraging.otp.imap import ImapConfig, ImapOtpProvider

        return ImapOtpProvider(imap if imap is not None else ImapConfig.from_env())
    from treinvertraging.otp.manual import get_manual_provider

    return get_manual_provider()
