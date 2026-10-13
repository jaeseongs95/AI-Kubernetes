"""Development worker tools bound to authority, workspace root and the trusted policy.

Call order (first failure wins): session -> invocation -> local arguments ->
stored result (resend) -> workspace root re-verification -> aik_authority
request (VM intent for effects) -> cancel check -> effect -> VM receipt.
Stored results and interruption markers live in process memory only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time

from aik_authority import (
    STATUS_AUTHORIZED, AuthorityCode as C, AuthorityDenied, AuthorityError, EffectKind, EffectOutcomeUnknown,
    EffectReceipt, ToolCall, ToolCallAuthorization, ToolSession,
)
from aik_runtime.errors import InvalidRecord, UnsafeStoragePath
from aik_runtime.models import MAX_INTEGER, identifier
from aik_runtime.paths import _inspect
from aik_runtime.storage import _sync_directory
from aik_vm_bridge.binding import DIGEST_PATTERN
from aik_workspace import (
    ExecutionContext, InputCondition, InvalidInputPath, WorkspaceUnavailable, normalize_input_path,
    sha256_digest,
)
from aik_workspace.inputs import read_verified

from .errors import DevToolCode as D, DevToolError, EffectUnknown
from .policy import (
    EFFECT_KINDS, TOOL_CREATE_FILE, TOOL_READ_FILE, TOOL_RUN_VERIFICATION, TOOL_UPDATE_FILE,
    UNSUPPORTED_CAPABILITIES, DevToolPolicy, VerificationCommand, rebuild_policy,
)


_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ENV_KEYS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR",
             "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE")
_POLL_SECONDS = 0.02
_TEMP_PREFIX = ".aik-devtools-"


# ----------------------------------------------------------------- targets

def _id(value: object, field_name: str) -> str:
    try:
        identifier(value, field_name)
    except InvalidRecord:
        raise DevToolError(D.INVALID_REQUEST, subject=field_name) from None
    return value


def _path(value: object) -> str:
    try:
        return normalize_input_path(value)
    except InvalidInputPath:
        raise DevToolError(D.PATH_REJECTED, subject="path") from None


def _segment(value: str) -> str:
    """Keep ASCII-safe names; encode others (Korean, spaces, ':') as ``u.<utf-8 hex>``."""
    if _SEGMENT.fullmatch(value) and not value.startswith("u."):
        return value
    return "u." + value.encode("utf-8").hex()


def files_scope_target(workspace_id: str, directory: str | None = None) -> str:
    """Authorization target for the workspace files, optionally one directory prefix."""
    base = f"workspace://{_segment(_id(workspace_id, 'workspace_id'))}/files"
    if directory is None:
        return base
    return base + "/" + "/".join(_segment(part) for part in _path(directory).split("/"))


def file_target(workspace_id: str, path: str) -> str:
    return files_scope_target(workspace_id, path)


def verification_scope_target(workspace_id: str) -> str:
    return f"verification://{_segment(_id(workspace_id, 'workspace_id'))}"


def verification_target(workspace_id: str, command_id: str) -> str:
    return verification_scope_target(workspace_id) + "/" + _segment(_id(command_id, "command_id"))


# ----------------------------------------------------------------- records

@dataclass(frozen=True, slots=True)
class Invocation:
    """Worker-supplied identity of one tool call; checked by the authority gateway."""

    tool_call_id: str
    input_revision: str
    authorization_revision: int
    effect_id: str | None = None
    cancel: threading.Event | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _id(self.tool_call_id, "tool_call_id")
        _id(self.input_revision, "input_revision")
        if type(self.authorization_revision) is not int or not 1 <= self.authorization_revision <= MAX_INTEGER:
            raise DevToolError(D.INVALID_REQUEST, subject="authorization_revision")
        if self.effect_id is not None:
            _id(self.effect_id, "effect_id")
        if self.cancel is not None and not isinstance(self.cancel, threading.Event):
            raise DevToolError(D.INVALID_REQUEST, subject="cancel")


@dataclass(frozen=True, slots=True)
class ReceiptState:
    """Result of forwarding the observation to VM; never a completion verdict."""

    forwarded: bool
    reported_status: str
    vm_state: str | None
    reconciliation_required: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class FileReadResult:
    tool_call_id: str
    path: str
    digest: str
    size: int
    content: bytes
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class FileWriteResult:
    tool_call_id: str
    effect_id: str
    path: str
    status: str  # created / updated / already_applied
    digest: str
    size: int
    previous_digest: str | None
    receipt: ReceiptState | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Observed execution of a policy command. ``passed`` comes only from exit code and oracles."""

    tool_call_id: str
    effect_id: str
    command_id: str
    definition_digest: str
    status: str  # completed / timed_out / cancelled / oracle_mismatch / oracle_changed
    exit_code: int | None
    passed: bool
    oracle_ok: bool
    timed_out: bool
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    output_limit_exceeded: bool
    duration_ms: int
    output_digest: str
    receipt: ReceiptState | None = None
    replayed: bool = False


