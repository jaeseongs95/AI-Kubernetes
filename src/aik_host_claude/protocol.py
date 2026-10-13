"""AIK-015 host protocol assumptions and pure parsers for Claude Code.

UNVERIFIED ASSUMPTIONS. None of the following was confirmed against current
official documents:
- the command names
- the exit codes
- the JSON field names
- the auth method values
- the minimum version

``HostCommands`` makes the commands replaceable without code changes. The
parsers read only these bounded fields:
- ``loggedIn``, ``authMethod``, ``accountId``, ``expired``
- ``type``, ``subtype``, ``is_error``, ``result``, ``session_id``, ``model``

Unknown fields are ignored and never copied. stderr is never parsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re

from aik_provider.contracts import AuthState
from aik_security.redaction import OutputRedactor

from .runner import SOURCES, CommandResult

MODEL_PLACEHOLDER = "{model}"
MIN_HOST_VERSION = (1, 0, 0)          # unverified assumption
STATUS_UNSUPPORTED_EXIT = 2           # unverified: usage error for an unknown subcommand
SUBSCRIPTION_METHODS = frozenset({"claude.ai", "subscription"})       # unverified
API_KEY_METHODS = frozenset({"api_key", "apiKey", "api-key", "console"})  # unverified
MAX_JSON_BYTES = 4 * 1024 * 1024
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_VERSION = re.compile(r"\s*v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?![0-9])")
_VERSION_TEXT = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_ARG = re.compile(r"[-A-Za-z0-9{}._:/=]{1,128}\Z")
_FINGERPRINT_DOMAIN = b"AIK-015/claude-host-account/v1\x00"


class HostState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"            # executable not found / not startable
    UNSUPPORTED = "unsupported"  # present, but version/status protocol not supported
    UNKNOWN = "unknown"          # timeout or unreadable output; not a verdict


class AuthMethod(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"
    NONE = "none"
    UNKNOWN = "unknown"


def account_fingerprint(account_id: str) -> str:
    """Domain-separated digest of a *host-provided* account identifier.

    The input is the identifier reported by the official host. It is never
    derived from a label, an e-mail address or a token. Only the digest is
    retained.
    """
    if type(account_id) is not str or _ACCOUNT.fullmatch(account_id) is None:
        raise ValueError("INVALID_ACCOUNT_ID")
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN)
    digest.update(account_id.encode("utf-8"))
    return digest.hexdigest()


def safe_account_id(value: object) -> str | None:
    """Accept only a bounded opaque identifier. Reject e-mail and token-like values."""
    if type(value) is not str or _ACCOUNT.fullmatch(value) is None:
        return None
    return value if OutputRedactor().redact_text(value) == value else None


def _reject_constant(_value: str):
    raise ValueError("NON_FINITE_JSON")


def _json_object(data: object) -> dict | None:
    if type(data) is not bytes or not data or len(data) > MAX_JSON_BYTES:
        return None
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if type(value) is dict else None


def parse_version(result: CommandResult) -> str | None:
    if type(result) is not CommandResult or result.exit_code != 0:
        return None
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    match = _VERSION.match(text)
    if match is None:
        return None
    return ".".join(str(int(part)) for part in match.groups())


def version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def parse_status(stdout: bytes):
    """Return ``(AuthState, AuthMethod, account_id | None, reasons)`` or None if unrecognized.

    The process exit code is checked by the caller; this parser sees stdout only.
    """
    payload = _json_object(stdout)
    if payload is None:
        return None
    logged_in = payload.get("loggedIn")
    if type(logged_in) is not bool:
        return None
    if not logged_in:
        if payload.get("expired") is True:
            return AuthState.EXPIRED, AuthMethod.NONE, None, ("AUTH_EXPIRED",)
        return AuthState.UNAUTHENTICATED, AuthMethod.NONE, None, ("AUTH_REQUIRED",)
    if payload.get("expired") is True:
        # "Logged in" and "expired" at once is contradictory: never treat it as usable.
        return AuthState.UNKNOWN, AuthMethod.UNKNOWN, None, ("AUTH_STATUS_CONTRADICTORY",)
    method = payload.get("authMethod")
    if type(method) is str and method in API_KEY_METHODS:
        # The host is using an API key, not a subscription login: never run on it.
        return AuthState.UNAUTHENTICATED, AuthMethod.API_KEY, None, ("API_KEY_AUTH_NOT_SUBSCRIPTION",)
    if type(method) is not str or method not in SUBSCRIPTION_METHODS:
        return AuthState.UNKNOWN, AuthMethod.UNKNOWN, None, ("AUTH_METHOD_UNKNOWN",)
    account = safe_account_id(payload.get("accountId"))
    if account is None:
        return AuthState.UNKNOWN, AuthMethod.SUBSCRIPTION, None, ("ACCOUNT_NOT_OBSERVED",)
    return AuthState.AUTHENTICATED, AuthMethod.SUBSCRIPTION, account, ()


@dataclass(frozen=True, slots=True)
class JobOutput:
    session_id: str
    model: str | None
    text: str
    succeeded: bool


def parse_job(result: CommandResult) -> JobOutput | None:
    """Interpret the assumed non-interactive JSON result. None means unknown outcome."""
    if type(result) is not CommandResult:
        return None
    payload = _json_object(result.stdout)
    if payload is None or payload.get("type") != "result":
        return None
    redactor = OutputRedactor()
    session = payload.get("session_id")
    if type(session) is not str or _JOB_ID.fullmatch(session) is None or redactor.safe_id(session) != session:
        return None
    is_error = payload.get("is_error")
    if type(is_error) is not bool:
        return None
    subtype = payload.get("subtype")
    if subtype is not None and type(subtype) is not str:
        return None
    text = payload.get("result", "")
    if text is None:
        text = ""
    if type(text) is not str:
        return None
    if result.exit_code != 0 and not is_error:
        return None  # inconsistent: a failing process that claims success
    if not is_error and subtype != "success":
        # No error reported, but no recognized success either: the host may have
        # acted, so this is not a known failure.
        return None
    model = payload.get("model")
    if not (type(model) is str and _MODEL.fullmatch(model) and redactor.redact_text(model) == model):
        model = None
    succeeded = not is_error and subtype == "success" and result.exit_code == 0
    return JobOutput(session, model, text, succeeded)


def _argument_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > 32:
        raise ValueError("INVALID_HOST_COMMAND")
    for argument in value:
        if type(argument) is not str or _ARG.fullmatch(argument) is None:
            raise ValueError("INVALID_HOST_COMMAND")
    return value


@dataclass(frozen=True, slots=True)
class HostCommands:
    """Configurable host argv. All defaults are UNVERIFIED assumptions."""

    executable: str = "claude"
    version: tuple[str, ...] = ("--version",)
    status: tuple[str, ...] = ("auth", "status", "--json")
    login: tuple[str, ...] = ("auth", "login")
    logout: tuple[str, ...] = ("auth", "logout")
    execute: tuple[str, ...] = ("-p", "--output-format", "json", "--model", MODEL_PLACEHOLDER)

    def __post_init__(self) -> None:
        if (type(self.executable) is not str or not self.executable or len(self.executable) > 4096
                or any(char in self.executable for char in "\0\r\n")):
            raise ValueError("INVALID_HOST_EXECUTABLE")
        for name in ("version", "status", "login", "logout"):
            if MODEL_PLACEHOLDER in _argument_tuple(getattr(self, name)):
                raise ValueError("INVALID_HOST_COMMAND")
        if _argument_tuple(self.execute).count(MODEL_PLACEHOLDER) != 1:
            raise ValueError("INVALID_HOST_COMMAND")

    def argv(self, name: str, model: str | None = None) -> tuple[str, ...]:
        if name not in ("version", "status", "login", "logout", "execute"):
            raise ValueError("INVALID_HOST_COMMAND")
        arguments = getattr(self, name)
        if name == "execute":
            if type(model) is not str or not model:
                raise ValueError("INVALID_MODEL")
            arguments = tuple(model if item == MODEL_PLACEHOLDER else item for item in arguments)
        return (self.executable, *arguments)


@dataclass(frozen=True, slots=True)
class HostStatus:
    """One runtime observation of the host. It is not a qualification.

    It holds only the account *fingerprint*; the raw host account identifier
    is never retained.
    """

    host_state: HostState
    auth_state: AuthState
    auth_method: AuthMethod
    account_fingerprint: str | None
    version: str | None
    source: str
    reasons: tuple[str, ...] = ()
    qualification_status: str = "NOT_RUN"

    def __post_init__(self) -> None:
        if type(self.host_state) is not HostState or type(self.auth_state) is not AuthState:
            raise ValueError("INVALID_HOST_STATUS")
        if type(self.auth_method) is not AuthMethod:
            raise ValueError("INVALID_HOST_STATUS")
        if self.account_fingerprint is not None and (
            type(self.account_fingerprint) is not str or _FINGERPRINT.fullmatch(self.account_fingerprint) is None
        ):
            raise ValueError("INVALID_ACCOUNT_FINGERPRINT")
        if self.auth_state is AuthState.AUTHENTICATED and (
            self.account_fingerprint is None or self.auth_method is not AuthMethod.SUBSCRIPTION
            or self.host_state is not HostState.PRESENT
        ):
            raise ValueError("AUTHENTICATED_ACCOUNT_NOT_OBSERVED")
        if self.version is not None and (type(self.version) is not str or _VERSION_TEXT.fullmatch(self.version) is None):
            raise ValueError("INVALID_HOST_VERSION")
        if self.source not in SOURCES:
            raise ValueError("INVALID_SOURCE")
        if type(self.reasons) is not tuple or any(type(reason) is not str for reason in self.reasons):
            raise ValueError("INVALID_REASONS")
        if self.qualification_status != "NOT_RUN":
            raise ValueError("QUALIFICATION_REQUIRES_EXTERNAL_EVIDENCE")

    @classmethod
    def unobserved(cls, host_state: HostState, source: str, reasons: tuple[str, ...],
                   version: str | None = None) -> "HostStatus":
        return cls(host_state, AuthState.UNKNOWN, AuthMethod.UNKNOWN, None, version, source, reasons)


__all__ = [
    "MODEL_PLACEHOLDER", "MIN_HOST_VERSION", "STATUS_UNSUPPORTED_EXIT", "SUBSCRIPTION_METHODS",
    "API_KEY_METHODS", "HostState", "AuthMethod", "account_fingerprint", "safe_account_id",
    "parse_version", "version_tuple", "parse_status", "JobOutput", "parse_job", "HostCommands",
    "HostStatus",
]
