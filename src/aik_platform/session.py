"""Session-lock port: whether the interactive desktop session is locked.

``session_locked()`` returns True/False, or raises ``PlatformUnavailable``
when the platform cannot answer. Callers must treat a raise as locked
(fail-closed). No adapter offers a lock ``generation``: a lock that starts and
ends between two polls is not observed.

- Windows: the input desktop cannot be switched to while the session is locked.
- Linux: systemd-logind ``LockedHint`` of ``$XDG_SESSION_ID`` via ``loginctl``,
  with an absolute timeout and no shell. No session ID, no ``loginctl``, or an
  unexpected answer is unavailable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

from ._os import is_windows
from .errors import PlatformUnavailable

_SESSION_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_TIMEOUT_SECONDS = 2.0


def session_locked(*, environ=None, run=None, which=None) -> bool:
    if is_windows():
        return _windows_locked()
    if sys.platform.startswith("linux"):
        return _logind_locked(os.environ if environ is None else environ, run or subprocess.run,
                              which or shutil.which)
    raise PlatformUnavailable("SESSION_SIGNAL_UNAVAILABLE")


def _windows_locked() -> bool:
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


def _logind_locked(environ, run, which) -> bool:
    session = environ.get("XDG_SESSION_ID", "")
    if not _SESSION_ID.fullmatch(session):
        raise PlatformUnavailable("SESSION_SIGNAL_UNAVAILABLE")
    executable = which("loginctl")
    if not executable or not os.path.isabs(executable):
        raise PlatformUnavailable("SESSION_SIGNAL_UNAVAILABLE")
    try:
        completed = run([executable, "show-session", session, "--property=LockedHint", "--value"],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=_TIMEOUT_SECONDS,
                        check=False, shell=False)
    except (OSError, subprocess.SubprocessError):
        raise PlatformUnavailable("SESSION_SIGNAL_UNAVAILABLE") from None
    answer = completed.stdout.strip() if completed.returncode == 0 else b""
    if answer == b"yes":
        return True
    if answer == b"no":
        return False
    raise PlatformUnavailable("SESSION_SIGNAL_UNAVAILABLE")
