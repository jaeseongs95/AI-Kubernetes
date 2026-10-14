"""Project folder selection for the desktop engine (AIK-087).

``WorkspaceService`` puts the ``workspaces.*`` engine commands on top of the
AIK-084 ``WorkspaceManager``, the AIK-007 settings selection and the AIK-021
connection scope store. It adds no lifecycle rule of its own:

- Registering a folder (open or create) needs a VM project, as AIK-084 decided.
  Without a configured VM project port the registration fails closed with
  ``VM_NOT_CONFIGURED`` and leaves no folder, registry entry or setting behind.
- Every status comes from ``WorkspaceManager.status``; the required user action
  is ``aik_workspace.REQUIRED_ACTION`` for that status.
- Reads never create a file or directory. A missing registry, settings or scope
  file is reported as absent.
- Folder paths reach this service only from the main process, which takes them
  from its own folder dialog; the renderer never supplies a path.

Every failure is a ``WorkspaceCommandError`` with a fixed code and no path.
"""

from __future__ import annotations

from pathlib import Path
import secrets

from aik_config import SettingsStore, Workspace
from aik_config.errors import InvalidSetting, NoActiveWorkspace, WorkspaceBindingError
from aik_config.models import ConnectionProfile
from aik_runtime.errors import InvalidRecord, RevisionConflict, StorageBusy, StorageError
from aik_workspace import (
    REQUIRED_ACTION, InvalidWorkspaceRoot, RootConflict, RootUnavailable, VmProjectUnavailable,
    WorkspaceError, WorkspaceManager, WorkspaceNotFound, WorkspaceRegistry, WorkspaceStatus,
    WorkspaceUnavailable,
)
from aik_workspace.roots import check_new_folder_name

WORKSPACE_CODES = frozenset({
    "INVALID_INPUT", "INVALID_NAME", "INVALID_FOLDER_NAME", "ROOT_INVALID", "ROOT_UNAVAILABLE",
    "ROOT_CONFLICT", "UNKNOWN_WORKSPACE", "REVISION_CONFLICT", "VM_NOT_CONFIGURED",
    "VM_PROJECT_UNAVAILABLE", "BINDING_MISMATCH", "STORE_BUSY", "STORE_UNAVAILABLE", "WORKSPACE_ERROR",
    "WORKSPACES_NOT_CONFIGURED",
    *("WORKSPACE_" + status.value.upper() for status in WorkspaceStatus if status is not WorkspaceStatus.READY),
})
_ID_PREFIX = "ws-"


class WorkspaceCommandError(Exception):
    """A ``workspaces.*`` command failed; ``code`` is one of ``WORKSPACE_CODES``."""

    def __init__(self, code: str):
        if code not in WORKSPACE_CODES:
            raise ValueError("Unknown workspace command code.")
        super().__init__(code)
        self.code = code


class UnconfiguredProjects:
    """The VM project port while no VM is connected: every call fails closed."""

    def create_project(self, workspace_id: str, canonical_root: str, name: str) -> str:
        raise VmProjectUnavailable("No VM is configured.")

    def lookup_project(self, project_id: str):
        raise VmProjectUnavailable("No VM is configured.")


def _code(error: BaseException, vm_configured: bool) -> str:
    if isinstance(error, WorkspaceCommandError):
        return error.code
    if isinstance(error, WorkspaceUnavailable):
        return "WORKSPACE_" + error.status.value.upper()
    if isinstance(error, VmProjectUnavailable):
        return "VM_PROJECT_UNAVAILABLE" if vm_configured else "VM_NOT_CONFIGURED"
    if isinstance(error, RootConflict):
        return "ROOT_CONFLICT"
    if isinstance(error, RootUnavailable):
        return "ROOT_UNAVAILABLE"
    if isinstance(error, InvalidWorkspaceRoot):
        return "ROOT_INVALID"
    if isinstance(error, WorkspaceNotFound):
        return "UNKNOWN_WORKSPACE"
    if isinstance(error, RevisionConflict):
        return "REVISION_CONFLICT"
    if isinstance(error, WorkspaceBindingError):
        return "BINDING_MISMATCH"
    if isinstance(error, (InvalidRecord, InvalidSetting)):
        return "INVALID_INPUT"
    if isinstance(error, StorageBusy):
        return "STORE_BUSY"
    if isinstance(error, (StorageError, OSError)):
        return "STORE_UNAVAILABLE"
    if isinstance(error, WorkspaceError):
        return "WORKSPACE_ERROR"
    raise error


