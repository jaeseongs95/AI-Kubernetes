"""Injectable lock signal sources. A source is a callable returning True when locked.

A source may also expose an integer ``generation`` attribute that increases on
every lock. AuthLifecycle compares it between polls, so a lock that was set and
released between two polls still invalidates earlier validation.
"""

from __future__ import annotations

from threading import Lock

from aik_platform import PlatformError, is_windows
from aik_platform.session import session_locked

from .models import AuthLifecycleError


class ManualLockSignal:
    """A user-controlled switch, for example the application's "lock vault" setting."""

    def __init__(self, locked: bool = False):
        if type(locked) is not bool:
            raise AuthLifecycleError("INVALID_LOCK_SIGNAL")
        self._locked = locked
        self._generation = 1 if locked else 0
        self._guard = Lock()

    def lock(self) -> None:
        with self._guard:
            self._locked = True
            self._generation += 1  # Monotonic: records that a lock happened, even if released later.

    def unlock(self) -> None:
        with self._guard:
            self._locked = False

    @property
    def generation(self) -> int:
        with self._guard:
            return self._generation

    def __call__(self) -> bool:
        with self._guard:
            return self._locked


def os_session_locked() -> bool:
    """The operating system's session-lock signal (``aik_platform.session``).

    Windows uses the input-desktop heuristic and Linux the systemd-logind
    ``LockedHint``. Where the platform cannot answer it raises, and
    AuthLifecycle treats a raising source as locked (fail-closed). This source
    has no ``generation``: a lock that starts and ends between two polls is not
    observed. Neither adapter has run on a real locked desktop session (NOT_RUN).
    """
    try:
        return session_locked()
    except PlatformError:
        raise AuthLifecycleError("SESSION_SIGNAL_UNAVAILABLE") from None


def windows_session_locked() -> bool:
    """Backward-compatible name: the Windows adapter only; raises elsewhere."""
    if not is_windows():
        raise AuthLifecycleError("SESSION_SIGNAL_UNAVAILABLE")
    return os_session_locked()
