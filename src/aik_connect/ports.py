"""Vendor-neutral connector ports (AIK-046/047).

A vendor package implements one of these for each execution path it offers;
the desktop composition root hands the implementations to ``ConnectionService``.
Nothing here imports a vendor package, and no port ever returns a secret.

- ``KeyVerifier`` (API path): the service stores the key in the vault itself;
  the verifier only checks the stored reference with its vendor.
- ``HostLogin`` (subscription path): the official host CLI owns the login and
  its tokens; the connector starts/observes/cancels that login and binds the
  account the host reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Protocol, runtime_checkable

from aik_connection import Connection

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
MAX_DISPLAY_NAME = 64
METHODS = {"api": "api_key", "subscription": "host_login"}


def _check_id(value: object, field: str) -> None:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(f"Invalid identifier field: {field}.")


def _check_code(value: object) -> None:
    if value is not None and (type(value) is not str or _CODE.fullmatch(value) is None):
        raise ValueError("A result code must be an upper-case fixed code.")


@dataclass(frozen=True, slots=True)
class ConnectorInfo:
    """What a connector offers: one vendor and one execution path."""

    provider: str
    execution_kind: str
    official_host: str
    host_profile: str
    display_name: str

    def __post_init__(self) -> None:
        for field in ("provider", "official_host", "host_profile"):
            _check_id(getattr(self, field), field)
        if self.execution_kind not in METHODS:
            raise ValueError("A connector serves the api or subscription execution path.")
        name = self.display_name
        if type(name) is not str or not 1 <= len(name) <= MAX_DISPLAY_NAME or name != name.strip() \
                or any(ord(char) < 32 for char in name):
            raise ValueError("A display name must be 1-64 printable characters.")

    @property
    def method(self) -> str:
        return METHODS[self.execution_kind]


class CheckState(str, Enum):
    VERIFIED = "verified"
    UNAUTHENTICATED = "unauthenticated"
    LOCKED = "locked"
    ACCOUNT_CHANGED = "account_changed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One observed authentication check. ``account_binding`` is set only when verified."""

    state: CheckState
    account_binding: str | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not CheckState:
            raise ValueError("A check result needs a CheckState.")
        if self.account_binding is not None:
            _check_id(self.account_binding, "account_binding")
            if self.state is not CheckState.VERIFIED:
                raise ValueError("Only a verified check names an account.")
        _check_code(self.code)


class LoginState(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    SIGNED_IN = "signed_in"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class LoginStatus:
    state: LoginState
    cancel_supported: bool
    code: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not LoginState or type(self.cancel_supported) is not bool:
            raise ValueError("A login status needs a LoginState and a cancel flag.")
        _check_code(self.code)


@runtime_checkable
class KeyVerifier(Protocol):
    info: ConnectorInfo

    def accepts(self, secret: str) -> bool:
        """Local format check only (no network); the secret is not kept."""

    def verify(self, connection: Connection) -> CheckResult:
        """Check the key behind ``connection.credential_ref`` with the vendor."""


@runtime_checkable
class HostLogin(Protocol):
    info: ConnectorInfo

    def start_login(self, connection_id: str) -> LoginStatus:
        """Start the official interactive login (or report that it is already signed in)."""

    def login_status(self) -> LoginStatus:
        """Observe the login started last; IDLE when none was started."""

    def cancel_login(self) -> bool:
        """Stop a running login; False when nothing ran or cancelling is not supported."""

    def connect(self, connection_id: str) -> CheckResult:
        """Bind the account the host reports now; VERIFIED carries its account binding."""

    def check(self, connection: Connection) -> CheckResult:
        """Re-observe the host account for a stored connection."""

    def logout(self, connection: Connection) -> None:
        """Ask the official host to sign out (a local effect on this machine)."""


__all__ = ["ConnectorInfo", "CheckState", "CheckResult", "LoginState", "LoginStatus", "KeyVerifier",
           "HostLogin", "METHODS"]
