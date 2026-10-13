"""PEER bridge errors carry fixed codes; AGS error text is never copied into them."""

from __future__ import annotations


class AgsPeerError(Exception):
    """Fixed code-only failure.

    ``outcome`` classifies a CLI call: ``not_sent`` (the process never ran),
    ``rejected`` (AGS answered ok:false), ``unknown`` (timeout or unreadable
    answer; delivery may or may not have happened). AGS error strings are kept
    only as ``detail_length`` and ``detail_digest`` (sha256 hex), never as text.
    """

    def __init__(self, code: str, *, outcome: str | None = None,
                 detail_digest: str | None = None, detail_length: int | None = None):
        self.code = code
        self.outcome = outcome
        self.detail_digest = detail_digest
        self.detail_length = detail_length
        super().__init__(code)
