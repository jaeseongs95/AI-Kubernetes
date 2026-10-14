"""Connection onboarding and management service (AIK-046/047).

Composes ``aik_connection.ConnectionStore`` (metadata), the per-user vault
(secrets) and vendor connectors (``ports``). Every result is a plain DTO with
fixed codes; no secret, credential reference or vendor message leaves here.

Rules:

- Reads never create storage: without a settings database the list is empty.
- An API key goes into the vault here and nowhere else. A failed registration
  deletes the new vault entry again (best effort, reported when unconfirmed).
  A new key that the vendor rejects outright (UNAUTHENTICATED) is not kept:
  the connection and its vault entry are removed and ``KEY_REJECTED`` lets the
  user correct the key. An unknown outcome (for example no network) keeps the
  connection so the check can be retried. A rejected replacement key stays in
  place (the old key is never read back), leaving the connection unauthenticated.
- ``credential_ref`` is immutable, so a key change replaces the vault entry in
  place after a rebind to a fresh account marker (verification is cleared and
  the binding revision moves on), then checks again.
- API connections have no separate logout: removing the key is deleting the
  connection. Subscription logout asks the official host to sign out.
- An API key's ``account_binding`` is a random marker, not derived from the
  key: the vendor's account cannot be observed from a key, so two connections
  with the same key are not detected.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
from pathlib import Path
from typing import Callable, Iterable

from aik_config import InvalidReference, InvalidSetting, RevisionConflict, SettingsError, SettingsStore, Workspace
from aik_connection import (
    AuthContext, Connection, ConnectionInUse, ConnectionSpec, ConnectionStore, DuplicateConnection,
    UnknownConnection, VaultCredentialRefChecker,
)
from aik_connection.models import check_label, utc_now
from aik_runtime.errors import StorageBusy, StorageError
from aik_vault import CredentialNotFound, VaultAccessDenied, VaultBackend, VaultError, VaultUnavailable

from .ports import CheckResult, CheckState, ConnectorInfo, HostLogin, KeyVerifier, LoginState, LoginStatus

DEFAULT_WORKSPACE = "local"
DEFAULT_WORKSPACE_NAME = "로컬 작업공간"


class ConnectError(Exception):
    """A fixed code for the GUI; never carries secret or vendor text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Check:
    binding_revision: int
    state: str
    code: str | None
    checked_at: str


@dataclass(slots=True)
class _Login:
    connector: HostLogin
    connection_id: str
    account_label: str | None
    existing: bool


def _info_dto(info: ConnectorInfo) -> dict:
    return {"provider": info.provider, "execution_kind": info.execution_kind, "method": info.method,
            "display_name": info.display_name}


def _vault_code(error: VaultError) -> str:
    if isinstance(error, VaultAccessDenied):
        return "VAULT_LOCKED"
    if isinstance(error, VaultUnavailable):
        return "VAULT_UNAVAILABLE"
    if error.code == "INVALID_SECRET":
        return "INVALID_SECRET"
    return "VAULT_ERROR"


def _store_code(error: Exception) -> str:
    if isinstance(error, ConnectionInUse):
        return "CONNECTION_IN_USE"
    if isinstance(error, DuplicateConnection):
        return "DUPLICATE_CONNECTION"
    if isinstance(error, UnknownConnection):
        return "UNKNOWN_CONNECTION"
    if isinstance(error, RevisionConflict):
        return "REVISION_CONFLICT"
    if isinstance(error, StorageBusy):
        return "STORE_BUSY"
    if isinstance(error, InvalidSetting):
        return "INVALID_INPUT"
    return "STORE_UNAVAILABLE"


