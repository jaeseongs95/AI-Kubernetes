"""Development tool errors carry fixed codes and field names, never file contents or paths."""

from __future__ import annotations

from enum import StrEnum


class DevToolCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_POLICY = "INVALID_POLICY"
    WORKSPACE_MISMATCH = "WORKSPACE_MISMATCH"
    WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"
    PATH_REJECTED = "PATH_REJECTED"
    PATH_UNSAFE = "PATH_UNSAFE"
    PATH_PROTECTED = "PATH_PROTECTED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    STALE_INPUT = "STALE_INPUT"
    TOO_LARGE = "TOO_LARGE"
    WRITE_FAILED = "WRITE_FAILED"
    COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"
    COMMAND_UNAVAILABLE = "COMMAND_UNAVAILABLE"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    AUTHORIZATION_INVALID = "AUTHORIZATION_INVALID"
    CALL_CONFLICT = "CALL_CONFLICT"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class DevToolError(Exception):
    """A machine-readable tool failure.

    ``authority_code`` / ``vm_code`` keep the aik_authority / VM codes unchanged;
    ``escalation`` is True when a new VM authorization would be needed.
    """

    def __init__(self, code, *, subject=None, authority_code=None, vm_code=None, escalation=False):
        self.code = DevToolCode(code)
        self.subject = subject
        self.authority_code = authority_code
        self.vm_code = vm_code
        self.escalation = bool(escalation)
        text = self.code.value
        if authority_code:
            text += f" authority_code={authority_code}"
        if vm_code:
            text += f" vm_code={vm_code}"
        if subject:
            text += f" subject={subject}"
        super().__init__(text)


class EffectUnknown(DevToolError):
    """The effect may or may not have happened; it is never re-executed automatically."""


class InvalidDevToolPolicy(DevToolError, ValueError):
    """A trusted development tool policy violates its contract."""

    def __init__(self, code=DevToolCode.INVALID_POLICY, **kwargs):
        super().__init__(code, **kwargs)
