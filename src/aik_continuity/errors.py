"""Continuity errors carry classifications only, never directive text or secrets."""

from aik_runtime.errors import StorageError


class ContinuityError(StorageError):
    """The continuity operation could not be completed."""


class InvalidContinuityRecord(ContinuityError, ValueError):
    """A record or argument violates the bounded continuity contract."""


class ContinuityConflict(ContinuityError):
    """A different record already uses this identity, or the expected revision is stale."""


class RevisionRegression(ContinuityConflict):
    """A revision or directive position would move backwards; the stored one was kept."""


class InvalidContinuityDatabase(ContinuityError):
    """The file is damaged, foreign, tampered or uses an unsupported continuity schema."""
