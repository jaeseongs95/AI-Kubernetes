"""Runtime locations and a stable process lock shared by all repository calls."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import re
import stat
import time

from aik_platform import PlatformUnavailable, is_link_or_reparse_point, lock_ops
from aik_platform.paths import component_file, is_local_drive

from .errors import StorageBusy, UnsafeStoragePath


_WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\..*)?\Z", re.I)
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_LEDGER_NAMES = {"backlog.sqlite3" + suffix for suffix in (
    "", "-wal", "-shm", "-journal", ".maintenance.lock", ".storage.lock"
)}


def default_database_path() -> Path:
    """The per-user product default (``aik_platform.paths``), never the cwd.

    Windows uses LocalAppData; Linux uses $XDG_STATE_HOME (or ~/.local/state).
    An explicit relative XDG value is rejected by ``validate_database_path``.
    """
    try:
        return component_file("runtime", "runtime.sqlite3")
    except PlatformUnavailable:
        raise UnsafeStoragePath("Windows runtime storage requires LOCALAPPDATA.") from None


def _inspect(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise UnsafeStoragePath("The runtime path cannot be inspected.") from None
    if is_link_or_reparse_point(info):
        raise UnsafeStoragePath("Runtime paths must not use symlinks or reparse points.")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise UnsafeStoragePath("Runtime paths must use regular files and directories.")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise UnsafeStoragePath("Runtime files must not have hardlink aliases.")


def _inside_repository(path: Path) -> bool:
    for parent in path.parents:
        marker = parent / ".git"
        if marker.exists() or marker.is_symlink():
            return True
        if (parent / "HEAD").is_file() and (parent / "objects").is_dir() and (
            (parent / "refs").is_dir() or (parent / "packed-refs").is_file()
        ):
            return True
    # Source archives may not contain .git, but their planning DB is still not
    # an operational data destination.
    return (_SOURCE_ROOT / "planning" / "schema.sql").is_file() and path.is_relative_to(_SOURCE_ROOT)


def _require_local_drive(path: Path) -> None:
    if not is_local_drive(path):
        raise UnsafeStoragePath("Runtime storage requires a known local writable drive.")


def validate_database_path(path: Path | str, *, create_parent: bool = False) -> Path:
    """Reject source roots, aliases and special paths before creating anything."""
    try:
        target = Path(path)
        raw = str(target)
        if not target.is_absolute() or raw.startswith(("\\\\", "//")) or ".." in target.parts:
            raise UnsafeStoragePath("Runtime storage requires an absolute local path without parent traversal.")
        for part in target.parts[1:]:
            if part.casefold() == ".git":
                raise UnsafeStoragePath("Git metadata paths are not runtime storage locations.")
            if any(ord(char) < 32 or char in '<>:"|?*\\' for char in part) or part.endswith((" ", ".")):
                raise UnsafeStoragePath("The runtime path contains a nonportable filename.")
            if _WINDOWS_DEVICE.fullmatch(part):
                raise UnsafeStoragePath("Windows device names are not runtime files.")
        if target.name.casefold() in _LEDGER_NAMES or _inside_repository(target):
            raise UnsafeStoragePath("Runtime data must remain outside development ledgers and Git repositories.")
        _require_local_drive(target)
        for component in [*reversed(target.parents), target]:
            _inspect(component)
        if target.exists() and not target.is_file():
            raise UnsafeStoragePath("The runtime database must be a regular file.")
        canonical = target.resolve(strict=False)
        # Resolve aliases before mkdir as well as after it: a local SUBST
        # drive or short name must not create directories inside a repository.
        if _inside_repository(canonical) or canonical.name.casefold() in _LEDGER_NAMES:
            raise UnsafeStoragePath("The resolved runtime path belongs to a development ledger or repository.")
        _require_local_drive(canonical)
        for component in [*reversed(canonical.parents), canonical]:
            _inspect(component)
        if create_parent:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for component in [*reversed(target.parents), target]:
                _inspect(component)
            if _inside_repository(target):
                raise UnsafeStoragePath("The runtime directory became a source repository.")
        canonical = target.resolve(strict=False)
        # Also inspect the resolved namespace (for example Windows SUBST or
        # short-name aliases), before callers use it as the lock/DB identity.
        if _inside_repository(canonical) or canonical.name.casefold() in _LEDGER_NAMES:
            raise UnsafeStoragePath("The resolved runtime path belongs to a development ledger or repository.")
        _require_local_drive(canonical)
        for component in [*reversed(canonical.parents), canonical]:
            _inspect(component)
        return canonical
    except UnsafeStoragePath:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise UnsafeStoragePath("The runtime database path is invalid or inaccessible.") from None


@contextmanager
def database_lock(path: Path, timeout: float = 5.0):
    """Serialize cooperating processes, including schema checks and migrations.

    Keep the lockfile after release: unlinking it could let two waiters lock
    different inodes. Process termination releases the OS lock automatically.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Storage lock timeout must be a finite nonnegative number.")
    canonical = validate_database_path(path)
    lockfile = canonical.with_name(canonical.name + ".storage.lock")
    validate_database_path(lockfile)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(lockfile, flags, 0o600)
    except OSError:
        raise UnsafeStoragePath("The runtime lockfile could not be opened safely.") from None
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafeStoragePath("The runtime lockfile must be a regular file without aliases.")
        _inspect(lockfile)
        if not os.path.samestat(info, lockfile.stat()):
            raise UnsafeStoragePath("The runtime lockfile changed while opening.")
        # Unbuffered stream: the lock ops' own seek on the descriptor is enough.
        acquire, release = lock_ops(stream.fileno())

        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise StorageBusy("The operating system could not acquire the runtime lock.") from None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StorageBusy("Timed out waiting for another runtime storage operation.") from None
                time.sleep(min(0.02, remaining))
        try:
            validate_database_path(canonical)
            _inspect(lockfile)
            if not os.path.samestat(info, lockfile.stat()):
                raise UnsafeStoragePath("The runtime lockfile changed while waiting.")
            if info.st_size == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            release()
