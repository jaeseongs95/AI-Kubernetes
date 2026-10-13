"""Vault errors carry fixed codes and messages; never a secret or ciphertext."""

from __future__ import annotations


class VaultError(Exception):
    """The vault operation could not be completed."""

    code = "VAULT_ERROR"
    default_message = "The vault operation could not be completed."

    def __init__(self, message: str | None = None):
        # Callers inside this package pass only fixed strings.
        super().__init__(message or self.default_message)


class InvalidSecret(VaultError, ValueError):
    code = "INVALID_SECRET"
    default_message = "The secret value is not acceptable for the vault."


class InvalidCredentialRef(VaultError, ValueError):
    code = "INVALID_CREDENTIAL_REF"
    default_message = "The credential reference is not a vault reference."


class CredentialNotFound(VaultError):
    code = "CREDENTIAL_NOT_FOUND"
    default_message = "No stored credential exists for this reference."


class VaultAccessDenied(VaultError):
    code = "VAULT_ACCESS_DENIED"
    default_message = "The current OS user cannot access or unprotect this credential."


class VaultCorrupt(VaultError):
    code = "VAULT_CORRUPT"
    default_message = "The stored credential file is damaged or has an unknown format."


class VaultUnavailable(VaultError):
    code = "VAULT_UNAVAILABLE"
    default_message = "The per-user secret protection backend is unavailable."


class VaultBusy(VaultError):
    code = "VAULT_BUSY"
    default_message = "Another vault operation did not release the lock before the deadline."


class VaultStorageError(VaultError):
    code = "VAULT_IO_ERROR"
    default_message = "The vault files could not be read or written."


class UnsafeVaultPath(VaultError, ValueError):
    code = "VAULT_UNSAFE_PATH"
    default_message = "The vault location is not a private per-user runtime location."
