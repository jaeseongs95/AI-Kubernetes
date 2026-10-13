"""AIK workspace settings: typed records, repository and export/import.

This package stores user-editable settings (workspace identity, connection
references, director/worker profiles, concurrency/budget values and permission
defaults) in a SQLite file separate from the runtime metadata database. It does
not store secret material, authorize execution or perform admission.
"""

from aik_runtime.errors import (
    InvalidDatabase, RevisionConflict, StorageBusy, StorageError, UnsafeStoragePath,
)

from .errors import (
    CrossWorkspaceReference, InvalidReference, InvalidSetting, InvalidSettingsDocument,
    NoActiveWorkspace, SettingsError, WorkspaceBindingError, WorkspaceBindingMismatch,
    WorkspaceBindingRequired,
)
from .models import (
    AgentProfile, ConnectionProfile, ExecutionTarget, ImportReport, PermissionDefaults,
    Revisioned, Workspace, WorkspaceBinding, WorkspaceBindingAuthority, WorkspaceLimits,
    WorkspaceSettings,
)
from .storage import EXPORT_EXCLUDED, EXPORT_FORMAT, SettingsStore, SettingsTransaction, default_settings_path

__all__ = [
    "SettingsStore", "SettingsTransaction", "default_settings_path", "EXPORT_FORMAT", "EXPORT_EXCLUDED",
    "Workspace", "ConnectionProfile", "AgentProfile", "WorkspaceLimits", "PermissionDefaults",
    "Revisioned", "WorkspaceSettings", "WorkspaceBinding", "WorkspaceBindingAuthority",
    "ExecutionTarget", "ImportReport",
    "SettingsError", "InvalidSetting", "InvalidReference", "CrossWorkspaceReference",
    "InvalidSettingsDocument", "WorkspaceBindingError", "WorkspaceBindingRequired",
    "WorkspaceBindingMismatch", "NoActiveWorkspace",
    "StorageError", "RevisionConflict", "InvalidDatabase", "StorageBusy", "UnsafeStoragePath",
]
