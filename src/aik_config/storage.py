"""Settings repository with revision checks, atomic transactions and path rules.

Each operation opens a fresh SQLite connection under the aik_runtime process
lock and the same path validation as runtime storage, but in its own file with
its own application ID. Existing files are identified from their header before
SQLite opens them, so foreign databases (including runtime storage) are not
touched. When a rollback journal is present, SQLite recovery and validation
first run on a private copy, so a rejected file and its journal are preserved.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import fields
import errno
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from aik_runtime.errors import InvalidDatabase, InvalidRecord, RevisionConflict, StorageBusy, StorageError
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, default_database_path, validate_database_path

from . import schema
from .errors import (
    CrossWorkspaceReference, InvalidReference, InvalidSetting, InvalidSettingsDocument,
    NoActiveWorkspace, WorkspaceBindingMismatch, WorkspaceBindingRequired,
)
from .models import (
    AgentProfile, ConnectionProfile, ExecutionTarget, ImportReport, PermissionDefaults, Revisioned,
    Workspace, WorkspaceBinding, WorkspaceLimits, WorkspaceSettings, check_identifier,
)


EXPORT_FORMAT = "aik.settings-export"
EXPORT_VERSION = 1
EXPORT_SCOPE = "settings"
EXPORT_EXCLUDED = (
    "secret_values", "credential_material", "authentication_sessions",
    "canonical_root_binding", "vm_project_binding", "active_workspace_selection",
    "revisions_and_history", "runtime_state", "vm_ags_ledgers_and_artifacts",
)
MAX_IMPORT_RECORDS = 10000

_SPEC = {
    Workspace: ("workspaces", "id"),
    ConnectionProfile: ("connections", "id"),
    AgentProfile: ("agent_profiles", "id"),
    WorkspaceLimits: ("workspace_limits", "workspace_id"),
    PermissionDefaults: ("permission_defaults", "workspace_id"),
}
_COLUMNS = {model: tuple(field.name for field in fields(model)) for model in _SPEC}
_BOOLEAN = frozenset({"needs_authentication"})


def default_settings_path() -> Path:
    """Per-user location next to, but distinct from, the runtime database."""
    runtime = default_database_path()
    if os.name == "nt":
        return runtime.parent.parent / "config" / "settings.sqlite3"
    return runtime.with_name("settings.sqlite3")


def _spec(model) -> tuple:
    try:
        return _SPEC[model]
    except (KeyError, TypeError):
        raise InvalidSetting("Only supported settings models may be stored.") from None


def _revision(value: object, *, allow_create: bool) -> None:
    minimum = 0 if allow_create else 1
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise InvalidSetting("An expected revision must be a bounded integer.")


def _record(value):
    model = type(value)
    _spec(model)
    # Reconstruct: object.__setattr__ can bypass a frozen dataclass initializer.
    return model(**{name: getattr(value, name) for name in _COLUMNS[model]})


def _column_sql(model) -> str:
    return ", ".join(f'"{name}"' for name in (*_COLUMNS[model], "revision"))


def _decode(model, row) -> Revisioned:
    try:
        values = dict(zip(_COLUMNS[model], row[:-1]))
        for name in _BOOLEAN & values.keys():
            stored = values[name]
            if type(stored) is not int or stored not in (0, 1):
                raise InvalidSetting("Invalid stored boolean.")
            values[name] = bool(stored)
        revision = row[-1]
        if type(revision) is not int or not 1 <= revision <= MAX_INTEGER:
            raise InvalidSetting("Invalid stored revision.")
        return Revisioned(model(**values), revision)
    except (InvalidRecord, TypeError, ValueError, KeyError, IndexError):
        raise InvalidDatabase("A stored settings record violates its contract.") from None


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the settings database lock.") from None
    raise StorageError("The settings database operation could not be completed.") from None


def _sync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # No portable directory fsync on Windows; rename durability is OS-owned.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------- export doc

def _exact(value: object, keys: tuple, what: str) -> dict:
    if type(value) is not dict or set(value) != set(keys):
        raise InvalidSettingsDocument(f"The settings export {what} has missing or unsupported fields.")
    return value


def _list(value: object, what: str) -> list:
    if type(value) is not list:
        raise InvalidSettingsDocument(f"The settings export {what} must be a list.")
    return value


def _too_many_records() -> InvalidSettingsDocument:
    return InvalidSettingsDocument("The settings export exceeds the import record limit.")


def _parse_document(document: object) -> tuple:
    """Validate a whole export before any write; returns (records, report)."""
    top = _exact(document, ("format", "format_version", "scope", "contains_secret_values", "excluded",
                            "workspaces"), "document")
    if top["format"] != EXPORT_FORMAT or type(top["format_version"]) is not int \
            or top["format_version"] != EXPORT_VERSION or top["scope"] != EXPORT_SCOPE:
        raise InvalidSettingsDocument("The document is not a supported AIK settings export.")
    if top["contains_secret_values"] is not False:
        raise InvalidSettingsDocument("Settings imports must not contain secret values.")
    if any(type(item) is not str for item in _list(top["excluded"], "exclusion list")):
        raise InvalidSettingsDocument("The settings export exclusion list is invalid.")

    entries = _list(top["workspaces"], "workspace list")
    if len(entries) > MAX_IMPORT_RECORDS:
        raise _too_many_records()
    owners: dict[str, str] = {}
    total = 0
    for entry in entries:
        entry = _exact(entry, ("id", "name", "requires_binding", "connections", "profiles", "limits",
                               "permissions"), "workspace")
        connections = _list(entry["connections"], "connection list")
        profiles = _list(entry["profiles"], "profile list")
        # Count from list lengths before iterating or constructing anything, so
        # the limit also bounds the CPU and memory spent on an oversized input.
        total += 1 + len(connections) + len(profiles)
        total += (entry["limits"] is not None) + (entry["permissions"] is not None)
        if total > MAX_IMPORT_RECORDS:
            raise _too_many_records()
        for connection in connections:
            connection = _exact(connection, ("id", "provider", "auth_mode", "requires_authentication"),
                                "connection")
            if type(connection["id"]) is str:
                if connection["id"] in owners:
                    raise InvalidSettingsDocument("The settings export repeats a connection ID.")
                owners[connection["id"]] = entry["id"]

    records, workspace_ids, needs_auth, seen_workspaces, seen_profiles = [], [], [], set(), set()
    for entry in entries:
        if type(entry["requires_binding"]) is not bool:
            raise InvalidSettingsDocument("requires_binding must be a boolean.")
        workspace = Workspace(entry["id"], entry["name"])  # Imported workspaces are unbound.
        if workspace.id in seen_workspaces:
            raise InvalidSettingsDocument("The settings export repeats a workspace ID.")
        seen_workspaces.add(workspace.id)
        workspace_ids.append(workspace.id)
        records.append(workspace)
        for item in entry["connections"]:
            if type(item["requires_authentication"]) is not bool:
                raise InvalidSettingsDocument("requires_authentication must be a boolean.")
            connection = ConnectionProfile(
                item["id"], workspace.id, item["provider"], auth_mode=item["auth_mode"],
                needs_authentication=item["auth_mode"] != "none",
            )
            if item["requires_authentication"] != connection.needs_authentication:
                raise InvalidSettingsDocument("A connection authentication marker is inconsistent.")
            if connection.needs_authentication:
                needs_auth.append(connection.id)
            records.append(connection)
        for item in _list(entry["profiles"], "profile list"):
            item = _exact(item, ("id", "role", "connection_id", "model", "effort"), "profile")
            profile = AgentProfile(item["id"], workspace.id, item["role"], item["connection_id"],
                                   item["model"], effort=item["effort"])
            if profile.id in seen_profiles:
                raise InvalidSettingsDocument("The settings export repeats a profile ID.")
            seen_profiles.add(profile.id)
            owner = owners.get(profile.connection_id)
            if owner is None:
                raise InvalidReference("A profile refers to a connection absent from the export.")
            if owner != workspace.id:
                raise CrossWorkspaceReference("A profile refers to another workspace's connection.")
            records.append(profile)
        if entry["limits"] is not None:
            item = _exact(entry["limits"], ("max_parallel_workers", "budget_limit", "budget_unit",
                                            "budget_period"), "limits")
            records.append(WorkspaceLimits(workspace.id, **item))
        if entry["permissions"] is not None:
            item = _exact(entry["permissions"], ("file_write", "shell_command", "network"), "permissions")
            records.append(PermissionDefaults(workspace.id, **item))
        if len(records) > MAX_IMPORT_RECORDS:
            raise _too_many_records()
    report = ImportReport(tuple(workspace_ids), tuple(needs_auth), tuple(workspace_ids))
    return records, report


# --------------------------------------------------------------- transaction

class SettingsTransaction:
    """A typed settings view valid only inside its owning transaction."""

    def __init__(self, connection: sqlite3.Connection, authority):
        self._connection = connection
        self._authority = authority
        self._active = True

    def _require_active(self) -> None:
        if not self._active or not self._connection.in_transaction:
            raise StorageError("The settings transaction is no longer active.")

    @contextmanager
    def _write(self, name: str = "aik_settings_write"):
        self._require_active()
        self._connection.execute(f'SAVEPOINT "{name}"')
        try:
            yield
        except BaseException:
            self._connection.execute(f'ROLLBACK TO "{name}"')
            self._connection.execute(f'RELEASE "{name}"')
            raise
        else:
            self._connection.execute(f'RELEASE "{name}"')

    def _fetch(self, model, key: str) -> Revisioned | None:
        table, key_column = _SPEC[model]
        row = self._connection.execute(
            f'SELECT {_column_sql(model)} FROM "{table}" WHERE "{key_column}" = ?', (key,),
        ).fetchone()
        return None if row is None else _decode(model, row)

    def _all(self, model, workspace_id: str | None = None) -> tuple:
        table, key_column = _SPEC[model]
        if workspace_id is None:
            rows = self._connection.execute(f'SELECT {_column_sql(model)} FROM "{table}" ORDER BY "{key_column}"')
        else:
            rows = self._connection.execute(
                f'SELECT {_column_sql(model)} FROM "{table}" WHERE "workspace_id" = ? ORDER BY "{key_column}"',
                (workspace_id,),
            )
        return tuple(_decode(model, row) for row in rows)

    def _authority_binding(self, workspace_id: str) -> WorkspaceBinding:
        """The current AIK-084 binding for a workspace, or a fail-closed error."""
        if self._authority is None:
            raise WorkspaceBindingRequired("No AIK-084 binding authority is available to confirm the workspace.")
        try:
            binding = self._authority.binding_for(workspace_id)
        except Exception:
            raise WorkspaceBindingRequired("The AIK-084 workspace binding could not be confirmed.") from None
        if binding is None:
            raise WorkspaceBindingRequired("AIK-084 reports no binding for the workspace.")
        if type(binding) is not WorkspaceBinding:
            raise WorkspaceBindingMismatch("The settings binding differs from the AIK-084 binding.")
        try:
            # Reconstruct: object.__setattr__ can bypass the binding validator.
            binding = WorkspaceBinding(binding.workspace_id, binding.canonical_root, binding.vm_project_id)
        except InvalidRecord:
            raise WorkspaceBindingMismatch("The AIK-084 binding violates its contract.") from None
        if binding.workspace_id != workspace_id:
            raise WorkspaceBindingMismatch("The settings binding differs from the AIK-084 binding.")
        return binding

    def _verify_binding(self, workspace: Workspace) -> ExecutionTarget:
        if not workspace.bound:
            raise WorkspaceBindingRequired("The workspace has no AIK-084 canonical root and VM project binding.")
        binding = self._authority_binding(workspace.id)
        if (binding.canonical_root, binding.vm_project_id) != (workspace.canonical_root, workspace.vm_project_id):
            raise WorkspaceBindingMismatch("The settings binding differs from the AIK-084 binding.")
        return ExecutionTarget(workspace.id, workspace.canonical_root, workspace.vm_project_id)

    def _check_references(self, value, previous) -> None:
        if previous is not None and hasattr(value, "workspace_id") and previous.workspace_id != value.workspace_id:
            raise InvalidSetting("A settings record cannot move to another workspace.")
        if isinstance(value, Workspace):
            if previous is not None and previous.bound:
                if (previous.canonical_root, previous.vm_project_id) != (value.canonical_root, value.vm_project_id):
                    raise InvalidSetting("A bound workspace root and VM project change only through rebind.")
            elif value.bound:
                # Setting a new binding requires AIK-084 confirmation; without
                # an authority this fails closed instead of storing it unchecked.
                self._verify_binding(value)
            return
        if self._fetch(Workspace, value.workspace_id) is None:
            raise InvalidReference("The referenced workspace does not exist.")
        if isinstance(value, AgentProfile):
            connection = self._fetch(ConnectionProfile, value.connection_id)
            if connection is None:
                raise InvalidReference("The referenced connection does not exist.")
            if connection.value.workspace_id != value.workspace_id:
                raise CrossWorkspaceReference("A profile cannot use another workspace's connection.")

    def save(self, value, expected_revision: int = 0) -> Revisioned:
        self._require_active()
        value = _record(value)
        _revision(expected_revision, allow_create=True)
        model = type(value)
        table, key_column = _SPEC[model]
        key = getattr(value, key_column)
        names = _COLUMNS[model]
        try:
            with self._write():
                current = self._fetch(model, key)
                if expected_revision == 0:
                    if current is not None:
                        raise RevisionConflict("The settings record already exists.")
                elif current is None or current.revision != expected_revision:
                    raise RevisionConflict("The settings record revision has changed or the record is absent.")
                elif expected_revision == MAX_INTEGER:
                    raise RevisionConflict("The settings revision cannot be incremented.")
                self._check_references(value, None if current is None else current.value)
                if expected_revision == 0:
                    placeholders = ", ".join("?" for _ in range(len(names) + 1))
                    self._connection.execute(
                        f'INSERT INTO "{table}" ({_column_sql(model)}) VALUES ({placeholders})',
                        [*(getattr(value, name) for name in names), 1],
                    )
                    revision = 1
                else:
                    revision = expected_revision + 1
                    others = [name for name in names if name != key_column]
                    assignments = ", ".join(f'"{name}" = ?' for name in others)
                    updated = self._connection.execute(
                        f'UPDATE "{table}" SET {assignments}, "revision" = ? '
                        f'WHERE "{key_column}" = ? AND "revision" = ?',
                        [*(getattr(value, name) for name in others), revision, key, expected_revision],
                    )
                    if updated.rowcount != 1:
                        raise RevisionConflict("The settings record revision has changed.")
        except sqlite3.IntegrityError:
            raise InvalidReference("Settings references or constraints are inconsistent.") from None
        except sqlite3.Error as error:
            _sqlite_failure(error)
        return Revisioned(value, revision)

    def rebind_workspace(self, workspace_id: str, expected_revision: int) -> Revisioned:
        """Store the current AIK-084 binding; the authority is the only source.

        Callers cannot supply a root or VM project here. Without an authority,
        or when it fails or reports no binding, nothing is changed.
        """
        self._require_active()
        check_identifier(workspace_id, "workspace_id")
        _revision(expected_revision, allow_create=False)
        try:
            with self._write():
                current = self._fetch(Workspace, workspace_id)
                if current is None:
                    raise InvalidReference("The workspace to bind does not exist.")
                if current.revision != expected_revision or expected_revision == MAX_INTEGER:
                    raise RevisionConflict("The workspace revision has changed.")
                binding = self._authority_binding(workspace_id)
                value = Workspace(workspace_id, current.value.name, binding.canonical_root, binding.vm_project_id)
                revision = expected_revision + 1
                updated = self._connection.execute(
                    'UPDATE "workspaces" SET "canonical_root" = ?, "vm_project_id" = ?, "revision" = ? '
                    'WHERE "id" = ? AND "revision" = ?',
                    (value.canonical_root, value.vm_project_id, revision, workspace_id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise RevisionConflict("The workspace revision has changed.")
        except sqlite3.IntegrityError:
            raise InvalidReference("The workspace binding is inconsistent.") from None
        except sqlite3.Error as error:
            _sqlite_failure(error)
        return Revisioned(value, revision)

    def get(self, model, key: str) -> Revisioned | None:
        self._require_active()
        _spec(model)
        check_identifier(key, "key")
        try:
            return self._fetch(model, key)
        except sqlite3.Error as error:
            _sqlite_failure(error)

    def list(self, model, workspace_id: str | None = None, limit: int = 100, after_key: str | None = None) -> tuple:
        self._require_active()
        table, key_column = _spec(model)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidSetting("The query limit must be an integer between 1 and 1000.")
        conditions, parameters = [], []
        if workspace_id is not None:
            if model is Workspace:
                raise InvalidSetting("Workspaces are not filtered by workspace.")
            check_identifier(workspace_id, "workspace_id")
            conditions.append('"workspace_id" = ?')
            parameters.append(workspace_id)
        if after_key is not None:
            check_identifier(after_key, "after_key")
            conditions.append(f'"{key_column}" > ?')
            parameters.append(after_key)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        try:
            rows = self._connection.execute(
                f'SELECT {_column_sql(model)} FROM "{table}"{where} ORDER BY "{key_column}" LIMIT ?',
                (*parameters, limit),
            )
            return tuple(_decode(model, row) for row in rows)
        except sqlite3.Error as error:
            _sqlite_failure(error)

    def delete(self, model, key: str, expected_revision: int) -> None:
        self._require_active()
        table, key_column = _spec(model)
        check_identifier(key, "key")
        _revision(expected_revision, allow_create=False)
        try:
            with self._write():
                deleted = self._connection.execute(
                    f'DELETE FROM "{table}" WHERE "{key_column}" = ? AND "revision" = ?', (key, expected_revision),
                )
                if deleted.rowcount != 1:
                    raise RevisionConflict("The settings record revision has changed or the record is absent.")
        except sqlite3.IntegrityError:
            raise InvalidReference("A referenced or selected settings record cannot be deleted.") from None
        except sqlite3.Error as error:
            _sqlite_failure(error)

    def active_workspace(self) -> Revisioned | None:
        """The selected workspace ID; execution roots are never stored separately."""
        self._require_active()
        try:
            row = self._connection.execute(
                'SELECT "workspace_id", "revision" FROM "active_workspace" WHERE "slot" = \'active\''
            ).fetchone()
        except sqlite3.Error as error:
            _sqlite_failure(error)
        if row is None:
            return None
        try:
            check_identifier(row[0], "workspace_id")
            _revision(row[1], allow_create=False)
        except InvalidSetting:
            raise InvalidDatabase("The stored workspace selection violates its contract.") from None
        return Revisioned(row[0], row[1])

    def select_workspace(self, workspace_id: str, expected_revision: int = 0) -> Revisioned:
        self._require_active()
        check_identifier(workspace_id, "workspace_id")
        _revision(expected_revision, allow_create=True)
        try:
            with self._write():
                workspace = self._fetch(Workspace, workspace_id)
                if workspace is None:
                    raise InvalidReference("The selected workspace does not exist.")
                if self._authority is not None and workspace.value.bound:
                    self._verify_binding(workspace.value)
                current = self.active_workspace()
                if expected_revision == 0:
                    if current is not None:
                        raise RevisionConflict("A workspace selection already exists.")
                    self._connection.execute(
                        'INSERT INTO "active_workspace" ("slot", "workspace_id", "revision") VALUES (\'active\', ?, 1)',
                        (workspace_id,),
                    )
                    revision = 1
                else:
                    if current is None or current.revision != expected_revision or expected_revision == MAX_INTEGER:
                        raise RevisionConflict("The workspace selection revision has changed.")
                    revision = expected_revision + 1
                    updated = self._connection.execute(
                        'UPDATE "active_workspace" SET "workspace_id" = ?, "revision" = ? '
                        'WHERE "slot" = \'active\' AND "revision" = ?',
                        (workspace_id, revision, expected_revision),
                    )
                    if updated.rowcount != 1:
                        raise RevisionConflict("The workspace selection revision has changed.")
        except sqlite3.IntegrityError:
            raise InvalidReference("The workspace selection is inconsistent.") from None
        except sqlite3.Error as error:
            _sqlite_failure(error)
        return Revisioned(workspace_id, revision)

    def resolve_execution_target(self) -> ExecutionTarget:
        """Derive the execution root only from the current selection (fail-closed)."""
        self._require_active()
        active = self.active_workspace()
        if active is None:
            raise NoActiveWorkspace("No workspace is selected.")
        try:
            workspace = self._fetch(Workspace, active.value)
        except sqlite3.Error as error:
            _sqlite_failure(error)
        if workspace is None:
            raise InvalidDatabase("The selected workspace is missing.")
        return self._verify_binding(workspace.value)

    def workspace_settings(self, workspace_id: str) -> WorkspaceSettings | None:
        self._require_active()
        check_identifier(workspace_id, "workspace_id")
        try:
            workspace = self._fetch(Workspace, workspace_id)
            if workspace is None:
                return None
            profiles = self._all(AgentProfile, workspace_id)
            limits = self._fetch(WorkspaceLimits, workspace_id) or Revisioned(WorkspaceLimits(workspace_id), 0)
            permissions = (self._fetch(PermissionDefaults, workspace_id)
                           or Revisioned(PermissionDefaults(workspace_id), 0))
            return WorkspaceSettings(
                workspace=workspace,
                connections=self._all(ConnectionProfile, workspace_id),
                directors=tuple(item for item in profiles if item.value.role == "director"),
                workers=tuple(item for item in profiles if item.value.role == "worker"),
                limits=limits,
                permissions=permissions,
            )
        except sqlite3.Error as error:
            _sqlite_failure(error)

    def export_settings(self, workspace_ids=None) -> dict:
        """Settings-only export: no secrets, bindings, selection or history."""
        self._require_active()
        try:
            if workspace_ids is None:
                workspaces = [entry.value for entry in self._all(Workspace)]
            else:
                if isinstance(workspace_ids, str):
                    raise InvalidSetting("Workspace IDs must be given as a collection.")
                workspaces, seen = [], set()
                for workspace_id in workspace_ids:
                    check_identifier(workspace_id, "workspace_id")
                    if workspace_id in seen:
                        continue
                    seen.add(workspace_id)
                    found = self._fetch(Workspace, workspace_id)
                    if found is None:
                        raise InvalidReference("An exported workspace does not exist.")
                    workspaces.append(found.value)
            entries = []
            for workspace in workspaces:
                limits = self._fetch(WorkspaceLimits, workspace.id)
                permissions = self._fetch(PermissionDefaults, workspace.id)
                entries.append({
                    "id": workspace.id,
                    "name": workspace.name,
                    "requires_binding": True,
                    "connections": [
                        {"id": item.value.id, "provider": item.value.provider, "auth_mode": item.value.auth_mode,
                         "requires_authentication": item.value.auth_mode != "none"}
                        for item in self._all(ConnectionProfile, workspace.id)
                    ],
                    "profiles": [
                        {"id": item.value.id, "role": item.value.role, "connection_id": item.value.connection_id,
                         "model": item.value.model, "effort": item.value.effort}
                        for item in self._all(AgentProfile, workspace.id)
                    ],
                    "limits": None if limits is None else {
                        "max_parallel_workers": limits.value.max_parallel_workers,
                        "budget_limit": limits.value.budget_limit,
                        "budget_unit": limits.value.budget_unit,
                        "budget_period": limits.value.budget_period,
                    },
                    "permissions": None if permissions is None else {
                        "file_write": permissions.value.file_write,
                        "shell_command": permissions.value.shell_command,
                        "network": permissions.value.network,
                    },
                })
        except sqlite3.Error as error:
            _sqlite_failure(error)
        return {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_VERSION,
            "scope": EXPORT_SCOPE,
            "contains_secret_values": False,
            "excluded": list(EXPORT_EXCLUDED),
            "workspaces": entries,
        }

    def import_settings(self, document) -> ImportReport:
        """Validate the whole document, then create every record or none."""
        self._require_active()
        records, report = _parse_document(document)
        with self._write("aik_settings_import"):
            for record in records:
                self.save(record, expected_revision=0)
        return report


# --------------------------------------------------------------------- store

class SettingsStore:
    """Typed settings API for the UI, persisted outside source repositories."""

    def __init__(self, path: Path | str | None = None, timeout: float = 5.0, binding_authority=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidSetting("Storage timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        self.binding_authority = binding_authority
        self.path = validate_database_path(default_settings_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._initialize()

    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Settings storage requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_files(self) -> bool:
        """Validate companions and the header; True when the database exists."""
        validate_database_path(self.path)
        for suffix in ("-wal", "-shm"):
            companion = self.path.with_name(self.path.name + suffix)
            validate_database_path(companion)
            if companion.exists():
                raise InvalidDatabase("Settings storage requires a DELETE-mode database without WAL companions.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        if not self.path.exists():
            if journal.exists():
                raise InvalidDatabase("An orphan rollback journal cannot initialize settings storage.")
            return False
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
                or int.from_bytes(header[68:72], "big") != schema.APPLICATION_ID:
            raise InvalidDatabase("This file is not an AIK settings database.")
        return True

    @staticmethod
    def _validate_contents(connection: sqlite3.Connection) -> None:
        schema.validate_schema(connection, full=True)
        for model, (table, _) in _SPEC.items():
            for row in connection.execute(f'SELECT {_column_sql(model)} FROM "{table}"'):
                _decode(model, row)

    def _validate_recovery_copy(self) -> None:
        """Let SQLite apply an existing journal to a private copy first.

        Opening the real file would replay its hot journal in place before any
        check, so a damaged pair or a foreign journal would rewrite bytes that
        are then rejected. The source pair is only used after the copy passes.
        """
        journal = self.path.with_name(self.path.name + "-journal")
        if not journal.exists():
            return
        temporary = Path(tempfile.mkdtemp(prefix=".aik-settings-check-", dir=self.path.parent))
        try:
            checked = temporary / "checked.sqlite3"
            shutil.copyfile(self.path, checked)
            shutil.copyfile(journal, checked.with_name(checked.name + "-journal"))
            try:
                with closing(self._connect(checked)) as connection:
                    self._validate_contents(connection)
            except sqlite3.Error:
                raise InvalidDatabase("The settings database could not be safely recovered.") from None
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _initialize(self) -> None:
        try:
            if self._check_files():
                self._validate_recovery_copy()
                with closing(self._connect(self.path)) as connection:
                    self._validate_contents(connection)
                return
            temporary = Path(tempfile.mkdtemp(prefix=".aik-settings-init-", dir=self.path.parent))
            try:
                created = temporary / "settings.sqlite3"
                with closing(self._connect(created, create=True)) as connection:
                    schema.create_schema(connection)
                    schema.validate_schema(connection, full=True)
                os.chmod(created, 0o600)
                _sync_file(created)
                if self._check_files():
                    raise StorageError("The settings destination changed during initialization.")
                os.replace(created, self.path)
                _sync_directory(self.path.parent)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The settings database files could not be processed safely.") from None

    @contextmanager
    def transaction(self):
        """Commit all contained settings operations together, or roll them back."""
        with database_lock(self.path, self.timeout):
            try:
                exists = self._check_files()
                if exists:
                    self._validate_recovery_copy()
            except OSError:
                raise StorageError("The settings database files could not be inspected.") from None
            if not exists:
                raise InvalidDatabase("The settings database is missing; it is not recreated implicitly.")
            connection = None
            transaction = None
            try:
                connection = self._connect(self.path)
                connection.execute("BEGIN IMMEDIATE")
                schema.validate_schema(connection)
                transaction = SettingsTransaction(connection, self.binding_authority)
                yield transaction
                connection.commit()
            except BaseException as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if isinstance(error, sqlite3.Error):
                    _sqlite_failure(error)
                raise
            finally:
                if transaction is not None:
                    transaction._active = False
                if connection is not None:
                    connection.close()

    def save(self, value, expected_revision: int = 0) -> Revisioned:
        with self.transaction() as tx:
            return tx.save(value, expected_revision)

    def rebind_workspace(self, workspace_id: str, expected_revision: int) -> Revisioned:
        with self.transaction() as tx:
            return tx.rebind_workspace(workspace_id, expected_revision)

    def get(self, model, key: str) -> Revisioned | None:
        with self.transaction() as tx:
            return tx.get(model, key)

    def list(self, model, workspace_id: str | None = None, limit: int = 100, after_key: str | None = None) -> tuple:
        with self.transaction() as tx:
            return tx.list(model, workspace_id, limit, after_key)

    def delete(self, model, key: str, expected_revision: int) -> None:
        with self.transaction() as tx:
            tx.delete(model, key, expected_revision)

    def active_workspace(self) -> Revisioned | None:
        with self.transaction() as tx:
            return tx.active_workspace()

    def select_workspace(self, workspace_id: str, expected_revision: int = 0) -> Revisioned:
        with self.transaction() as tx:
            return tx.select_workspace(workspace_id, expected_revision)

    def resolve_execution_target(self) -> ExecutionTarget:
        with self.transaction() as tx:
            return tx.resolve_execution_target()

    def workspace_settings(self, workspace_id: str) -> WorkspaceSettings | None:
        with self.transaction() as tx:
            return tx.workspace_settings(workspace_id)

    def export_settings(self, workspace_ids=None) -> dict:
        with self.transaction() as tx:
            return tx.export_settings(workspace_ids)

    def import_settings(self, document) -> ImportReport:
        with self.transaction() as tx:
            return tx.import_settings(document)
