"""AIK-016 authentication lifecycle: expiry, key rotation, locks and forced logout.

The state machine reuses the AIK-010 connection errors, the AIK-009 vault
reference rules, the AIK-011 provider contracts and the AIK-019 pool
registry. It stores references, IDs and revisions only. It never stores a
secret value, contacts a provider by itself, or decides completion or refunds.
"""

from .lifecycle import AuthLifecycle
from .models import (
    LOCK_SESSION, LOCK_VAULT, AuthEvent, AuthEventKind, AuthLifecycleError, AuthSnapshot,
    AuthState, LockPolicy, RunRecord, RunStatus, RunTicket,
)
from .signals import ManualLockSignal, windows_session_locked

__all__ = [
    "AuthLifecycle", "AuthState", "AuthEventKind", "AuthEvent", "AuthSnapshot",
    "RunStatus", "RunTicket", "RunRecord", "LockPolicy", "AuthLifecycleError",
    "LOCK_VAULT", "LOCK_SESSION", "ManualLockSignal", "windows_session_locked",
]
