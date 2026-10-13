"""Per-user, per-workspace instance lock and atomic reconnect endpoint record.

Liveness is decided only by an OS file lock that the kernel releases when the
owning process dies (POSIX flock, Windows msvcrt.locking). The PID inside the
endpoint record is informational and never used to decide liveness.
Paths reuse aik_runtime.paths validation (no repositories, ledgers, aliases).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from aik_runtime.errors import UnsafeStoragePath
from aik_runtime.paths import validate_database_path


FORMAT_VERSION = 1
ENDPOINT_KINDS = ("named-pipe", "unix-socket")
_WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_COMPONENT = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_INSTANCE = re.compile(r"[0-9a-f]{32}\Z")
_PIPE = re.compile(r"\\\\\.\\pipe\\aik-[A-Za-z0-9_-]{1,200}\Z")
_PIPE_PREFIX = "\\\\.\\pipe\\aik-"
_MAX_RECORD_BYTES = 4096
_MAX_PID = 2**63 - 1
_RECORD_KEYS = frozenset(("format_version", "instance_id", "workspace_id", "component", "pid", "started_at", "endpoint"))
_ENDPOINT_KEYS = frozenset(("kind", "address"))


class SupervisorError(Exception):
    """The supervisor operation could not be completed."""


class InvalidWorkspace(SupervisorError, ValueError):
    """The workspace or component identifier is not a bounded portable name."""


class InvalidEndpoint(SupervisorError):
    """The endpoint record is malformed, foreign, or contains unexpected fields."""


class SupervisorUnavailable(SupervisorError):
    """Another owner holds the lock but no usable endpoint appeared in time."""


class PlacementRejected(SupervisorError):
    """New placements are not admitted in the current lifecycle state."""


def validate_workspace_id(value: object) -> str:
    if type(value) is not str or _WORKSPACE.fullmatch(value) is None:
        raise InvalidWorkspace("Invalid workspace identifier.")
    return value


def validate_component(value: object) -> str:
    if type(value) is not str or _COMPONENT.fullmatch(value) is None:
        raise InvalidWorkspace("Invalid supervisor component name.")
    return value


def default_state_root(os_name: str | None = None, environ=None) -> Path:
    """Per-user state root; Windows uses LocalAppData, never the cwd."""
    os_name = os.name if os_name is None else os_name
    env = os.environ if environ is None else environ
    if os_name == "nt":
        base = env.get("LOCALAPPDATA")
        if not base:
            raise UnsafeStoragePath("Windows supervisor state requires LOCALAPPDATA.")
        return Path(base) / "AIK" / "supervisor"
    # Development/test fallback only; it does not add Linux product support.
    base = env.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "aik" / "supervisor"


@dataclass(frozen=True, slots=True)
class InstancePaths:
    directory: Path
    lock: Path
    endpoint: Path
    socket: Path


def instance_paths(workspace_id: str, component: str = "director", state_root=None, *,
                   create: bool = False) -> InstancePaths:
    validate_workspace_id(workspace_id)
    validate_component(component)
    try:
        root = default_state_root() if state_root is None else Path(state_root)
    except TypeError:
        raise UnsafeStoragePath("The supervisor state root is invalid.") from None
    lock = validate_database_path(root / workspace_id / f"{component}.instance.lock", create_parent=create)
    endpoint = validate_database_path(lock.parent / f"{component}.endpoint.json")
    return InstancePaths(lock.parent, lock, endpoint, lock.parent / f"{component}.sock")


class InstanceLock:
    """An exclusive OS lock held for the whole owner lifetime."""

    def __init__(self, path: Path, descriptor: int, release):
        self.path = path
        self._descriptor = descriptor
        self._release = release

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def release(self) -> None:
        if self._descriptor is None:
            return
        try:
            self._release()
        finally:
            os.close(self._descriptor)
            self._descriptor = None


def _lock_ops(descriptor: int, os_name: str):
    if os_name == "nt":
        import msvcrt

        def acquire():
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

        def release():
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def acquire():
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    return acquire, release


def try_acquire(paths: InstancePaths, os_name: str | None = None) -> InstanceLock | None:
    """Return the held lock, or None when another live owner holds it.

    The lockfile is never unlinked: deleting it could let two processes lock
    different inodes. Process death releases the lock automatically.
    """
    validate_database_path(paths.lock)
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(paths.lock, flags, 0o600)
    except OSError:
        raise UnsafeStoragePath("The supervisor lockfile could not be opened safely.") from None
    keep = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafeStoragePath("The supervisor lockfile must be a regular file without aliases.")
        if not os.path.samestat(info, os.stat(paths.lock, follow_symlinks=False)):
            raise UnsafeStoragePath("The supervisor lockfile changed while opening.")
        acquire, release = _lock_ops(descriptor, os.name if os_name is None else os_name)
        try:
            acquire()
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return None
            raise SupervisorError("The operating system could not acquire the instance lock.") from None
        keep = True
        return InstanceLock(paths.lock, descriptor, release)
    finally:
        if not keep:
            os.close(descriptor)


def _parse_timestamp(value: object) -> None:
    if type(value) is not str:
        raise InvalidEndpoint("The endpoint start time must be a timezone-aware timestamp.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidEndpoint("The endpoint start time must be a timezone-aware timestamp.") from None
    if stamp.utcoffset() is None:
        raise InvalidEndpoint("The endpoint start time must be a timezone-aware timestamp.")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A local-only reconnect address; no network host and no credential."""

    kind: str
    address: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in ENDPOINT_KINDS:
            raise InvalidEndpoint("Unknown local endpoint kind.")
        if (type(self.address) is not str or not 1 <= len(self.address) <= 260
                or any(ord(char) < 32 for char in self.address)):
            raise InvalidEndpoint("The endpoint address is not a bounded local address.")
        if self.kind == "named-pipe" and _PIPE.fullmatch(self.address) is None:
            raise InvalidEndpoint("A named-pipe endpoint must use the local AIK pipe namespace.")
        if self.kind == "unix-socket" and (not self.address.startswith("/") or ".." in self.address.split("/")):
            raise InvalidEndpoint("A unix-socket endpoint must be an absolute local path.")


