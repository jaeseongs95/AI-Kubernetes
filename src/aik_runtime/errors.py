"""Storage errors contain classifications, never supplied credential values."""


class StorageError(Exception):
    """The storage operation could not be completed."""


class InvalidRecord(StorageError, ValueError):
    """A record does not satisfy the explicit metadata contract."""


class RevisionConflict(StorageError):
    """The expected revision no longer identifies the stored record."""


class InvalidDatabase(StorageError):
    """The input is damaged, foreign, or uses an unsupported schema."""


class MigrationError(StorageError):
    """A schema upgrade, its durability, or its recovery could not be confirmed."""

    def __init__(self, message: str, *, recovery_directory=None):
        super().__init__(message)
        self.recovery_directory = recovery_directory


class UnsafeStoragePath(StorageError, ValueError):
    """The path is not a private runtime location outside source repositories."""


class StorageBusy(StorageError):
    """Another storage operation did not release the lock before its deadline."""
