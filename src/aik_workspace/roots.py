"""Canonical workspace roots: absolute, link-free, outside Git internals and dev ledgers."""

from __future__ import annotations

import errno
import os
from pathlib import Path, PurePath

from aik_runtime.errors import UnsafeStoragePath
# Reused internals of the repository path rules; see docs/workspace.ko.md.
from aik_runtime.paths import _WINDOWS_DEVICE, _inspect, _require_local_drive

from .errors import InvalidWorkspaceRoot, RootUnavailable, WorkspaceError


MAX_ROOT_LENGTH = 4096
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_FOLDER_FORBIDDEN = frozenset('<>:"|?*\\/')


def absolute_path(path: object) -> Path:
    """Syntax checks only. Relative paths are rejected: the process cwd is not an authority."""
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if type(path) is not str:
        raise InvalidWorkspaceRoot("A workspace root must be a path string.")
    if not path or len(path) > MAX_ROOT_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise InvalidWorkspaceRoot("A workspace root must be a bounded printable path.")
    if path.startswith(("\\\\", "//")):
        raise InvalidWorkspaceRoot("Network paths are not workspace roots.")
    target = Path(path)
    if not target.is_absolute():
        raise InvalidWorkspaceRoot("A workspace root must be absolute.")
    if ".." in target.parts:
        raise InvalidWorkspaceRoot("A workspace root cannot contain parent traversal.")
    for part in target.parts[1:]:
        if part.casefold() == ".git":
            raise InvalidWorkspaceRoot("Git metadata directories are not workspace roots.")
        if _WINDOWS_DEVICE.fullmatch(part):
            raise InvalidWorkspaceRoot("Windows device names are not workspace roots.")
    return target


def check_new_folder_name(name: object) -> None:
    if (type(name) is not str or name in ("", ".", "..") or name.endswith((" ", "."))
            or any(ord(char) < 32 or char in _FOLDER_FORBIDDEN for char in name)
            or name.casefold() == ".git" or _WINDOWS_DEVICE.fullmatch(name)):
        raise InvalidWorkspaceRoot("The new workspace folder name is not portable.")


def _check_components(path: Path) -> None:
    for component in [*reversed(path.parents), path]:
        try:
            _inspect(component)
        except UnsafeStoragePath:
            raise InvalidWorkspaceRoot("Workspace roots must not use symlinks, reparse points or special files.") from None


def safe_directory(path: object) -> Path:
    """An existing directory reached without links; returns its resolved form."""
    target = absolute_path(path)
    try:
        _check_components(target)
        if not target.exists():
            raise RootUnavailable("The workspace folder does not exist.")
        if not target.is_dir():
            raise InvalidWorkspaceRoot("A workspace root must be a directory.")
        resolved = target.resolve(strict=True)
        absolute_path(str(resolved))
        _check_components(resolved)
        return resolved
    except WorkspaceError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError):
        raise RootUnavailable("The workspace folder cannot be inspected.") from None


def is_git_internal(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if candidate.name.casefold() == ".git":
            return True
        if (candidate / "HEAD").is_file() and (candidate / "objects").is_dir() and (
            (candidate / "refs").is_dir() or (candidate / "packed-refs").is_file()
        ):
            return True
    return False


def root_key(root: str) -> tuple:
    parts = PurePath(root).parts
    return tuple(part.casefold() for part in parts) if os.name == "nt" else parts


def overlaps(first: str, second: str) -> bool:
    """True when the roots are equal or one contains the other."""
    a, b = root_key(first), root_key(second)
    length = min(len(a), len(b))
    return a[:length] == b[:length]


def ledger_conflict(path: Path) -> bool:
    ledgers = []
    if (_SOURCE_ROOT / "planning" / "schema.sql").is_file():
        ledgers.append(_SOURCE_ROOT)
    for candidate in (path, *path.parents):
        if (candidate / "planning" / "backlog.sqlite3").exists():
            ledgers.append(candidate)
    return any(overlaps(str(path), str(ledger)) for ledger in ledgers)


def canonical_root(path: object) -> Path:
    """Canonicalize a workspace root or raise; drive roots, Git internals and ledgers fail."""
    resolved = safe_directory(path)
    if resolved.parent == resolved:
        raise InvalidWorkspaceRoot("A filesystem or drive root cannot be a workspace.")
    try:
        if is_git_internal(resolved):
            raise InvalidWorkspaceRoot("Git metadata directories are not workspace roots.")
        if ledger_conflict(resolved):
            raise InvalidWorkspaceRoot("Development ledgers and the AIK source repository are not workspace roots.")
    except WorkspaceError:
        raise
    except OSError:
        raise RootUnavailable("The workspace folder cannot be inspected.") from None
    return resolved


def root_identity(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_dev, info.st_ino


def probe_access(path: Path) -> None:
    """Raise PermissionError when the root cannot be listed, read and written."""
    with os.scandir(path) as entries:
        next(entries, None)
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError(errno.EACCES, "workspace root access denied")


def host_cwd_support(root: Path) -> bool:
    """Whether this host can bind a native/API cwd to the root (no wider isolation implied)."""
    if os.name == "nt":
        try:
            _require_local_drive(root)
        except (UnsafeStoragePath, OSError):
            return False
        return True
    return os.name == "posix"
