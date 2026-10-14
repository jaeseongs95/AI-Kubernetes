"""Durability and path-inspection port."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat

from ._os import is_windows

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def sync_file(path: Path) -> None:
    """Flush a file's contents to stable storage."""
    with Path(path).open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    """Flush a directory entry after a rename, where the platform allows it.

    Windows does not expose portable directory fsync through Python. File
    contents are flushed there, but rename power-loss durability is OS-owned.
    """
    if is_windows():
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def is_link_or_reparse_point(info: os.stat_result) -> bool:
    """True for a symlink, or a Windows reparse point (junction, mount point)."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def restrict_to_owner(path: Path, *, directory: bool = False) -> None:
    """Limit a file (0600) or directory (0700) to the current user on POSIX.

    Windows relies on the per-user profile's inherited ACL; changing ACLs is
    not part of this port. Raises the ``OSError`` from ``chmod`` unchanged.
    """
    if is_windows():
        return
    os.chmod(path, 0o700 if directory else 0o600)


def paths_case_insensitive() -> bool:
    """Whether path names compare case-insensitively (the NTFS default on Windows)."""
    return is_windows()
