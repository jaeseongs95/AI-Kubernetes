"""AIK-010 connection metadata: several accounts and subscription/API paths per vendor.

Each connection is registered under an immutable ``connection_id`` and a
store-assigned immutable ``provenance_id``. The minimal reference record lives in
``aik_config.SettingsStore`` (``ConnectionProfile``). Details that aik_config does
not model (account label, execution kind, official host/profile, non-sensitive
account binding and its revision, vault reference, verification time) live in a
separate ``connections.sqlite3`` file next to the settings database.

No secret value is stored. ``credential_ref`` only holds an opaque reference
issued by ``aik_vault``. No provider is contacted.
"""

from .errors import (
    AuthContextMismatch, AuthenticationRequired, ConnectionInUse, ConnectionMetadataError,
    CredentialRefUnavailable, DuplicateConnection, InconsistentConnectionStore,
    ReferenceCheckUnavailable, UnknownConnection,
)
from .models import (
    AUTH_KINDS, EXECUTION_AUTH, EXECUTION_KINDS, AuthContext, Connection, ConnectionProvenance,
    ConnectionReference, ConnectionSpec, DeletionImpact,
)
from .references import (
    CredentialRefChecker, ReferenceSource, RuntimePoolReferences, VaultCredentialRefChecker,
)
from .store import CONNECTIONS_APPLICATION_ID, ConnectionStore

__all__ = [
    "ConnectionStore", "CONNECTIONS_APPLICATION_ID",
    "ConnectionSpec", "Connection", "AuthContext", "ConnectionProvenance",
    "ConnectionReference", "DeletionImpact",
    "AUTH_KINDS", "EXECUTION_KINDS", "EXECUTION_AUTH",
    "CredentialRefChecker", "VaultCredentialRefChecker", "ReferenceSource", "RuntimePoolReferences",
    "ConnectionMetadataError", "DuplicateConnection", "UnknownConnection", "CredentialRefUnavailable",
    "AuthContextMismatch", "AuthenticationRequired", "ConnectionInUse",
    "ReferenceCheckUnavailable", "InconsistentConnectionStore",
]
