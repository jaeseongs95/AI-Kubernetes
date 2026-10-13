"""Validation command: python -m aik_profiles validate FILE [FILE ...].

Each result line is JSON with the file, status and (when invalid) only the
error classification. Field values are never echoed. Exit codes: 0 all valid,
1 at least one invalid, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .errors import InvalidProfile
from .models import load_profile_document, parse_json


MAX_DOCUMENT_BYTES = 1 << 20


def _validate_file(name: str) -> dict:
    try:
        data = Path(name).read_bytes()
    except OSError:
        return {"file": name, "status": "invalid", "error": "Unreadable"}
    if len(data) > MAX_DOCUMENT_BYTES:
        return {"file": name, "status": "invalid", "error": "DocumentTooLarge"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"file": name, "status": "invalid", "error": "InvalidEncoding"}
    try:
        profile = load_profile_document(parse_json(text))
    except InvalidProfile as error:
        return {"file": name, "status": "invalid", "error": type(error).__name__}
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
