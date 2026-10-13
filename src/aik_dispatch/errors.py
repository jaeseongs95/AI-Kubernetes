"""Fixed-code dispatch failures; messages never contain provider text or secrets."""

from __future__ import annotations


DISPATCH_ERROR_CODES = frozenset({
    "IDENTITY_MISMATCH", "CONFLICT", "INVALID_STATE", "REPLAY_MISMATCH", "INVALID_JOURNAL",
    "OUTCOME_UNKNOWN", "REJECTED",
})


class DispatchError(Exception):
    """A machine-readable dispatch failure. ``subject`` is a fixed field name, never input text."""

    def __init__(self, code: str, *, subject: str | None = None):
        if code not in DISPATCH_ERROR_CODES:
            raise ValueError("Unknown dispatch error code.")
        self.code = code
        self.subject = subject
        super().__init__(code if subject is None else f"{code}: {subject}")


class DispatchOutcomeUnknown(DispatchError):
    """The provider effect may or may not have happened. It is never resent automatically."""

    def __init__(self, code: str = "OUTCOME_UNKNOWN", *, subject: str | None = None):
        super().__init__(code, subject=subject)


class DispatchRejected(DispatchError):
    """The provider guard reported a known failure before any effect; nothing was started."""

    def __init__(self, code: str = "REJECTED", *, subject: str | None = None):
        super().__init__(code, subject=subject)
