"""Bounded stdio JSON-lines protocol of the desktop engine (AIK-045).

Framing:

- The first stdin line is ``{"kind": "bootstrap", "protocol_version", "nonce"}``.
  The nonce is a per-process capability that the main process passes on the
  private pipe, never in argv or the environment.
- The engine answers ``{"kind": "ready", "protocol_version", "pid",
  "engine_version"}``, or ``{"kind": "incompatible", "supported_versions"}``
  and exits with code 3 when the protocol versions differ.
- Every later line is one request; every answer is one line. A line longer
  than ``MAX_FRAME`` bytes or a malformed line ends the session (exit 2).

The engine does not write product state. Its status is what it observed now:
an absent state directory or an unconfigured read model is reported as such.
This is a pipe between processes of one user, not an OS security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, BinaryIO

from aik_platform import PlatformError, family
from aik_platform.paths import state_root

PROTOCOL_VERSION = 1
ENGINE_VERSION = "0.1.0"
MAX_FRAME = 65536
COMMANDS = frozenset({"system.status", "engine.info", "readmodel.sources"})
EXIT_INVALID = 2
EXIT_INCOMPATIBLE = 3
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_FIELDS = frozenset({"protocol_version", "request_id", "command", "payload", "capability"})


class ProtocolError(ValueError):
    """A request that cannot be answered; ``str(error)`` is the fixed code."""


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Developer or installer configuration; never chosen by the renderer."""

    readmodel_path: Path | None = None
    user_scope: str | None = None

    def __post_init__(self):
        if (self.readmodel_path is None) != (self.user_scope is None):
            raise ValueError("The read model path and user scope are configured together.")
        if self.readmodel_path is not None and not Path(self.readmodel_path).is_absolute():
            raise ValueError("The read model path must be absolute.")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("INVALID_REQUEST")
        result[key] = value
    return result


def parse_frame(raw: bytes) -> dict:
    if len(raw) > MAX_FRAME:
        raise ProtocolError("INVALID_REQUEST")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("INVALID_REQUEST") from None
    if type(value) is not dict:
        raise ProtocolError("INVALID_REQUEST")
    return value


