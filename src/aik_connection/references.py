"""Adapters: vault reference confirmation and external reference sources."""

from __future__ import annotations

from typing import Protocol

from aik_runtime import Pool, RuntimeStore
from aik_vault import CredentialNotFound, VaultBackend, VaultError, is_credential_ref

from .errors import CredentialRefUnavailable
from .models import ConnectionReference


class CredentialRefChecker(Protocol):
    def check(self, credential_ref: str) -> None:
        """Return when the reference exists; raise CredentialRefUnavailable otherwise."""


class VaultCredentialRefChecker:
    """Confirms a reference through the public aik_vault interface.

    aik_vault exposes no metadata-only existence call, so this adapter calls
    ``resolve`` and immediately discards the result. The plaintext is never
    stored, returned, compared or logged; Python cannot erase the transient str.
    """

    def __init__(self, vault: VaultBackend):
        if not isinstance(vault, VaultBackend):
            raise TypeError("A vault backend is required.")
        self._vault = vault

    def __repr__(self) -> str:
        return "VaultCredentialRefChecker()"

    def check(self, credential_ref: str) -> None:
        if not is_credential_ref(credential_ref):
            raise CredentialRefUnavailable("The credential reference is not an aik_vault reference.")
        try:
            resolved = self._vault.resolve(credential_ref)
        except CredentialNotFound:
            raise CredentialRefUnavailable("The vault has no credential for the reference.") from None
        except VaultError:
            raise CredentialRefUnavailable("The vault credential could not be confirmed.") from None
        except Exception:
            raise CredentialRefUnavailable("The vault credential could not be confirmed.") from None
        del resolved


class ReferenceSource(Protocol):
    def references_to(self, connection_id: str) -> tuple:
        """Return ConnectionReference records referring to the connection ID."""


class RuntimePoolReferences:
    """Reports aik_runtime pools using a connection_id; they are never removed here."""

    _PAGE = 1000

    def __init__(self, runtime_store: RuntimeStore):
        if not isinstance(runtime_store, RuntimeStore):
            raise TypeError("A runtime store is required.")
        self._store = runtime_store

    def references_to(self, connection_id: str) -> tuple:
        found = []
        with self._store.transaction() as tx:
            after = None
            while True:
                page = tx.list(Pool, limit=self._PAGE, after_id=after)
                for entry in page:
                    if entry.value.connection_id == connection_id:
                        found.append(ConnectionReference("runtime_pool", entry.value.id, None, False))
                if len(page) < self._PAGE:
                    break
                after = page[-1].value.id
        return tuple(found)
