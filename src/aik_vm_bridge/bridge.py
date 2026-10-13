"""Typed command/query bridge over an injected VM EngineApplication port.

AIK never edits the VM ledger: every Goal/Plan/Task change is a public façade
command on the port. Responses are preserved as VM-owned snapshots and VM IDs
are only referenced. ``run_once`` is treated as "reserve/observe then return";
an unclear result is UNKNOWN and is never relaunched automatically.
Duplicate-request, in-flight and reconciliation guards are per bridge
instance and in memory only; they are not durable and not shared.

Job states are interpreted against the upstream ``RuntimeJobStatus`` values of
the pinned revision (``JOB_STATUS_PHASES``). Any other value is UNKNOWN.
Only a Core-consumed observation (``consumed``) of the latest job launched for
an Attempt can release that Attempt's guard; ``provider_terminal`` never does.
Project-level guards are never released by an observation; only an explicit
``reconcile_project`` call clears them.

Two bound-execution paths exist:

* The extension path (``run_once`` with ``RunOnceRequest``) needs an upstream
  ``aik-task-execution-binding-v1`` extension that VM does not have; it stays
  fail-closed against the real VM.
* The hosted path (``run_once_hosted`` and ``confirm_attempt``) is active when
  the bridge has a ``binding_store`` and the handshake declares
  ``aik-hosted-runtime-v1``. VM reserves the Attempt itself; the injected
  ``AikHostedRuntime`` binds the AIK assignment durably before the first
  provider effect; ``confirm_attempt`` completes the owner-confirmed
  ``TaskExecutionBinding`` from ``attempt_detail`` joined with ``status``. In
  hosted mode, observe/cancel/inventory compare against that confirmed binding.
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
from .engine_port import ADAPTER_CODES, INVALID_ENGINE_RESPONSE, PROJECT_MISMATCH, EnginePortError
from .errors import BridgeCode, EffectOutcome, VmBridgeError
from .hosted_runtime import HOSTED_ASSIGNMENT_FIELDS, ExecutionBindingStore
from .surface import (
    BINDING_EXTENSION, HOSTED_RUNTIME_EXTENSION, CapabilityReport, Command, DeclaredSurface, Handshake,
    check_handshake, load_surface, preflight,
)


_VM_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_STATUS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_KNOWN = EffectOutcome.KNOWN_FAILURE
_UNKNOWN = EffectOutcome.UNKNOWN
# VM codes treated as "rejected before any effect" on effectful calls. This is
# an assumption that has not been checked against the installed VM's error
# table; every other code on an effectful call is reported as UNKNOWN.
_PRE_EFFECT_VM_CODES = frozenset({"STALE_EXECUTION_INPUT", "CORE_CAPABILITY_DENIED", "MODEL_BINDING_CHANGED"})
_ADAPTER_RESPONSE_CODES = frozenset({INVALID_ENGINE_RESPONSE, PROJECT_MISMATCH})
_OWNER_REQUIRED = ("project_id", "task_id", "attempt_id", "goal_revision_id", "plan_revision_id",
                   "execution_spec_digest")
_RUN_REQUIRED = ("task_id", "attempt_id")
# Hosted owner query: VM AttemptDetail has no goal revision; it is joined from status.
_HOSTED_OWNER_REQUIRED = ("project_id", "attempt_id", "task_id", "execution_spec_digest", "plan_revision_id")
_NESTED_ATTEMPT_KEYS = ("attempt_id", "task_id", "execution_spec_digest")
HOSTED_STATES = ("confirmed", "binding_pending")


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
class HostedRunOnceRequest:
    """run_once on the hosted path: VM reserves the Attempt; AIK binds in its runtime."""

    request_id: str
    project_id: str
    resume: bool = False

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.project_id, "project_id")
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
    # Provider/job terminal (display phase). Never a VM completion verdict and,
    # for provider_terminal, never a reason to release a relaunch guard.
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


# Upstream RuntimeJobStatus (vibemarshal 32bb0f9, domain.py) -> bridge phase.
# - provider_terminal: the provider turn ended. Core has not consumed it, and
#   it is not proof of a completed or confirmed external effect. Its phase is
#   TERMINAL for display only; it never releases a guard.
# - consumed: Core consumed the job result (core_consumed=True). This is the
#   only observation that may release an Attempt guard.
# - collector_lost: the collector lost the provider turn; it may still act.
# - cancelled: Core may still have to settle a cancelled job
#   (unsettled_cancelled_runtime_job), so the effect state is not settled.
# Both of the latter are UNKNOWN on purpose: they never release a guard.
JOB_STATUS_PHASES = MappingProxyType({
    "scheduled": JobPhase.PENDING,
    "running": JobPhase.RUNNING,
    "interrupting": JobPhase.RUNNING,
    "provider_terminal": JobPhase.TERMINAL,
    "collector_lost": JobPhase.UNKNOWN,
    "consumed": JobPhase.TERMINAL,
    "cancelled": JobPhase.UNKNOWN,
})


@dataclass(frozen=True, slots=True)
class JobObservation:
    phase: JobPhase
    vm_status: str | None = None
    job_id: str | None = None

    @property
    def core_consumed(self) -> bool:
        """True only when VM reports ``consumed``; ``provider_terminal`` is not consumption."""
        return self.vm_status == "consumed"


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


@dataclass(frozen=True, slots=True)
class HostedConfirmation:
    """Result of ``confirm_attempt``.

    ``confirmed``: the owner-confirmed binding is stored in the AIK journal.
    ``binding_pending``: no provider effect has been bound for the Attempt yet;
    this is not a success claim. Neither state is a VM completion verdict.
    """

    state: str
    vm_project_id: str
    vm_attempt_id: str
    vm_task_id: str | None = None
    binding_digest: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not str or self.state not in HOSTED_STATES:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="state")


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


def _owner_view(detail: Mapping[str, Any]) -> dict:
    """Top-level owner fields, completed from a nested ``attempt`` mapping when absent."""
    view = dict(detail)
    nested = detail.get("attempt")
    if isinstance(nested, Mapping):
        for key in _NESTED_ATTEMPT_KEYS:
            value = nested.get(key)
            if value is None:
                continue
            if view.get(key) is None:
                view[key] = value
            elif view[key] != value:
                raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject=key)
    return view


def _active_revision(status: Mapping[str, Any], key: str) -> str | None:
    """An active revision ID from the top level or ``project``; both must agree."""
    candidates = [status.get(key)]
    project = status.get("project")
    if isinstance(project, Mapping):
        candidates.append(project.get(key))
    values = [item for item in candidates if item is not None]
    for item in values:
        try:
            identifier(item, key)
        except InvalidRecord:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject=key) from None
    if len(set(values)) > 1:
        raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject=key)
    return values[0] if values else None


def _parse_job(response: Mapping[str, Any]) -> JobObservation:
    """Read ``job``, the upstream ``runtime_job`` dump, or RunOnceResult fields."""
    job = response.get("job")
    if not isinstance(job, Mapping):
        runtime = response.get("runtime_job")
        job = runtime if isinstance(runtime, Mapping) else None
    if job is not None:
        status, job_id = job.get("status"), job.get("job_id")
    elif response.get("runtime_job_status") is not None or response.get("runtime_job_id") is not None:
        status, job_id = response.get("runtime_job_status"), response.get("runtime_job_id")
    else:
        status, job_id = response.get("job_status"), response.get("job_id")
    try:
        identifier(job_id, "job_id")
    except InvalidRecord:
        job_id = None
    if type(status) is not str or _STATUS.fullmatch(status) is None:
        return JobObservation(JobPhase.UNKNOWN, None, job_id)
    return JobObservation(JOB_STATUS_PHASES.get(status, JobPhase.UNKNOWN), status, job_id)


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
    """Keep the VM's original code; never copy raw messages or tracebacks.

    Classifications made by ``engine_port`` (``ADAPTER_CODES``) are not VM
    codes, so they are never reported as ``vm_code``.
    """
    code = getattr(error, "code", None)
    if isinstance(error, EnginePortError) and code in ADAPTER_CODES:
        if code in _ADAPTER_RESPONSE_CODES:
            return VmBridgeError(BridgeCode.INVALID_RESPONSE, _UNKNOWN if effectful else _KNOWN, subject=label)
        code = None
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


def _store_call(function, *args):
    """Call the binding store; keep its fixed-code errors, classify anything else."""
    failure = None
    try:
        return function(*args)
    except VmBridgeError as error:
        failure = error
    except Exception:
        failure = VmBridgeError(BridgeCode.BINDING_STORE_FAILED, subject="binding_store")
    raise failure from None


# ----- ports ----------------------------------------------------------------

class VmApplicationPort(Protocol):
    """Adapter over the installed public EngineApplication; returns plain mappings.

    ``handshake`` describes the installed façade (surface revision, engine
    schema revision, method signatures, declared extensions).
    ``engine_port.EngineApplicationPort`` implements this contract for the
    pinned upstream revision.
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
                 now: Callable[[], datetime] | None = None,
                 binding_store: ExecutionBindingStore | None = None):
        self._surface = surface if isinstance(surface, DeclaredSurface) else load_surface(surface)
        try:
            selected = tuple(dict.fromkeys(Command(c) for c in (tuple(Command) if commands is None else commands)))
        except (TypeError, ValueError):
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="commands") from None
        if not selected:
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="commands")
        if binding_store is not None and not isinstance(binding_store, ExecutionBindingStore):
            raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="binding_store")
        preflight(self._surface, selected).require()
        absent = [c.value for c in selected if not callable(getattr(port, c.value, None))]
        if not callable(getattr(port, "handshake", None)):
            absent.insert(0, "handshake")
        if absent:
            raise VmBridgeError(BridgeCode.CAPABILITY_MISSING, subject=absent[0], details=tuple(absent))
        self._port = port
        self._commands = selected
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._binding_store = binding_store
        self._handshake: Handshake | None = None
        self._lock = threading.RLock()
        self._used_requests: set[str] = set()
        self._unresolved: set[str] = set()
        self._unresolved_projects: set[str] = set()
        self._inflight: set[str] = set()
        # Attempts whose latest launched job this bridge observed as consumed.
        self._terminal_observed: set[str] = set()
        # Per-Attempt launch generation and the job ID of the latest launch
        # (None when the launch result did not identify a job).
        self._launch_generation: dict[str, int] = {}
        self._launched_job: dict[str, str | None] = {}

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
    def binding_store(self) -> ExecutionBindingStore | None:
        return self._binding_store

    @property
    def unresolved_attempts(self) -> frozenset:
        """Attempts whose effect state is UNKNOWN; run_once refuses them until a Core-consumed observation."""
        with self._lock:
            return frozenset(self._unresolved)

    @property
    def unresolved_projects(self) -> frozenset:
        """Projects with an UNKNOWN project-level effect; run_once refuses them until ``reconcile_project``."""
        with self._lock:
            return frozenset(self._unresolved_projects)

    def reconcile_project(self, project_id: str) -> None:
        """Explicitly clear a project-level UNKNOWN guard.

        A project command cannot be correlated with any job, so no observation
        clears this guard. The caller must have reconciled the VM project state
        itself; the bridge verifies nothing here and attempt guards remain.
        """
        _id(project_id, "project_id")
        with self._lock:
            self._unresolved_projects.discard(project_id)

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

    def _hosted(self, handshake: Handshake | None) -> bool:
        return (self._binding_store is not None and handshake is not None
                and HOSTED_RUNTIME_EXTENSION in handshake.extensions)

    def _require_hosted(self, handshake: Handshake) -> None:
        if not self._hosted(handshake):
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="aik-hosted-runtime")

    def _require_bind_extension(self, handshake: Handshake) -> None:
        """The upstream binding extension (bind_execution/read_execution_binding)."""
        if (BINDING_EXTENSION not in handshake.extensions
                or not callable(getattr(self._port, "bind_execution", None))
                or not callable(getattr(self._port, "read_execution_binding", None))):
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="aik-bound-runtime")

    def _require_extension(self, handshake: Handshake) -> None:
        """A persisted-binding source: the hosted journal, or the upstream extension."""
        if self._hosted(handshake):
            return
        self._require_bind_extension(handshake)

    def _call(self, label: str, effectful: bool, function, *args, **kwargs) -> dict:
        outcome = _UNKNOWN if effectful else _KNOWN
        failure = None
        try:
            response = function(*args, **kwargs)
        except Exception as error:
            failure = _mapped(error, label, effectful)
        if failure is not None:
            # Raised outside the handler: neither __cause__ nor __context__
            # keeps the port exception or its raw message.
            raise failure from None
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

    @staticmethod
    def _digest_of(persisted: object, attempt: str, label: str) -> str | None:
        if persisted is None:
            return None
        if not isinstance(persisted, Mapping):
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject=label)
        digest = persisted.get("binding_digest")
        if type(digest) is not str or DIGEST_PATTERN.fullmatch(digest) is None:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject=label)
        reported = persisted.get("vm_attempt_id")
        if reported is not None and reported != attempt:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject=label)
        return digest

    def _port_persisted_digest(self, attempt: str) -> str | None:
        """The binding digest VM persisted through the upstream extension, or None."""
        try:
            persisted = self._port.read_execution_binding(attempt)
        except Exception as error:
            raise _mapped(error, "read_execution_binding", False) from None
        return self._digest_of(persisted, attempt, "read_execution_binding")

    def _persisted_digest(self, attempt: str) -> str | None:
        """The persisted binding digest for the Attempt, or None if absent.

        Hosted mode reads the owner-confirmed binding from the AIK journal;
        otherwise the upstream extension is read through the port.
        """
        if self._hosted(self._handshake):
            persisted = _store_call(self._binding_store.read_execution_binding, attempt)
            return self._digest_of(persisted, attempt, "read_execution_binding")
        return self._port_persisted_digest(attempt)

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

    # -- bound execution (upstream extension path) --
    def run_once(self, request: RunOnceRequest) -> BridgeReceipt:
        self._check(request, RunOnceRequest)
        handshake = self._ready(Command.RUN_ONCE, Command.ATTEMPT_DETAIL)
        self._require_bind_extension(handshake)
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
        persisted = self._port_persisted_digest(attempt)
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
            # has observed Core consume its latest job (resume does not bypass this).
            raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _KNOWN, subject="attempt_bound")
        with self._lock:
            self._terminal_observed.discard(attempt)
            # A new launch invalidates every observation started before it.
            self._launch_generation[attempt] = self._launch_generation.get(attempt, 0) + 1
            self._launched_job[attempt] = None

        response = self._effect(attempt, None, "run_once", self._port.run_once, binding.vm_project_id,
                                resume=request.resume)
        try:
            receipt = self._receipt(Command.RUN_ONCE, request.request_id, binding.vm_project_id, response,
                                    effectful=True, binding=binding, with_job=True)
            _same_execution(receipt.refs, binding, _UNKNOWN, _RUN_REQUIRED)
        except VmBridgeError:
            self._mark_unknown(attempt, None)
            raise
        with self._lock:
            self._launched_job[attempt] = receipt.job.job_id
        if receipt.job.phase is JobPhase.UNKNOWN:
            self._mark_unknown(attempt, None)
        return receipt

    # -- hosted execution (aik-hosted-runtime-v1) --
    def run_once_hosted(self, request: HostedRunOnceRequest) -> tuple:
        """VM reserves the Attempt and dispatches through the injected hosted runtime.

        Returns ``(receipt, confirmation)``; ``confirmation`` is None when the
        response does not identify an Attempt. A failed confirmation after the
        effect is raised with outcome UNKNOWN and marks the Attempt unresolved.
        """
        self._check(request, HostedRunOnceRequest)
        handshake = self._ready(Command.RUN_ONCE, Command.ATTEMPT_DETAIL, Command.STATUS)
        self._require_hosted(handshake)
        project = request.project_id
        with self._lock:
            if project in self._unresolved_projects:
                raise VmBridgeError(BridgeCode.RECONCILIATION_REQUIRED, _UNKNOWN, subject="vm_project_id")
            self._claim(request.request_id)
        response = self._effect(None, project, "run_once", self._port.run_once, project, resume=request.resume)
        try:
            receipt = self._receipt(Command.RUN_ONCE, request.request_id, project, response,
                                    effectful=True, with_job=True)
        except VmBridgeError as error:
            if error.outcome is _UNKNOWN:
                self._mark_unknown(None, project)
            raise
        attempt = receipt.refs.attempt_id
        job = receipt.job
        if attempt is None:
            reported = job.vm_status is not None or job.job_id is not None
            if reported and job.phase is JobPhase.UNKNOWN:
                # A job of unknown state that cannot be tied to an Attempt.
                self._mark_unknown(None, project)
            return receipt, None
        with self._lock:
            self._terminal_observed.discard(attempt)
            self._launch_generation[attempt] = self._launch_generation.get(attempt, 0) + 1
            self._launched_job[attempt] = job.job_id
        if job.phase is JobPhase.UNKNOWN:
            self._mark_unknown(attempt, None)
        failure = None
        confirmation = None
        try:
            confirmation = self._confirm(project, attempt)
        except VmBridgeError as error:
            failure = VmBridgeError(error.code, _UNKNOWN, vm_code=error.vm_code, subject=error.subject,
                                    details=error.details)
        if failure is not None:
            self._mark_unknown(attempt, None)
            raise failure from None
        return receipt, confirmation

    def confirm_attempt(self, project_id: str, attempt_id: str, *, request_id: str | None = None) -> HostedConfirmation:
        """Complete and store the owner-confirmed binding of a hosted Attempt.

        Reads the AIK effect binding, the VM owner query (``attempt_detail``)
        and the active Goal/Plan revisions (``status``). ``binding_pending``
        means no provider effect has been bound yet; it is not a success.
        """
        _id(project_id, "project_id")
        _id(attempt_id, "attempt_id")
        if request_id is not None:
            _id(request_id, "request_id")
        handshake = self._ready(Command.ATTEMPT_DETAIL, Command.STATUS)
        self._require_hosted(handshake)
        if request_id is not None:
            self._claim(request_id)
        return self._confirm(project_id, attempt_id)

    def _confirm(self, project_id: str, attempt_id: str) -> HostedConfirmation:
        store = self._binding_store
        stored = _store_call(store.read_attempt, attempt_id)
        if stored is None:
            return HostedConfirmation("binding_pending", project_id, attempt_id, None, None)
        if stored.vm_project_id != project_id:
            raise VmBridgeError(BridgeCode.BINDING_MISMATCH, subject="vm_project_id")
        first = stored.first_intent()
        if first is None:
            # Only resume/interrupt intents: no create/start binding exists yet.
            return HostedConfirmation("binding_pending", project_id, attempt_id, None, None)

        detail = self._call("attempt_detail", False, self._port.attempt_detail, attempt_id)
        owner = _extract_refs(_owner_view(detail), _KNOWN)
        for subject in _HOSTED_OWNER_REQUIRED:
            if getattr(owner, subject) is None:
                raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject=subject)
        if owner.project_id != project_id:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="project_id")
        if owner.attempt_id != attempt_id:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="attempt_id")

        status = self._call("status", False, self._port.status, project_id)
        status_refs = _extract_refs(status, _KNOWN)
        if status_refs.project_id is not None and status_refs.project_id != project_id:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="project_id")
        goal = _active_revision(status, "active_goal_revision_id")
        plan = _active_revision(status, "active_plan_revision_id")
        if goal is None:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="active_goal_revision_id")
        if plan is None:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="active_plan_revision_id")
        if status_refs.plan_revision_id is not None and status_refs.plan_revision_id != plan:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="plan_revision_id")
        if owner.plan_revision_id != plan:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="plan_revision_id")
        if owner.goal_revision_id is not None and owner.goal_revision_id != goal:
            raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="goal_revision_id")
        if first.vm_call_id is None:
            # The trace scope did not identify the provider call; the v1
            # binding requires it.
            raise VmBridgeError(BridgeCode.SEAM_UNAVAILABLE, subject="trace_call_id")

        values = {name: stored.assignment[name] for name in HOSTED_ASSIGNMENT_FIELDS}
        values.update(
            vm_goal_revision_id=goal, vm_plan_revision_id=plan, vm_task_id=owner.task_id,
            vm_attempt_id=attempt_id, vm_execution_spec_digest=owner.execution_spec_digest,
            vm_runtime_intent_id=first.vm_intent_id, vm_provider_call_id=first.vm_call_id,
        )
        failed = False
        binding = None
        try:
            binding = TaskExecutionBinding(**values)
        except (VmBridgeError, TypeError):
            failed = True
        if failed:
            raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="binding") from None
        digest = _store_call(store.confirm_owner, binding)
        return HostedConfirmation("confirmed", project_id, attempt_id, owner.task_id, digest)

    # -- observation and cancellation --
    def _release_after_consumption(self, attempt: str, generation: int | None, job_id: str | None) -> None:
        """Release an Attempt guard only for its latest launch, observed consumed."""
        with self._lock:
            if attempt in self._inflight or self._launch_generation.get(attempt, 0) != generation:
                return  # A launch started during this observation: the result is stale.
            expected = self._launched_job.get(attempt)
            if expected is not None and job_id != expected:
                return  # A consumed older job says nothing about the latest launch.
            self._unresolved.discard(attempt)
            self._terminal_observed.add(attempt)

    def observe(self, request: ObserveRequest) -> BridgeReceipt:
        self._check(request, ObserveRequest)
        self._ready(Command.OBSERVE)
        self._claim(request.request_id)
        binding = request.binding
        generation = None
        if binding is not None:
            self._require_persisted(binding)
            with self._lock:
                generation = self._launch_generation.get(binding.vm_attempt_id, 0)
        failure = None
        try:
            response = self._call("observe", False, self._port.observe, request.project_id)
        except VmBridgeError as error:
            if error.code is not BridgeCode.QUERY_FAILED:
                raise
            # Upstream observe may settle, reattach or tick runtime jobs; an
            # unclassified failure can therefore follow VM-side changes. No
            # guard is added, but "no effect" is not claimed either.
            failure = VmBridgeError(BridgeCode.OUTCOME_UNKNOWN, _UNKNOWN, subject="observe")
        if failure is not None:
            raise failure from None
        receipt = self._receipt(Command.OBSERVE, request.request_id, request.project_id, response,
                                effectful=False, binding=binding, with_job=True)
        if binding is not None and receipt.refs.attempt_id == binding.vm_attempt_id:
            _same_execution(receipt.refs, binding, _KNOWN)
            if receipt.job.core_consumed:
                # Pending/running/provider_terminal/unknown keep the guard:
                # Core has not consumed the job, which may still act.
                self._release_after_consumption(binding.vm_attempt_id, generation, receipt.job.job_id)
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
