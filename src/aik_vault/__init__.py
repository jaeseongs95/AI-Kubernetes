"""AIK-009 per-user secret vault addressed only by opaque credential references.

Plaintext exists only at the put/replace/resolve call boundary. SQLite rows,
settings, and logs hold the opaque ``secret://aik-vault/<id>`` reference.
The product backend is Windows per-user DPAPI. The fake backend is test-only.
"""

from .backend import (
    MAX_SECRET_BYTES, REF_NAMESPACE, REF_PREFIX, FakeVaultBackend, VaultBackend,
    default_vault_directory, is_credential_ref, parse_credential_ref,
)
from .dpapi import (
    CRYPTPROTECT_LOCAL_MACHINE, CRYPTPROTECT_UI_FORBIDDEN, PROTECT_FLAGS,
    DpapiCallError, DpapiVaultBackend, open_user_vault,
)
from .errors import (
    CredentialNotFound, InvalidCredentialRef, InvalidSecret, UnsafeVaultPath,
    VaultAccessDenied, VaultBusy, VaultCorrupt, VaultError, VaultStorageError,
    VaultUnavailable,
)

__all__ = [
    "VaultBackend", "DpapiVaultBackend", "FakeVaultBackend", "open_user_vault",
    "default_vault_directory", "parse_credential_ref", "is_credential_ref",
    "MAX_SECRET_BYTES", "REF_NAMESPACE", "REF_PREFIX",
    "CRYPTPROTECT_LOCAL_MACHINE", "CRYPTPROTECT_UI_FORBIDDEN", "PROTECT_FLAGS", "DpapiCallError",
    "VaultError", "InvalidSecret", "InvalidCredentialRef", "CredentialNotFound",
    "VaultAccessDenied", "VaultCorrupt", "VaultUnavailable", "VaultBusy",
    "VaultStorageError", "UnsafeVaultPath",
]
