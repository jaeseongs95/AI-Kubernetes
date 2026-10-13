"""AIK hosted runtime over the VM design seams (AIK-031/008, ``aik-hosted-runtime-v1``).

VM has no ``bind_execution``/``read_execution_binding`` extension, and its
design does not plan one. VM does accept an injected runtime that implements
its ``CodexRuntimePort``: ``EngineApplication(service, runtime=...)`` builds
its ``RuntimeJobSupervisor`` from that runtime. While VM calls a runtime port
method, its operation trace scope (``current_operation_trace_scope()``)
identifies the Attempt, runtime intent and provider call.

``AikHostedRuntime`` is that injected runtime. Before any provider effect it:

1. reads the trace scope at call time (the VM trace module is imported lazily,
   only when a call happens, never when this module is imported);
2. decides the AIK assignment (route/connection/lease);
3. checks placement/lease/pool usability with ``binding.check_assignment``;
4. records the binding durably in an AIK-owned SQLite journal
   (``aik_runtime.binding_journal``);
5. only then calls the inner runtime of the original connection.

Existing threads are routed to the connection that created them; account
rotation never moves an existing thread to another account. The owner-confirmed
``TaskExecutionBinding`` is completed later by ``VmBridge.confirm_attempt``
from the VM owner query (``attempt_detail``) joined with ``status``.

This module grants no authorization and makes no VM verdicts. Unit tests use
fakes only; installed VM, Windows, real providers and AGS-governed dispatch
are NOT_RUN.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Callable, Mapping

from aik_runtime.binding_journal import BindingJournal, JournalBlocked, JournalConflict
from aik_runtime.errors import InvalidRecord, StorageError
from aik_runtime.models import Lease, Placement, Pool, identifier

from .binding import AIK_BINDING_FIELDS, TaskExecutionBinding, check_assignment, validate_binding_field
from .errors import BridgeCode, EffectOutcome, VmBridgeError
from .surface import HOSTED_RUNTIME_EXTENSION


TRACE_MODULE = "flowmarshal.engine.operation_trace"
TRACE_FIELDS = ("attempt_id", "intent_id", "call_id", "thread_id", "turn_id")
HOSTED_ASSIGNMENT_FIELDS = ("vm_instance_id", "vm_project_id", *AIK_BINDING_FIELDS)
EFFECT_OPERATIONS = ("create_thread", "start_turn", "resume", "interrupt")
_FIRST_INTENT_OPERATIONS = ("create_thread", "start_turn")
_KNOWN = EffectOutcome.KNOWN_FAILURE
_UNKNOWN = EffectOutcome.UNKNOWN
_CURRENT_IDENTITY_FIELDS = ("vm_instance_id", "vm_project_id", "placement_id", "pool_id", "lease_id",
                            "lease_fencing_token")

__all__ = [
    "HOSTED_RUNTIME_EXTENSION", "TRACE_MODULE", "HOSTED_ASSIGNMENT_FIELDS", "EFFECT_OPERATIONS",
    "TraceRef", "HostedAssignment", "ThreadRoute", "StoredIntent", "StoredAttempt",
    "ExecutionBindingStore", "AikHostedRuntime", "read_operation_trace",
]


def _canonical(mapping: Mapping[str, Any]) -> str:
    return json.dumps(dict(mapping), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


def _require_id(value: object, subject: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    try:
        identifier(value, subject)
    except InvalidRecord:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject=subject) from None


def _path(value: object) -> Path | None:
    if value is None:
        return None
    failed = False
    try:
        return Path(value)
    except TypeError:
        failed = True
    if failed:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="cwd") from None
    return None


# ----- trace ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TraceRef:
    """The VM operation trace scope fields this runtime reads (all optional)."""

    attempt_id: str | None = None
    intent_id: str | None = None
    call_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None

    def __post_init__(self) -> None:
        for name in TRACE_FIELDS:
            value = getattr(self, name)
            if value is not None:
                try:
                    identifier(value, name)
                except InvalidRecord:
                    raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="trace_scope") from None

    @property
    def task_execution(self) -> bool:
        return self.attempt_id is not None


def _trace_from(scope: object) -> TraceRef:
    values = {}
    for name in TRACE_FIELDS:
        values[name] = scope.get(name) if isinstance(scope, Mapping) else getattr(scope, name, None)
    return TraceRef(**values)


def read_operation_trace() -> TraceRef | None:
    """The VM trace scope at call time; None when the module or the scope is absent."""
    try:
        module = importlib.import_module(TRACE_MODULE)
        reader = getattr(module, "current_operation_trace_scope", None)
    except Exception:
        return None
    if not callable(reader):
        return None
    failed = False
    scope = None
    try:
        scope = reader()
    except Exception:
        failed = True
    if failed:
        raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="trace_scope") from None
    return None if scope is None else _trace_from(scope)


# ----- assignment -----------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HostedAssignment:
    """AIK-owned assignment for a hosted runtime call; grants no authorization."""

    vm_instance_id: str
    vm_project_id: str
    cluster_id: str
    route_id: str
    route_revision: str
    connection_id: str
    connection_revision: str
    provider: str
    auth_kind: str
    provider_account_fingerprint: str
    pool_id: str
    placement_id: str
    lease_id: str
    lease_fencing_token: int
    support_matrix_digest: str
    input_manifest_digest: str
    workspace_root_ref: str
    placement: Placement
    lease: Lease
    pool: Pool

    def __post_init__(self) -> None:
        for name in HOSTED_ASSIGNMENT_FIELDS:
            validate_binding_field(name, getattr(self, name))
        for name, kind in (("placement", Placement), ("lease", Lease), ("pool", Pool)):
            if type(getattr(self, name)) is not kind:
                raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject=name)

    def to_mapping(self) -> dict[str, Any]:
        """IDs, revisions and fencing only; placement/lease/pool objects are excluded."""
        return {name: getattr(self, name) for name in HOSTED_ASSIGNMENT_FIELDS}

    def assignment_digest(self) -> str:
        return _digest_text(_canonical(self.to_mapping()))


def _rebuild_model(value: object, kind: type, subject: str):
    if type(value) is not kind:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject=subject)
    failed = False
    try:
        return kind(**{item.name: getattr(value, item.name) for item in fields(kind)})
    except (InvalidRecord, TypeError, AttributeError):
        failed = True
    if failed:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject=subject) from None
    return None


def _rebuild_assignment(value: object) -> HostedAssignment:
    """Reconstruct so object.__setattr__ forgeries are validated again."""
    if type(value) is not HostedAssignment:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="assignment")
    values = {name: getattr(value, name) for name in HOSTED_ASSIGNMENT_FIELDS}
    values["placement"] = _rebuild_model(value.placement, Placement, "placement")
    values["lease"] = _rebuild_model(value.lease, Lease, "lease")
    values["pool"] = _rebuild_model(value.pool, Pool, "pool")
    return HostedAssignment(**values)


def _assignment_from_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(HOSTED_ASSIGNMENT_FIELDS):
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="assignment")
    result = {}
    for name in HOSTED_ASSIGNMENT_FIELDS:
        validate_binding_field(name, value[name])
        result[name] = value[name]
    return result


def _assignment_record(value: object) -> tuple[dict, str, str]:
    if type(value) is HostedAssignment:
        mapping = _rebuild_assignment(value).to_mapping()
    else:
        mapping = _assignment_from_mapping(value)
    text = _canonical(mapping)
    return mapping, text, _digest_text(text)


def _store_damaged(subject: str) -> VmBridgeError:
    return VmBridgeError(BridgeCode.BINDING_STORE_FAILED, subject=subject)


def _parse_stored_assignment(text: str, digest: str) -> Mapping[str, Any]:
    failed = False
    mapping = None
    try:
        mapping = _assignment_from_mapping(json.loads(text))
        if _digest_text(_canonical(mapping)) != digest:
            failed = True
    except (ValueError, TypeError, VmBridgeError):
        failed = True
    if failed:
        raise _store_damaged("assignment_json") from None
    return MappingProxyType(mapping)


# ----- store ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ThreadRoute:
    connection_id: str
    thread_id: str
    vm_attempt_id: str | None
    kind: str
    assignment: Mapping[str, Any]
    assignment_digest: str


@dataclass(frozen=True, slots=True)
class StoredIntent:
    vm_intent_id: str
    vm_call_id: str | None
    operation: str
    thread_id: str | None
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class StoredAttempt:
    vm_attempt_id: str
    vm_project_id: str
    assignment: Mapping[str, Any]
    assignment_digest: str
    intents: tuple
    unknown: bool

    def first_intent(self) -> StoredIntent | None:
        """The first create_thread or start_turn intent recorded for the Attempt."""
        return next((item for item in self.intents if item.operation in _FIRST_INTENT_OPERATIONS), None)


class ExecutionBindingStore:
    """Typed, fail-closed API over the AIK-owned durable binding journal.

    Construction errors keep their ``aik_runtime`` classification (for example
    ``UnsafeStoragePath`` for a development ledger or Git path, or
    ``InvalidDatabase`` for a foreign file). Operation failures are
    ``VmBridgeError`` values with fixed codes.
    """

    def __init__(self, path: Path | str, *, timeout: float = 5.0, now: Callable[[], datetime] | None = None):
        if now is not None and not callable(now):
            raise TypeError("now must be callable.")
        self._journal = BindingJournal(path, timeout=timeout)
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "ExecutionBindingStore()"

    @property
    def path(self) -> Path:
        return self._journal.path

    def _stamp(self) -> str:
        try:
            value = self._now()
        except Exception:
            value = None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="now")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _call(self, function, /, **kwargs):
        failure = None
        with self._lock:
            try:
                return function(**kwargs)
            except JournalConflict as error:
                failure = VmBridgeError(BridgeCode.BINDING_MISMATCH, subject=error.subject)
            except JournalBlocked as error:
                failure = VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _UNKNOWN, subject=error.subject)
            except InvalidRecord:
                failure = VmBridgeError(BridgeCode.INVALID_REQUEST, subject="binding_store_input")
            except (StorageError, OSError, ValueError, TypeError):
                failure = _store_damaged("binding_store")
        raise failure from None

    # -- writes --
    def bind_effect(self, trace: TraceRef | None, assignment, operation: str, *, connection_id: str | None = None,
                    thread_id: str | None = None, lease_usable: bool = True) -> None:
        """Durably bind Attempt + intent (Task execution) and log the effect, in one transaction.

        Without ``trace.attempt_id`` the call is a preparation call: no Attempt
        binding is recorded, only the effect log entry.
        """
        if trace is not None and type(trace) is not TraceRef:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="trace")
        if type(operation) is not str or operation not in EFFECT_OPERATIONS:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="operation")
        if type(lease_usable) is not bool:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="lease_usable")
        mapping, text, digest = _assignment_record(assignment)
        connection = mapping["connection_id"] if connection_id is None else connection_id
        _require_id(connection, "connection_id")
        _require_id(thread_id, "thread_id", optional=True)
        attempt = None
        if trace is not None and trace.attempt_id is not None:
            if trace.intent_id is None:
                raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="trace_intent_id")
            attempt = {
                "vm_attempt_id": trace.attempt_id, "vm_project_id": mapping["vm_project_id"],
                "assignment_json": text, "assignment_digest": digest, "vm_intent_id": trace.intent_id,
                "vm_call_id": trace.call_id, "trace_thread_id": trace.thread_id, "trace_turn_id": trace.turn_id,
            }
        self._call(self._journal.record_effect, connection_id=connection, operation=operation,
                   lease_usable=lease_usable, recorded_at=self._stamp(), thread_id=thread_id, attempt=attempt)

    def record_thread(self, *, connection_id: str, thread_id: str, kind: str, vm_attempt_id: str | None,
                      assignment) -> bool:
        """Record a thread route; True means the thread ID also exists on another connection."""
        _require_id(connection_id, "connection_id")
        _require_id(thread_id, "thread_id")
        _require_id(vm_attempt_id, "vm_attempt_id", optional=True)
        _, text, digest = _assignment_record(assignment)
        return self._call(self._journal.record_thread, connection_id=connection_id, thread_id=thread_id, kind=kind,
                          vm_attempt_id=vm_attempt_id, assignment_json=text, assignment_digest=digest,
                          recorded_at=self._stamp())

    def confirm_owner(self, binding: TaskExecutionBinding) -> str:
        """Persist the owner-confirmed binding; returns its digest. Immutable once stored."""
        if type(binding) is not TaskExecutionBinding:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="binding")
        binding = TaskExecutionBinding.from_mapping(binding.to_mapping())
        stored = self.read_attempt(binding.vm_attempt_id)
        if stored is None:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="attempt_binding")
        for name in HOSTED_ASSIGNMENT_FIELDS:
            if getattr(binding, name) != stored.assignment[name]:
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject=name)
        first = stored.first_intent()
        if first is None or first.vm_intent_id != binding.vm_runtime_intent_id:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="vm_runtime_intent_id")
        if first.vm_call_id != binding.vm_provider_call_id:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="vm_provider_call_id")
        return self._call(self._journal.confirm_owner, vm_attempt_id=binding.vm_attempt_id,
                          binding_json=_canonical(binding.to_mapping()), binding_digest=binding.digest(),
                          confirmed_at=self._stamp())

    def mark_unknown(self, *, attempt_id: str | None = None, thread_id: str | None = None,
                     reason: str = "outcome_unknown") -> None:
        _require_id(attempt_id, "vm_attempt_id", optional=True)
        _require_id(thread_id, "thread_id", optional=True)
        _require_id(reason, "reason")
        if attempt_id is None and thread_id is None:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="target")
        self._call(self._journal.mark_unknown, reason=reason, recorded_at=self._stamp(),
                   vm_attempt_id=attempt_id, thread_id=thread_id)

    def reconcile_attempt(self, attempt_id: str) -> bool:
        """Explicitly clear an Attempt's unknown mark (the caller reconciled it through VM)."""
        _require_id(attempt_id, "vm_attempt_id")
        return self._call(self._journal.reconcile_attempt, vm_attempt_id=attempt_id)

    def reconcile_thread(self, thread_id: str) -> bool:
        """Explicitly clear a thread's unknown mark (the caller reconciled it through VM)."""
        _require_id(thread_id, "thread_id")
        return self._call(self._journal.reconcile_thread, thread_id=thread_id)

    # -- reads --
    def route_for_thread(self, thread_id: str) -> ThreadRoute | None:
        _require_id(thread_id, "thread_id")
        rows = self._call(self._journal.thread_routes, thread_id=thread_id)
        if not rows:
            return None
        if len({row["connection_id"] for row in rows}) != 1:
            raise VmBridgeError(BridgeCode.THREAD_ID_COLLISION, subject="thread_id")
        if len(rows) != 1:
            raise _store_damaged("thread_routes")
        row = rows[0]
        assignment = _parse_stored_assignment(row["assignment_json"], row["assignment_digest"])
        if assignment["connection_id"] != row["connection_id"]:
            raise _store_damaged("thread_routes")
        return ThreadRoute(row["connection_id"], row["thread_id"], row["vm_attempt_id"], row["kind"],
                           assignment, row["assignment_digest"])

    def read_attempt(self, attempt_id: str) -> StoredAttempt | None:
        _require_id(attempt_id, "vm_attempt_id")
        data = self._call(self._journal.attempt, vm_attempt_id=attempt_id)
        if data is None:
            return None
        assignment = _parse_stored_assignment(data["assignment_json"], data["assignment_digest"])
        if assignment["vm_project_id"] != data["vm_project_id"]:
            raise _store_damaged("attempt_bindings")
        intents = tuple(StoredIntent(**item) for item in data["intents"])
        return StoredAttempt(attempt_id, data["vm_project_id"], assignment, data["assignment_digest"],
                             intents, data["unknown"])

    def read_execution_binding(self, attempt_id: str) -> dict | None:
        """``{"vm_attempt_id", "binding_digest"}`` of an owner-confirmed binding, or None."""
        _require_id(attempt_id, "vm_attempt_id")
        data = self._call(self._journal.owner_confirmation, vm_attempt_id=attempt_id)
        if data is None:
            return None
        valid = False
        try:
            binding = TaskExecutionBinding.from_mapping(json.loads(data["binding_json"]))
            valid = binding.vm_attempt_id == attempt_id and binding.digest() == data["binding_digest"]
        except (ValueError, TypeError, VmBridgeError):
            valid = False
        if not valid:
            raise _store_damaged("binding_json") from None
        return {"vm_attempt_id": attempt_id, "binding_digest": data["binding_digest"]}

    @property
    def unknown_attempts(self) -> frozenset:
        return self._call(self._journal.unknown_targets)[0]

    @property
    def unknown_threads(self) -> frozenset:
        return self._call(self._journal.unknown_targets)[1]

    def effect_records(self) -> tuple:
        """Before-effect log entries (metadata only; no prompts or payloads)."""
        return tuple(MappingProxyType(item) for item in self._call(self._journal.effect_log))


