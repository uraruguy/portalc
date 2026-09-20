"""Every failure portalc can produce, with a name a human can act on.

A portalc error is never printed as a traceback first. It is printed as
what broke, where, and what to do next; the traceback is available behind
`--debug` for the cases where it actually helps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PortalcError(Exception):
    """Base class. Carries a human title, detail lines and a next step."""

    kind = "error"
    exit_code = 1

    def __init__(
        self,
        title: str,
        detail: str | list[str] = "",
        hint: str = "",
        data: dict[str, Any] | None = None,
    ):
        super().__init__(title)
        self.title = title
        self.detail = [detail] if isinstance(detail, str) else list(detail)
        self.detail = [d for d in self.detail if d]
        self.hint = hint
        self.data = data or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "hint": self.hint,
            "data": self.data,
        }


class UsageError(PortalcError):
    """The command was asked to do something that does not make sense."""

    kind = "usage"
    exit_code = 2


class ConfigError(PortalcError):
    """portal.yaml, a flow file or the lockfile is missing or malformed."""

    kind = "config"
    exit_code = 2


class PortalAuthError(PortalcError):
    """The session is not authenticated. A human is needed, not a retry."""

    kind = "auth"
    exit_code = 3


class PortalBlockedError(PortalcError):
    """The portal actively refused us (anti-bot, rate limit, 403)."""

    kind = "blocked"
    exit_code = 4


class PortalDownError(PortalcError):
    """The portal could not be reached or did not render in time."""

    kind = "down"
    exit_code = 5


class PortalDriftError(PortalcError):
    """The portal responded, but no longer matches the agreed schema.

    This is the error the whole product exists to raise: never return data
    that does not match the contract, and never call a mismatch a success.
    """

    kind = "drift"
    exit_code = 6


class BrowserUnavailableError(PortalcError):
    """No browser backend could be started or attached to."""

    kind = "browser"
    exit_code = 7


class VerificationFailed(PortalcError):
    """`portalc verify` found at least one failing check."""

    kind = "verify"
    exit_code = 8


@dataclass
class Finding:
    """One thing `verify` checked, and how it went."""

    check: str
    ok: bool
    message: str
    severity: str = "error"  # error | warn | info
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "ok": self.ok,
            "message": self.message,
            "severity": self.severity,
            "data": self.data,
        }
