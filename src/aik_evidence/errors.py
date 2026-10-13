"""Evidence errors carry fixed codes only; never paths, bytes or secret values."""

from __future__ import annotations


class EvidenceError(Exception):
    """An evidence operation could not be completed. ``code`` is a fixed string."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidEvidenceRecord(EvidenceError, ValueError):
    """An argument or record does not satisfy the artifact manifest contract."""


class InvalidEvidenceDatabase(EvidenceError):
    """The relay database is foreign, damaged or uses an unsupported schema."""


class EvidencePathRejected(EvidenceError):
    """The workspace root or relative path is outside the allowed scope."""


class EvidenceUnavailable(EvidenceError):
    """The workspace file is missing or unsafe to read (``condition``)."""

    def __init__(self, code: str, condition: str):
        self.condition = condition
        super().__init__(code)


class EvidenceConflict(EvidenceError):
    """A replayed callback or transform disagrees with the stored record."""

    def __init__(self, code: str, conflict):
        self.conflict = conflict
        super().__init__(code)


class EvidenceIntegrityError(EvidenceError):
    """Stored or exported bytes are missing or do not match the recorded digest."""

    def __init__(self, code: str, status: str, observed_sha256: str | None = None):
        self.status = status
        self.observed_sha256 = observed_sha256
        super().__init__(code)


class UnknownArtifact(EvidenceError):
    """No artifact record has this identity."""


class EvidenceExportExists(EvidenceError):
    """The export destination already exists; it is never overwritten."""
