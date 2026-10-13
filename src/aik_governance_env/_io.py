"""Strict JSON parsing and bounded, non-link file reads."""

from __future__ import annotations

import json
import os
import stat

from .errors import GovernanceEnvError


class _DuplicateKey(ValueError):
    pass


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name):
    raise ValueError("non-finite constant")


def loads_strict(raw) -> object:
    """Parse UTF-8 JSON, rejecting duplicate keys and NaN/Infinity. Raises ValueError."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("bytes required")
    try:
        text = bytes(raw).decode("utf-8")
        return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("too deep") from None


def dumps_document(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read_regular_file(path, *, limit: int, code: str, check: str | None = None) -> bytes:
    """Read a regular non-link file of at most ``limit`` bytes, else fail with ``code``."""
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise GovernanceEnvError(code, check=check)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise GovernanceEnvError(code, check=check)
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not os.path.samestat(info, os.fstat(stream.fileno())):
                raise GovernanceEnvError(code, check=check)
            data = stream.read(limit + 1)
    except OSError:
        raise GovernanceEnvError(code, check=check) from None
    except (TypeError, ValueError) as error:
        if isinstance(error, GovernanceEnvError):
            raise
        raise GovernanceEnvError(code, check=check) from None
    if len(data) > limit:
        raise GovernanceEnvError(code, check=check)
    return data
