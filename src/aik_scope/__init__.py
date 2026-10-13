"""AIK-021 workspace connection scope and safe settings transfer.

A workspace may use only connections that its ``ConnectionScope`` lists
explicitly. A connection owned by another workspace is usable only when the
owner also marks it as shared. When no scope is stored, every connection is
denied (fail-closed).

``SettingsTransfer`` moves settings between workspaces, users or machines. It
reuses ``aik_config.SettingsStore`` export/import, ``aik_connection``
registration and ``aik_profiles`` documents. Exports contain no API key, no
official host token and no credential value. A secret reference that cannot be
resolved on the destination is restored as "reconnection required". A transfer
always requires reauthentication and workspace root rebinding. It never claims
that Goals or evidence were restored; full product state restore belongs to
AIK-058 and its GUI entry point belongs to AIK-088.
"""

from .errors import (
    ConnectionNotInScope, InvalidScope, InvalidTransferDocument, ScopeError, TransferBlocked,
    TransferIncomplete,
)
from .models import (
    DENIAL_REASONS, FULL_STATE_RESTORE_TASK, GUI_ENTRY_TASK, MAX_SCOPE_CONNECTIONS, PENDING_KINDS,
    ConnectionScope, PendingRecord, ReconnectReport, ScopeDecision, ScopeViolation, TransferReport,
)
from .store import SCOPES_APPLICATION_ID, ScopeStore
from .transfer import TRANSFER_EXCLUDED, TRANSFER_FORMAT, TRANSFER_VERSION, SettingsTransfer

__all__ = [
    "ScopeStore", "SCOPES_APPLICATION_ID", "SettingsTransfer",
    "TRANSFER_FORMAT", "TRANSFER_VERSION", "TRANSFER_EXCLUDED",
    "ConnectionScope", "ScopeDecision", "ScopeViolation", "PendingRecord", "TransferReport", "ReconnectReport",
    "DENIAL_REASONS", "PENDING_KINDS", "MAX_SCOPE_CONNECTIONS", "FULL_STATE_RESTORE_TASK", "GUI_ENTRY_TASK",
    "ScopeError", "InvalidScope", "ConnectionNotInScope", "InvalidTransferDocument",
    "TransferBlocked", "TransferIncomplete",
]
