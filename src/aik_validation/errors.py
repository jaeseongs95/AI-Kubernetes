"""Validation bridge errors carry fixed codes, never supplied values."""


class ValidationError(ValueError):
    """Fixed code-only failure; the message never contains supplied values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