class Engine:
    """Answers validated requests. One instance serves one bootstrap session."""

    def __init__(self, nonce: str, config: EngineConfig | None = None, *, environ=None):
        if type(nonce) is not str or not _NONCE.fullmatch(nonce):
            raise ValueError("A 64 hex digit nonce is required.")
        self._nonce = nonce
        self._config = config or EngineConfig()
        self._environ = environ
        self._readmodel = None

    # -- observations ------------------------------------------------------------

    def _state_root(self) -> str:
        try:
            root = state_root(environ=self._environ)
        except PlatformError:
            return "UNAVAILABLE"
        try:
            if not root.exists():
                return "ABSENT"  # created on first use; not an error
            if root.is_dir() and os.access(root, os.W_OK | os.X_OK):
                return "WRITABLE"
        except OSError:
            return "UNAVAILABLE"
        return "NOT_WRITABLE"

    def _open_readmodel(self):
        """Return (state, read model or None). Never creates a database."""
        if self._config.readmodel_path is None:
            return "NOT_CONFIGURED", None
        if self._readmodel is not None:
            return "READY", self._readmodel
        from aik_readmodel import InvalidReadModelDatabase, ReadModel, ReadModelError, ScopeMismatch
        from aik_runtime.errors import StorageBusy

        path = Path(self._config.readmodel_path)
        try:
            if not path.is_file():
                return "ABSENT", None
            model = ReadModel(path, user_scope=self._config.user_scope)
        except ScopeMismatch:
            return "SCOPE_MISMATCH", None
        except StorageBusy:
            return "BUSY", None
        except (InvalidReadModelDatabase, ReadModelError, OSError, ValueError):
            return "INVALID", None
        self._readmodel = model
        return "READY", model

    def _sources(self) -> tuple[str, list]:
        state, model = self._open_readmodel()
        if model is None:
            return state, []
        from aik_readmodel import ReadModelError
        from aik_runtime.errors import StorageBusy

        try:
            statuses = model.cursor_status()
        except StorageBusy:
            return "BUSY", []
        except (ReadModelError, OSError, ValueError):
            self._readmodel = None  # reopen with a full check next time
            return "INVALID", []
        return state, [{
            "source": status.source,
            "applied_cursor": status.applied_cursor,
            "max_seen_cursor": status.max_seen_cursor,
            "open_gaps": len(status.open_gaps),
            "conflicts": status.conflicts,
            "stale": status.stale,
        } for status in (statuses[name] for name in sorted(statuses))]

    # -- commands ----------------------------------------------------------------

    def system_status(self) -> dict:
        root = self._state_root()
        readmodel, _ = self._sources()
        # No VM connection is configured anywhere in AIK yet; say so instead of guessing.
        director = "NOT_CONFIGURED"
        if root in ("UNAVAILABLE", "NOT_WRITABLE"):
            state = "BLOCKED"
        elif readmodel != "READY" or director != "RUNNING":
            state = "DEGRADED"
        else:
            state = "READY"
        return {"state": state, "director_state": director}

    def engine_info(self) -> dict:
        readmodel, _ = self._sources()
        return {
            "engine_version": ENGINE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "platform_family": family(),
            "python_version": "%d.%d.%d" % sys.version_info[:3],
            "frozen": bool(getattr(sys, "frozen", False)),
            "state_root": self._state_root(),
            "readmodel": readmodel,
            "director_state": "NOT_CONFIGURED",
        }

    def readmodel_sources(self) -> dict:
        readmodel, sources = self._sources()
        return {"readmodel": readmodel, "authoritative": False, "sources": sources}

    # -- dispatch ----------------------------------------------------------------

    def _validate(self, request: dict) -> None:
        if set(request) != _REQUEST_FIELDS:
            raise ProtocolError("INVALID_REQUEST")
        if type(request["protocol_version"]) is not int or request["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("INCOMPATIBLE_PROTOCOL")
        if type(request["request_id"]) is not str or not _UUID4.fullmatch(request["request_id"]):
            raise ProtocolError("INVALID_REQUEST")
        capability = request["capability"]
        if type(capability) is not str or not _NONCE.fullmatch(capability) \
                or not hmac.compare_digest(capability, self._nonce):
            raise ProtocolError("UNAUTHORIZED")
        if type(request["command"]) is not str or request["command"] not in COMMANDS:
            raise ProtocolError("UNSUPPORTED_COMMAND")
        if type(request["payload"]) is not dict or request["payload"]:
            raise ProtocolError("INVALID_REQUEST")

    def dispatch(self, request: dict) -> dict:
        request_id = request.get("request_id")
        command = request.get("command")
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id if type(request_id) is str and _UUID4.fullmatch(request_id) else None,
            "command": command if type(command) is str and command in COMMANDS else None,
        }
        try:
            self._validate(request)
        except ProtocolError as error:
            return {**envelope, "ok": False, "error": {"code": str(error)}}
        handler = {"system.status": self.system_status, "engine.info": self.engine_info,
                   "readmodel.sources": self.readmodel_sources}[command]
        try:
            result = handler()
        except Exception:  # noqa: BLE001 - a fixed code, never a traceback, crosses the pipe
            return {**envelope, "ok": False, "error": {"code": "INTERNAL_ERROR"}}
        return {**envelope, "ok": True, "result": result}


def _write(stream: BinaryIO, value: dict) -> None:
    stream.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
    stream.flush()


def serve(stdin: BinaryIO, stdout: BinaryIO, config: EngineConfig | None = None, *, environ=None) -> int:
    try:
        bootstrap = parse_frame(stdin.readline(MAX_FRAME + 1))
    except (ProtocolError, OSError):
        return EXIT_INVALID
    if set(bootstrap) != {"kind", "protocol_version", "nonce"} or bootstrap["kind"] != "bootstrap" \
            or type(bootstrap["nonce"]) is not str or not _NONCE.fullmatch(bootstrap["nonce"]):
        return EXIT_INVALID
    if type(bootstrap["protocol_version"]) is not int or bootstrap["protocol_version"] != PROTOCOL_VERSION:
        _write(stdout, {"kind": "incompatible", "supported_versions": [PROTOCOL_VERSION]})
        return EXIT_INCOMPATIBLE
    engine = Engine(bootstrap["nonce"], config, environ=environ)
    _write(stdout, {"kind": "ready", "protocol_version": PROTOCOL_VERSION, "pid": os.getpid(),
                    "engine_version": ENGINE_VERSION})
    while True:
        raw = stdin.readline(MAX_FRAME + 1)
        if not raw:
            return 0
        try:
            request = parse_frame(raw)
        except ProtocolError:
            return EXIT_INVALID
        _write(stdout, engine.dispatch(request))


def _config(argv: list[str]) -> EngineConfig:
    if not argv:
        return EngineConfig()
    if len(argv) == 4 and argv[0] == "--readmodel" and argv[2] == "--user-scope":
        return EngineConfig(Path(argv[1]), argv[3])
    raise ValueError("usage: [--readmodel ABSOLUTE_PATH --user-scope ID]")


def main(argv: list[str] | None = None) -> int:
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except ValueError:
        return EXIT_INVALID
    return serve(sys.stdin.buffer, sys.stdout.buffer, config)
