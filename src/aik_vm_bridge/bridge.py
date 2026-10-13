"""Typed command/query bridge over an injected VM EngineApplication port.

AIK never edits the VM ledger: every Goal/Plan/Task change is a public façade
command on the port. Responses are preserved as VM-owned snapshots and VM IDs
are only referenced. ``run_once`` is treated as "reserve/observe then return";
an unclear result is UNKNOWN and is never relaunched automatically.
Duplicate-request, in-flight and reconciliation guards are per bridge
instance and in memory only; they are not durable and not shared.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re
import threading
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import Lease, Placement, Pool, identifier

from .binding import DIGEST_PATTERN, TaskExecutionBinding, verify_assignment
from .errors import BridgeCode, EffectOutcome, VmBridgeError
from .surface import (
    BINDING_EXTENSION, CapabilityReport, Command, DeclaredSurface, Handshake, check_handshake,
    load_surface, preflight,
)


_VM_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_STATUS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_KNOWN = EffectOutcome.KNOWN_FAILURE
_UNKNOWN = EffectOutcome.UNKNOWN
# VM codes treated as "rejected before any effect" on effectful calls. This is
# an assumption that has not been checked against the installed VM's error
# table; every other code on an effectful call is reported as UNKNOWN.
_PRE_EFFECT_VM_CODES = frozenset({"STALE_EXECUTION_INPUT", "CORE_CAPABILITY_DENIED", "MODEL_BINDING_CHANGED"})
_OWNER_REQUIRED = ("project_id", "task_id", "attempt_id", "goal_revision_id", "plan_revision_id",
                   "execution_spec_digest")
_RUN_REQUIRED = ("task_id", "attempt_id")


def _id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject=field) from None


def _optional_id(value: object, field: str) -> None:
    if value is not None:
        _id(value, field)


def _text(value: object, field: str, limit: int) -> None:
    if type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject=field)


def _binding(value: object, project_id: str | None) -> None:
    if type(value) is not TaskExecutionBinding:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="binding")
    if project_id is not None and value.vm_project_id != project_id:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="project_id")


# ----- typed requests -------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GoalRequest:
    """prepare / revise: project-level preparation; no Task or Attempt exists."""

    request_id: str
    project_id: str
    source_request: str
    candidate_count: int | None = None

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        _text(self.source_request, "source_request", 100_000)
        if self.candidate_count is not None and (type(self.candidate_count) is not int or not 1 <= self.candidate_count <= 64):
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="candidate_count")


@dataclass(frozen=True, slots=True)
class AuthorizeRequest:
    request_id: str
    project_id: str
    source: str
    selected_plan_revision_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        _text(self.source, "source", 256)
        _optional_id(self.selected_plan_revision_id, "selected_plan_revision_id")


@dataclass(frozen=True, slots=True)
class RunOnceRequest:
    request_id: str
    binding: TaskExecutionBinding
    placement: Placement
    lease: Lease
    pool: Pool
    resume: bool = False

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _binding(self.binding, None)
        if type(self.resume) is not bool:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="resume")


@dataclass(frozen=True, slots=True)
class ObserveRequest:
    request_id: str
    project_id: str
    binding: TaskExecutionBinding | None = None

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        if self.binding is not None:
            _binding(self.binding, self.project_id)


@dataclass(frozen=True, slots=True)
class CancelRequest:
    request_id: str
    project_id: str
    reason: str
    binding: TaskExecutionBinding | None = None

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        _text(self.reason, "reason", 2_000)
        if self.binding is not None:
            _binding(self.binding, self.project_id)


@dataclass(frozen=True, slots=True)
class PauseRequest:
    request_id: str
    project_id: str
    reason: str

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        _text(self.reason, "reason", 2_000)


@dataclass(frozen=True, slots=True)
class ReplanRequest:
    request_id: str
    project_id: str
    rationale: str

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        _text(self.rationale, "rationale", 20_000)


@dataclass(frozen=True, slots=True)
class ProjectQuery:
    request_id: str
    project_id: str

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")


@dataclass(frozen=True, slots=True)
class FinalReportRequest:
    request_id: str
    project_id: str
    goal_verdict_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
        _optional_id(self.goal_verdict_id, "goal_verdict_id")


@dataclass(frozen=True, slots=True)
class AttemptQuery:
    request_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.attempt_id, "attempt_id")


@dataclass(frozen=True, slots=True)
class InventoryRequest:
    request_id: str
    binding: TaskExecutionBinding

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _binding(self.binding, None)


# ----- receipts -------------------------------------------------------------

class JobPhase(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    TERMINAL = "terminal"  # provider/job terminal, never a VM completion verdict
    UNKNOWN = "unknown"


_PENDING = frozenset({"queued", "pending", "reserved", "prepared"})
_RUNNING = frozenset({"running", "started", "in_progress"})
_TERMINAL = frozenset({"succeeded", "completed", "failed", "cancelled", "interrupted", "proven_no_effect", "expired"})


@dataclass(frozen=True, slots=True)
class JobObservation:
    phase: JobPhase
    vm_status: str | None = None
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class VmRefs:
    """VM-owned identifiers copied verbatim from a façade response."""

    project_id: str | None = None
    goal_id: str | None = None
    goal_revision_id: str | None = None
    plan_revision_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    job_id: str | None = None
    authorization_id: str | None = None
    execution_spec_digest: str | None = None
    history_cursor: int | None = None


@dataclass(frozen=True, slots=True)
class BridgeReceipt:
    command: Command
    request_id: str
    project_id: str | None
    refs: VmRefs
    payload: Mapping[str, Any]
    binding_digest: str | None = None
    job: JobObservation | None = None


class _FrozenList(list):
    """A list snapshot whose ordinary mutating methods are refused.

    It still compares equal to plain lists. Copies (copy/pickle) are plain lists.
    """

    __slots__ = ()

    def _readonly(self, *args, **kwargs):
        raise TypeError("VM response snapshots are read-only.")

    append = extend = insert = remove = pop = clear = sort = reverse = _readonly
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _readonly

    def __reduce_ex__(self, protocol):
        return (list, (list(self),))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    if type(value) is tuple:
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    return value


_REF_ID_KEYS = ("project_id", "goal_id", "goal_revision_id", "plan_revision_id", "task_id",
                "attempt_id", "job_id", "authorization_id")


def _extract_refs(response: Mapping[str, Any], outcome: EffectOutcome) -> VmRefs:
    values: dict[str, Any] = {}
    for key in _REF_ID_KEYS:
        value = response.get(key)
        if value is not None:
            try:
                identifier(value, key)
            except InvalidRecord:
                raise VmBridgeError(BridgeCode.INVALID_RESPONSE, outcome, subject=key) from None
            values[key] = value
    digest = response.get("execution_spec_digest")
    if digest is not None:
        if type(digest) is not str or DIGEST_PATTERN.fullmatch(digest) is None:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, outcome, subject="execution_spec_digest")
        values["execution_spec_digest"] = digest
    cursor = response.get("history_cursor")
    if cursor is not None:
        if type(cursor) is not int or cursor < 0:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, outcome, subject="history_cursor")
        values["history_cursor"] = cursor
    return VmRefs(**values)


def _parse_job(response: Mapping[str, Any]) -> JobObservation:
    job = response.get("job")
    if isinstance(job, Mapping):
        status, job_id = job.get("status"), job.get("job_id")
    else:
        status, job_id = response.get("job_status"), response.get("job_id")
    try:
        identifier(job_id, "job_id")
    except InvalidRecord:
        job_id = None
    if type(status) is not str or _STATUS.fullmatch(status) is None:
        return JobObservation(JobPhase.UNKNOWN, None, job_id)
    phase = (JobPhase.PENDING if status in _PENDING else JobPhase.RUNNING if status in _RUNNING
             else JobPhase.TERMINAL if status in _TERMINAL else JobPhase.UNKNOWN)
    return JobObservation(phase, status, job_id)


def _same_execution(refs: VmRefs, binding: TaskExecutionBinding, outcome: EffectOutcome,
                    required: tuple[str, ...] = ()) -> None:
    """Reject disagreeing fields; ``required`` fields must also be present."""
    pairs = (
        ("project_id", refs.project_id, binding.vm_project_id),
        ("task_id", refs.task_id, binding.vm_task_id),
        ("attempt_id", refs.attempt_id, binding.vm_attempt_id),
        ("goal_revision_id", refs.goal_revision_id, binding.vm_goal_revision_id),
        ("plan_revision_id", refs.plan_revision_id, binding.vm_plan_revision_id),
        ("execution_spec_digest", refs.execution_spec_digest, binding.vm_execution_spec_digest),
    )
    for subject, observed, bound in pairs:
        if observed is not None and observed != bound:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, outcome, subject=subject)
    for subject, observed, _ in pairs:
        if observed is None and subject in required:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, outcome, subject=subject)


def _mapped(error: Exception, label: str, effectful: bool) -> VmBridgeError:
    """Keep the VM's original code; never copy raw messages or tracebacks."""
    code = getattr(error, "code", None)
    if type(code) is str and _VM_CODE.fullmatch(code):
        if effectful:
            # Only codes known to mean "rejected before any effect" are known
            # failures; any other coded failure of an effect may have acted.
            outcome = _KNOWN if code in _PRE_EFFECT_VM_CODES else _UNKNOWN
        else:
            outcome = _UNKNOWN if "UNKNOWN" in code.split("_") else _KNOWN
        return VmBridgeError(BridgeCode.VM_REJECTED, outcome, vm_code=code, subject=label)
    if effectful:
        return VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject=label)
    return VmBridgeError(BridgeCode.QUERY_FAILED, _KNOWN, subject=label)


