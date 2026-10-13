"""Bounded JSON-lines IPC and a real, fixed synthetic worker for AIK-002.

The private inherited pipe is the transport. A per-process nonce is provided in
the first stdin frame, never in argv, the environment, or the renderer API.
This is not an OS security boundary against another trusted same-user process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROTOCOL = 1
WORKSPACE = "foundation-spike"
MAX_FRAME = 65536
COMMANDS = frozenset({"health", "worker_probe", "vault_probe", "auth_vault_probe"})
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
NONCE = re.compile(r"^[0-9a-f]{64}$")
SAMPLE = b"AI-Kubernetes foundation synthetic worker\n"
MESSAGES = {
    "INVALID_REQUEST": "The request does not match the spike protocol.",
    "UNAUTHORIZED": "The request is not authorized for this sidecar.",
    "INCOMPATIBLE_PROTOCOL": "The frontend and sidecar protocol versions differ.",
    "UNKNOWN_WORKSPACE": "The request is outside the spike workspace.",
    "UNSUPPORTED_COMMAND": "This command is not available in the spike.",
    "WORKER_FAILED": "The fixed worker could not complete.",
    "INTERNAL_ERROR": "The spike could not complete this request.",
}


class ProtocolError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("INVALID_REQUEST")
        result[key] = value
    return result


def parse_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_FRAME:
        raise ProtocolError("INVALID_REQUEST")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("INVALID_REQUEST") from exc
    if type(value) is not dict:
        raise ProtocolError("INVALID_REQUEST")
    return value


def validate_request(request: dict[str, Any], nonce: str) -> None:
    if set(request) != {"protocol_version", "request_id", "command", "workspace_id", "payload", "capability"}:
        raise ProtocolError("INVALID_REQUEST")
    if type(request["protocol_version"]) is not int or request["protocol_version"] != PROTOCOL:
        raise ProtocolError("INCOMPATIBLE_PROTOCOL")
    if not isinstance(request["request_id"], str) or not UUID.fullmatch(request["request_id"]):
        raise ProtocolError("INVALID_REQUEST")
    if not isinstance(request["capability"], str) or not NONCE.fullmatch(request["capability"]) or not hmac.compare_digest(request["capability"], nonce):
        raise ProtocolError("UNAUTHORIZED")
    if request["workspace_id"] != WORKSPACE:
        raise ProtocolError("UNKNOWN_WORKSPACE")
    if not isinstance(request["command"], str) or request["command"] not in COMMANDS:
        raise ProtocolError("UNSUPPORTED_COMMAND")
    if type(request["payload"]) is not dict or request["payload"]:
        raise ProtocolError("INVALID_REQUEST")


def _error(request: dict[str, Any], code: str) -> dict[str, Any]:
    request_id = request.get("request_id")
    command = request.get("command")
    return {
        "protocol_version": PROTOCOL,
        "request_id": request_id if isinstance(request_id, str) and UUID.fullmatch(request_id) else None,
        "workspace_id": WORKSPACE,
        "command": command if isinstance(command, str) and command in COMMANDS else None,
        "ok": False,
        "error": {"code": code, "message": MESSAGES[code]},
    }


def fixed_worker() -> dict[str, Any]:
    # No caller-selected path, command, input, network, or credential is accepted.
    with tempfile.TemporaryDirectory(prefix="AIK worker ") as directory:
        target = Path(directory) / "synthetic-result.txt"
        with target.open("xb") as stream:
            stream.write(SAMPLE)
            stream.flush()
            os.fsync(stream.fileno())
        actual = target.read_bytes()
        if actual != SAMPLE:
            raise RuntimeError("WORKER_FAILED")
        digest = hashlib.sha256(actual).hexdigest()
    return {
        "status": "PASS",
        "code": "WORKER_PROBE_OK",
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "artifact_bytes": len(actual),
        "artifact_sha256": digest,
        "temporary_artifact_removed": not target.exists(),
    }


def _worker_environment() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "PATH"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def run_worker() -> dict[str, Any]:
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--worker"]
    else:
        entry = Path(__file__).resolve().parents[1] / "sidecar_entry.py"
        args = [sys.executable, "-I", str(entry), "--worker"]
    process = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_worker_environment(),
        timeout=10,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode != 0:
        raise RuntimeError("WORKER_FAILED")
    result = parse_frame(process.stdout)
    expected_keys = {"status", "code", "pid", "parent_pid", "artifact_bytes", "artifact_sha256", "temporary_artifact_removed"}
    if (
        set(result) != expected_keys
        or result["status"] != "PASS"
        or result["code"] != "WORKER_PROBE_OK"
        or type(result["pid"]) is not int
        or result["pid"] == os.getpid()
        or result["parent_pid"] != os.getpid()
        or result["artifact_bytes"] != len(SAMPLE)
        or result["artifact_sha256"] != hashlib.sha256(SAMPLE).hexdigest()
        or result["temporary_artifact_removed"] is not True
    ):
        raise RuntimeError("WORKER_FAILED")
    return result


def dispatch(request: dict[str, Any], nonce: str) -> dict[str, Any]:
    try:
        validate_request(request, nonce)
    except ProtocolError as exc:
        return _error(request, str(exc))
    try:
        if request["command"] == "health":
            result = {
                "status": "PASS",
                "code": "SIDECAR_READY",
                "pid": os.getpid(),
                "platform": sys.platform,
                "python_version": ".".join(map(str, sys.version_info[:3])),
                "frozen": bool(getattr(sys, "frozen", False)),
                "product_integration": "NOT_RUN",
            }
        elif request["command"] == "worker_probe":
            result = run_worker()
        elif request["command"] == "auth_vault_probe":
            from aik_spike.auth_vault import probe

            result = probe()
        else:
            from aik_spike.vault import roundtrip

            result = roundtrip()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return _error(request, "WORKER_FAILED" if request["command"] == "worker_probe" else "INTERNAL_ERROR")
    return {
        "protocol_version": PROTOCOL,
        "request_id": request["request_id"],
        "workspace_id": WORKSPACE,
        "command": request["command"],
        "ok": True,
        "result": result,
    }


def _write(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")), flush=True)


def serve() -> int:
    try:
        bootstrap = parse_frame(sys.stdin.buffer.readline(MAX_FRAME + 1))
        if (
            set(bootstrap) != {"kind", "protocol_version", "nonce"}
            or bootstrap["kind"] != "bootstrap"
            or type(bootstrap["protocol_version"]) is not int
            or bootstrap["protocol_version"] != PROTOCOL
            or not isinstance(bootstrap["nonce"], str)
            or not NONCE.fullmatch(bootstrap["nonce"])
        ):
            return 2
        nonce = bootstrap["nonce"]
    except (ProtocolError, OSError):
        return 2
    _write({"kind": "ready", "protocol_version": PROTOCOL, "pid": os.getpid()})
    for _ in range(1000):
        raw = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not raw:
            return 0
        try:
            request = parse_frame(raw)
        except ProtocolError:
            _write(_error({}, "INVALID_REQUEST"))
            return 2
        _write(dispatch(request, nonce))
    return 2


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        try:
            _write(fixed_worker())
        except OSError:
            return 1
        return 0
    if sys.argv[1:]:
        return 2
    return serve()
