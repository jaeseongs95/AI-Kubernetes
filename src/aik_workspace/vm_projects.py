"""VM project creation/lookup port and an adapter over aik_vm_bridge.

The declared EngineApplication surface has ``status(project_id)`` but no
project creation command. Lookup therefore goes through ``VmBridge.status``;
creation requires an explicitly injected seam and otherwise fails closed.

The upstream (32bb0f9) status reports the project as ``status["project"]``
with ``id`` and ``root``. ``aik_vm_bridge.EngineApplicationPort`` lifts these
to ``project_id``/``project_root``. This port reads either form; when both are
present they must agree. A missing root or a different project is
unconfirmed, never executable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
import uuid

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier
from aik_vm_bridge import BridgeCode, ProjectQuery, VmBridge, VmBridgeError

from .errors import VmProjectUnavailable


# Codes for an absent project. Upstream raises EngineLedgerError without a code;
# EngineApplicationPort classifies only the exact observed "project not found"
# message as PROJECT_NOT_FOUND. The other codes remain for other adapters.
NOT_FOUND_CODES = frozenset({"PROJECT_NOT_FOUND", "NOT_FOUND", "UNKNOWN_PROJECT"})


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    """The VM-owned project as observed; ``root`` is the VM ProjectMap root.

    ``root=None`` means the root was not reported. The manager treats that as
    an unconfirmed binding (``vm_project_unknown``), never as executable.
    """

    project_id: str
    root: str | None = None

    def __post_init__(self) -> None:
        identifier(self.project_id, "project_id")
        if self.root is not None and (type(self.root) is not str or not 1 <= len(self.root) <= 4096):
            raise InvalidRecord("Invalid VM project root.")


class VmProjectPort(Protocol):
    def create_project(self, workspace_id: str, canonical_root: str, name: str) -> str: ...
    def lookup_project(self, project_id: str) -> ProjectInfo | None: ...


def _merge(top: Any, nested: Any) -> Any:
    if top is None:
        return nested
    if nested is not None and nested != top:
        raise VmProjectUnavailable("The VM project response is inconsistent.")
    return top


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
        try:
            payload = receipt.payload
            reported_id = payload.get("project_id")
            root = payload.get("project_root")
            project = payload.get("project")
            if isinstance(project, Mapping):
                reported_id = _merge(reported_id, project.get("id"))
                root = _merge(root, project.get("root"))
        except (AttributeError, TypeError):
            raise VmProjectUnavailable("The VM project response is malformed.") from None
        if reported_id is not None and reported_id != project_id:
            raise VmProjectUnavailable("The VM project response refers to another project.")
        if root is None:
            raise VmProjectUnavailable("The VM project response did not report its root.")
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
