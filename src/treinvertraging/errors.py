"""Exception types shared by all modules."""

from __future__ import annotations


class TreinvertragingError(Exception):
    """Base class for all application errors."""


class LoginRequired(TreinvertragingError):
    """The session is not (or no longer) logged in and automatic login failed."""


class BlockedByBotProtection(TreinvertragingError):
    """The site served a bot-protection challenge or block page (Akamai, reCAPTCHA, ...)."""


class LayoutChanged(TreinvertragingError):
    """The page does not look as configured (unknown heading, missing label, ...)."""


class InvalidInput(TreinvertragingError):
    """The site rejected the supplied data. Never retried."""


class TakeoverTimeout(TreinvertragingError):
    """A human takeover was requested but nobody finished it in time."""
