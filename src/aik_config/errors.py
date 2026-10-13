"""Settings errors carry classifications only, never supplied setting values."""

from aik_runtime.errors import InvalidRecord, StorageError


class SettingsError(StorageError):
    """A settings operation could not be completed."""


class InvalidSetting(InvalidRecord, SettingsError):
    """A settings record does not satisfy its typed contract."""


class InvalidReference(InvalidSetting):
    """A referenced workspace, connection or profile is absent or still referenced."""


class CrossWorkspaceReference(InvalidReference):
    """A record refers to a connection owned by a different workspace."""


class InvalidSettingsDocument(InvalidSetting):
    """A settings export document is malformed, foreign or carries unsupported fields."""


class WorkspaceBindingError(SettingsError):
    """The AIK-084 canonical root / VM project binding does not permit execution."""


class WorkspaceBindingRequired(WorkspaceBindingError):
    """The workspace is unbound or its binding cannot be confirmed (fail-closed)."""


class WorkspaceBindingMismatch(WorkspaceBindingError):
    """The stored settings binding differs from the AIK-084 authority."""


class NoActiveWorkspace(SettingsError):
    """No workspace is selected, so no execution target exists."""
