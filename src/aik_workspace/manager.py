"""Workspace lifecycle, binding authority, selection and input binding (AIK-084)."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path

from aik_config import (
    SettingsStore, Workspace, WorkspaceBinding, WorkspaceBindingError, WorkspaceBindingMismatch,
)
from aik_runtime.errors import InvalidRecord, RevisionConflict
from aik_runtime.models import MAX_INTEGER, identifier
from aik_vm_bridge.binding import DIGEST_PATTERN, TaskExecutionBinding

from . import roots
from .errors import (
    InputBindingMismatch, InputCondition, InputNotCurrent, InputTampered, InvalidWorkspaceRoot,
    RootConflict, RootUnavailable, SettingsNotConnected, VmProjectUnavailable, WorkspaceError,
    WorkspaceNotFound, WorkspaceStatus, WorkspaceUnavailable,
)
from .inputs import normalize_input_path, read_verified, sha256_digest
from .models import (
    INPUT_MODE_SHARED_ROOT, ROOT_REF_PREFIX, ExecutionContext, InputEntry, InputManifest, InputRecord,
    InputState, WorkspaceRecord,
)
from .registry import RegistryState, WorkspaceRegistry
from .vm_projects import ProjectInfo


DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
_S = WorkspaceStatus
# Placeholder used only to validate a complete record before any VM effect.
_PROVISIONAL_PROJECT_ID = "aik-provisional-project"


def _same_root(first: str, second: str) -> bool:
    return os.path.normcase(first) == os.path.normcase(second)


def _verifiable_identity(identity: object) -> bool:
    """A root identity must fit the record and have a nonzero inode (fail-closed)."""
    return (type(identity) is tuple and len(identity) == 2
            and all(type(value) is int for value in identity)
            and 0 <= identity[0] <= MAX_INTEGER and 1 <= identity[1] <= MAX_INTEGER)


def _identity_matches(record: WorkspaceRecord, identity: tuple[int, int]) -> bool:
    # Without an inode a replaced folder cannot be distinguished: never trust it.
    if record.root_inode == 0 or identity[1] == 0:
        return False
    return (record.root_device, record.root_inode) == identity


def _root_ref(record: WorkspaceRecord) -> str:
    text = f"{record.id}\0{record.canonical_root}\0{record.vm_project_id}"
    return ROOT_REF_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _positive_revision(value: object) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise InvalidRecord("An expected revision must be a positive bounded integer.")


class WorkspaceManager:
    """Implements aik_config.WorkspaceBindingAuthority via ``binding_for``."""

    def __init__(self, registry: WorkspaceRegistry, projects, *, settings: SettingsStore | None = None,
                 host_check=None, max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES):
        if type(registry) is not WorkspaceRegistry:
            raise TypeError("A WorkspaceRegistry is required.")
        for name in ("create_project", "lookup_project"):
            if not callable(getattr(projects, name, None)):
                raise TypeError("The VM project port lacks a required method.")
        if host_check is not None and not callable(host_check):
            raise TypeError("host_check must be callable.")
        if type(max_input_bytes) is not int or not 1 <= max_input_bytes <= MAX_INTEGER:
            raise InvalidRecord("max_input_bytes must be a positive integer.")
        self._registry = registry
        self._projects = projects
        self._host_check = host_check or roots.host_cwd_support
        self._max_input_bytes = max_input_bytes
        self._settings: SettingsStore | None = None
        if settings is not None:
            self.connect_settings(settings)

    # ------------------------------------------------------------ settings link
    def connect_settings(self, settings: SettingsStore) -> None:
        if type(settings) is not SettingsStore or settings.binding_authority is not self:
            raise SettingsNotConnected("The settings store must use this manager as its binding authority.")
        self._settings = settings

    def _require_settings(self) -> SettingsStore:
        if self._settings is None:
            raise SettingsNotConnected("Workspace selection requires a connected settings store.")
        return self._settings

    def _sync_settings(self, record: WorkspaceRecord, *, strict: bool) -> bool:
        """Mirror the verified binding into settings; settings re-check it through binding_for."""
        settings = self._settings
        if settings is None:
            return True
        try:
            with settings.transaction() as tx:
                current = tx.get(Workspace, record.id)
                if current is None:
                    tx.save(Workspace(record.id, record.name, record.canonical_root, record.vm_project_id), 0)
                elif (current.value.canonical_root, current.value.vm_project_id) != (
                        record.canonical_root, record.vm_project_id):
                    tx.rebind_workspace(record.id, current.revision)
            return True
        except WorkspaceBindingError:
            if strict:
                raise
            return False

    # ------------------------------------------------------------ VM port
    def _lookup(self, project_id: str) -> ProjectInfo | None:
        try:
            info = self._projects.lookup_project(project_id)
        except VmProjectUnavailable:
            raise
        except Exception:
            raise VmProjectUnavailable("The VM project state could not be confirmed.") from None
        if info is None:
            return None
        if type(info) is not ProjectInfo or info.project_id != project_id:
            raise VmProjectUnavailable("The VM project response is inconsistent.")
        return info

    def _create_project(self, workspace_id: str, root: str, name: str) -> str:
        try:
            project_id = self._projects.create_project(workspace_id, root, name)
        except VmProjectUnavailable:
            raise
        except Exception:
            raise VmProjectUnavailable("The VM project could not be created.") from None
        try:
            identifier(project_id, "vm_project_id")
        except InvalidRecord:
            raise VmProjectUnavailable("The VM returned an invalid project ID.") from None
        return project_id

    # ------------------------------------------------------------ records
    def _record(self, workspace_id: str) -> WorkspaceRecord:
        try:
            identifier(workspace_id, "workspace_id")
        except InvalidRecord:
            raise WorkspaceNotFound("The workspace is not registered.") from None
        record = self._registry.read().workspaces.get(workspace_id)
        if record is None:
            raise WorkspaceNotFound("The workspace is not registered.")
        return record

    @staticmethod
    def _current(state: RegistryState, workspace_id: str, expected_revision: int) -> WorkspaceRecord:
        _positive_revision(expected_revision)
        record = state.workspaces.get(workspace_id)
        if record is None:
            raise WorkspaceNotFound("The workspace is not registered.")
        if record.revision != expected_revision or record.revision == MAX_INTEGER:
            raise RevisionConflict("The workspace revision has changed.")
        return record

    @staticmethod
    def _check_conflicts(state: RegistryState, root: str, identity: tuple[int, int] | None,
                         exclude: str | None) -> None:
        for other in state.workspaces.values():
            if other.id == exclude or other.archived:
                continue
            if roots.overlaps(other.canonical_root, root) or (
                    identity is not None and identity[1] != 0 and (other.root_device, other.root_inode) == identity):
                raise RootConflict("The root duplicates or nests another workspace root.")

    def get(self, workspace_id: str) -> WorkspaceRecord | None:
        try:
            return self._record(workspace_id)
        except WorkspaceNotFound:
            return None

    def list_workspaces(self) -> tuple:
        return tuple(record for _, record in sorted(self._registry.read().workspaces.items()))

    # ------------------------------------------------------------ status
    def _host_supported(self, path: Path) -> bool:
        try:
            return bool(self._host_check(path))
        except Exception:
            return False

    def _status(self, record: WorkspaceRecord) -> WorkspaceStatus:
        if record.archived:
            return _S.ARCHIVED
        root = Path(record.canonical_root)
        if not self._host_supported(root):
            return _S.UNSUPPORTED_HOST
        try:
            os.lstat(root)
        except FileNotFoundError:
            return _S.ROOT_MISSING
        except PermissionError:
            return _S.ACCESS_DENIED
        except OSError:
            return _S.ROOT_UNSAFE
        try:
            canonical = roots.canonical_root(root)
        except RootUnavailable:
            return _S.ACCESS_DENIED
        except InvalidWorkspaceRoot:
            return _S.ROOT_UNSAFE
        if not _same_root(str(canonical), record.canonical_root):
            return _S.ROOT_UNSAFE
        try:
            identity = roots.root_identity(canonical)
        except OSError:
            return _S.ACCESS_DENIED
        if not _identity_matches(record, identity):
            return _S.ROOT_REPLACED
        try:
            roots.probe_access(canonical)
        except OSError:
            return _S.ACCESS_DENIED
        try:
            info = self._lookup(record.vm_project_id)
        except VmProjectUnavailable:
            return _S.VM_PROJECT_UNKNOWN
        if info is None:
            return _S.VM_PROJECT_MISSING
        if info.root is None:
            # An unreported ProjectMap root cannot confirm the project-root binding.
            return _S.VM_PROJECT_UNKNOWN
        if not _same_root(info.root, record.canonical_root):
            return _S.VM_PROJECT_MISMATCH
        return _S.READY

    def status(self, workspace_id: str) -> WorkspaceStatus:
        return self._status(self._record(workspace_id))

    def execution_context(self, workspace_id: str) -> ExecutionContext:
        """Re-verify root, identity, access, host support and VM project; then derive cwd."""
        record = self._record(workspace_id)
        status = self._status(record)
        if status is not _S.READY:
            raise WorkspaceUnavailable(status)
        return ExecutionContext(record.id, record.canonical_root, record.vm_project_id, record.revision,
                                _root_ref(record))

    def binding_for(self, workspace_id: str) -> WorkspaceBinding | None:
        """WorkspaceBindingAuthority: None unless the binding is executable right now."""
        try:
            context = self.execution_context(workspace_id)
        except WorkspaceError:
            return None
        return WorkspaceBinding(context.workspace_id, context.canonical_root, context.vm_project_id)

    # ------------------------------------------------------------ lifecycle
    def _prepare_root(self, root_path) -> tuple[Path, tuple[int, int]]:
        canonical = roots.canonical_root(root_path)
        if not self._host_supported(canonical):
            raise WorkspaceUnavailable(_S.UNSUPPORTED_HOST)
        try:
            roots.probe_access(canonical)
            identity = roots.root_identity(canonical)
        except OSError:
            raise WorkspaceUnavailable(_S.ACCESS_DENIED) from None
        if not _verifiable_identity(identity):
            # Out-of-range IDs cannot be recorded; inode 0 cannot detect replacement.
            raise WorkspaceUnavailable(_S.ROOT_UNSAFE)
        return canonical, identity

    def _register(self, workspace_id: str, name: str, root_path) -> WorkspaceRecord:
        Workspace(workspace_id, name)
        canonical, identity = self._prepare_root(root_path)
        root = str(canonical)
        # Validate every record field before the VM effect; only the project ID is provisional.
        WorkspaceRecord(workspace_id, name, root, identity[0], identity[1], _PROVISIONAL_PROJECT_ID)
        state = self._registry.read()
        if workspace_id in state.workspaces:
            raise RevisionConflict("The workspace is already registered.")
        self._check_conflicts(state, root, identity, None)
        project_id = self._create_project(workspace_id, root, name)
        record = WorkspaceRecord(workspace_id, name, root, identity[0], identity[1], project_id)

        def insert(current: RegistryState) -> None:
            if workspace_id in current.workspaces:
                raise RevisionConflict("The workspace is already registered.")
            self._check_conflicts(current, root, identity, None)
            current.workspaces[workspace_id] = record

        self._registry.update(insert)
        try:
            self._sync_settings(record, strict=True)
        except BaseException:
            def remove(current: RegistryState) -> None:
                if current.workspaces.get(workspace_id) == record:
                    del current.workspaces[workspace_id]
            self._registry.update(remove)
            raise
        return record

    def open_workspace(self, workspace_id: str, name: str, root_path) -> WorkspaceRecord:
        """Bind an existing folder; its contents are not modified."""
        return self._register(workspace_id, name, root_path)

    def _check_new_root_location(self, parent: Path, target: Path) -> None:
        """Reject Git internals, ledgers and unsupported hosts before any directory is created."""
        try:
            if roots.is_git_internal(parent):
                raise InvalidWorkspaceRoot("Git metadata directories are not workspace roots.")
            if roots.ledger_conflict(target):
                raise InvalidWorkspaceRoot("Development ledgers and the AIK source repository are not workspace roots.")
        except WorkspaceError:
            raise
        except OSError:
            raise RootUnavailable("The workspace folder cannot be inspected.") from None
        if not self._host_supported(parent):
            raise WorkspaceUnavailable(_S.UNSUPPORTED_HOST)

    def create_workspace(self, workspace_id: str, name: str, root_path) -> WorkspaceRecord:
        """Create a new empty folder under an existing safe parent and bind it."""
        Workspace(workspace_id, name)
        requested = roots.absolute_path(root_path)
        roots.check_new_folder_name(requested.name)
        parent = roots.safe_directory(requested.parent)
        target = parent / requested.name
        if os.path.lexists(target):
            raise InvalidWorkspaceRoot("The new workspace folder already exists; open it instead.")
        self._check_new_root_location(parent, target)
        state = self._registry.read()
        if workspace_id in state.workspaces:
            raise RevisionConflict("The workspace is already registered.")
        self._check_conflicts(state, str(target), None, None)
        try:
            os.mkdir(target)
        except OSError:
            raise RootUnavailable("The workspace folder could not be created.") from None
        try:
            return self._register(workspace_id, name, target)
        except BaseException:
            try:
                target.rmdir()
            except OSError:
                pass
            raise

    def archive_workspace(self, workspace_id: str, expected_revision: int) -> WorkspaceRecord:
        def mutate(state: RegistryState) -> WorkspaceRecord:
            record = self._current(state, workspace_id, expected_revision)
            if record.archived:
                raise WorkspaceUnavailable(_S.ARCHIVED)
            updated = replace(record, state="archived", revision=record.revision + 1)
            state.workspaces[workspace_id] = updated
            return updated
        return self._registry.update(mutate)

    def restore_workspace(self, workspace_id: str, expected_revision: int) -> WorkspaceRecord:
        def mutate(state: RegistryState) -> WorkspaceRecord:
            record = self._current(state, workspace_id, expected_revision)
            if not record.archived:
                raise RevisionConflict("The workspace is not archived.")
            self._check_conflicts(state, record.canonical_root, (record.root_device, record.root_inode), workspace_id)
            updated = replace(record, state="active", revision=record.revision + 1)
            state.workspaces[workspace_id] = updated
            return updated
        record = self._registry.update(mutate)
        self._sync_settings(record, strict=False)
        return record

    def rebind_root(self, workspace_id: str, root_path, expected_revision: int) -> WorkspaceRecord:
        """Explicitly re-bind after the folder moved, was replaced or was re-selected."""
        canonical, identity = self._prepare_root(root_path)
        root = str(canonical)

        def mutate(state: RegistryState) -> WorkspaceRecord:
            record = self._current(state, workspace_id, expected_revision)
            if record.archived:
                raise WorkspaceUnavailable(_S.ARCHIVED)
            self._check_conflicts(state, root, identity, workspace_id)
            updated = replace(record, canonical_root=root, root_device=identity[0], root_inode=identity[1],
                              revision=record.revision + 1)
            state.workspaces[workspace_id] = updated
            return updated

        record = self._registry.update(mutate)
        self._sync_settings(record, strict=False)
        return record

    def rebind_project(self, workspace_id: str, expected_revision: int) -> WorkspaceRecord:
        """Create a new VM project only when the old one is confirmed missing or mismatched."""
        record = self._record(workspace_id)
        _positive_revision(expected_revision)
        if record.revision != expected_revision:
            raise RevisionConflict("The workspace revision has changed.")
        status = self._status(record)
        if status is _S.READY:
            raise RevisionConflict("The VM project binding is current; no rebind is required.")
        if status not in (_S.VM_PROJECT_MISSING, _S.VM_PROJECT_MISMATCH):
            raise WorkspaceUnavailable(status)
        project_id = self._create_project(workspace_id, record.canonical_root, record.name)

        def mutate(state: RegistryState) -> WorkspaceRecord:
            current = self._current(state, workspace_id, expected_revision)
            if (current.canonical_root, current.root_device, current.root_inode) != (
                    record.canonical_root, record.root_device, record.root_inode):
                raise RevisionConflict("The workspace root changed during VM project rebind.")
            updated = replace(current, vm_project_id=project_id, revision=current.revision + 1)
            state.workspaces[workspace_id] = updated
            return updated

        updated = self._registry.update(mutate)
        self._sync_settings(updated, strict=False)
        return updated

    # ------------------------------------------------------------ selection
    def select(self, workspace_id: str) -> ExecutionContext:
        """Switch the settings selection only after root and VM project re-verification."""
        settings = self._require_settings()
        record = self._record(workspace_id)
        self.execution_context(workspace_id)
        self._sync_settings(record, strict=True)
        with settings.transaction() as tx:
            active = tx.active_workspace()
            tx.select_workspace(workspace_id, 0 if active is None else active.revision)
        return self.current_execution()

    def current_execution(self) -> ExecutionContext:
        """The execution root derived only from the current selection (fail-closed).

        Task launches must not use this; they use the context returned by
        ``verify_task_binding`` so a later selection change cannot redirect them.
        """
        settings = self._require_settings()
        target = settings.resolve_execution_target()
        context = self.execution_context(target.workspace_id)
        if not _same_root(context.canonical_root, target.canonical_root) or \
                context.vm_project_id != target.vm_project_id:
            raise WorkspaceBindingMismatch("The settings binding differs from the AIK-084 binding.")
        return context

    # ------------------------------------------------------------ inputs
    def _read(self, context: ExecutionContext, relative: str):
        return read_verified(Path(context.canonical_root), relative, self._max_input_bytes)

    @staticmethod
    def _check_expected_digest(expected_digest) -> None:
        if expected_digest is not None and (type(expected_digest) is not str
                                            or DIGEST_PATTERN.fullmatch(expected_digest) is None):
            raise InvalidRecord("An expected digest must be a sha256 digest.")

    @staticmethod
    def _check_producer(producer_task_id, expected_digest) -> None:
        if producer_task_id is None:
            return
        identifier(producer_task_id, "producer_task_id")
        if expected_digest is None:
            # A predecessor artifact is only bound to the bytes its producer declared.
            raise InvalidRecord("A predecessor artifact requires the producer's declared digest.")

    def _read_current(self, context: ExecutionContext, relative: str, expected_digest) -> tuple[str, int]:
        condition, data = self._read(context, relative)
        if condition is not InputCondition.CURRENT:
            raise InputNotCurrent("The input cannot be read safely.", ((relative, condition.value),))
        digest = sha256_digest(data)
        if expected_digest is not None and digest != expected_digest:
            raise InputTampered("The input bytes differ from the declared digest.")
        return digest, len(data)

    def bind_input(self, workspace_id: str, path: str, *, producer_task_id: str | None = None,
                   expected_digest: str | None = None) -> InputRecord:
        """Record an input / predecessor artifact by digest. Producers must pass their declared digest."""
        context = self.execution_context(workspace_id)
        relative = normalize_input_path(path)
        self._check_expected_digest(expected_digest)
        self._check_producer(producer_task_id, expected_digest)
        digest, size = self._read_current(context, relative, expected_digest)
        record = InputRecord(workspace_id, relative, digest, size, 1, producer_task_id)

        def insert(state: RegistryState) -> None:
            workspace = state.workspaces.get(workspace_id)
            if workspace is None or workspace.revision != context.workspace_revision or workspace.archived:
                raise RevisionConflict("The workspace binding changed while binding an input.")
            if (workspace_id, relative) in state.inputs:
                raise RevisionConflict("The input is already bound; accept a change explicitly.")
            state.inputs[(workspace_id, relative)] = record

        self._registry.update(insert)
        return record

    def accept_input_change(self, workspace_id: str, path: str, expected_revision: int, *,
                            producer_task_id: str | None = None,
                            expected_digest: str | None = None) -> InputRecord:
        """Explicitly accept new bytes; older manifests become stale."""
        context = self.execution_context(workspace_id)
        relative = normalize_input_path(path)
        _positive_revision(expected_revision)
        self._check_expected_digest(expected_digest)
        self._check_producer(producer_task_id, expected_digest)
        digest, size = self._read_current(context, relative, expected_digest)

        def mutate(state: RegistryState) -> InputRecord:
            workspace = state.workspaces.get(workspace_id)
            if workspace is None or workspace.revision != context.workspace_revision or workspace.archived:
                raise RevisionConflict("The workspace binding changed while accepting an input.")
            current = state.inputs.get((workspace_id, relative))
            if current is None or current.revision != expected_revision or current.revision == MAX_INTEGER:
                raise RevisionConflict("The input revision has changed or the input is unbound.")
            updated = InputRecord(workspace_id, relative, digest, size, current.revision + 1, producer_task_id)
            state.inputs[(workspace_id, relative)] = updated
            return updated

        return self._registry.update(mutate)

    def _inputs_of(self, workspace_id: str) -> dict:
        return {path: record for (owner, path), record in self._registry.read().inputs.items()
                if owner == workspace_id}

    def input_states(self, workspace_id: str) -> tuple:
        context = self.execution_context(workspace_id)
        states = []
        for relative, record in sorted(self._inputs_of(workspace_id).items()):
            condition, data = self._read(context, relative)
            observed = None if data is None else sha256_digest(data)
            if condition is InputCondition.CURRENT and observed != record.digest:
                condition = InputCondition.STALE
            states.append(InputState(relative, condition, record.digest, observed))
        return tuple(states)

    def prepare_inputs(self, workspace_id: str, paths=None) -> InputManifest:
        """Shared-root preparation: every listed input must be current, else nothing is prepared."""
        context = self.execution_context(workspace_id)
        records = self._inputs_of(workspace_id)
        if paths is None:
            selected = sorted(records)
        else:
            if isinstance(paths, str):
                raise InvalidRecord("Input paths must be given as a collection.")
            selected = sorted({normalize_input_path(item) for item in paths})
        failures, entries = [], []
        for relative in selected:
            record = records.get(relative)
            if record is None:
                failures.append((relative, InputCondition.UNBOUND.value))
                continue
            condition, data = self._read(context, relative)
            if condition is InputCondition.CURRENT and sha256_digest(data) != record.digest:
                condition = InputCondition.STALE
            if condition is not InputCondition.CURRENT:
                failures.append((relative, condition.value))
                continue
            entries.append(InputEntry(relative, record.digest, record.size, record.revision, record.producer_task_id))
        if failures:
            raise InputNotCurrent("Some inputs are not current; nothing was prepared.", tuple(failures))
        return InputManifest(workspace_id, context.vm_project_id, context.workspace_revision,
                             context.workspace_root_ref, INPUT_MODE_SHARED_ROOT, tuple(entries))

    def _context_for_manifest(self, manifest: InputManifest) -> ExecutionContext:
        if type(manifest) is not InputManifest:
            raise InputBindingMismatch("A workspace input manifest is required.")
        try:
            context = self.execution_context(manifest.workspace_id)
        except WorkspaceNotFound:
            raise InputBindingMismatch("The manifest refers to an unknown workspace.") from None
        if (context.vm_project_id, context.workspace_revision, context.workspace_root_ref) != (
                manifest.vm_project_id, manifest.workspace_revision, manifest.workspace_root_ref):
            raise InputBindingMismatch("The manifest does not match the current workspace binding.")
        return context

    def read_input(self, manifest: InputManifest, path: str) -> bytes:
        """Return the exact bytes bound in the manifest, or fail before any use."""
        context = self._context_for_manifest(manifest)
        relative = normalize_input_path(path)
        entry = next((item for item in manifest.entries if item.path == relative), None)
        if entry is None:
            raise InputBindingMismatch("The input is not part of the manifest.")
        record = self._registry.read().inputs.get((manifest.workspace_id, relative))
        if record is None or record.revision != entry.revision or record.digest != entry.digest:
            raise InputNotCurrent("The manifest input is stale.", ((relative, InputCondition.STALE.value),))
        condition, data = self._read(context, relative)
        if condition is not InputCondition.CURRENT:
            raise InputNotCurrent("The input cannot be read safely.", ((relative, condition.value),))
        if sha256_digest(data) != entry.digest:
            raise InputTampered("The input bytes differ from the bound digest.")
        return data

    def verify_task_binding(self, binding: TaskExecutionBinding, manifest: InputManifest) -> ExecutionContext:
        """Pre-effect check of a VM task binding against the current inputs and root.

        Returns the verified ``ExecutionContext`` of the manifest's workspace.
        The task launch must use this context (its ``cwd``), not
        ``current_execution()``, so a later selection switch cannot redirect it.
        """
        if type(binding) is not TaskExecutionBinding:
            raise InputBindingMismatch("A task execution binding is required.")
        context = self._context_for_manifest(manifest)
        if (binding.input_manifest_digest != manifest.digest() or binding.vm_project_id != manifest.vm_project_id
                or binding.workspace_root_ref != manifest.workspace_root_ref):
            raise InputBindingMismatch("The task binding does not reference this input manifest.")
        current = self.prepare_inputs(manifest.workspace_id, [entry.path for entry in manifest.entries])
        if current.digest() != manifest.digest():
            raise InputNotCurrent("The task inputs changed after preparation.")
        return context

    def confirm_inputs_after_effect(self, manifest: InputManifest) -> None:
        """Post-effect re-check for consumers that read the shared root directly.

        ``shared_root`` does not freeze files: a native consumer reading through
        ``cwd`` may observe bytes changed after ``verify_task_binding``. Callers
        must classify the effect's input as unknown/failed when this raises.
        A change that was reverted before this check is not detectable.
        """
        context = self._context_for_manifest(manifest)
        records = self._registry.read().inputs
        failures = []
        for entry in manifest.entries:
            record = records.get((manifest.workspace_id, entry.path))
            if record is None or (record.revision, record.digest) != (entry.revision, entry.digest):
                failures.append((entry.path, InputCondition.STALE.value))
                continue
            condition, data = self._read(context, entry.path)
            if condition is InputCondition.CURRENT and sha256_digest(data) != entry.digest:
                condition = InputCondition.STALE
            if condition is not InputCondition.CURRENT:
                failures.append((entry.path, condition.value))
        if failures:
            raise InputNotCurrent("Inputs changed around the effect; treat the consumed input as unknown.",
                                  tuple(failures))
