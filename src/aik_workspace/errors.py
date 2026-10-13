"""Workspace errors carry classifications and fixed status names, never secrets.

Exception messages are fixed text without filesystem paths. The only exception
is ``InputNotCurrent.details``: it lists ``(workspace-relative input path,
condition)`` pairs. Absolute roots are not included there either.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class WorkspaceStatus(StrEnum):
    READY = "ready"
    ARCHIVED = "archived"
    ROOT_MISSING = "root_missing"
    ROOT_REPLACED = "root_replaced"
    ROOT_UNSAFE = "root_unsafe"
    ACCESS_DENIED = "access_denied"
    UNSUPPORTED_HOST = "unsupported_host"
    VM_PROJECT_MISSING = "vm_project_missing"
    VM_PROJECT_MISMATCH = "vm_project_mismatch"
    VM_PROJECT_UNKNOWN = "vm_project_unknown"


# The concrete user action for each non-executable state (re-select / re-bind).
REQUIRED_ACTION = MappingProxyType({
    WorkspaceStatus.READY: "none",
    WorkspaceStatus.ARCHIVED: "restore_workspace",
    WorkspaceStatus.ROOT_MISSING: "reselect_folder",
    WorkspaceStatus.ROOT_REPLACED: "confirm_rebind_root",
    WorkspaceStatus.ROOT_UNSAFE: "reselect_folder",
    WorkspaceStatus.ACCESS_DENIED: "restore_access_or_reselect",
    WorkspaceStatus.UNSUPPORTED_HOST: "not_executable",
    WorkspaceStatus.VM_PROJECT_MISSING: "rebind_vm_project",
    WorkspaceStatus.VM_PROJECT_MISMATCH: "rebind_vm_project",
    WorkspaceStatus.VM_PROJECT_UNKNOWN: "retry_vm_query",
})


class InputCondition(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    UNSAFE = "unsafe"
    UNBOUND = "unbound"


class WorkspaceError(Exception):
    """A workspace operation could not be completed."""


class InvalidWorkspaceRoot(WorkspaceError, ValueError):
    """The path is not an acceptable canonical workspace root."""


class RootUnavailable(InvalidWorkspaceRoot):
    """The folder is missing or cannot be inspected."""


class RootConflict(WorkspaceError):
    """The root duplicates or nests another active workspace root."""


class WorkspaceNotFound(WorkspaceError):
    """The workspace is not registered."""


class WorkspaceUnavailable(WorkspaceError):
    """The workspace is registered but is not executable right now (fail-closed)."""

    def __init__(self, status, message: str | None = None):
        self.status = WorkspaceStatus(status)
        self.action = REQUIRED_ACTION[self.status]
        super().__init__(message or f"The workspace is not executable: {self.status.value}.")


class VmProjectUnavailable(WorkspaceError):
    """The VM project could not be created or its state could not be confirmed."""


class SettingsNotConnected(WorkspaceError):
    """Selection needs a SettingsStore that uses this manager as its binding authority."""


class InputError(WorkspaceError):
    """An input or predecessor artifact cannot be used."""


class InvalidInputPath(InputError, ValueError):
    """An input path is not a safe path relative to the workspace root."""


class InputNotCurrent(InputError):
    """An input is stale, missing, unsafe or unbound.

    ``details`` lists ``(workspace-relative path, condition)`` pairs; the
    message itself contains no path.
    """

    def __init__(self, message: str, details=()):
        self.details = tuple(details)
        super().__init__(message)


class InputTampered(InputError):
    """The bytes read do not match the bound digest."""


class InputBindingMismatch(InputError):
    """A manifest or task binding does not belong to the current workspace binding."""
