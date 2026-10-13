"""Records for the auth lifecycle. No field holds secret material."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aik_provider.contracts import ConnectionBinding


LOCK_VAULT = "vault"
LOCK_SESSION = "windows_session"


class AuthLifecycleError(ValueError):
    """Fixed code-only failure; never contains provider text or secret values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AuthState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    LOCKED = "locked"
    REVOKED = "revoked"
    ROTATING = "rotating"
    UNKNOWN = "unknown"


class AuthEventKind(StrEnum):
    REGISTERED = "registered"
    REVALIDATED = "revalidated"
    REVALIDATION_FAILED = "revalidation_failed"
    EXPIRED = "expired"
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    ROTATION_STARTED = "rotation_started"
    ROTATION_CANCELLED = "rotation_cancelled"
    ROTATED = "rotated"
    REAUTHENTICATED = "reauthenticated"
    LOGOUT_FORCED = "logout_forced"
    DELETED = "deleted"
    RECLAIM_REQUIRED = "reclaim_required"
    RUN_STARTED = "run_started"
    RUN_CLOSED = "run_closed"


class RunStatus(StrEnum):
    ACTIVE = "active"
    RECLAIM_REQUIRED = "reclaim_required"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class LockPolicy:
    """User-selected lock policy. Every blocking policy needs an injected signal source."""

    vault_lock_blocks: bool = False
    session_lock_blocks: bool = False
    reclaim_running_on_lock: bool = False

    def __post_init__(self) -> None:
        for name in ("vault_lock_blocks", "session_lock_blocks", "reclaim_running_on_lock"):
            if type(getattr(self, name)) is not bool:
                raise AuthLifecycleError("INVALID_LOCK_POLICY")


@dataclass(frozen=True, slots=True)
class AuthSnapshot:
    connection_id: str
    state: AuthState
    revision: int
    binding_revision: int
    credential_version: int
    credential_ref: str
    lock_reasons: tuple[str, ...]
    expires_at: str | None
    deleted: bool

    @property
    def placeable(self) -> bool:
        return self.state is AuthState.VALID and not self.deleted


@dataclass(frozen=True, slots=True)
class AuthEvent:
    kind: AuthEventKind
    connection_id: str
    revision: int
    state: AuthState
    at: str
    reason: str | None = None
    run_ids: tuple[str, ...] = ()

    @property
    def reauth_required(self) -> bool:
        return self.state in (AuthState.EXPIRED, AuthState.REVOKED)


@dataclass(frozen=True, slots=True)
class RunTicket:
    """The binding and reference version a new run must use."""

    run_id: str
    connection_id: str
    binding: ConnectionBinding
    credential_version: int


@dataclass(frozen=True, slots=True)
class RunRecord:
    """outcome is an observed provider job state or None; never a refund verdict."""

    run_id: str
    connection_id: str
    binding_revision: int
    credential_version: int
    credential_ref: str
    status: RunStatus
    outcome: str | None
    reclaim_reason: str | None
