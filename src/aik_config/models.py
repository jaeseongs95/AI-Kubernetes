"""Typed AIK settings records. No record has a field for secret material."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Generic, Protocol, TypeVar
import unicodedata

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier

from .errors import InvalidSetting


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_WINDOWS_ROOT = re.compile(r"[A-Za-z]:[\\/]")
MAX_ROOT_LENGTH = 4096
MAX_NAME_LENGTH = 128

AUTH_MODES = ("none", "api_key", "host_session")
PROFILE_ROLES = ("director", "worker")
BUDGET_UNITS = ("usd_cents", "tokens")
BUDGET_PERIODS = ("daily", "monthly")
PERMISSION_DECISIONS = ("deny", "ask", "allow")


def check_identifier(value: object, field: str = "id") -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise InvalidSetting(f"Invalid identifier field: {field}.") from None


def check_positive_integer(value: object, field: str) -> None:
    # type() excludes bool, float and numeric strings; zero and negatives fail.
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise InvalidSetting(f"Invalid positive integer field: {field}.")


def _choice(value: object, field: str, choices: tuple) -> None:
    if type(value) is not str or value not in choices:
        raise InvalidSetting(f"Unknown value for field: {field}.")


def _display_name(value: object) -> None:
    if (
        type(value) is not str or not 1 <= len(value) <= MAX_NAME_LENGTH or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise InvalidSetting("A workspace name must be 1-128 printable characters without edge spaces.")


def check_canonical_root(value: object) -> None:
    """Syntax only: canonicalization and existence belong to AIK-084."""
    if (
        type(value) is not str or not 1 <= len(value) <= MAX_ROOT_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidSetting("A canonical root must be a bounded path string.")
    if value.startswith(("\\\\", "//")):
        raise InvalidSetting("A canonical root must not be a network path.")
    if not (_WINDOWS_ROOT.match(value) or value.startswith("/")):
        raise InvalidSetting("A canonical root must be absolute.")
    if any(part in (".", "..") for part in re.split(r"[\\/]", value)):
        raise InvalidSetting("A canonical root cannot contain traversal segments.")


@dataclass(frozen=True, slots=True)
class Workspace:
    """Workspace identity. The root/VM project pair is an AIK-084 binding."""

    id: str
    name: str
    canonical_root: str | None = None
    vm_project_id: str | None = None

    def __post_init__(self) -> None:
        check_identifier(self.id)
        _display_name(self.name)
        if (self.canonical_root is None) != (self.vm_project_id is None):
            raise InvalidSetting("A workspace binding requires both canonical root and VM project.")
        if self.canonical_root is not None:
            check_canonical_root(self.canonical_root)
            check_identifier(self.vm_project_id, "vm_project_id")

    @property
    def bound(self) -> bool:
        return self.canonical_root is not None


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """Minimal connection reference for validation; full CRUD is AIK-010."""

    id: str
    workspace_id: str
    provider: str
    auth_mode: str = "none"
    needs_authentication: bool = False

    def __post_init__(self) -> None:
        check_identifier(self.id)
        check_identifier(self.workspace_id, "workspace_id")
        check_identifier(self.provider, "provider")
        _choice(self.auth_mode, "auth_mode", AUTH_MODES)
        if type(self.needs_authentication) is not bool:
            raise InvalidSetting("needs_authentication must be a boolean.")
        if self.auth_mode == "none" and self.needs_authentication:
            raise InvalidSetting("A connection without authentication cannot require authentication.")


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """A director or worker profile bound to a connection in the same workspace."""

    id: str
    workspace_id: str
    role: str
    connection_id: str
    model: str
    effort: str | None = None

    def __post_init__(self) -> None:
        check_identifier(self.id)
        check_identifier(self.workspace_id, "workspace_id")
        _choice(self.role, "role", PROFILE_ROLES)
        check_identifier(self.connection_id, "connection_id")
        if type(self.model) is not str or _MODEL.fullmatch(self.model) is None:
            raise InvalidSetting("A model must be a bounded model identifier.")
        if self.effort is not None:
            check_identifier(self.effort, "effort")


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    """Stored concurrency/budget values only; admission is not performed here."""

    workspace_id: str
    max_parallel_workers: int = 1
    budget_limit: int | None = None
    budget_unit: str = "usd_cents"
    budget_period: str = "monthly"

    def __post_init__(self) -> None:
        check_identifier(self.workspace_id, "workspace_id")
        check_positive_integer(self.max_parallel_workers, "max_parallel_workers")
        if self.budget_limit is not None:
            check_positive_integer(self.budget_limit, "budget_limit")
        _choice(self.budget_unit, "budget_unit", BUDGET_UNITS)
        _choice(self.budget_period, "budget_period", BUDGET_PERIODS)


@dataclass(frozen=True, slots=True)
class PermissionDefaults:
    """Default decisions for a workspace; enforcement belongs to other tasks."""

    workspace_id: str
    file_write: str = "ask"
    shell_command: str = "ask"
    network: str = "deny"

    def __post_init__(self) -> None:
        check_identifier(self.workspace_id, "workspace_id")
        for field in ("file_write", "shell_command", "network"):
            _choice(getattr(self, field), field, PERMISSION_DECISIONS)


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """What the AIK-084 authority reports as the bound root and VM project."""

    workspace_id: str
    canonical_root: str
    vm_project_id: str

    def __post_init__(self) -> None:
        check_identifier(self.workspace_id, "workspace_id")
        check_canonical_root(self.canonical_root)
        check_identifier(self.vm_project_id, "vm_project_id")


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """The only execution root derived from the selected, verified workspace."""

    workspace_id: str
    canonical_root: str
    vm_project_id: str


class WorkspaceBindingAuthority(Protocol):
    """Interface that AIK-084 implements; it is not implemented in this package."""

    def binding_for(self, workspace_id: str) -> WorkspaceBinding | None: ...


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Revisioned(Generic[T]):
    value: T
    revision: int


@dataclass(frozen=True, slots=True)
class WorkspaceSettings:
    """UI view of one workspace. Revision 0 marks unstored defaults."""

    workspace: Revisioned
    connections: tuple
    directors: tuple
    workers: tuple
    limits: Revisioned
    permissions: Revisioned


@dataclass(frozen=True, slots=True)
class ImportReport:
    workspace_ids: tuple
    connections_requiring_authentication: tuple
    workspaces_requiring_binding: tuple
