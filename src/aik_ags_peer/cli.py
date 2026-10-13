"""Bounded client for the AGS 2.9.0 session-message CLI contract.

One request is one ``node session-message-cli.mjs`` process: a single JSON
object on stdin and a single JSON line on stdout. Bodies never go into argv.
There is no retry: a timed-out call may or may not have reached the broker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

from .errors import AgsPeerError
from .models import strict_json_loads


STATE_DIR_ENV = "AGENT_GOVERNANCE_SESSION_MESSAGE_STATE_DIR"
PROTOCOL_VERSION = "1.0.0"
OPERATIONS = ("prepare", "send", "claim", "acknowledge", "status", "pending")
MAX_STDOUT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MIN_NODE_MAJOR = 24
_NODE_VERSION = re.compile(r"v(\d{1,4})\.(\d{1,6})\.(\d{1,6})\Z")


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int
    stdout: bytes


def default_runner(argv, *, input, env, timeout):
    """subprocess.run wrapper; stderr is discarded so AGS diagnostics are never logged."""
    completed = subprocess.run(
        list(argv), input=input, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False,
    )
    return RunResult(completed.returncode, completed.stdout)


def _absolute(value: object) -> Path:
    try:
        path = Path(value)  # type: ignore[arg-type]
    except TypeError:
        raise AgsPeerError("INVALID_PATH") from None
    if not path.is_absolute() or ".." in path.parts:
        raise AgsPeerError("INVALID_PATH")
    return path


def _invalid() -> AgsPeerError:
    return AgsPeerError("AGS_INVALID_RESPONSE", outcome="unknown")


class AgsSessionCli:
    def __init__(self, node, cli_path, state_dir, *, timeout: float = 30.0, runner=None):
        self._node = _absolute(node)
        self._cli_path = _absolute(cli_path)
        try:
            info = self._cli_path.stat()
        except OSError:
            raise AgsPeerError("INVALID_PATH") from None
        if not stat.S_ISREG(info.st_mode):
            raise AgsPeerError("INVALID_PATH")
        self._state_dir = _absolute(state_dir)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 600):
            raise AgsPeerError("INVALID_TIMEOUT")
        if runner is not None and not callable(runner):
            raise AgsPeerError("INVALID_ARGUMENT")
        self._timeout = float(timeout)
        self._runner = runner or default_runner
        self._ensure_state_dir()

    @property
    def node(self) -> Path:
        return self._node

    @property
    def cli_path(self) -> Path:
        return self._cli_path

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def _ensure_state_dir(self) -> None:
        try:
            info = self._state_dir.lstat()
        except FileNotFoundError:
            try:
                self._state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = self._state_dir.lstat()
            except OSError:
                raise AgsPeerError("INVALID_PATH") from None
        except OSError:
            raise AgsPeerError("INVALID_PATH") from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise AgsPeerError("INVALID_PATH")

    def _environment(self) -> dict:
        env = dict(os.environ)
        env[STATE_DIR_ENV] = str(self._state_dir)
        return env

    def _run(self, argv: tuple, request: bytes, timeout: float):
        try:
            return self._runner(argv, input=request, env=self._environment(), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AgsPeerError("AGS_TIMEOUT", outcome="unknown") from None
        except (FileNotFoundError, PermissionError):
            raise AgsPeerError("AGS_UNAVAILABLE", outcome="not_sent") from None
        except OSError:
            # The process may have started (for example a broken stdin pipe).
            raise AgsPeerError("AGS_UNAVAILABLE", outcome="unknown") from None

    def call(self, operation: str, payload: Mapping) -> dict:
        if type(operation) is not str or operation not in OPERATIONS:
            raise AgsPeerError("UNSUPPORTED_OPERATION")
        if not isinstance(payload, Mapping):
            raise AgsPeerError("INVALID_ARGUMENT")
        try:
            request = json.dumps(
                {"operation": operation, "payload": dict(payload)},
                ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise AgsPeerError("INVALID_ARGUMENT") from None
        if len(request) > MAX_REQUEST_BYTES:
            raise AgsPeerError("INVALID_ARGUMENT")
        self._ensure_state_dir()
        result = self._run((str(self._node), str(self._cli_path)), request, self._timeout)
        return self._parse(result)

    @staticmethod
    def _parse(result) -> dict:
        stdout = getattr(result, "stdout", None)
        returncode = getattr(result, "returncode", None)
        if type(stdout) is str:
            stdout = stdout.encode("utf-8")
        if not isinstance(stdout, (bytes, bytearray)) or type(returncode) is not int:
            raise _invalid()
        if len(stdout) > MAX_STDOUT_BYTES:
            raise _invalid()
        try:
            text = bytes(stdout).decode("utf-8")
        except UnicodeDecodeError:
            raise _invalid() from None
        line = text[:-1] if text.endswith("\n") else text
        if line.endswith("\r"):
            line = line[:-1]
        if not line or "\n" in line or "\r" in line:
            raise _invalid()
        try:
            document = strict_json_loads(line)
        except (ValueError, RecursionError):
            raise _invalid() from None
        if (not isinstance(document, dict) or document.get("protocolVersion") != PROTOCOL_VERSION
                or type(document.get("ok")) is not bool):
            raise _invalid()
        if document["ok"] is False:
            error = document.get("error")
            if type(error) is str:
                raise AgsPeerError(
                    "AGS_REJECTED", outcome="rejected",
                    detail_digest=hashlib.sha256(error.encode("utf-8")).hexdigest(),
                    detail_length=len(error),
                )
            raise AgsPeerError("AGS_REJECTED", outcome="rejected")
        if returncode != 0 or not isinstance(document.get("data"), dict):
            raise _invalid()
        return document["data"]

    def node_version(self) -> str:
        """Return ``vMAJOR.MINOR.PATCH`` when node is v24 or newer."""
        result = self._run((str(self._node), "--version"), b"", min(self._timeout, 10.0))
        stdout = getattr(result, "stdout", None)
        if type(stdout) is str:
            stdout = stdout.encode("utf-8")
        if not isinstance(stdout, (bytes, bytearray)) or getattr(result, "returncode", None) != 0:
            raise AgsPeerError("NODE_UNSUPPORTED")
        try:
            version = bytes(stdout[:64]).decode("ascii").strip()
        except UnicodeDecodeError:
            raise AgsPeerError("NODE_UNSUPPORTED") from None
        match = _NODE_VERSION.fullmatch(version)
        if match is None or int(match.group(1)) < MIN_NODE_MAJOR:
            raise AgsPeerError("NODE_UNSUPPORTED")
        return version

    def stop_broker(self, *, wait: float = 5.0) -> bool:
        """Test cleanup only: SIGTERM the broker recorded in endpoint.json.

        Acts only on Linux, and only when /proc/<pid>/cmdline names the
        session-message-broker and this state directory as an argument. Other platforms return False without acting.
        The product path never calls this automatically.
        """
        endpoint = self._state_dir / "endpoint.json"
        try:
            info = endpoint.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            return False
        try:
            document = json.loads(endpoint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        pid = document.get("pid") if isinstance(document, dict) else None
        if type(pid) is not int or pid <= 1 or pid == os.getpid():
            return False
        if not sys.platform.startswith("linux"):
            return False
        proc = Path(f"/proc/{pid}/cmdline")
        try:
            cmdline = proc.read_bytes()
        except OSError:
            return False
        # Only the broker serving this state directory (``--state-directory <dir>``).
        if b"session-message-broker" not in cmdline or os.fsencode(str(self._state_dir)) not in cmdline.split(b"\0"):
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return False
        deadline = time.monotonic() + max(0.0, float(wait))
        while time.monotonic() < deadline:
            try:
                if b"session-message-broker" not in proc.read_bytes():
                    break
            except OSError:
                break
            time.sleep(0.05)
        return True
