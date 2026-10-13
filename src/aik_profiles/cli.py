"""Validation command: python -m aik_profiles validate FILE [FILE ...].

Each result line is JSON with the file, status and (when invalid) only the
error classification. Field values are never echoed. Exit codes: 0 all valid,
1 at least one invalid, 2 usage error. Only regular files are read, and at
most MAX_DOCUMENT_BYTES + 1 bytes are read from each.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys

from .errors import InvalidProfile
from .models import load_profile_document, parse_json


MAX_DOCUMENT_BYTES = 1 << 20
_CHUNK = 1 << 16


def _read_document(name: str) -> tuple[bytes | None, str | None]:
    """Return (data, None) or (None, classification) without an unbounded or blocking read."""
    try:
        info = os.stat(name)
    except (OSError, ValueError):
        return None, "Unreadable"
    if not stat.S_ISREG(info.st_mode):
        return None, "NotRegularFile"
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        descriptor = os.open(name, flags)
    except (OSError, ValueError):
        return None, "Unreadable"
    chunks = []
    try:
        # The path may have been replaced after stat(); check the opened object.
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, "NotRegularFile"
        remaining = MAX_DOCUMENT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, _CHUNK))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, "Unreadable"
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > MAX_DOCUMENT_BYTES:
        return None, "DocumentTooLarge"
    return data, None


def _validate_file(name: str) -> dict:
    data, error = _read_document(name)
    if error is not None:
        return {"file": name, "status": "invalid", "error": error}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"file": name, "status": "invalid", "error": "InvalidEncoding"}
    try:
        profile = load_profile_document(parse_json(text))
    except InvalidProfile as failure:
        return {"file": name, "status": "invalid", "error": type(failure).__name__}
    return {"file": name, "status": "valid", "role": profile.role, "id": profile.id}


def main(argv=None, stdout=None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    parser = argparse.ArgumentParser(prog="python -m aik_profiles",
                                     description="Validate AIK role profile documents.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate profile document files")
    validate.add_argument("files", nargs="+")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_request:
        return 2 if exit_request.code not in (0, None) else 0
    failed = False
    for name in args.files:
        result = _validate_file(name)
        failed = failed or result["status"] != "valid"
        stdout.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
    return 1 if failed else 0