class _Entry:
    __slots__ = ("session_id", "fingerprint", "result", "error")

    def __init__(self, session_id: str, fingerprint: str, result=None, error=None):
        self.session_id = session_id
        self.fingerprint = fingerprint
        self.result = result
        self.error = error


class _Refused(Exception):
    """No effect was performed; the receipt is ``failed``."""

    def __init__(self, error: DevToolError):
        super().__init__(error.code.value)
        self.error = error


class _Unknown(Exception):
    """The effect state could not be confirmed; the receipt is ``unknown``."""

    def __init__(self, subject: str):
        super().__init__(subject)
        self.subject = subject


def _refuse(code: D, subject: str) -> _Refused:
    return _Refused(DevToolError(code, subject=subject))


def _clone(error: DevToolError) -> DevToolError:
    return type(error)(error.code, subject=error.subject, authority_code=error.authority_code,
                       vm_code=error.vm_code, escalation=error.escalation)


def _digest_text(payload: dict) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


def _environment() -> dict:
    """Fixed allowlisted environment; other variables (and any secret in them) are not passed."""
    environment = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _kill(process: subprocess.Popen) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _captured(stream, limit: int) -> tuple[bytes, int]:
    stream.flush()
    size = os.fstat(stream.fileno()).st_size
    stream.seek(0)
    return stream.read(limit), size


def _write_temp(directory: Path, data: bytes, mode: int) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=_TEMP_PREFIX, suffix=".tmp", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


# ----------------------------------------------------------------- tools