class WorkspaceService:
    """Engine-facing workspace commands; one instance serves one engine process."""

    def __init__(self, settings_path: Path | str, registry_path: Path | str, *, projects=None,
                 host_check=None, new_id=None):
        self._settings_path = Path(settings_path)
        self._registry_path = Path(registry_path)
        self._vm_configured = projects is not None
        self._projects = projects if projects is not None else UnconfiguredProjects()
        self._host_check = host_check
        self._new_id = new_id or (lambda: _ID_PREFIX + secrets.token_hex(8))
        self._manager: WorkspaceManager | None = None
        self._settings: SettingsStore | None = None

    # -- wiring ---------------------------------------------------------------
    def _open(self, *, create: bool) -> WorkspaceManager | None:
        """The manager with settings attached; None for a read before anything exists."""
        if self._manager is not None:
            if self._settings is None and (create or self._settings_path.exists()):
                self._attach_settings()
            return self._manager
        if not create and not self._registry_path.exists():
            return None
        options = {} if self._host_check is None else {"host_check": self._host_check}
        manager = WorkspaceManager(WorkspaceRegistry(self._registry_path), self._projects, **options)
        self._manager = manager
        if create or self._settings_path.exists():
            self._attach_settings()
        return manager

    def _attach_settings(self) -> None:
        assert self._manager is not None
        settings = SettingsStore(self._settings_path, binding_authority=self._manager)
        self._manager.connect_settings(settings)
        self._settings = settings

    def _run(self, action):
        try:
            return action()
        except Exception as error:  # noqa: BLE001 - mapped to a fixed code or re-raised
            raise WorkspaceCommandError(_code(error, self._vm_configured)) from None

    @staticmethod
    def _name(value: object) -> str:
        try:
            Workspace(_ID_PREFIX + "check", value)  # type: ignore[arg-type]
        except InvalidSetting:
            raise WorkspaceCommandError("INVALID_NAME") from None
        return value  # type: ignore[return-value]

    @staticmethod
    def _folder_name(value: object) -> str:
        try:
            check_new_folder_name(value)
        except (InvalidWorkspaceRoot, TypeError):
            raise WorkspaceCommandError("INVALID_FOLDER_NAME") from None
        return value  # type: ignore[return-value]

    @staticmethod
    def _text(value: object) -> str:
        if type(value) is not str or not value:
            raise WorkspaceCommandError("INVALID_INPUT")
        return value

    @staticmethod
    def _revision(value: object) -> int:
        if type(value) is not int or value < 1:
            raise WorkspaceCommandError("INVALID_INPUT")
        return value

    # -- DTOs -----------------------------------------------------------------
    def _status(self, manager: WorkspaceManager, workspace_id: str) -> str:
        try:
            return manager.status(workspace_id).value
        except WorkspaceNotFound:
            raise WorkspaceCommandError("UNKNOWN_WORKSPACE") from None

    def _dto(self, manager: WorkspaceManager, record, active_id: str | None) -> dict:
        status = self._status(manager, record.id)
        return {
            "workspace_id": record.id,
            "name": record.name,
            "root": record.canonical_root,
            "state": record.state,
            "status": status,
            "required_action": REQUIRED_ACTION[WorkspaceStatus(status)],
            "revision": record.revision,
            "active": record.id == active_id,
        }

    def _active(self) -> tuple[str | None, int]:
        if self._settings is None:
            return None, 0
        selected = self._settings.active_workspace()
        return (None, 0) if selected is None else (selected.value, selected.revision)

    def _active_dto(self, manager: WorkspaceManager, active_id: str | None, revision: int) -> dict | None:
        if active_id is None:
            return None
        record = manager.get(active_id)
        if record is None:
            # The selection names a settings-only workspace, e.g. the unbound "local" one.
            return {"workspace_id": active_id, "registered": False, "status": None,
                    "required_action": "reselect_folder", "cwd": None, "selection_revision": revision}
        status = self._status(manager, active_id)
        cwd = None
        if status == WorkspaceStatus.READY.value:
            try:
                cwd = manager.current_execution().canonical_root
            except (WorkspaceError, WorkspaceBindingError, NoActiveWorkspace):
                status = None
        return {"workspace_id": active_id, "registered": True, "status": status,
                "required_action": REQUIRED_ACTION[WorkspaceStatus(status)] if status else "reselect_folder",
                "cwd": cwd, "selection_revision": revision}

    # -- reads ----------------------------------------------------------------
    def list(self) -> dict:
        def read() -> dict:
            manager = self._open(create=False)
            base = {"vm": "CONFIGURED" if self._vm_configured else "NOT_CONFIGURED"}
            if manager is None:
                return {**base, "registry": "ABSENT", "active": None, "workspaces": []}
            active_id, revision = self._active()
            records = manager.list_workspaces()
            return {**base, "registry": "READY", "active": self._active_dto(manager, active_id, revision),
                    "workspaces": [self._dto(manager, record, active_id) for record in records]}
        return self._run(read)

    def scope(self, workspace_id: object) -> dict:
        """Which stored connections this workspace may use (AIK-021); read only."""
        workspace_id = self._text(workspace_id)

        def read() -> dict:
            manager = self._open(create=False)
            if manager is None or manager.get(workspace_id) is None:
                raise WorkspaceCommandError("UNKNOWN_WORKSPACE")
            if self._settings is None:
                return {"workspace_id": workspace_id, "scope": "ABSENT", "connections": []}
            connections = [entry.value for entry in self._settings.list(ConnectionProfile, limit=100)]
            scope_path = self._settings_path.with_name("scopes.sqlite3")
            if not scope_path.exists():
                rows = [self._scope_row(profile, "NO_SCOPE") for profile in connections]
                return {"workspace_id": workspace_id, "scope": "ABSENT", "connections": rows}
            from aik_scope import ConnectionNotInScope, ScopeStore

            store = ScopeStore(self._settings, scope_path)
            rows = []
            for profile in connections:
                try:
                    store.check(workspace_id, profile.id)
                    rows.append(self._scope_row(profile, None))
                except ConnectionNotInScope as error:
                    rows.append(self._scope_row(profile, error.reason))
            return {"workspace_id": workspace_id, "scope": "READY", "connections": rows}
        return self._run(read)

    @staticmethod
    def _scope_row(profile: ConnectionProfile, reason: str | None) -> dict:
        return {"connection_id": profile.id, "provider": profile.provider,
                "owner_workspace_id": profile.workspace_id, "allowed": reason is None, "reason": reason}

    # -- writes ---------------------------------------------------------------
    def _register(self, register) -> dict:
        def write() -> dict:
            manager = self._open(create=True)
            assert manager is not None
            record = register(manager, self._new_id())
            active_id, _ = self._active()
            return self._dto(manager, record, active_id)
        return self._run(write)

    def open(self, name: object, root: object) -> dict:
        """Bind an existing folder; its contents are not changed."""
        name, root = self._name(name), self._text(root)
        return self._register(lambda manager, new_id: manager.open_workspace(new_id, name, root))

    def create(self, name: object, parent: object, folder_name: object) -> dict:
        """Create ``folder_name`` under an existing ``parent`` folder and bind it."""
        name, parent = self._name(name), self._text(parent)
        folder_name = self._folder_name(folder_name)
        return self._register(
            lambda manager, new_id: manager.create_workspace(new_id, name, Path(parent) / folder_name))

    def select(self, workspace_id: object) -> dict:
        workspace_id = self._text(workspace_id)

        def write() -> dict:
            manager = self._open(create=True)
            assert manager is not None
            if manager.get(workspace_id) is None:
                raise WorkspaceCommandError("UNKNOWN_WORKSPACE")
            manager.select(workspace_id)
            active_id, revision = self._active()
            return self._active_dto(manager, active_id, revision)
        return self._run(write)

    def _update(self, workspace_id: object, expected_revision: object, change) -> dict:
        workspace_id, expected_revision = self._text(workspace_id), self._revision(expected_revision)

        def write() -> dict:
            manager = self._open(create=True)
            assert manager is not None
            record = change(manager, workspace_id, expected_revision)
            active_id, _ = self._active()
            return self._dto(manager, record, active_id)
        return self._run(write)

    def rebind_root(self, workspace_id: object, root: object, expected_revision: object) -> dict:
        """Re-bind a moved, replaced or re-selected folder to the same workspace."""
        root = self._text(root)
        return self._update(workspace_id, expected_revision,
                            lambda manager, wid, rev: manager.rebind_root(wid, root, rev))

    def archive(self, workspace_id: object, expected_revision: object) -> dict:
        return self._update(workspace_id, expected_revision,
                            lambda manager, wid, rev: manager.archive_workspace(wid, rev))

    def restore(self, workspace_id: object, expected_revision: object) -> dict:
        return self._update(workspace_id, expected_revision,
                            lambda manager, wid, rev: manager.restore_workspace(wid, rev))
