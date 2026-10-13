"""Scope errors carry classifications and IDs only, never secret values."""

from aik_config.errors import InvalidSetting, InvalidSettingsDocument, SettingsError


class ScopeError(SettingsError):
    """A scope or settings transfer operation could not be completed."""


class InvalidScope(InvalidSetting, ScopeError):
    """A scope record or argument violates its contract."""


class ConnectionNotInScope(ScopeError):
    """The workspace may not use the connection. ``reason`` is a fixed code."""

    def __init__(self, reason: str, workspace_id: str, connection_id: str):
        super().__init__(f"The connection is not usable by this workspace ({reason}).")
        self.reason = reason
        self.workspace_id = workspace_id
        self.connection_id = connection_id


class InvalidTransferDocument(InvalidSettingsDocument, ScopeError):
    """A transfer document is malformed, foreign or carries unsupported fields."""


class TransferBlocked(ScopeError):
    """The export or import was refused before any write (fail-closed)."""


class TransferIncomplete(ScopeError):
    """An import stopped after partial writes; restored records stay non-executable."""

    def __init__(self, stage: str, completed: tuple):
        super().__init__(
            "The settings transfer stopped after partial writes; restored records remain "
            "unbound or unverified until repaired."
        )
        self.stage = stage
        self.completed = completed
