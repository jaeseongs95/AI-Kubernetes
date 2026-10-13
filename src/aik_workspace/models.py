"""Immutable AIK-084 records. No field holds secrets, Task bodies or verdicts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from aik_config.models import Workspace
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier
from aik_vm_bridge.binding import DIGEST_PATTERN

from .errors import InputCondition, InvalidInputPath
from .inputs import normalize_input_path


MANIFEST_FORMAT = "aik.workspace-input-manifest"
MANIFEST_VERSION = 1
INPUT_MODE_SHARED_ROOT = "shared_root"
ROOT_REF_PREFIX = "aik-workspace://ws/"
WORKSPACE_STATES = ("active", "archived")


def _integer(value: object, field: str, minimum: int) -> None:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise InvalidRecord(f"Invalid integer field: {field}.")


def _digest(value: object, field: str) -> None:
    if type(value) is not str or DIGEST_PATTERN.fullmatch(value) is None:
        raise InvalidRecord(f"Invalid digest field: {field}.")


def _optional_identifier(value: object, field: str) -> None:
    if value is not None:
        identifier(value, field)


def _input_path(value: object) -> None:
    try:
        if normalize_input_path(value) != value:
            raise InvalidRecord("Invalid input path.")
    except InvalidInputPath:
        raise InvalidRecord("Invalid input path.") from None


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """A registered workspace: canonical root, its filesystem identity and VM project."""

    id: str
    name: str
    canonical_root: str
    root_device: int
    root_inode: int
    vm_project_id: str
    state: str = "active"
    revision: int = 1

    def __post_init__(self) -> None:
        # Reuse the settings contract for ID, display name, root syntax and VM project ID.
        Workspace(self.id, self.name, self.canonical_root, self.vm_project_id)
        _integer(self.root_device, "root_device", 0)
        _integer(self.root_inode, "root_inode", 0)
        if type(self.state) is not str or self.state not in WORKSPACE_STATES:
            raise InvalidRecord("Unknown workspace lifecycle state.")
        _integer(self.revision, "revision", 1)

    @property
    def archived(self) -> bool:
        return self.state == "archived"


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """The verified execution root; native/API launches must use ``cwd``."""

    workspace_id: str
    canonical_root: str
    vm_project_id: str
    workspace_revision: int
    workspace_root_ref: str

    @property
    def cwd(self) -> Path:
        return Path(self.canonical_root)

    def subprocess_kwargs(self) -> dict:
        return {"cwd": self.canonical_root}


@dataclass(frozen=True, slots=True)
class InputRecord:
    """A bound input or predecessor artifact, identified by its bytes digest."""

    workspace_id: str
    path: str
    digest: str
    size: int
    revision: int = 1
    producer_task_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.workspace_id, "workspace_id")
        _input_path(self.path)
        _digest(self.digest, "digest")
        _integer(self.size, "size", 0)
        _integer(self.revision, "revision", 1)
        _optional_identifier(self.producer_task_id, "producer_task_id")


@dataclass(frozen=True, slots=True)
class InputState:
    path: str
    condition: InputCondition
    recorded_digest: str
    observed_digest: str | None


@dataclass(frozen=True, slots=True)
class InputEntry:
    path: str
    digest: str
    size: int
    revision: int
    producer_task_id: str | None = None

    def __post_init__(self) -> None:
        _input_path(self.path)
        _digest(self.digest, "digest")
        _integer(self.size, "size", 0)
        _integer(self.revision, "revision", 1)
        _optional_identifier(self.producer_task_id, "producer_task_id")

    def to_mapping(self) -> dict:
        return {"path": self.path, "digest": self.digest, "size": self.size, "revision": self.revision,
                "producer_task_id": self.producer_task_id}


@dataclass(frozen=True, slots=True)
class InputManifest:
    """Inputs prepared for one workspace binding; its digest feeds input_manifest_digest."""

    workspace_id: str
    vm_project_id: str
    workspace_revision: int
    workspace_root_ref: str
    mode: str
    entries: tuple

    def __post_init__(self) -> None:
        identifier(self.workspace_id, "workspace_id")
        identifier(self.vm_project_id, "vm_project_id")
        _integer(self.workspace_revision, "workspace_revision", 1)
        if type(self.workspace_root_ref) is not str or not self.workspace_root_ref.startswith(ROOT_REF_PREFIX):
            raise InvalidRecord("Invalid workspace root reference.")
        if self.mode != INPUT_MODE_SHARED_ROOT:
            raise InvalidRecord("Unsupported input preparation mode.")
        if type(self.entries) is not tuple or any(type(entry) is not InputEntry for entry in self.entries):
            raise InvalidRecord("Manifest entries must be a tuple of input entries.")
        paths = [entry.path for entry in self.entries]
        if paths != sorted(set(paths)):
            raise InvalidRecord("Manifest entries must be unique and ordered.")

    def to_mapping(self) -> dict:
        return {
            "format": MANIFEST_FORMAT, "version": MANIFEST_VERSION,
            "workspace_id": self.workspace_id, "vm_project_id": self.vm_project_id,
            "workspace_revision": self.workspace_revision, "workspace_root_ref": self.workspace_root_ref,
            "mode": self.mode, "entries": [entry.to_mapping() for entry in self.entries],
        }

    def digest(self) -> str:
        text = json.dumps(self.to_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()
