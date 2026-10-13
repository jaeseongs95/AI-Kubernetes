"""Unverified Codex CLI host protocol assumptions (AIK-014).

None of these commands or phrases were confirmed against current official
Codex documentation. They are isolated here so a verified protocol can be
injected without changing the bridge. The parser returns only enums and a
candidate host-provided account identifier; it never keeps the raw output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re


class LoginState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    EXPIRED = "expired"
    PENDING = "pending"
    UNKNOWN = "unknown"


class LoginMethod(StrEnum):
    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ParsedStatus:
    login: LoginState
    method: LoginMethod
    # Host-provided identifier candidate; the bridge validates and hashes it
    # immediately and never stores or returns the raw value.
    account_id: str | None = field(default=None, repr=False)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VERSION = re.compile(r"(?i)\bcodex(?:-cli)?\s+v?\d+\.\d+\.\d+")
_EXPIRED = re.compile(r"(?i)\bexpired\b|\blog\s?in again\b|\bre-?authenticate\b")
_LOGGED_OUT = re.compile(r"(?i)\bnot logged in\b")
_SUBSCRIPTION = re.compile(r"(?i)\blogged in using chatgpt\b")
_API_KEY = re.compile(r"(?i)\blogged in using an? api key\b")
_ACCOUNT = re.compile(r"(?im)^[ \t]*account(?:[ _-]?id)?[ \t]*[:=][ \t]*(\S+)[ \t]*$")


def _text(output: object) -> str | None:
    if type(output) is not bytes:
        return None
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _ANSI.sub("", text).replace("\r\n", "\n")


class CodexCliProtocol:
    """Assumed CLI surface: ``--version``, ``login status``, ``login``, ``logout``."""

    name = "codex-cli-unverified-v1"
    version_args = ("--version",)
    status_args = ("login", "status")
    login_args = ("login",)
    logout_args = ("logout",)

    def version_supported(self, exit_code: int, output: bytes) -> bool:
        text = _text(output)
        return type(exit_code) is int and exit_code == 0 and text is not None and _VERSION.search(text) is not None

    def parse_status(self, exit_code: int, output: bytes) -> ParsedStatus:
        unknown = ParsedStatus(LoginState.UNKNOWN, LoginMethod.UNKNOWN)
        text = _text(output)
        if text is None or type(exit_code) is not int:
            return unknown
        if _EXPIRED.search(text):
            return ParsedStatus(LoginState.EXPIRED, LoginMethod.UNKNOWN)
        if _LOGGED_OUT.search(text):
            return ParsedStatus(LoginState.LOGGED_OUT, LoginMethod.UNKNOWN)
        subscription = _SUBSCRIPTION.search(text) is not None
        api_key = _API_KEY.search(text) is not None
        if exit_code != 0 or subscription == api_key:
            return unknown
        if api_key:
            return ParsedStatus(LoginState.LOGGED_IN, LoginMethod.API_KEY)
        accounts = {match.group(1) for match in _ACCOUNT.finditer(text)}
        account = accounts.pop() if len(accounts) == 1 else None
        return ParsedStatus(LoginState.LOGGED_IN, LoginMethod.SUBSCRIPTION, account)


__all__ = ["CodexCliProtocol", "LoginState", "LoginMethod", "ParsedStatus"]
