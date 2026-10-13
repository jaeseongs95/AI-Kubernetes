"""Connection errors carry classifications and IDs only, never secret values."""

from aik_config.errors import InvalidReference, InvalidSetting, SettingsError


class ConnectionMetadataError(SettingsError):
    """A connection metadata operation could not be completed."""


class DuplicateConnection(InvalidSetting):
    """The ID, label, account/execution path or credential reference is already registered."""


class UnknownConnection(InvalidReference):
    """No managed connection exists for the given connection ID."""


class CredentialRefUnavailable(InvalidReference):
    """The vault reference is absent or its existence could not be confirmed (fail-closed)."""


class AuthContextMismatch(ConnectionMetadataError):
    """The observed authentication context differs from the registered binding."""


class AuthenticationRequired(AuthContextMismatch):
    """The connection binding has not been verified for its current binding revision."""


class ReferenceCheckUnavailable(ConnectionMetadataError):
    """A reference source could not report its references, so deletion is blocked."""


class InconsistentConnectionStore(ConnectionMetadataError):
    """Settings and connection details were published only partially."""


class ConnectionInUse(InvalidReference):
    """The connection is still referenced; ``impact`` lists the references."""

    def __init__(self, impact):
        super().__init__("The connection is still referenced; deletion was refused. See the impact report.")
        self.impact = impact