class ConnectionService:
    def __init__(self, settings_path: Path | str, connectors: Iterable, *,
                 vault_factory: Callable[[], VaultBackend], workspace_id: str = DEFAULT_WORKSPACE,
                 reference_sources=()):
        self._settings_path = Path(settings_path)
        if not self._settings_path.is_absolute():
            raise ValueError("The settings path must be absolute.")
        self._vault_factory = vault_factory
        self._workspace_id = workspace_id
        self._sources = tuple(reference_sources)
        self._connectors: dict[tuple[str, str], object] = {}
        for connector in connectors:
            info = getattr(connector, "info", None)
            if type(info) is not ConnectorInfo:
                raise TypeError("Each connector needs a ConnectorInfo.")
            expected = KeyVerifier if info.execution_kind == "api" else HostLogin
            if not isinstance(connector, expected):
                raise TypeError("The connector does not implement the port of its execution path.")
            key = (info.provider, info.execution_kind)
            if key in self._connectors:
                raise ValueError("Two connectors serve the same provider and execution path.")
            self._connectors[key] = connector
        self._lock = threading.RLock()
        self._vault: VaultBackend | None = None
        self._store: ConnectionStore | None = None
        self._checks: dict[str, _Check] = {}
        self._login: _Login | None = None

    # -- wiring ---------------------------------------------------------------
    def _vault_or_fail(self) -> VaultBackend:
        if self._vault is None:
            try:
                self._vault = self._vault_factory()
            except VaultError as error:
                raise ConnectError(_vault_code(error)) from None
            except Exception:
                raise ConnectError("VAULT_UNAVAILABLE") from None
        return self._vault

    def _open_store(self, *, create: bool) -> ConnectionStore | None:
        if self._store is not None:
            return self._store
        # A read needs both files: opening ConnectionStore would create the details file.
        if not create and not (self._settings_path.exists()
                               and self._settings_path.with_name("connections.sqlite3").exists()):
            return None
        try:
            settings = SettingsStore(self._settings_path)
            if settings.get(Workspace, self._workspace_id) is None:
                if not create:
                    return None
                settings.save(Workspace(self._workspace_id, DEFAULT_WORKSPACE_NAME))
            checker = _LazyChecker(self._vault_or_fail)
            self._store = ConnectionStore(settings, credential_checker=checker, reference_sources=self._sources)
        except ConnectError:
            raise
        except (SettingsError, StorageError, OSError) as error:
            raise ConnectError(_store_code(error)) from None
        return self._store

    def _store_or_fail(self) -> ConnectionStore:
        store = self._open_store(create=True)
        assert store is not None
        return store

    def _connector(self, provider: object, execution_kind: str):
        connector = self._connectors.get((provider, execution_kind)) if type(provider) is str else None
        if connector is None:
            raise ConnectError("UNSUPPORTED_PROVIDER")
        return connector

    def _get(self, connection_id: object):
        if type(connection_id) is not str:
            raise ConnectError("INVALID_INPUT")
        store = self._open_store(create=False)
        if store is None:
            raise ConnectError("UNKNOWN_CONNECTION")
        try:
            entry = store.get(connection_id)
        except (SettingsError, StorageError) as error:
            raise ConnectError(_store_code(error)) from None
        if entry is None or entry.value.workspace_id != self._workspace_id:
            raise ConnectError("UNKNOWN_CONNECTION")
        return store, entry

    @staticmethod
    def _revision(value: object) -> int:
        if type(value) is not int or value < 1:
            raise ConnectError("INVALID_INPUT")
        return value

    @staticmethod
    def _label(value: object) -> str:
        try:
            check_label(value)
        except InvalidSetting:
            raise ConnectError("INVALID_LABEL") from None
        return value  # type: ignore[return-value]

    # -- DTOs -----------------------------------------------------------------
    def _state(self, connection: Connection) -> str:
        check = self._checks.get(connection.connection_id)
        if check is not None and check.binding_revision == connection.binding_revision \
                and check.state != CheckState.VERIFIED.value:
            return check.state
        if (connection.provider, connection.execution_kind) not in self._connectors:
            return "unsupported"
        return "verified" if connection.verified_at is not None else "unverified"

    def _usage(self, store: ConnectionStore, connection_id: str) -> dict:
        try:
            impact = store.impact(connection_id)
        except (SettingsError, StorageError):
            return {"references": None, "blocking": None, "external_checked": False}
        return {"references": len(impact.references), "blocking": len(impact.blocking),
                "external_checked": impact.external_references_checked}

    def _dto(self, store: ConnectionStore, entry) -> dict:
        connection: Connection = entry.value
        connector = self._connectors.get((connection.provider, connection.execution_kind))
        check = self._checks.get(connection.connection_id)
        last = None
        if check is not None and check.binding_revision == connection.binding_revision:
            last = {"state": check.state, "code": check.code, "checked_at": check.checked_at}
        return {
            "connection_id": connection.connection_id,
            "account_label": connection.account_label,
            "provider": connection.provider,
            "display_name": connector.info.display_name if connector is not None else connection.provider,
            "execution_kind": connection.execution_kind,
            "method": "api_key" if connection.execution_kind == "api" else "host_login",
            "state": self._state(connection),
            "verified_at": connection.verified_at,
            "revision": entry.revision,
            "binding_revision": connection.binding_revision,
            "last_check": last,
            "usage": self._usage(store, connection.connection_id),
        }

    # -- reads ----------------------------------------------------------------
    def catalog(self) -> dict:
        return {"connectors": [_info_dto(connector.info) for _, connector in sorted(self._connectors.items())]}

    def list(self) -> dict:
        with self._lock:
            store = self._open_store(create=False)
            if store is None:
                return {"store": "ABSENT", "connections": []}
            try:
                entries = store.list(self._workspace_id)
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            entries = sorted(entries, key=lambda item: item.value.connection_id)
            return {"store": "READY", "connections": [self._dto(store, entry) for entry in entries]}

    def impact(self, connection_id: object) -> dict:
        with self._lock:
            store, entry = self._get(connection_id)
            try:
                impact = store.impact(entry.value.connection_id)
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            return {
                "connection_id": impact.connection_id,
                "references": [{"kind": item.kind, "id": item.id, "removable": item.removable}
                               for item in impact.references],
                "external_checked": impact.external_references_checked,
            }

    def login_state(self) -> dict:
        with self._lock:
            login = self._login
            if login is None:
                return {"state": LoginState.IDLE.value, "provider": None, "cancel_supported": False, "code": None,
                        "connection_id": None}
            status = self._observe_login(login)
            return {"state": status.state.value, "provider": login.connector.info.provider,
                    "cancel_supported": status.cancel_supported, "code": status.code,
                    "connection_id": login.connection_id if login.existing else None}

    # -- API key path -----------------------------------------------------------
    def add_api_key(self, provider: object, account_label: object, secret: object) -> dict:
        with self._lock:
            verifier: KeyVerifier = self._connector(provider, "api")
            label = self._label(account_label)
            self._check_secret(verifier, secret)
            store = self._store_or_fail()
            vault = self._vault_or_fail()
            try:
                reference = vault.put(secret)  # type: ignore[arg-type]
            except VaultError as error:
                raise ConnectError(_vault_code(error)) from None
            info = verifier.info
            spec = None
            try:
                spec = ConnectionSpec(
                    connection_id="conn-" + secrets.token_hex(8), workspace_id=self._workspace_id,
                    provider=info.provider, account_label=label, auth_kind="api_key", execution_kind="api",
                    official_host=info.official_host, host_profile=info.host_profile,
                    account_binding=_marker(), credential_ref=reference,
                )
                entry = store.register(spec)
            except Exception as error:  # noqa: BLE001 - mapped to a code after cleanup
                code = _store_code(error) if isinstance(error, (SettingsError, StorageError)) else "STORE_UNAVAILABLE"
                cleaned = _delete_quietly(vault, reference)
                raise ConnectError(code if cleaned else "SECRET_CLEANUP_UNCONFIRMED") from None
            report = self._check_and_report(store, entry)
            if report["check"]["state"] == CheckState.UNAUTHENTICATED.value:
                self._discard_rejected(store, report["connection"], reference, vault)
            return report

    def _discard_rejected(self, store: ConnectionStore, connection: dict, reference: str, vault: VaultBackend):
        try:
            store.delete(connection["connection_id"], connection["revision"])
        except (SettingsError, StorageError):
            raise ConnectError("KEY_REJECTED_CLEANUP_UNCONFIRMED") from None
        self._checks.pop(connection["connection_id"], None)
        if not _delete_quietly(vault, reference):
            raise ConnectError("SECRET_CLEANUP_UNCONFIRMED")
        raise ConnectError("KEY_REJECTED")

    def rotate_key(self, connection_id: object, expected_revision: object, secret: object) -> dict:
        with self._lock:
            store, entry = self._get(connection_id)
            connection: Connection = entry.value
            if connection.execution_kind != "api":
                raise ConnectError("WRONG_METHOD")
            verifier: KeyVerifier = self._connector(connection.provider, "api")
            revision = self._revision(expected_revision)
            self._check_secret(verifier, secret)
            vault = self._vault_or_fail()
            try:
                # Clear the verification first: a failure below leaves an unverified connection.
                entry = store.rebind(connection.connection_id, revision, account_binding=_marker())
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            try:
                vault.replace(connection.credential_ref, secret)  # type: ignore[arg-type]
            except VaultError as error:
                raise ConnectError(_vault_code(error)) from None
            return self._check_and_report(store, entry)

    @staticmethod
    def _check_secret(verifier: KeyVerifier, secret: object) -> None:
        try:
            accepted = type(secret) is str and bool(secret) and verifier.accepts(secret)
        except Exception:  # noqa: BLE001 - a connector failure is a refusal, not a crash
            accepted = False
        if not accepted:
            raise ConnectError("INVALID_SECRET")

    # -- checks -----------------------------------------------------------------
    def check(self, connection_id: object) -> dict:
        with self._lock:
            store, entry = self._get(connection_id)
            return self._check_and_report(store, entry)

    def _run_check(self, connection: Connection) -> CheckResult:
        connector = self._connector(connection.provider, connection.execution_kind)
        try:
            result = connector.verify(connection) if connection.execution_kind == "api" else connector.check(connection)
        except Exception:  # noqa: BLE001 - vendor faults become an unknown state
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")
        if type(result) is not CheckResult:
            return CheckResult(CheckState.UNKNOWN, code="CHECK_FAILED")
        if result.state is CheckState.VERIFIED and connection.execution_kind == "subscription" \
                and result.account_binding != connection.account_binding:
            return CheckResult(CheckState.ACCOUNT_CHANGED, code="ACCOUNT_CHANGED")
        return result

    def _check_and_report(self, store: ConnectionStore, entry) -> dict:
        connection: Connection = entry.value
        result = self._run_check(connection)
        try:
            if result.state is CheckState.VERIFIED:
                entry = store.mark_verified(connection.connection_id, _context(connection), entry.revision)
            elif result.state in (CheckState.UNAUTHENTICATED, CheckState.ACCOUNT_CHANGED) \
                    and connection.verified_at is not None:
                entry = store.invalidate(connection.connection_id, entry.revision)
        except (SettingsError, StorageError) as error:
            raise ConnectError(_store_code(error)) from None
        self._checks[connection.connection_id] = _Check(
            entry.value.binding_revision, result.state.value, result.code, utc_now())
        return {"connection": self._dto(store, entry),
                "check": {"state": result.state.value, "code": result.code}}

    # -- metadata ----------------------------------------------------------------
    def rename(self, connection_id: object, expected_revision: object, account_label: object) -> dict:
        with self._lock:
            store, entry = self._get(connection_id)
            label = self._label(account_label)
            try:
                entry = store.rename(entry.value.connection_id, label, self._revision(expected_revision))
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            return {"connection": self._dto(store, entry)}

    def delete(self, connection_id: object, expected_revision: object, acknowledge_impact: object) -> dict:
        with self._lock:
            store, entry = self._get(connection_id)
            if type(acknowledge_impact) is not bool:
                raise ConnectError("INVALID_INPUT")
            connection: Connection = entry.value
            revision = self._revision(expected_revision)
            try:
                impact = store.impact(connection.connection_id)
                if impact.in_use and (not acknowledge_impact or impact.blocking):
                    raise ConnectError("CONNECTION_IN_USE")
                store.delete(connection.connection_id, revision, force=impact.in_use, acknowledged_impact=impact)
            except ConnectError:
                raise
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            self._checks.pop(connection.connection_id, None)
            secret_deleted = None
            if connection.credential_ref is not None:
                try:
                    secret_deleted = _delete_quietly(self._vault_or_fail(), connection.credential_ref)
                except ConnectError:
                    secret_deleted = False
            return {"connection_id": connection.connection_id, "removed_references": len(impact.references),
                    "secret_deleted": secret_deleted}

    # -- subscription path -------------------------------------------------------
    def _observe_login(self, login: _Login) -> LoginStatus:
        try:
            status = login.connector.login_status()
        except Exception:  # noqa: BLE001
            return LoginStatus(LoginState.FAILED, False, "LOGIN_OBSERVE_FAILED")
        return status if type(status) is LoginStatus else LoginStatus(LoginState.FAILED, False, "LOGIN_OBSERVE_FAILED")

    def start_login(self, provider: object, account_label: object = None, connection_id: object = None) -> dict:
        with self._lock:
            if self._login is not None and self._observe_login(self._login).state is LoginState.PENDING:
                raise ConnectError("LOGIN_IN_PROGRESS")
            if connection_id is not None:  # re-authentication of a stored subscription connection
                _, entry = self._get(connection_id)
                if entry.value.execution_kind != "subscription":
                    raise ConnectError("WRONG_METHOD")
                connector: HostLogin = self._connector(entry.value.provider, "subscription")
                login = _Login(connector, entry.value.connection_id, None, True)
            else:
                connector = self._connector(provider, "subscription")
                login = _Login(connector, "conn-" + secrets.token_hex(8), self._label(account_label), False)
            try:
                status = connector.start_login(login.connection_id)
            except Exception:  # noqa: BLE001
                raise ConnectError("LOGIN_START_FAILED") from None
            if type(status) is not LoginStatus:
                raise ConnectError("LOGIN_START_FAILED")
            self._login = login
            return self.login_state()

    def cancel_login(self) -> dict:
        with self._lock:
            login = self._login
            if login is None:
                raise ConnectError("NO_LOGIN")
            try:
                cancelled = login.connector.cancel_login() is True
            except Exception:  # noqa: BLE001
                cancelled = False
            if cancelled:
                self._login = None
            return {"cancelled": cancelled, "login": self.login_state()}

    def complete_login(self) -> dict:
        with self._lock:
            login = self._login
            if login is None:
                raise ConnectError("NO_LOGIN")
            status = self._observe_login(login)
            if status.state is LoginState.PENDING:
                raise ConnectError("LOGIN_PENDING")
            try:
                result = login.connector.connect(login.connection_id)
            except Exception:  # noqa: BLE001
                raise ConnectError("NOT_SIGNED_IN") from None
            if type(result) is not CheckResult or result.state is not CheckState.VERIFIED \
                    or result.account_binding is None:
                raise ConnectError("NOT_SIGNED_IN")
            store = self._store_or_fail()
            info = login.connector.info
            try:
                if login.existing:
                    entry = store.get(login.connection_id)
                    if entry is None:
                        raise ConnectError("UNKNOWN_CONNECTION")
                    if entry.value.account_binding != result.account_binding:
                        raise ConnectError("ACCOUNT_CHANGED")
                else:
                    entry = store.register(ConnectionSpec(
                        connection_id=login.connection_id, workspace_id=self._workspace_id,
                        provider=info.provider, account_label=login.account_label, auth_kind="host_session",
                        execution_kind="subscription", official_host=info.official_host,
                        host_profile=info.host_profile, account_binding=result.account_binding,
                    ))
                entry = store.mark_verified(login.connection_id, _context(entry.value), entry.revision)
            except ConnectError:
                raise
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            self._login = None
            self._checks[login.connection_id] = _Check(entry.value.binding_revision, CheckState.VERIFIED.value,
                                                       None, utc_now())
            return {"connection": self._dto(store, entry), "check": {"state": "verified", "code": None}}

    def logout(self, connection_id: object, expected_revision: object) -> dict:
        with self._lock:
            store, entry = self._get(connection_id)
            connection: Connection = entry.value
            if connection.execution_kind != "subscription":
                raise ConnectError("WRONG_METHOD")  # an API key is removed by deleting the connection
            connector: HostLogin = self._connector(connection.provider, "subscription")
            revision = self._revision(expected_revision)
            if revision != entry.revision:
                raise ConnectError("REVISION_CONFLICT")
            try:
                connector.logout(connection)
            except Exception:  # noqa: BLE001
                raise ConnectError("LOGOUT_FAILED") from None
            try:
                if connection.verified_at is not None:
                    entry = store.invalidate(connection.connection_id, revision)
            except (SettingsError, StorageError) as error:
                raise ConnectError(_store_code(error)) from None
            self._checks[connection.connection_id] = _Check(
                connection.binding_revision, CheckState.UNAUTHENTICATED.value, "SIGNED_OUT", utc_now())
            return {"connection": self._dto(store, entry)}


class _LazyChecker:
    """Confirms vault references only when a write needs it (reads never open the vault)."""

    def __init__(self, vault_factory: Callable[[], VaultBackend]):
        self._vault_factory = vault_factory

    def check(self, credential_ref: str) -> None:
        VaultCredentialRefChecker(self._vault_factory()).check(credential_ref)


def _marker() -> str:
    return "key-" + secrets.token_hex(12)


def _context(connection: Connection) -> AuthContext:
    return AuthContext(connection.connection_id, connection.provider, connection.auth_kind,
                       connection.execution_kind, connection.official_host, connection.host_profile,
                       connection.account_binding, connection.binding_revision)


def _delete_quietly(vault: VaultBackend, reference: str) -> bool:
    try:
        vault.delete(reference)
    except CredentialNotFound:
        return True
    except Exception:  # noqa: BLE001 - reported as an unconfirmed cleanup
        return False
    return True


__all__ = ["ConnectionService", "ConnectError", "DEFAULT_WORKSPACE"]
