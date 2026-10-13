"""AIK-083: bounded file snapshots for independent acceptance evidence.

The receipt identifies bytes actually validated, not a later pathname read.
Stat checks detect ordinary edits/replacements; they are not an OS sandbox or
an atomic multi-file transaction against a hostile same-user process.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 10000


class ContractError(ValueError):
    """Only a fixed diagnostic code is exposed to callers."""


def _regular(info) -> bool:
    return stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)


def _stamp(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, getattr(info, "st_file_attributes", 0))


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    data: bytes
    stamp: tuple

    def require_unchanged(self) -> None:
        try:
            current = self.path.lstat()
        except OSError:
            raise ContractError("FILE_CHANGED_DURING_CHECK") from None
        if not _regular(current) or _stamp(current) != self.stamp:
            raise ContractError("FILE_CHANGED_DURING_CHECK")


def read_snapshot(path: Path, *, maximum: int = MAX_BYTES) -> FileSnapshot:
    """Read a regular non-symlink file once, with a size bound before allocation."""
    if type(maximum) is not int or not 1 <= maximum <= MAX_BYTES:
        raise ContractError("INVALID_READ_LIMIT")
    target = Path(path)
    descriptor = None
    try:
        before = target.lstat()
        if not _regular(before):
            raise ContractError("FILE_NOT_REGULAR")
        if before.st_size > maximum:
            raise ContractError("OVERSIZED_JSON")
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if not _regular(opened) or _stamp(opened) != _stamp(before):
            raise ContractError("FILE_CHANGED_DURING_CHECK")
        blocks, total = [], 0
        while True:
            block = os.read(descriptor, min(65536, maximum + 1 - total))
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise ContractError("OVERSIZED_JSON")
            blocks.append(block)
        if total != opened.st_size or _stamp(os.fstat(descriptor)) != _stamp(opened):
            raise ContractError("FILE_CHANGED_DURING_CHECK")
        snapshot = FileSnapshot(target, b"".join(blocks), _stamp(opened))
        snapshot.require_unchanged()
        return snapshot
    except ContractError:
        raise
    except (OSError, ValueError, TypeError):
        raise ContractError("FILE_READ_FAILED") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def parse_json(raw: bytes, *, maximum: int = MAX_BYTES) -> dict:
    """Parse one immutable snapshot; reject ambiguous, non-finite or huge trees."""
    if type(raw) is not bytes:
        raise ContractError("INVALID_JSON")
    if len(raw) > maximum:
        raise ContractError("OVERSIZED_JSON")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ContractError("NONFINITE_JSON")
        return result

    def invalid_constant(_value):
        raise ContractError("NONFINITE_JSON")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_float=finite_float, parse_constant=invalid_constant)
        if type(result) is not dict:
            raise ContractError("JSON_OBJECT_REQUIRED")
        pending, count = [(result, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > MAX_NODES or depth > MAX_DEPTH:
                raise ContractError("JSON_COMPLEXITY_LIMIT")
            if type(item) is dict:
                # Each object key is counted and checked as a JSON string too.
                if count + len(pending) + 2 * len(item) > MAX_NODES:
                    raise ContractError("JSON_COMPLEXITY_LIMIT")
                pending.extend((key, depth + 1) for key in item)
                pending.extend((value, depth + 1) for value in item.values())
            elif type(item) is list:
                if count + len(pending) + len(item) > MAX_NODES:
                    raise ContractError("JSON_COMPLEXITY_LIMIT")
                pending.extend((value, depth + 1) for value in item)
            elif type(item) is str:
                item.encode("utf-8")  # reject escaped lone surrogate code points
        return result
    except ContractError:
        raise
    except RecursionError:
        raise ContractError("JSON_COMPLEXITY_LIMIT") from None
    except (UnicodeError, ValueError, OverflowError):
        raise ContractError("INVALID_JSON") from None


__all__ = ["ContractError", "FileSnapshot", "parse_json", "read_snapshot"]
