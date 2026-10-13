"""Scope and transfer records. No field holds secret material."""

from __future__ import annotations

from dataclasses import dataclass

from aik_config.errors import InvalidSetting
from aik_config.models import check_identifier

from .errors import InvalidScope


MAX_SCOPE_CONNECTIONS = 1024
DENIAL_REASONS = ("NO_SCOPE", "NOT_ALLOWED", "RECONNECTION_REQUIRED", "UNKNOWN_CONNECTION", "NOT_SHARED")
PENDING_KINDS = ("connection", "agent_profile", "execution_profile")
VIOLATION_KINDS = ("agent_profile", "execution_profile")
FULL_STATE_RESTORE_TASK = "AIK-058"
GUI_ENTRY_TASK = "AIK-088"


def check_scope_identifier(value: object, field: str = "id") -> None:
    try:
        check_identifier(value, field)
    except InvalidSetting:
        raise InvalidScope(f"Invalid identifier field: {field}.") from None


def _id_tuple(value: object, field: str) -> tuple:
    if type(value) is not tuple or len(value) > MAX_SCOPE_CONNECTIONS:
        raise InvalidScope(f"Field {field} must be a bounded tuple of connection IDs.")
    for item in value:
        check_scope_identifier(item, field)
    if len(set(value)) != len(value):
        raise InvalidScope("A connection ID is repeated.")
    return tuple(sorted(value))


@dataclass(frozen=True, slots=True)
class ConnectionScope:
    """Connections a workspace may use, and its own connections it shares.

    ``shared_connection_ids`` is effective only for connections that the
    settings store reports as owned by this workspace.
    """

    workspace_id: str
    allowed_connection_ids: tuple = ()
    shared_connection_ids: tuple = ()

    def __post_init__(self) -> None:
        check_scope_identifier(self.workspace_id, "workspace_id")
        object.__setattr__(self, "allowed_connection_ids",
                           _id_tuple(self.allowed_connection_ids, "allowed_connection_ids"))
        object.__setattr__(self, "shared_connection_ids",
                           _id_tuple(self.shared_connection_ids, "shared_connection_ids"))


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """An allowed use. ``shared`` is True when another workspace owns the connection."""

    workspace_id: str
    connection_id: str
    owner_workspace_id: str
    shared: bool


@dataclass(frozen=True, slots=True)
class ScopeViolation:
    kind: str
    record_id: str
    connection_id: str
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in VIOLATION_KINDS or self.reason not in DENIAL_REASONS:
            raise InvalidScope("Unknown scope violation classification.")
        check_scope_identifier(self.record_id, "record_id")
        check_scope_identifier(self.connection_id, "connection_id")


@dataclass(frozen=True, slots=True)
class PendingRecord:
    """A restored record waiting for its connection to be reconnected."""

    kind: str
    record_id: str
    workspace_id: str
    requires: tuple

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in PENDING_KINDS:
            raise InvalidScope("Unknown pending record kind.")
        check_scope_identifier(self.record_id, "record_id")
        check_scope_identifier(self.workspace_id, "workspace_id")
        if type(self.requires) is not tuple:
            raise InvalidScope("Pending requirements must be a tuple.")
        for item in self.requires:
            check_scope_identifier(item, "requires")


@dataclass(frozen=True, slots=True)
class ReconnectReport:
    """``conflict_record_ids`` stay pending because another record uses their ID."""

    connection_id: str
    restored_record_ids: tuple
    pending_record_ids: tuple
    conflict_record_ids: tuple = ()

    @property
    def requires_authentication(self) -> bool:
        # A reconnected connection is registered unverified; the official host
        # adapter must still confirm it (aik_connection.mark_verified).
        return True


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """Result of retrying pending profile restoration (SettingsTransfer.restore_pending)."""

    restored_record_ids: tuple
    conflict_record_ids: tuple
    pending_record_ids: tuple


@dataclass(frozen=True, slots=True)
class TransferReport:
    """Result of a settings import. A transfer is never execution-ready."""

    workspace_ids: tuple
    workspaces_requiring_binding: tuple
    connections_requiring_authentication: tuple
    connections_requiring_reconnection: tuple
    pending_profile_ids: tuple
    goals_and_evidence_restored: bool = False
    full_state_restore_task: str = FULL_STATE_RESTORE_TASK
    gui_entry_task: str = GUI_ENTRY_TASK

    def __post_init__(self) -> None:
        if self.goals_and_evidence_restored is not False:
            raise InvalidScope("A settings transfer never restores Goals or evidence.")
        if (self.full_state_restore_task, self.gui_entry_task) != (FULL_STATE_RESTORE_TASK, GUI_ENTRY_TASK):
            raise InvalidScope("Full state restore and its GUI entry belong to other tasks.")

    @property
    def execution_ready(self) -> bool:
        return False

    @property
    def required_actions(self) -> tuple:
        actions = [f"BIND_WORKSPACE_ROOT:{item}" for item in self.workspaces_requiring_binding]
        actions += [f"RECONNECT_CREDENTIAL:{item}" for item in self.connections_requiring_reconnection]
        actions += [f"AUTHENTICATE:{item}" for item in self.connections_requiring_authentication]
        return tuple(actions)
