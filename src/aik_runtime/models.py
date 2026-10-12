"""AIK-owned metadata only; VM Task authority and secret bytes stay elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Generic, TypeVar

from .errors import InvalidRecord


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_REFERENCE = re.compile(
    r"(?:secret|host-session)://[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._/-]{0,191}\Z"
)
MAX_INTEGER = 2**63 - 1


def identifier(value: object, field: str = "id") -> None:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise InvalidRecord(f"Invalid identifier field: {field}.")


def positive_integer(value: object, field: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise InvalidRecord(f"Invalid positive integer field: {field}.")


def _optional_identifier(value: object, field: str) -> None:
    if value is not None:
        identifier(value, field)


@dataclass(frozen=True, slots=True)
class CredentialRef:
    """An opaque locator; this record has no field for secret material."""

    id: str
    reference: str

    def __post_init__(self) -> None:
        identifier(self.id)
        if type(self.reference) is not str or _REFERENCE.fullmatch(self.reference) is None:
            raise InvalidRecord("A credential reference must be an opaque secret or host-session locator.")
        if any(part in ("", ".", "..") for part in self.reference.split("://", 1)[1].split("/")):
            raise InvalidRecord("A credential reference cannot contain traversal or empty segments.")


@dataclass(frozen=True, slots=True)
class Pool:
    id: str
    provider: str
    connection_id: str
    credential_ref_id: str | None = None
    max_parallel: int = 1
    enabled: bool = False

    def __post_init__(self) -> None:
        for field in ("id", "provider", "connection_id"):
            identifier(getattr(self, field), field)
        _optional_identifier(self.credential_ref_id, "credential_ref_id")
        positive_integer(self.max_parallel, "max_parallel")
        if type(self.enabled) is not bool:
            raise InvalidRecord("Pool enabled must be a boolean.")


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    id: str
    pool_id: str
    role: str
    model: str
    effort: str | None = None

    def __post_init__(self) -> None:
        identifier(self.id)
        identifier(self.pool_id, "pool_id")
        if type(self.role) is not str or self.role not in ("director", "worker", "reviewer"):
            raise InvalidRecord("Unknown execution profile role.")
        if type(self.model) is not str or _MODEL.fullmatch(self.model) is None:
            raise InvalidRecord("A model must be a bounded model identifier.")
        _optional_identifier(self.effort, "effort")


@dataclass(frozen=True, slots=True)
class Placement:
    """AIK assignment metadata with external VM/AGS IDs, never their verdicts."""

    id: str
    pool_id: str
    profile_id: str
    vm_instance_id: str
    vm_project_id: str
    vm_task_id: str
    vm_attempt_id: str
    ags_session_id: str | None = None
    state: str = "pending"

    def __post_init__(self) -> None:
        for field in ("id", "pool_id", "profile_id", "vm_instance_id", "vm_project_id", "vm_task_id", "vm_attempt_id"):
            identifier(getattr(self, field), field)
        _optional_identifier(self.ags_session_id, "ags_session_id")
        if type(self.state) is not str or self.state not in ("pending", "assigned", "released", "unknown", "failed"):
            raise InvalidRecord("Unknown AIK placement state.")


@dataclass(frozen=True, slots=True)
class Lease:
    """Persisted reservation metadata; this API does not grant execution rights."""

    id: str
    placement_id: str
    pool_id: str
    worker_id: str
    fencing_token: int
    expires_at: str
    state: str = "reserved"
    provider_job_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("id", "placement_id", "pool_id", "worker_id"):
            identifier(getattr(self, field), field)
        positive_integer(self.fencing_token, "fencing_token")
        _optional_identifier(self.provider_job_id, "provider_job_id")
        if type(self.state) is not str or self.state not in (
            "reserved", "active", "draining", "released", "failed", "unknown", "expired"
        ):
            raise InvalidRecord("Unknown AIK lease state.")
        if type(self.expires_at) is not str:
            raise InvalidRecord("Lease expiry requires a timezone-aware timestamp.")
        try:
            stamp = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
            normalized = stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError):
            raise InvalidRecord("Lease expiry requires a timezone-aware timestamp.") from None
        object.__setattr__(self, "expires_at", normalized)


T = TypeVar("T", CredentialRef, Pool, ExecutionProfile, Placement, Lease)


@dataclass(frozen=True, slots=True)
class Versioned(Generic[T]):
    value: T
    revision: int


MODEL_TABLES = {
    CredentialRef: "credential_refs",
    Pool: "pools",
    ExecutionProfile: "execution_profiles",
    Placement: "placements",
    Lease: "leases",
}
