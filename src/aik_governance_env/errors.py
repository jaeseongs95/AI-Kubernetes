"""Fixed code-only failures; messages never contain file contents or secrets."""

from __future__ import annotations


class GovernanceEnvError(ValueError):
    """A governance input could not be built or does not match the VM contract.

    ``code`` is a fixed identifier. ``check`` optionally names the failed
    structural check. Neither contains raw input text or key material.
    """

    def __init__(self, code: str, *, check: str | None = None):
        self.code = code
        self.check = check
        super().__init__(code if check is None else f"{code} ({check})")
