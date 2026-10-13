"""JEV bridge errors carry fixed codes and HTTP status only, never supplied values."""

from __future__ import annotations


class JevError(ValueError):
    """Fixed code-only failure; never contains response bodies, tokens or RAW text."""

    def __init__(self, code: str, *, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code if status is None else f"{code} (HTTP {status})")