def default_endpoint(paths: InstancePaths, workspace_id: str, component: str, instance_id: str,
                     os_name: str | None = None) -> Endpoint:
    if (os.name if os_name is None else os_name) == "nt":
        return Endpoint("named-pipe", f"{_PIPE_PREFIX}{component}-{workspace_id}-{instance_id}")
    return Endpoint("unix-socket", str(paths.socket))


@dataclass(frozen=True, slots=True)
class EndpointRecord:
    instance_id: str
    workspace_id: str
    component: str
    pid: int
    started_at: str
    endpoint: Endpoint

    def __post_init__(self) -> None:
        if type(self.instance_id) is not str or _INSTANCE.fullmatch(self.instance_id) is None:
            raise InvalidEndpoint("Invalid instance identifier.")
        validate_workspace_id(self.workspace_id)
        validate_component(self.component)
        if type(self.pid) is not int or not 1 <= self.pid <= _MAX_PID:
            raise InvalidEndpoint("Invalid process identifier.")
        _parse_timestamp(self.started_at)
        if type(self.endpoint) is not Endpoint:
            raise InvalidEndpoint("Invalid endpoint.")

    def to_json(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "instance_id": self.instance_id,
            "workspace_id": self.workspace_id,
            "component": self.component,
            "pid": self.pid,
            "started_at": self.started_at,
            "endpoint": {"kind": self.endpoint.kind, "address": self.endpoint.address},
        }

    @classmethod
    def from_json(cls, data: object) -> "EndpointRecord":
        if type(data) is not dict or set(data) != _RECORD_KEYS:
            raise InvalidEndpoint("The endpoint record has unexpected fields.")
        if type(data["format_version"]) is not int or data["format_version"] != FORMAT_VERSION:
            raise InvalidEndpoint("Unsupported endpoint record format.")
        endpoint = data["endpoint"]
        if type(endpoint) is not dict or set(endpoint) != _ENDPOINT_KEYS:
            raise InvalidEndpoint("The endpoint has unexpected fields.")
        try:
            return cls(data["instance_id"], data["workspace_id"], data["component"], data["pid"],
                       data["started_at"], Endpoint(endpoint["kind"], endpoint["address"]))
        except (SupervisorError, ValueError, TypeError):
            raise InvalidEndpoint("The endpoint record violates its contract.") from None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # No portable directory fsync on Windows; rename durability is OS-owned.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def write_endpoint(path: Path, record: EndpointRecord) -> None:
    """Atomically publish a record: temp file in the same directory, fsync, replace."""
    if type(record) is not EndpointRecord:
        raise InvalidEndpoint("Only endpoint records may be written.")
    validate_database_path(path)
    data = json.dumps(record.to_json(), sort_keys=True, ensure_ascii=True).encode("ascii")
    descriptor, temporary = tempfile.mkstemp(prefix=".endpoint-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _sync_directory(path.parent)


def read_endpoint(path: Path) -> EndpointRecord | None:
    validate_database_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise InvalidEndpoint("The endpoint record could not be read safely.") from None
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(_MAX_RECORD_BYTES + 1)
    if len(data) > _MAX_RECORD_BYTES:
        raise InvalidEndpoint("The endpoint record is too large.")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InvalidEndpoint("The endpoint record is not valid JSON.") from None
    return EndpointRecord.from_json(parsed)


def remove_endpoint(path: Path) -> None:
    validate_database_path(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _sync_directory(path.parent)