class DevTools:
    """Bounded development tools for one workspace. The trusted backend owns this object."""

    def __init__(self, *, authority, workspaces, policy: DevToolPolicy):
        if not all(callable(getattr(authority, name, None)) for name in ("request", "forward_receipt")):
            raise TypeError("An authority port with request and forward_receipt is required.")
        if not callable(getattr(workspaces, "execution_context", None)):
            raise TypeError("A workspace provider with execution_context is required.")
        self._policy = rebuild_policy(policy)
        self._authority = authority
        self._workspaces = workspaces
        self.workspace_id = self._policy.workspace_id
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    def __repr__(self) -> str:
        return f"DevTools(workspace_id={self.workspace_id!r})"

    def __reduce_ex__(self, protocol):
        raise TypeError("Development tools cannot be serialized.")

    # ------------------------------------------------------------- public
    def capabilities(self) -> dict:
        return {
            "tools": {tool: kind.value for tool, kind in EFFECT_KINDS.items()},
            "verifications": tuple(command.command_id for command in self._policy.verifications),
            "analysis_only_paths": self._policy.protected_paths(),
            "max_file_bytes": self._policy.max_file_bytes,
            "unsupported": UNSUPPORTED_CAPABILITIES,
            "guarantee": "api_restricted_tools_not_os_isolation",
        }

    def read_file(self, session: ToolSession, invocation: Invocation, path: str, *,
                  expected_digest: str | None = None) -> FileReadResult:
        relative = _path(path)
        if expected_digest is not None:
            self._check_digest(expected_digest)

        def perform(root: Path, inv: Invocation):
            data = self._read(root, relative)
            digest = sha256_digest(data)
            if expected_digest is not None and digest != expected_digest:
                raise _refuse(D.STALE_INPUT, "expected_digest")
            return FileReadResult(inv.tool_call_id, relative, digest, len(data), data), None

        arguments = {"path": relative, "expected_digest": expected_digest}
        return self._call(session, invocation, TOOL_READ_FILE, arguments,
                          file_target(self.workspace_id, relative), perform)

    def create_file(self, session: ToolSession, invocation: Invocation, path: str,
                    content: bytes) -> FileWriteResult:
        relative = self._writable(path)
        data = self._content(content)
        digest = sha256_digest(data)

        def perform(root: Path, inv: Invocation):
            status = self._create(root, relative, data, digest)
            return FileWriteResult(inv.tool_call_id, inv.effect_id, relative, status, digest, len(data), None), \
                "succeeded"

        arguments = {"path": relative, "content_sha256": digest, "size": len(data)}
        return self._call(session, invocation, TOOL_CREATE_FILE, arguments,
                          file_target(self.workspace_id, relative), perform)

    def update_file(self, session: ToolSession, invocation: Invocation, path: str, content: bytes, *,
                    expected_digest: str) -> FileWriteResult:
        """Compare-and-swap: the current bytes must have ``expected_digest``."""
        relative = self._writable(path)
        data = self._content(content)
        self._check_digest(expected_digest)
        digest = sha256_digest(data)

        def perform(root: Path, inv: Invocation):
            status = self._update(root, relative, data, digest, expected_digest)
            return FileWriteResult(inv.tool_call_id, inv.effect_id, relative, status, digest, len(data),
                                   expected_digest), "succeeded"

        arguments = {"path": relative, "content_sha256": digest, "size": len(data),
                     "expected_digest": expected_digest}
        return self._call(session, invocation, TOOL_UPDATE_FILE, arguments,
                          file_target(self.workspace_id, relative), perform)

    def run_verification(self, session: ToolSession, invocation: Invocation, command_id: str) -> VerificationResult:
        """Run a policy-defined command. The worker cannot supply argv, cwd, env or limits."""
        command = self._policy.verification(command_id) if type(command_id) is str else None
        if command is None:
            raise DevToolError(D.COMMAND_NOT_ALLOWED, subject="command_id")

        def perform(root: Path, inv: Invocation):
            return self._verify(root, inv, command)

        arguments = {"command_id": command.command_id, "definition_digest": command.definition_digest()}
        return self._call(session, invocation, TOOL_RUN_VERIFICATION, arguments,
                          verification_target(self.workspace_id, command.command_id), perform)

    def verify_result(self, result: object) -> bool:
        """True only for a result this instance actually produced (not a fabricated copy)."""
        if type(result) not in (FileReadResult, FileWriteResult, VerificationResult):
            return False
        with self._lock:
            entry = self._entries.get(result.tool_call_id)
            return entry is not None and entry.result is not None and \
                replace(result, replayed=False) == entry.result

    # ------------------------------------------------------------- core
    def _after_effect(self, tool_call_id: str) -> None:
        """Fault-injection seam between effect and receipt; intentionally a no-op."""

    def _call(self, session, invocation, tool_id, arguments, target, perform):
        session = self._session(session)
        if type(invocation) is not Invocation:
            raise DevToolError(D.INVALID_REQUEST, subject="invocation")
        kind = EFFECT_KINDS[tool_id]
        effectful = kind is not EffectKind.READ_ONLY
        if effectful == (invocation.effect_id is None):
            raise DevToolError(D.INVALID_REQUEST, subject="effect_id")
        tool_call_id = invocation.tool_call_id
        fingerprint = _digest_text({
            "tool_id": tool_id, "arguments": arguments, "target": target,
            "input_revision": invocation.input_revision,
            "authorization_revision": invocation.authorization_revision, "effect_id": invocation.effect_id,
        })
        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is not None:
                if entry.session_id != session.session_id or entry.fingerprint != fingerprint:
                    raise DevToolError(D.CALL_CONFLICT, subject="tool_call_id")
                if entry.error is not None:
                    raise _clone(entry.error)
                if entry.result is not None:
                    return replace(entry.result, replayed=True)
                if kind is EffectKind.NON_IDEMPOTENT:
                    raise EffectUnknown(D.OUTCOME_UNKNOWN, subject="interrupted")
                # Idempotent interruption: fall through and observe the real file state.
            root = self._root()
            call = self._tool_call(tool_id, target, invocation, arguments)
            self._authorize(session, call, kind)
            if effectful:
                # Interruption marker; replaced by the result or error below.
                self._entries[tool_call_id] = _Entry(session.session_id, fingerprint)
            if invocation.cancel is not None and invocation.cancel.is_set():
                error = DevToolError(D.CANCELLED, subject="before_effect")
                if effectful:
                    self._forward(session, tool_call_id, "cancelled")
                self._entries[tool_call_id] = _Entry(session.session_id, fingerprint, error=error)
                raise _clone(error)
            try:
                result, status = perform(root, invocation)
            except _Refused as refused:
                if effectful:
                    self._forward(session, tool_call_id, "failed")
                self._entries[tool_call_id] = _Entry(session.session_id, fingerprint, error=refused.error)
                raise _clone(refused.error) from None
            except (_Unknown, OSError) as error:
                if not effectful:
                    raise DevToolError(D.PATH_UNSAFE, subject="io") from None
                self._forward(session, tool_call_id, "unknown")
                subject = error.subject if isinstance(error, _Unknown) else "io"
                raise EffectUnknown(D.OUTCOME_UNKNOWN, subject=subject) from None
            self._after_effect(tool_call_id)
            if effectful:
                reference = result.digest if type(result) is FileWriteResult else result.output_digest
                result = replace(result, receipt=self._forward(session, tool_call_id, status, reference))
            self._entries[tool_call_id] = _Entry(session.session_id, fingerprint, result=result)
            return result

    def _session(self, session: object) -> ToolSession:
        if type(session) is not ToolSession or type(session.session_id) is not str:
            raise DevToolError(D.INVALID_REQUEST, subject="session")
        if session.workspace_id != self.workspace_id:
            raise DevToolError(D.WORKSPACE_MISMATCH, subject="session")
        return session

    def _root(self) -> Path:
        try:
            context = self._workspaces.execution_context(self.workspace_id)
        except WorkspaceUnavailable as error:
            raise DevToolError(D.WORKSPACE_UNAVAILABLE, subject=error.status.value) from None
        except Exception:
            raise DevToolError(D.WORKSPACE_UNAVAILABLE, subject="workspace") from None
        if type(context) is not ExecutionContext or context.workspace_id != self.workspace_id:
            raise DevToolError(D.WORKSPACE_UNAVAILABLE, subject="context")
        return Path(context.canonical_root)

    def _tool_call(self, tool_id, target, invocation, arguments) -> ToolCall:
        try:
            return ToolCall(invocation.tool_call_id, tool_id, self.workspace_id, target, invocation.input_revision,
                            invocation.authorization_revision, dict(arguments), invocation.effect_id)
        except AuthorityDenied as error:
            raise DevToolError(D.INVALID_REQUEST, subject=error.subject) from None

    def _authorize(self, session: ToolSession, call: ToolCall, kind: EffectKind) -> ToolCallAuthorization:
        try:
            decision = self._authority.request(session, call)
        except EffectOutcomeUnknown as error:
            raise EffectUnknown(D.OUTCOME_UNKNOWN, subject="intent", authority_code=error.code.value,
                                vm_code=error.vm_code) from None
        except AuthorityError as error:
            if error.code is C.RECONCILIATION_REQUIRED:
                raise EffectUnknown(D.RECONCILIATION_REQUIRED, subject="effect_id",
                                    authority_code=error.code.value) from None
            if error.code is C.EFFECT_ALREADY_AUTHORIZED:
                # A non-idempotent effect was authorized before (e.g. by a process
                # that died). Never re-run it; report unknown when no receipt exists.
                self._report_lost(session, call)
                raise EffectUnknown(D.OUTCOME_UNKNOWN, subject="result_lost",
                                    authority_code=error.code.value) from None
            raise DevToolError(D.AUTHORIZATION_DENIED, subject=error.subject, authority_code=error.code.value,
                               vm_code=error.vm_code, escalation=error.escalation) from None
        except Exception:
            if kind is EffectKind.READ_ONLY:
                raise DevToolError(D.AUTHORIZATION_INVALID, subject="authority") from None
            raise EffectUnknown(D.OUTCOME_UNKNOWN, subject="authority") from None
        if (type(decision) is not ToolCallAuthorization or decision.status != STATUS_AUTHORIZED
                or decision.tool_call_id != call.tool_call_id or decision.tool_id != call.tool_id
                or decision.effect_kind is not kind or decision.effect_id != call.effect_id
                or decision.session_id != session.session_id or decision.workspace_id != self.workspace_id):
            raise DevToolError(D.AUTHORIZATION_INVALID, subject="decision")
        return decision

    def _report_lost(self, session: ToolSession, call: ToolCall) -> None:
        reader = getattr(self._authority, "effect_state", None)
        if not callable(reader):
            return
        try:
            state = reader(call.effect_id)
        except Exception:
            return
        if state == "intent_recorded":
            self._forward(session, call.tool_call_id, "unknown")

    def _forward(self, session, tool_call_id: str, status: str, receipt_ref: str | None = None) -> ReceiptState:
        try:
            response = self._authority.forward_receipt(session, tool_call_id, status, receipt_ref=receipt_ref)
        except AuthorityError as error:
            return ReceiptState(False, status, None, True, error.code.value)
        except Exception:
            return ReceiptState(False, status, None, True, "RECEIPT_UNKNOWN")
        if type(response) is not EffectReceipt or response.tool_call_id != tool_call_id:
            return ReceiptState(False, status, None, True, "RECEIPT_UNKNOWN")
        return ReceiptState(True, status, response.vm_state, bool(response.reconciliation_required))

    # ------------------------------------------------------------- arguments
    def _writable(self, path: object) -> str:
        relative = _path(path)
        if self._policy.is_protected(relative):
            raise DevToolError(D.PATH_PROTECTED, subject="path")
        if relative.rsplit("/", 1)[-1].startswith(_TEMP_PREFIX):
            raise DevToolError(D.PATH_REJECTED, subject="path")
        return relative

    def _content(self, content: object) -> bytes:
        if type(content) is not bytes:
            raise DevToolError(D.INVALID_REQUEST, subject="content")
        if len(content) > self._policy.max_file_bytes:
            raise DevToolError(D.TOO_LARGE, subject="content")
        return content

    @staticmethod
    def _check_digest(value: object) -> None:
        if type(value) is not str or DIGEST_PATTERN.fullmatch(value) is None:
            raise DevToolError(D.INVALID_REQUEST, subject="expected_digest")

    # ------------------------------------------------------------- files
    def _unsafe(self, root: Path, relative: str) -> _Refused:
        current = root
        try:
            for part in relative.split("/"):
                current = current / part
                _inspect(current)
            info = os.lstat(current)
            if stat.S_ISREG(info.st_mode) and info.st_size > self._policy.max_file_bytes:
                return _refuse(D.TOO_LARGE, "path")
        except (UnsafeStoragePath, OSError):
            pass
        return _refuse(D.PATH_UNSAFE, "path")

    def _read(self, root: Path, relative: str) -> bytes:
        condition, data = read_verified(root, relative, self._policy.max_file_bytes)
        if condition is InputCondition.CURRENT:
            return data
        if condition is InputCondition.MISSING:
            raise _refuse(D.NOT_FOUND, "path")
        raise self._unsafe(root, relative)

    def _confirm(self, root: Path, relative: str, digest: str) -> None:
        condition, data = read_verified(root, relative, self._policy.max_file_bytes)
        if condition is not InputCondition.CURRENT or sha256_digest(data) != digest:
            raise _Unknown("confirm")

    @staticmethod
    def _ensure_parent(root: Path, parts: list) -> Path:
        current = root
        try:
            for part in parts[:-1]:
                current = current / part
                _inspect(current)
                try:
                    info = os.lstat(current)
                except FileNotFoundError:
                    os.mkdir(current, 0o755)
                    _inspect(current)
                    info = os.lstat(current)
                if not stat.S_ISDIR(info.st_mode):
                    raise _refuse(D.PATH_UNSAFE, "path")
            if not current.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
                raise _refuse(D.PATH_UNSAFE, "path")
        except UnsafeStoragePath:
            raise _refuse(D.PATH_UNSAFE, "path") from None
        return current

    def _create(self, root: Path, relative: str, data: bytes, digest: str) -> str:
        condition, existing = read_verified(root, relative, self._policy.max_file_bytes)
        if condition is InputCondition.CURRENT:
            if sha256_digest(existing) == digest:
                return "already_applied"
            raise _refuse(D.ALREADY_EXISTS, "path")
        if condition is not InputCondition.MISSING:
            raise self._unsafe(root, relative)
        parts = relative.split("/")
        parent = self._ensure_parent(root, parts)
        target = parent / parts[-1]
        temporary = _write_temp(parent, data, 0o644)
        try:
            # link() publishes complete bytes atomically and never replaces an
            # existing entry (including a symlink placed by another process).
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise _refuse(D.ALREADY_EXISTS, "path") from None
            except OSError:
                raise _refuse(D.WRITE_FAILED, "link") from None
        finally:
            temporary.unlink(missing_ok=True)
        _sync_directory(parent)
        self._confirm(root, relative, digest)
        return "created"

    def _update(self, root: Path, relative: str, data: bytes, digest: str, expected: str) -> str:
        condition, existing = read_verified(root, relative, self._policy.max_file_bytes)
        if condition is InputCondition.MISSING:
            raise _refuse(D.NOT_FOUND, "path")
        if condition is not InputCondition.CURRENT:
            raise self._unsafe(root, relative)
        current = sha256_digest(existing)
        if current == digest:
            return "already_applied"
        if current != expected:
            raise _refuse(D.STALE_INPUT, "expected_digest")
        target = root.joinpath(*relative.split("/"))
        mode = stat.S_IMODE(os.lstat(target).st_mode)
        temporary = _write_temp(target.parent, data, mode)
        try:
            condition, again = read_verified(root, relative, self._policy.max_file_bytes)
            if condition is not InputCondition.CURRENT or sha256_digest(again) != expected:
                raise _refuse(D.STALE_INPUT, "concurrent_change")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        _sync_directory(target.parent)
        self._confirm(root, relative, digest)
        return "updated"

    # ------------------------------------------------------------- verification
    def _oracles_current(self, root: Path, command: VerificationCommand) -> bool:
        for path, digest in command.oracle_files:
            condition, data = read_verified(root, path, self._policy.max_file_bytes)
            if condition is not InputCondition.CURRENT or sha256_digest(data) != digest:
                return False
        return True

    def _verify(self, root: Path, inv: Invocation, command: VerificationCommand):
        definition = command.definition_digest()
        if not self._oracles_current(root, command):
            digest = _digest_text({"command_id": command.command_id, "definition_digest": definition,
                                   "status": "oracle_mismatch"})
            result = VerificationResult(inv.tool_call_id, inv.effect_id, command.command_id, definition,
                                        "oracle_mismatch", None, False, False, False, "", "", 0, 0, False, False,
                                        False, 0, digest)
            return result, "failed"  # the command was not run
        run = self._execute(root, command, inv.cancel)
        oracle_ok = self._oracles_current(root, command)
        status = run["status"]
        if status == "completed" and not oracle_ok:
            status = "oracle_changed"
        limit = command.max_output_bytes
        stdout_truncated = run["stdout_bytes"] > limit
        stderr_truncated = run["stderr_bytes"] > limit
        output_digest = _digest_text({
            "command_id": command.command_id, "definition_digest": definition, "status": status,
            "exit_code": run["exit_code"], "stdout_bytes": run["stdout_bytes"], "stderr_bytes": run["stderr_bytes"],
            "stdout_sha256": hashlib.sha256(run["stdout"]).hexdigest(),
            "stderr_sha256": hashlib.sha256(run["stderr"]).hexdigest(),
        })
        result = VerificationResult(
            inv.tool_call_id, inv.effect_id, command.command_id, definition, status, run["exit_code"],
            status == "completed" and run["exit_code"] == 0 and oracle_ok, oracle_ok, status == "timed_out",
            run["stdout"].decode("utf-8", "replace"), run["stderr"].decode("utf-8", "replace"),
            run["stdout_bytes"], run["stderr_bytes"], stdout_truncated, stderr_truncated,
            stdout_truncated or stderr_truncated, run["duration_ms"], output_digest,
        )
        # The receipt reports the tool execution observation, not the verification verdict.
        receipt = {"completed": "succeeded", "oracle_changed": "succeeded",
                   "timed_out": "failed", "cancelled": "cancelled"}[status]
        return result, receipt

    @staticmethod
    def _execute(root: Path, command: VerificationCommand, cancel) -> dict:
        options = {"cwd": str(root), "stdin": subprocess.DEVNULL, "shell": False, "env": _environment(),
                   "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            options["start_new_session"] = True
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            started = time.monotonic()
            try:
                process = subprocess.Popen(list(command.argv), stdout=out, stderr=err, **options)
            except OSError:
                raise _refuse(D.COMMAND_UNAVAILABLE, "argv") from None
            deadline = started + float(command.timeout_seconds)
            status = "completed"
            try:
                while True:
                    try:
                        process.wait(timeout=_POLL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel is not None and cancel.is_set():
                        status = "cancelled"
                    elif time.monotonic() >= deadline:
                        status = "timed_out"
                    else:
                        continue
                    _kill(process)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise _Unknown("process_not_stopped") from None
                    break
            except BaseException:
                if process.poll() is None:
                    _kill(process)
                raise
            duration = int((time.monotonic() - started) * 1000)
            stdout, stdout_bytes = _captured(out, command.max_output_bytes)
            stderr, stderr_bytes = _captured(err, command.max_output_bytes)
        return {"status": status, "exit_code": process.returncode if status == "completed" else None,
                "stdout": stdout, "stderr": stderr, "stdout_bytes": stdout_bytes, "stderr_bytes": stderr_bytes,
                "duration_ms": duration}
