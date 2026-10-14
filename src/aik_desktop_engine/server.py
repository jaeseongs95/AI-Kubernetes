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
- ``connections.*`` requests run one at a time on a separate worker thread, so a
  slow vendor check never delays a status query. Answers may therefore arrive
  out of request order; the client matches them by ``request_id``.

Status and read-model queries observe only; they never create state. The
``connections.*`` commands (AIK-046/047) go to an injected
``aik_connect.ConnectionService``: reads stay side-effect free, writes change
connection metadata and the per-user vault. Requests that carry an API key
(``connections.add_key``/``rotate_key``) are sent only by the main process from
its own key entry window; the engine never echoes a payload and answers every
failure with a fixed code. Without a configured service the connection
commands answer ``CONNECTIONS_NOT_CONFIGURED``.
This is a pipe between processes of one user, not an OS security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
from typing import Any, BinaryIO

from aik_platform import PlatformError, family
from aik_platform.paths import state_root

PROTOCOL_VERSION = 1
ENGINE_VERSION = "0.1.0"
MAX_FRAME = 65536
_READ_COMMANDS = {"system.status": (), "engine.info": (), "readmodel.sources": ()}
# Exact payload keys per connection command (values are checked by the service).
CONNECTION_COMMANDS = {
    "connections.catalog": (),
    "connections.list": (),
    "connections.impact": ("connection_id",),
    "connections.login_state": (),
    "connections.add_key": ("provider", "account_label", "secret"),
    "connections.rotate_key": ("connection_id", "expected_revision", "secret"),
    "connections.check": ("connection_id",),
    "connections.rename": ("connection_id", "expected_revision", "account_label"),
    "connections.delete": ("connection_id", "expected_revision", "acknowledge_impact"),
    "connections.start_login": ("provider", "account_label", "connection_id"),
    "connections.cancel_login": (),
    "connections.complete_login": (),
    "connections.logout": ("connection_id", "expected_revision"),
}
PAYLOAD_FIELDS = {**_READ_COMMANDS, **CONNECTION_COMMANDS}
COMMANDS = frozenset(PAYLOAD_FIELDS)
_MAX_TEXT = 16384
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

    def __init__(self, nonce: str, config: EngineConfig | None = None, *, environ=None, connections=None):
        if type(nonce) is not str or not _NONCE.fullmatch(nonce):
            raise ValueError("A 64 hex digit nonce is required.")
        self._nonce = nonce
        self._config = config or EngineConfig()
        self._environ = environ
        self._readmodel = None
        self._connections = connections

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
        payload = request["payload"]
        if type(payload) is not dict or set(payload) != set(PAYLOAD_FIELDS[request["command"]]):
            raise ProtocolError("INVALID_REQUEST")
        for value in payload.values():
            if not (value is None or type(value) in (bool, int) or (type(value) is str and len(value) <= _MAX_TEXT)):
                raise ProtocolError("INVALID_REQUEST")

    def _connection_command(self, command: str, payload: dict) -> dict:
        from aik_connect import ConnectError

        service = self._connections
        if service is None:
            raise ConnectError("CONNECTIONS_NOT_CONFIGURED")
        name = command.split(".", 1)[1]
        if name in ("catalog", "list", "login_state", "cancel_login", "complete_login"):
            return getattr(service, name)()
        if name == "impact":
            return service.impact(payload["connection_id"])
        if name == "add_key":
            return service.add_api_key(payload["provider"], payload["account_label"], payload["secret"])
        if name == "rotate_key":
            return service.rotate_key(payload["connection_id"], payload["expected_revision"], payload["secret"])
        if name == "check":
            return service.check(payload["connection_id"])
        if name == "rename":
            return service.rename(payload["connection_id"], payload["expected_revision"], payload["account_label"])
        if name == "delete":
            return service.delete(payload["connection_id"], payload["expected_revision"],
                                  payload["acknowledge_impact"])
        if name == "start_login":
            return service.start_login(payload["provider"], payload["account_label"], payload["connection_id"])
        if name == "logout":
            return service.logout(payload["connection_id"], payload["expected_revision"])
        raise ProtocolError("UNSUPPORTED_COMMAND")

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
        from aik_connect import ConnectError

        payload = request["payload"]
        try:
            if command in CONNECTION_COMMANDS:
                result = self._connection_command(command, payload)
            else:
                result = {"system.status": self.system_status, "engine.info": self.engine_info,
                          "readmodel.sources": self.readmodel_sources}[command]()
        except ConnectError as error:
            return {**envelope, "ok": False, "error": {"code": error.code}}
        except Exception:  # noqa: BLE001 - a fixed code, never a traceback, crosses the pipe
            return {**envelope, "ok": False, "error": {"code": "INTERNAL_ERROR"}}
        finally:
            payload.clear()  # drop this frame's only reference to a submitted key
        return {**envelope, "ok": True, "result": result}


def _write(stream: BinaryIO, value: dict) -> None:
    stream.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
    stream.flush()


def serve(stdin: BinaryIO, stdout: BinaryIO, config: EngineConfig | None = None, *, environ=None,
          connections=None) -> int:
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
    engine = Engine(bootstrap["nonce"], config, environ=environ, connections=connections)
    output = threading.Lock()

    def emit(value: dict) -> None:
        with output:
            _write(stdout, value)

    pending: queue.Queue = queue.Queue()

    def connection_lane() -> None:
        while (request := pending.get()) is not None:
            try:
                emit(engine.dispatch(request))
            except OSError:
                return  # the client closed the pipe; nobody is left to answer

    lane = threading.Thread(target=connection_lane, name="aik-engine-connections", daemon=True)
    emit({"kind": "ready", "protocol_version": PROTOCOL_VERSION, "pid": os.getpid(),
          "engine_version": ENGINE_VERSION})
    lane.start()
    code = 0
    try:
        while True:
            raw = stdin.readline(MAX_FRAME + 1)
            if not raw:
                break
            try:
                request = parse_frame(raw)
            except ProtocolError:
                code = EXIT_INVALID
                break
            if request.get("command") in CONNECTION_COMMANDS:
                pending.put(request)
            else:
                emit(engine.dispatch(request))
    finally:
        pending.put(None)
        lane.join()  # finish (and answer) connection work already accepted
    return code


def _config(argv: list[str]) -> EngineConfig:
    if not argv:
        return EngineConfig()
    if len(argv) == 4 and argv[0] == "--readmodel" and argv[2] == "--user-scope":
        return EngineConfig(Path(argv[1]), argv[3])
    raise ValueError("usage: [--readmodel ABSOLUTE_PATH --user-scope ID]")


def main(argv: list[str] | None = None, *, connectors=None, vault_factory=None) -> int:
    """``connectors`` and ``vault_factory`` come from the composition root, because vendor
    packages live outside this package and connectors share the vault with the service."""
    try:
        config = _config(sys.argv[1:] if argv is None else argv)
    except ValueError:
        return EXIT_INVALID
    return serve(sys.stdin.buffer, sys.stdout.buffer, config,
                 connections=_connection_service(connectors, vault_factory))


def _connection_service(connectors, vault_factory=None):
    """The per-user connection service, or None when it cannot be set up here."""
    if connectors is None:
        return None
    from aik_config.storage import default_settings_path
    from aik_connect import ConnectionService
    from aik_vault import open_user_vault

    try:
        return ConnectionService(default_settings_path(), connectors,
                                 vault_factory=vault_factory or open_user_vault)
    except Exception:  # noqa: BLE001 - reported as CONNECTIONS_NOT_CONFIGURED by every command
        return None
