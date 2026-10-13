"""Fixed code-only lifecycle failures; messages never contain supplied values."""

from __future__ import annotations


class LifecycleError(ValueError):
    """A lifecycle request was refused with a fixed machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
