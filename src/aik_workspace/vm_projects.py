"""VM project creation/lookup port and an adapter over aik_vm_bridge.

The declared EngineApplication surface has ``status(project_id)`` but no
project creation command. Lookup therefore goes through ``VmBridge.status``;
creation requires an explicitly injected seam and otherwise fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol
import uuid

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier
from aik_vm_bridge import BridgeCode, ProjectQuery, VmBridge, VmBridgeError

from .errors import VmProjectUnavailable


# Assumed VM codes for an absent project; not verified against an installed VM.
NOT_FOUND_CODES = frozenset({"PROJECT_NOT_FOUND", "NOT_FOUND", "UNKNOWN_PROJECT"})


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    """The VM-owned project as observed; ``root`` is the VM ProjectMap root, if reported."""

    project_id: str
    root: str | None = None

    def __post_init__(self) -> None:
        identifier(self.project_id, "project_id")
        if self.root is not None and (type(self.root) is not str or not 1 <= len(self.root) <= 4096):
            raise InvalidRecord("Invalid VM project root.")


class VmProjectPort(Protocol):
    def create_project(self, workspace_id: str, canonical_root: str, name: str) -> str: ...
    def lookup_project(self, project_id: str) -> ProjectInfo | None: ...


class BridgeProjectPort:
    def __init__(self, bridge: VmBridge,
                 create_project: Callable[[str, str, str], str] | None = None):
        if not isinstance(bridge, VmBridge):
            raise TypeError("A VmBridge instance is required.")
        if create_project is not None and not callable(create_project):
            raise TypeError("create_project must be callable.")
        self._bridge = bridge
        self._create = create_project

    def lookup_project(self, project_id: str) -> ProjectInfo | None:
        try:
            identifier(project_id, "project_id")
        except InvalidRecord:
            raise VmProjectUnavailable("The VM project ID is invalid.") from None
        try:
            receipt = self._bridge.status(ProjectQuery(f"aik-ws-{uuid.uuid4().hex}", project_id))
        except VmBridgeError as error:
            if error.code == BridgeCode.VM_REJECTED and error.vm_code in NOT_FOUND_CODES:
                return None
            raise VmProjectUnavailable("The VM project state could not be confirmed.") from None
        root = receipt.payload.get("project_root")
        try:
            return ProjectInfo(project_id, root)
        except InvalidRecord:
            raise VmProjectUnavailable("The VM project response is malformed.") from None

    def create_project(self, workspace_id: str, canonical_root: str, name: str) -> str:
        if self._create is None:
            raise VmProjectUnavailable("No VM project creation seam is available.")
        try:
            project_id = self._create(workspace_id, canonical_root, name)
        except Exception:
            raise VmProjectUnavailable("The VM project could not be created.") from None
        try:
            identifier(project_id, "project_id")
        except InvalidRecord:
            raise VmProjectUnavailable("The VM returned an invalid project ID.") from None
        return project_id
