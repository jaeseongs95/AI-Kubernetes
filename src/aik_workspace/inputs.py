"""Relative input paths and verified reads inside a canonical workspace root."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from aik_runtime.errors import UnsafeStoragePath
# Reused internals of the repository path rules (symlink/reparse/hardlink and
# Windows device-name checks); not duplicated here.
from aik_runtime.paths import _WINDOWS_DEVICE, _inspect

from .errors import InputCondition, InvalidInputPath


MAX_INPUT_PATH_LENGTH = 1024
_FORBIDDEN = frozenset('<>:"|?*\\')


def normalize_input_path(path: object) -> str:
    """Accept only a portable POSIX-style path relative to the workspace root."""
    if type(path) is not str or not 1 <= len(path) <= MAX_INPUT_PATH_LENGTH:
        raise InvalidInputPath("An input path must be a bounded relative string.")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise InvalidInputPath("An input path cannot contain control characters.")
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
        raise InvalidInputPath("Input paths must be relative to the workspace root.")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise InvalidInputPath("Input paths cannot contain empty or traversal segments.")
        if any(char in _FORBIDDEN for char in part) or part.endswith((" ", ".")):
            raise InvalidInputPath("The input path contains a nonportable filename.")
        if part.casefold() == ".git":
            raise InvalidInputPath("Git metadata is not a workspace input.")
        if _WINDOWS_DEVICE.fullmatch(part):
            raise InvalidInputPath("Windows device names are not workspace inputs.")
    return path


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_verified(root: Path, relative: str, max_bytes: int) -> tuple[InputCondition, bytes | None]:
    """Read one regular file below ``root`` without following links or aliases.

    The resolved path, relative to the resolved root, must equal the requested
    components exactly. A case variant or a Windows 8.3 short name (for example
    an alias of ``.git``) therefore reads as ``unsafe`` instead of creating a
    second binding key for the same file.
    """
    parts = relative.split("/")
    target = root.joinpath(*parts)
    current = root
    try:
        for part in parts:
            current = current / part
            _inspect(current)
        info = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return InputCondition.MISSING, None
    except (UnsafeStoragePath, OSError):
        return InputCondition.UNSAFE, None
    if not stat.S_ISREG(info.st_mode):
        return InputCondition.UNSAFE, None
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return InputCondition.MISSING, None
    except OSError:
        return InputCondition.UNSAFE, None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or not os.path.samestat(opened, info) or opened.st_size > max_bytes):
                return InputCondition.UNSAFE, None
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            return InputCondition.UNSAFE, None
        resolved = target.resolve(strict=True)
        base = root.resolve(strict=True)
        try:
            observed = resolved.relative_to(base).parts
        except ValueError:
            return InputCondition.UNSAFE, None
        if observed != tuple(parts):
            return InputCondition.UNSAFE, None
    except OSError:
        return InputCondition.UNSAFE, None
    return InputCondition.CURRENT, data
