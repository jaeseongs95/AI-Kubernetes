"""Fixed code-only failures; messages never contain supplied values."""


class ReadModelError(Exception):
    """The read model operation could not be completed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidEvent(ReadModelError, ValueError):
    """A source event or batch does not satisfy the event contract."""


class ScopeMismatch(ReadModelError):
    """The event or database belongs to a different OS user scope."""


class InvalidReadModelDatabase(ReadModelError):
    """The file is foreign, damaged or inconsistent with this schema."""