# ----- runtime --------------------------------------------------------------

def _member(value: object, name: str):
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _receipt_thread_id(receipt: object) -> str | None:
    candidates = []
    binding = _member(receipt, "binding")
    if binding is not None:
        candidates.append(_member(binding, "thread_id"))
    payload = _member(receipt, "payload")
    if isinstance(payload, Mapping):
        candidates.append(payload.get("thread_id"))
        thread = payload.get("thread")
        if isinstance(thread, Mapping):
            candidates.append(thread.get("id"))
    values = {item for item in candidates if item is not None and type(item) is str}
    if len(values) != 1 or any(item is not None and type(item) is not str for item in candidates):
        return None
    value = values.pop()
    try:
        identifier(value, "thread_id")
    except InvalidRecord:
        return None
    return value


class AikHostedRuntime:
    """A VM ``CodexRuntimePort`` implementation that binds AIK routing before every effect.

    Method names and keyword signatures equal the VM port exactly. Return
    values are the inner runtime's objects, unchanged. Failures are
    ``VmBridgeError`` values without raw messages.
    """

    def __init__(self, runtime_for: Callable[[str], object], store: ExecutionBindingStore,
                 assignment_source: Callable[[Path | None, TraceRef | None], HostedAssignment | None], *,
                 trace_reader: Callable[[], object] | None = None, now: Callable[[], datetime] | None = None):
        if not callable(runtime_for) or not callable(assignment_source):
            raise TypeError("runtime_for and assignment_source must be callable.")
        if not isinstance(store, ExecutionBindingStore):
            raise TypeError("An ExecutionBindingStore is required.")
        if trace_reader is not None and not callable(trace_reader):
            raise TypeError("trace_reader must be callable.")
        if now is not None and not callable(now):
            raise TypeError("now must be callable.")
        self._runtime_for = runtime_for
        self._store = store
        self._assignment_source = assignment_source
        self._trace_reader = trace_reader
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._creating = threading.Lock()
        self._runtimes: dict[str, object] = {}
        self._memory_attempts: set[str] = set()
        self._memory_threads: set[str] = set()
        self._closed = False

    def __repr__(self) -> str:
        return "AikHostedRuntime()"

    def __reduce_ex__(self, protocol):
        raise TypeError("The hosted runtime cannot be serialized.")

    @property
    def store(self) -> ExecutionBindingStore:
        return self._store

    # -- internals --
    def _trace(self) -> TraceRef | None:
        if self._trace_reader is None:
            return read_operation_trace()
        failed = False
        value = None
        try:
            value = self._trace_reader()
        except Exception:
            failed = True
        if failed:
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="trace_scope") from None
        return None if value is None else _trace_from(value)

    @staticmethod
    def _shape(trace: TraceRef | None) -> None:
        if trace is not None and trace.attempt_id is not None and trace.intent_id is None:
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="trace_intent_id")

    def _assignment(self, cwd: Path | None, trace: TraceRef | None) -> HostedAssignment:
        failed = False
        value = None
        try:
            value = self._assignment_source(cwd, trace)
        except Exception:
            failed = True
        if failed:
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="assignment_source") from None
        if value is None:
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="assignment")
        return _rebuild_assignment(value)

    def _runtime(self, connection_id: str) -> object:
        with self._lock:
            if self._closed:
                raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="hosted_runtime_closed")
            existing = self._runtimes.get(connection_id)
        if existing is not None:
            return existing
        with self._creating:
            with self._lock:
                if self._closed:
                    raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="hosted_runtime_closed")
                existing = self._runtimes.get(connection_id)
            if existing is not None:
                return existing
            failed = False
            runtime = None
            try:
                runtime = self._runtime_for(connection_id)
            except Exception:
                failed = True
            if failed or runtime is None:
                raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="connection_runtime") from None
            with self._lock:
                self._runtimes[connection_id] = runtime
            return runtime

    @staticmethod
    def _method(runtime: object, name: str):
        function = getattr(runtime, name, None)
        if not callable(function):
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject=name)
        return function

    def _blocked(self, attempt: str | None, thread: str | None) -> None:
        with self._lock:
            if attempt is not None and attempt in self._memory_attempts:
                raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _UNKNOWN, subject="vm_attempt_id")
            if thread is not None and thread in self._memory_threads:
                raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _UNKNOWN, subject="thread_id")

    def _unknown(self, attempt: str | None, thread: str | None) -> None:
        """Mark unknown in memory, then durably (best effort: memory still blocks on failure)."""
        if attempt is None and thread is None:
            return
        with self._lock:
            if attempt is not None:
                self._memory_attempts.add(attempt)
            if thread is not None:
                self._memory_threads.add(thread)
        try:
            self._store.mark_unknown(attempt_id=attempt, thread_id=thread)
        except VmBridgeError:
            pass

    def _effect_call(self, function, attempt: str | None, thread: str | None, label: str, **kwargs):
        failed = False
        result = None
        try:
            result = function(**kwargs)
        except Exception:
            failed = True
        if not failed:
            return result
        self._unknown(attempt, thread)
        raise VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject=label) from None

    @staticmethod
    def _query_call(function, label: str, **kwargs):
        failed = False
        result = None
        try:
            result = function(**kwargs)
        except Exception:
            failed = True
        if not failed:
            return result
        raise VmBridgeError(BridgeCode.QUERY_FAILED, _KNOWN, subject=label) from None

    def _check_new(self, assignment: HostedAssignment, attempt: str | None) -> None:
        check_assignment(
            placement=assignment.placement, lease=assignment.lease, pool=assignment.pool, now=self._now,
            placement_id=assignment.placement_id, pool_id=assignment.pool_id, lease_id=assignment.lease_id,
            lease_fencing_token=assignment.lease_fencing_token, vm_instance_id=assignment.vm_instance_id,
            vm_project_id=assignment.vm_project_id, vm_attempt_id=attempt,
            connection_id=assignment.connection_id, provider=assignment.provider,
        )

    def _check_current(self, current: HostedAssignment, stored: Mapping[str, Any], attempt: str | None, *,
                       interrupt: bool) -> bool:
        """Same placement/lease/fencing as the original assignment; connection is not compared.

        Returns whether the lease is usable. interrupt tolerates an unusable
        lease (it is recorded), never a different placement/lease/fencing.
        """
        for name in _CURRENT_IDENTITY_FIELDS:
            if getattr(current, name) != stored[name]:
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject=name)
        common = dict(
            placement=current.placement, lease=current.lease, pool=current.pool, now=self._now,
            placement_id=stored["placement_id"], pool_id=stored["pool_id"], lease_id=stored["lease_id"],
            lease_fencing_token=stored["lease_fencing_token"], vm_instance_id=stored["vm_instance_id"],
            vm_project_id=stored["vm_project_id"], vm_attempt_id=attempt,
        )
        check_assignment(**common, require_usable=False)
        if not interrupt:
            check_assignment(**common)
            return True
        try:
            check_assignment(**common)
        except VmBridgeError as error:
            if error.code is BridgeCode.LEASE_NOT_USABLE:
                return False
            raise
        return True

    def _route(self, thread_id: str, trace: TraceRef | None, *, effect: bool) -> ThreadRoute:
        _require_id(thread_id, "thread_id")
        route = self._store.route_for_thread(thread_id)
        if route is None:
            raise VmBridgeError(BridgeCode.THREAD_ROUTE_UNKNOWN, subject="thread_id")
        attempt = None if trace is None else trace.attempt_id
        if effect:
            if route.kind == "task" and attempt != route.vm_attempt_id:
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="vm_attempt_id")
            if route.kind == "preparation" and attempt is not None:
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="vm_attempt_id")
        elif attempt is not None and attempt != route.vm_attempt_id:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="vm_attempt_id")
        if trace is not None and trace.thread_id is not None and trace.thread_id != thread_id:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="thread_id")
        return route

    def _prepare_existing(self, thread_id: str, operation: str, trace: TraceRef | None, cwd: Path | None, *,
                          interrupt: bool):
        self._shape(trace)
        route = self._route(thread_id, trace, effect=True)
        self._blocked(route.vm_attempt_id, thread_id)
        if route.kind == "task":
            stored = self._store.read_attempt(route.vm_attempt_id)
            if stored is None or stored.assignment_digest != route.assignment_digest:
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="assignment")
        current = self._assignment(cwd, trace)
        usable = self._check_current(current, route.assignment, route.vm_attempt_id, interrupt=interrupt)
        function = self._method(self._runtime(route.connection_id), operation)
        self._store.bind_effect(trace if route.kind == "task" else None, route.assignment, operation,
                                connection_id=route.connection_id, thread_id=thread_id, lease_usable=usable)
        return route, function

    # -- CodexRuntimePort --
    def verify_execution_policy(self, cwd):
        trace = self._trace()
        assignment = self._assignment(_path(cwd), trace)
        function = self._method(self._runtime(assignment.connection_id), "verify_execution_policy")
        return self._query_call(function, "verify_execution_policy", cwd=cwd)

    def list_models(self):
        trace = self._trace()
        assignment = self._assignment(None, trace)
        function = self._method(self._runtime(assignment.connection_id), "list_models")
        return self._query_call(function, "list_models")

    def create_thread(self, *, cwd, title, model, developer_instructions, ephemeral=False):
        trace = self._trace()
        self._shape(trace)
        attempt = None if trace is None else trace.attempt_id
        self._blocked(attempt, None)
        assignment = self._assignment(_path(cwd), trace)
        self._check_new(assignment, attempt)
        if attempt is not None:
            # Refuse another assignment for a bound Attempt before even building
            # the runtime of a (possibly rotated) connection.
            stored = self._store.read_attempt(attempt)
            if stored is not None and stored.assignment_digest != assignment.assignment_digest():
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="assignment")
        function = self._method(self._runtime(assignment.connection_id), "create_thread")
        self._store.bind_effect(trace if attempt is not None else None, assignment, "create_thread",
                                connection_id=assignment.connection_id)
        receipt = self._effect_call(function, attempt, None, "create_thread", cwd=cwd, title=title, model=model,
                                    developer_instructions=developer_instructions, ephemeral=ephemeral)
        thread_id = _receipt_thread_id(receipt)
        if thread_id is None:
            self._unknown(attempt, None)
            raise VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject="thread_id")
        recorded = False
        collision = False
        try:
            collision = self._store.record_thread(
                connection_id=assignment.connection_id, thread_id=thread_id,
                kind="task" if attempt is not None else "preparation", vm_attempt_id=attempt, assignment=assignment,
            )
            recorded = True
        except VmBridgeError:
            recorded = False
        if not recorded:
            self._unknown(attempt, thread_id)
            raise VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject="thread_route") from None
        if collision:
            self._unknown(attempt, thread_id)
            raise VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject="thread_id_collision")
        return receipt

    def start_turn(self, *, thread_id, cwd, prompt, model, effort, output_schema=None):
        trace = self._trace()
        route, function = self._prepare_existing(thread_id, "start_turn", trace, _path(cwd), interrupt=False)
        return self._effect_call(function, route.vm_attempt_id, thread_id, "start_turn", thread_id=thread_id,
                                 cwd=cwd, prompt=prompt, model=model, effort=effort, output_schema=output_schema)

    def read(self, *, thread_id):
        trace = self._trace()
        route = self._route(thread_id, trace, effect=False)
        function = self._method(self._runtime(route.connection_id), "read")
        return self._query_call(function, "read", thread_id=thread_id)

    def resume(self, *, thread_id, cwd):
        trace = self._trace()
        route, function = self._prepare_existing(thread_id, "resume", trace, _path(cwd), interrupt=False)
        return self._effect_call(function, route.vm_attempt_id, thread_id, "resume", thread_id=thread_id, cwd=cwd)

    def interrupt(self, *, thread_id, turn_id, timeout_seconds=5.0):
        trace = self._trace()
        route, function = self._prepare_existing(thread_id, "interrupt", trace, None, interrupt=True)
        return self._effect_call(function, route.vm_attempt_id, thread_id, "interrupt", thread_id=thread_id,
                                 turn_id=turn_id, timeout_seconds=timeout_seconds)

    def read_stored(self, *, thread_id, turn_id=None, timeout_seconds=5.0):
        trace = self._trace()
        route = self._route(thread_id, trace, effect=False)
        function = self._method(self._runtime(route.connection_id), "read_stored")
        return self._query_call(function, "read_stored", thread_id=thread_id, turn_id=turn_id,
                                timeout_seconds=timeout_seconds)

    def close(self, *, timeout_seconds=5.0):
        with self._lock:
            self._closed = True
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        failed = False
        for runtime in runtimes:
            function = getattr(runtime, "close", None)
            if not callable(function):
                continue
            try:
                function(timeout_seconds=timeout_seconds)
            except Exception:
                failed = True
        if failed:
            raise VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject="close") from None

    # -- reconciliation --
    def reconcile_attempt(self, vm_attempt_id: str) -> bool:
        """Explicitly clear an Attempt's unknown mark after reconciling it through VM."""
        _require_id(vm_attempt_id, "vm_attempt_id")
        cleared = self._store.reconcile_attempt(vm_attempt_id)
        with self._lock:
            present = vm_attempt_id in self._memory_attempts
            self._memory_attempts.discard(vm_attempt_id)
        return cleared or present

    def reconcile_thread(self, thread_id: str) -> bool:
        """Explicitly clear a thread's unknown mark after reconciling it through VM."""
        _require_id(thread_id, "thread_id")
        cleared = self._store.reconcile_thread(thread_id)
        with self._lock:
            present = thread_id in self._memory_threads
            self._memory_threads.discard(thread_id)
        return cleared or present

    @property
    def unknown_attempts(self) -> frozenset:
        with self._lock:
            memory = frozenset(self._memory_attempts)
        return memory | self._store.unknown_attempts

    @property
    def unknown_threads(self) -> frozenset:
        with self._lock:
            memory = frozenset(self._memory_threads)
        return memory | self._store.unknown_threads
