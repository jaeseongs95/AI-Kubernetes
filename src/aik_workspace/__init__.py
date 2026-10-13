"""AIK-084: user workspace lifecycle, canonical root / VM project binding and inputs.

The package implements ``aik_config.WorkspaceBindingAuthority``. Settings keep
the selected workspace; this package re-verifies the real root, its identity,
access and the VM project every time a binding is requested. It does not own
Task, Goal or verdict authority and it does not grant OS isolation.
"""

from .errors import (
    REQUIRED_ACTION, InputBindingMismatch, InputCondition, InputError, InputNotCurrent,
    InputTampered, InvalidInputPath, InvalidWorkspaceRoot, RootConflict, RootUnavailable,
    SettingsNotConnected, VmProjectUnavailable, WorkspaceError, WorkspaceNotFound,
    WorkspaceStatus, WorkspaceUnavailable,
)
from .inputs import normalize_input_path, sha256_digest
from .manager import DEFAULT_MAX_INPUT_BYTES, WorkspaceManager
from .models import (
    ExecutionContext, InputEntry, InputManifest, InputRecord, InputState, WorkspaceRecord,
)
from .registry import WorkspaceRegistry, default_registry_path
from .roots import canonical_root, host_cwd_support
from .vm_projects import BridgeProjectPort, ProjectInfo, VmProjectPort

__all__ = [
    "WorkspaceManager", "WorkspaceRegistry", "default_registry_path", "DEFAULT_MAX_INPUT_BYTES",
    "BridgeProjectPort", "VmProjectPort", "ProjectInfo",
    "WorkspaceRecord", "ExecutionContext", "InputRecord", "InputEntry", "InputManifest", "InputState",
    "canonical_root", "host_cwd_support", "normalize_input_path", "sha256_digest",
    "WorkspaceStatus", "InputCondition", "REQUIRED_ACTION",
    "WorkspaceError", "InvalidWorkspaceRoot", "RootUnavailable", "RootConflict", "WorkspaceNotFound",
    "WorkspaceUnavailable", "VmProjectUnavailable", "SettingsNotConnected",
    "InputError", "InvalidInputPath", "InputNotCurrent", "InputTampered", "InputBindingMismatch",
]
