"""Persistent workspace binding registry (JSON, atomic replace, shared process lock).

It stores workspace roots, VM project IDs and input digests only. It is not a
Task, Goal or input authority ledger: VM remains the owner of those.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import tempfile

from aik_platform import PlatformUnavailable
from aik_platform.fs import sync_directory as _sync_directory
from aik_platform.paths import component_file
from aik_runtime.errors import InvalidDatabase, InvalidRecord, StorageError, UnsafeStoragePath
from aik_runtime.paths import database_lock, validate_database_path

from .errors import WorkspaceError
from .models import InputRecord, WorkspaceRecord


REGISTRY_FORMAT = "aik.workspace-registry"
REGISTRY_VERSION = 1
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_WORKSPACE_KEYS = ("id", "name", "canonical_root", "root_device", "root_inode", "vm_project_id", "state", "revision")
_INPUT_KEYS = ("workspace_id", "path", "digest", "size", "revision", "producer_task_id")


def default_registry_path() -> Path:
    try:
        return component_file("workspace", "workspaces.json")
    except PlatformUnavailable:
        raise UnsafeStoragePath("Windows workspace storage requires LOCALAPPDATA.") from None


class RegistryState:
    """A mutable snapshot loaded under the lock; changes persist only via update()."""

    def __init__(self) -> None:
        self.workspaces: dict[str, WorkspaceRecord] = {}
        self.inputs: dict[tuple[str, str], InputRecord] = {}


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _exact(value: object, keys: tuple) -> dict:
    if type(value) is not dict or set(value) != set(keys):
        raise InvalidDatabase("The workspace registry has missing or unsupported fields.")
    return value


def _parse(document: object) -> RegistryState:
    try:
        top = _exact(document, ("format", "version", "workspaces", "inputs"))
        if top["format"] != REGISTRY_FORMAT or type(top["version"]) is not int or top["version"] != REGISTRY_VERSION:
            raise InvalidDatabase("This is not a supported AIK workspace registry.")
        if type(top["workspaces"]) is not list or type(top["inputs"]) is not list:
            raise InvalidDatabase("The workspace registry lists are malformed.")
        state = RegistryState()
        for item in top["workspaces"]:
            record = WorkspaceRecord(**_exact(item, _WORKSPACE_KEYS))
            if record.id in state.workspaces:
                raise InvalidDatabase("The workspace registry repeats a workspace.")
            state.workspaces[record.id] = record
        for item in top["inputs"]:
            record = InputRecord(**_exact(item, _INPUT_KEYS))
            key = (record.workspace_id, record.path)
            if record.workspace_id not in state.workspaces or key in state.inputs:
                raise InvalidDatabase("The workspace registry has inconsistent inputs.")
            state.inputs[key] = record
        return state
    except InvalidDatabase:
        raise
    except (InvalidRecord, WorkspaceError, TypeError, ValueError):
        raise InvalidDatabase("The workspace registry violates its contract.") from None


class WorkspaceRegistry:
    def __init__(self, path: Path | str | None = None, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidRecord("Registry timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        self.path = validate_database_path(default_registry_path() if path is None else path, create_parent=True)
        self.read()  # Validate an existing file now; a damaged file is never overwritten.

    @contextmanager
    def _locked(self):
        with database_lock(self.path, self.timeout):
            yield

    def read(self) -> RegistryState:
        with self._locked():
            return self._load()

    def update(self, mutate):
        """Load, mutate and persist atomically; an exception persists nothing."""
        with self._locked():
            state = self._load()
            result = mutate(state)
            self._store(state)
            return result

    def _load(self) -> RegistryState:
        validate_database_path(self.path)
        if not self.path.exists():
            return RegistryState()
        try:
            if self.path.stat().st_size > MAX_REGISTRY_BYTES:
                raise InvalidDatabase("The workspace registry exceeds its size limit.")
            data = self.path.read_bytes()
        except OSError:
            raise StorageError("The workspace registry could not be read.") from None
        try:
            document = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError):
            raise InvalidDatabase("The workspace registry is damaged.") from None
        return _parse(document)

    def _store(self, state: RegistryState) -> None:
        document = {
            "format": REGISTRY_FORMAT,
            "version": REGISTRY_VERSION,
            "workspaces": [{key: getattr(record, key) for key in _WORKSPACE_KEYS}
                           for _, record in sorted(state.workspaces.items())],
            "inputs": [{key: getattr(record, key) for key in _INPUT_KEYS}
                       for _, record in sorted(state.inputs.items())],
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".aik-workspaces-", suffix=".tmp", dir=self.path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            validate_database_path(self.path)
            os.replace(temporary, self.path)
            temporary = None
            _sync_directory(self.path.parent)
        except OSError:
            raise StorageError("The workspace registry could not be written safely.") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
