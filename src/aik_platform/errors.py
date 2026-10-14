from __future__ import annotations


class PlatformError(Exception):
    """A platform port could not complete; the message carries no secret or path."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PlatformUnavailable(PlatformError):
    """The current operating system has no adapter for this port, or it is unusable."""