# ----- ports ----------------------------------------------------------------

class VmApplicationPort(Protocol):
    """Adapter over the installed public EngineApplication; returns plain mappings.

    ``handshake`` describes the installed façade (surface revision, engine
    schema revision, method signatures, declared extensions).
    """

    def handshake(self) -> Mapping[str, Any]: ...
    def prepare(self, project_id: str, *, source_request: str, candidate_count: int | None = None) -> Mapping[str, Any]: ...
    def revise(self, project_id: str, *, source_request: str, candidate_count: int | None = None) -> Mapping[str, Any]: ...
    def authorize(self, project_id: str, *, source: str, selected_plan_revision_id: str | None = None) -> Mapping[str, Any]: ...
    def run_once(self, project_id: str, *, resume: bool = False) -> Mapping[str, Any]: ...
    def observe(self, project_id: str) -> Mapping[str, Any]: ...
    def pause(self, project_id: str, *, reason: str) -> Mapping[str, Any]: ...
    def cancel(self, project_id: str, *, reason: str) -> Mapping[str, Any]: ...
    def status(self, project_id: str) -> Mapping[str, Any]: ...
    def replan(self, project_id: str, *, rationale: str) -> Mapping[str, Any]: ...
    def final_report(self, project_id: str, *, goal_verdict_id: str | None = None) -> Mapping[str, Any]: ...
    def attempt_detail(self, attempt_id: str) -> Mapping[str, Any]: ...
    def model_binding_status(self, project_id: str) -> Mapping[str, Any]: ...


