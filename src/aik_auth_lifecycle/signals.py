"""Injectable lock signal sources. A source is a callable returning True when locked.

A source may also expose an integer ``generation`` attribute that increases on
every lock. AuthLifecycle compares it between polls, so a lock that was set and
released between two polls still invalidates earlier validation.
"""

from __future__ import annotations

import os
from threading import Lock

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


def windows_session_locked() -> bool:
    """Windows-only heuristic: the input desktop cannot be switched to while locked.

    NOT_RUN: this has not been executed on Windows. Outside Windows it raises,
    and AuthLifecycle treats a raising source as locked (fail-closed).
    This source has no ``generation``: a session lock that starts and ends
    between two polls is not observed.
    """
    if os.name != "nt":
        raise AuthLifecycleError("SESSION_SIGNAL_UNAVAILABLE")
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    user32.OpenInputDesktop.restype = ctypes.c_void_p
    user32.SwitchDesktop.argtypes = [ctypes.c_void_p]
    user32.SwitchDesktop.restype = ctypes.c_bool
    user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    user32.CloseDesktop.restype = ctypes.c_bool
    handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not handle:
        return True
    try:
        return not user32.SwitchDesktop(handle)
    finally:
        user32.CloseDesktop(handle)
