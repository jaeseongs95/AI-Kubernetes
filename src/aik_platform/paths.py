"""Per-user product state locations.

Windows: ``%LOCALAPPDATA%\\AIK\\<component>\\<name>``.
POSIX: ``$XDG_STATE_HOME/aik/<name>`` (default ``~/.local/state/aik``); the
POSIX layout is flat, as before this port existed. The cwd is never used.
"""

from __future__ import annotations

import os
from pathlib import Path

from ._os import is_windows
from .errors import PlatformUnavailable


def state_root(os_name: str | None = None, environ=None) -> Path:
    env = os.environ if environ is None else environ
    if is_windows(os_name):
        base = env.get("LOCALAPPDATA")
        if not base:
            raise PlatformUnavailable("LOCALAPPDATA_REQUIRED")
        return Path(base) / "AIK"
    base = env.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "aik"


def component_dir(component: str, os_name: str | None = None, environ=None) -> Path:
    """A directory owned by one component (for example ``vault`` or ``supervisor``)."""
    return state_root(os_name, environ) / component


def component_file(component: str, name: str, os_name: str | None = None, environ=None) -> Path:
    """A single state file; Windows groups it under the component directory."""
    root = state_root(os_name, environ)
    return root / component / name if is_windows(os_name) else root / name


def is_local_drive(path: Path) -> bool:
    """False when Windows reports the drive as remote or unknown; POSIX is not checked."""
    if not is_windows():
        return True
    import ctypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_uint
    # A mapped network drive also has a drive letter. String-only UNC
    # rejection would incorrectly treat it as a local SQLite filesystem.
    return function(Path(path).anchor) in (2, 3, 6)  # removable, fixed, RAM disk


def supports_native_cwd() -> bool:
    """Whether this platform has an adapter for binding a native/API working directory."""
    return is_windows() or os.name == "posix"