class VmBindingExtensionPort(Protocol):
    """Required upstream extension for durable pre-effect binding (not in VM today)."""

    def bind_execution(self, binding: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def read_execution_binding(self, vm_attempt_id: str) -> Mapping[str, Any] | None: ...


# ----- bridge ---------------------------------------------------------------

class VmBridge:
    def __init__(self, port: object, surface: DeclaredSurface | Mapping[str, Any] | str, *,
                 commands: Iterable[Command | str] | None = None,
                 now: Callable[[], datetime] | None = None):
        self._surface = surface if isinstance(surface, DeclaredSurface) else load_surface(surface)
        try:
            selected = tuple(dict.fromkeys(Command(c) for c in (tuple(Command) if commands is None else commands)))
        except (TypeError, ValueError):
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="commands") from None
        if not selected:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="commands")
        preflight(self._surface, selected).require()
        absent = [c.value for c in selected if not callable(getattr(port, c.value, None))]
        if not callable(getattr(port, "handshake", None)):
            absent.insert(0, "handshake")
        if absent:
            raise VmBridgeError(BridgeCode.CAPABILITY_MISSING, subject=absent[0], details=tuple(absent))
        self._port = port
        self._commands = selected
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._handshake: Handshake | None = None
        self._lock = threading.RLock()
        self._used_requests: set[str] = set()
        self._unresolved: set[str] = set()
        self._unresolved_projects: set[str] = set()
        self._inflight: set[str] = set()
        self._terminal_observed: set[str] = set()

    # -- capability & version --
    def preflight(self) -> CapabilityReport:
        return preflight(self._surface, self._commands)

    def handshake(self) -> Handshake:
        try:
            raw = self._port.handshake()
        except Exception as error:
            raise _mapped(error, "handshake", False) from None
        self._handshake = check_handshake(self._surface, raw, self._commands)
        return self._handshake

    @property
    def unresolved_attempts(self) -> frozenset:
        """Attempts whose effect state is UNKNOWN; run_once refuses them until a terminal observation."""
        with self._lock:
            return frozenset(self._unresolved)

    @property
    def unresolved_projects(self) -> frozenset:
        """Projects with an UNKNOWN project-level effect; run_once refuses them until a terminal observation."""
        with self._lock:
            return frozenset(self._unresolved_projects)

    def _ready(self, *commands: Command) -> Handshake:
        for command in commands:
            if command not in self._commands:
                raise VmBridgeError(BridgeCode.CAPABILITY_MISSING, subject=command.value)
        return self._handshake if self._handshake is not None else self.handshake()

    def _claim(self, request_id: str) -> None:
        # Once claimed (just before the first port call of the operation), a
        # request ID is never reused, including after failures. Local
        # pre-rejections before the claim do not consume it. IDs stay local.
        with self._lock:
            if request_id in self._used_requests:
                raise VmBridgeError(BridgeCode.DUPLICATE_REQUEST, subject="request_id")
            self._used_requests.add(request_id)

    def _mark_unknown(self, attempt: str | None, project: str | None) -> None:
        with self._lock:
            if attempt is not None:
                self._unresolved.add(attempt)
            if project is not None:
                self._unresolved_projects.add(project)

    def _require_extension(self, handshake: Handshake) -> None:
        if (BINDING_EXTENSION not in handshake.extensions
                or not callable(getattr(self._port, "bind_execution", None))
                or not callable(getattr(self._port, "read_execution_binding", None))):
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="aik-bound-runtime")

    def _call(self, label: str, effectful: bool, function, *args, **kwargs) -> dict:
        outcome = _UNKNOWN if effectful else _KNOWN
        try:
            response = function(*args, **kwargs)
        except Exception as error:
            raise _mapped(error, label, effectful) from None
        if not isinstance(response, Mapping):
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, outcome, subject=label)
        try:
            return copy.deepcopy(dict(response))
        except Exception:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, outcome, subject=label) from None

    def _effect(self, attempt: str | None, project: str | None, label: str, function, *args, **kwargs) -> dict:
        try:
            return self._call(label, True, function, *args, **kwargs)
        except VmBridgeError as error:
            if error.outcome is _UNKNOWN:
                self._mark_unknown(attempt, project)
            raise

    def _receipt(self, command: Command, request_id: str, project_id: str | None, response: dict, *,
                 effectful: bool, binding: TaskExecutionBinding | None = None, with_job: bool = False) -> BridgeReceipt:
        outcome = _UNKNOWN if effectful else _KNOWN
        refs = _extract_refs(response, outcome)
        if project_id is not None and refs.project_id is not None and refs.project_id != project_id:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, outcome, subject="project_id")
        return BridgeReceipt(
            command, request_id, project_id, refs, _freeze(response),
            None if binding is None else binding.digest(), _parse_job(response) if with_job else None,
        )

    def _persisted_digest(self, attempt: str) -> str | None:
        """The binding digest VM persisted for the Attempt, or None if absent."""
        try:
            persisted = self._port.read_execution_binding(attempt)
        except Exception as error:
            raise _mapped(error, "read_execution_binding", False) from None
        if persisted is None:
            return None
        if not isinstance(persisted, Mapping):
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="read_execution_binding")
        digest = persisted.get("binding_digest")
        if type(digest) is not str or DIGEST_PATTERN.fullmatch(digest) is None:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="read_execution_binding")
        return digest

    def _require_persisted(self, binding: TaskExecutionBinding) -> None:
        """Observe/cancel/inventory only with the persisted original binding."""
        self._require_extension(self._handshake)
        if self._persisted_digest(binding.vm_attempt_id) != binding.digest():
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="persisted_binding")

    @staticmethod
    def _check(request: object, expected: type) -> None:
        if type(request) is not expected:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="request")

    # -- project-level commands (no Task/Attempt is manufactured) --
    def _project_command(self, command: Command, request, *args, **kwargs) -> BridgeReceipt:
        self._ready(command)
        self._claim(request.request_id)
        response = self._effect(None, request.project_id, command.value, getattr(self._port, command.value),
                                *args, **kwargs)
        try:
            return self._receipt(command, request.request_id, request.project_id, response, effectful=True)
        except VmBridgeError as error:
            if error.outcome is _UNKNOWN:
                self._mark_unknown(None, request.project_id)
            raise

    def prepare(self, request: GoalRequest) -> BridgeReceipt:
        self._check(request, GoalRequest)
        return self._project_command(Command.PREPARE, request, request.project_id,
                                     source_request=request.source_request, candidate_count=request.candidate_count)

    def revise(self, request: GoalRequest) -> BridgeReceipt:
        self._check(request, GoalRequest)
        return self._project_command(Command.REVISE, request, request.project_id,
                                     source_request=request.source_request, candidate_count=request.candidate_count)

    def authorize(self, request: AuthorizeRequest) -> BridgeReceipt:
        self._check(request, AuthorizeRequest)
        return self._project_command(Command.AUTHORIZE, request, request.project_id, source=request.source,
                                     selected_plan_revision_id=request.selected_plan_revision_id)

    def pause(self, request: PauseRequest) -> BridgeReceipt:
        self._check(request, PauseRequest)
        return self._project_command(Command.PAUSE, request, request.project_id, reason=request.reason)

    def replan(self, request: ReplanRequest) -> BridgeReceipt:
        self._check(request, ReplanRequest)
        return self._project_command(Command.REPLAN, request, request.project_id, rationale=request.rationale)

    # -- queries --
    def status(self, request: ProjectQuery) -> BridgeReceipt:
        self._check(request, ProjectQuery)
        self._ready(Command.STATUS)
        self._claim(request.request_id)
        response = self._call("status", False, self._port.status, request.project_id)
        return self._receipt(Command.STATUS, request.request_id, request.project_id, response, effectful=False)

    def final_report(self, request: FinalReportRequest) -> BridgeReceipt:
        self._check(request, FinalReportRequest)
        self._ready(Command.FINAL_REPORT)
        self._claim(request.request_id)
        response = self._call("final_report", False, self._port.final_report, request.project_id,
                              goal_verdict_id=request.goal_verdict_id)
        return self._receipt(Command.FINAL_REPORT, request.request_id, request.project_id, response, effectful=False)

    def attempt_detail(self, request: AttemptQuery) -> BridgeReceipt:
        self._check(request, AttemptQuery)
        self._ready(Command.ATTEMPT_DETAIL)
        self._claim(request.request_id)
        response = self._call("attempt_detail", False, self._port.attempt_detail, request.attempt_id)
        receipt = self._receipt(Command.ATTEMPT_DETAIL, request.request_id, None, response, effectful=False)
        if receipt.refs.attempt_id is not None and receipt.refs.attempt_id != request.attempt_id:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="attempt_id")
        return receipt

    def inventory(self, request: InventoryRequest) -> BridgeReceipt:
        self._check(request, InventoryRequest)
        self._ready(Command.MODEL_BINDING_STATUS)
        self._claim(request.request_id)
        self._require_persisted(request.binding)
        project_id = request.binding.vm_project_id
        response = self._call("model_binding_status", False, self._port.model_binding_status, project_id)
        return self._receipt(Command.MODEL_BINDING_STATUS, request.request_id, project_id, response,
                             effectful=False, binding=request.binding)

    # -- bound execution --
    def run_once(self, request: RunOnceRequest) -> BridgeReceipt:
        self._check(request, RunOnceRequest)
        handshake = self._ready(Command.RUN_ONCE, Command.ATTEMPT_DETAIL)
        self._require_extension(handshake)
        binding = request.binding
        attempt = binding.vm_attempt_id
        project = binding.vm_project_id
        verify_assignment(binding, placement=request.placement, lease=request.lease, pool=request.pool, now=self._now)
        with self._lock:
            if attempt in self._unresolved:
                raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _UNKNOWN, subject="vm_attempt_id")
            if project in self._unresolved_projects:
                raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _UNKNOWN, subject="vm_project_id")
            if attempt in self._inflight:
                raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _KNOWN, subject="attempt_in_flight")
            self._claim(request.request_id)
            self._inflight.add(attempt)
        try:
            return self._run_bound(request, binding, attempt)
        finally:
            with self._lock:
                self._inflight.discard(attempt)

    def _run_bound(self, request: RunOnceRequest, binding: TaskExecutionBinding, attempt: str) -> BridgeReceipt:
        # Owner query: the exact existing Attempt and its Task, before any effect.
        # Every compared field must be present; absence is not agreement.
        detail = self._call("attempt_detail", False, self._port.attempt_detail, attempt)
        owner = _extract_refs(detail, _KNOWN)
        if owner.attempt_id is None or owner.task_id is None:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="attempt_detail")
        _same_execution(owner, binding, _KNOWN, _OWNER_REQUIRED)

        digest = binding.digest()
        persisted = self._persisted_digest(attempt)
        if persisted is None:
            echo = self._effect(attempt, None, "bind_execution", self._port.bind_execution, binding.to_mapping())
            if echo.get("binding_digest") != digest:
                self._mark_unknown(attempt, None)
                raise VmBridgeError(BridgeCode.BINDING_MISMATCH, _UNKNOWN, subject="bind_execution")
        elif persisted != digest:
            # Another binding owns this Attempt; never rebind or relaunch it.
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, _KNOWN, subject="persisted_binding")
        elif attempt not in self._terminal_observed:
            # Already bound with this binding; relaunch only after this bridge
            # has observed a terminal job for it (resume does not bypass this).
            raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _KNOWN, subject="attempt_bound")
        with self._lock:
            self._terminal_observed.discard(attempt)

        response = self._effect(attempt, None, "run_once", self._port.run_once, binding.vm_project_id,
                                resume=request.resume)
        try:
            receipt = self._receipt(Command.RUN_ONCE, request.request_id, binding.vm_project_id, response,
                                    effectful=True, binding=binding, with_job=True)
            _same_execution(receipt.refs, binding, _UNKNOWN, _RUN_REQUIRED)
        except VmBridgeError:
            self._mark_unknown(attempt, None)
            raise
        if receipt.job.phase is JobPhase.UNKNOWN:
            self._mark_unknown(attempt, None)
        return receipt

    def observe(self, request: ObserveRequest) -> BridgeReceipt:
        self._check(request, ObserveRequest)
        self._ready(Command.OBSERVE)
        self._claim(request.request_id)
        binding = request.binding
        if binding is not None:
            self._require_persisted(binding)
        response = self._call("observe", False, self._port.observe, request.project_id)
        receipt = self._receipt(Command.OBSERVE, request.request_id, request.project_id, response,
                                effectful=False, binding=binding, with_job=True)
        terminal = receipt.job.phase is JobPhase.TERMINAL
        if binding is not None and receipt.refs.attempt_id == binding.vm_attempt_id:
            _same_execution(receipt.refs, binding, _KNOWN)
            if terminal:
                # Pending/running keeps the guard: the job may still act.
                with self._lock:
                    self._unresolved.discard(binding.vm_attempt_id)
                    self._terminal_observed.add(binding.vm_attempt_id)
        if terminal:
            with self._lock:
                self._unresolved_projects.discard(request.project_id)
        return receipt

    def cancel(self, request: CancelRequest) -> BridgeReceipt:
        self._check(request, CancelRequest)
        self._ready(Command.CANCEL)
        self._claim(request.request_id)
        binding = request.binding
        if binding is not None:
            self._require_persisted(binding)
        attempt = None if binding is None else binding.vm_attempt_id
        # cancel is a project-level façade call, so an unclear result also
        # blocks run_once for the whole project.
        response = self._effect(attempt, request.project_id, "cancel", self._port.cancel, request.project_id,
                                reason=request.reason)
        try:
            return self._receipt(Command.CANCEL, request.request_id, request.project_id, response,
                                 effectful=True, binding=binding)
        except VmBridgeError as error:
            if error.outcome is _UNKNOWN:
                self._mark_unknown(attempt, request.project_id)
            raise
