"""Review R1 regression tests for the AIK hosted runtime (``aik-hosted-runtime-v1``).

These tests cover review findings F1-F9 with fakes only. No VM installation,
provider, credential, network or subprocess is used. Every journal is created
in a disposable directory outside the repository.

The VM design contract is imitated as follows:

* The VM operation trace module (``flowmarshal.engine.operation_trace``) is
  replaced by a fake module in ``sys.modules``. The fake module keeps its scope
  in a ``ContextVar``. The provider call can therefore read the scope only on
  the thread that opened it, as with the VM supervisor.
* ``run_once`` may return before any provider call happens. The dispatch then
  runs later on a separate thread (contract items 3 and 4).

This module imports no VM code and does not import tests/test_vm_hosted_runtime.py;
the fakes it needs are copied here. Passing on Linux is development
verification only. The installed VM, Windows RuntimeJob, real models and
AGS-governed dispatch remain NOT_RUN.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import contextvars
import copy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import Lease, Placement, Pool  # noqa: E402
from aik_runtime.errors import InvalidDatabase, StorageBusy  # noqa: E402
from aik_vm_bridge.binding import TaskExecutionBinding  # noqa: E402
from aik_vm_bridge.bridge import HostedConfirmation, HostedRunOnceRequest, VmBridge  # noqa: E402
from aik_vm_bridge.engine_port import EngineApplicationPort  # noqa: E402
from aik_vm_bridge.errors import BridgeCode, EffectOutcome, VmBridgeError  # noqa: E402
from aik_vm_bridge.hosted_runtime import (  # noqa: E402
    TRACE_MODULE, AikHostedRuntime, ExecutionBindingStore, HostedAssignment, TraceRef, read_operation_trace,
)
from aik_vm_bridge.surface import HOSTED_RUNTIME_EXTENSION, PINNED_SURFACE_REVISION  # noqa: E402


SECRET = "SYNTHETIC_SECRET_MUST_NOT_LEAK"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2029, 6, 1, tzinfo=timezone.utc)
KNOWN = EffectOutcome.KNOWN_FAILURE
UNKNOWN = EffectOutcome.UNKNOWN
JOIN_TIMEOUT = 10.0
BINDING_JOURNAL_APPLICATION_ID = 0x41494B42
OTHER_AIK_APPLICATION_ID = 0x41494B45
SIGNATURES = {
    "prepare": "self, project_id: str, *, source_request: str, candidate_count: int | None=None",
    "revise": "self, project_id: str, *, source_request: str, candidate_count: int | None=None",
    "authorize": ("self, project_id: str, *, source: str, operating_policy: GoalOperatingPolicy | None=None, "
                  "capability: GoalAuthorizationCapability | None=None, authorization_target: "
                  "GoalAuthorizationTarget | None=None, selected_plan_revision_id: str | None=None"),
    "replan": "self, project_id: str, *, rationale: str",
    "run_once": ("self, project_id: str, *, resume: bool=False, proposal: ExecutionSpecProposal | None=None, "
                 "goal_validation_step: ValidationExecutionStep | None=None, "
                 "goal_validation_retry: GoalValidationRetryRequest | None=None"),
    "observe": "self, project_id: str",
    "pause": "self, project_id: str, *, reason: str='사용자 요청'",
    "cancel": "self, project_id: str, *, reason: str='사용자 요청'",
    "status": "self, project_id: str",
    "final_report": "self, project_id: str, *, goal_verdict_id: str | None=None",
    "attempt_detail": "self, attempt_id: str",
    "model_binding_status": "self, project_id: str, *, inventory: ModelInventory | None=None",
}


class SimulatedCrash(BaseException):
    """Stands in for process death during the inner provider call (not an Exception)."""


# ----- synthetic data -------------------------------------------------------

def surface_dict() -> dict:
    return {
        "format": "aik-upstream-surface-v1", "revision": PINNED_SURFACE_REVISION,
        "classes": [{"name": "EngineApplication", "methods": [
            {"name": "__init__", "signature": "self, service: EngineService, *, runtime=None"},
            *({"name": name, "signature": sig} for name, sig in SIGNATURES.items()),
        ]}],
    }


def make_assignment(connection_id: str = "connection-1", *, attempt_id: str = "attempt-1",
                    task_id: str = "task-1", route_revision: str = "3", fencing: int = 5,
                    expires_at: str = "2030-01-01T00:00:00Z", lease_state: str = "active") -> HostedAssignment:
    pool = Pool("pool-1", "synthetic", connection_id, enabled=True)
    placement = Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "project-1", task_id,
                          attempt_id, state="assigned")
    lease = Lease("lease-1", "placement-1", "pool-1", "worker-1", fencing, expires_at, state=lease_state)
    fingerprint = ("f" if connection_id == "connection-1" else "e") * 64
    return HostedAssignment(
        vm_instance_id="vm-instance-1", vm_project_id="project-1", cluster_id="cluster-1", route_id="route-1",
        route_revision=route_revision, connection_id=connection_id, connection_revision="7",
        provider="synthetic", auth_kind="api_key", provider_account_fingerprint=fingerprint, pool_id="pool-1",
        placement_id="placement-1", lease_id="lease-1", lease_fencing_token=fencing,
        support_matrix_digest=DIGEST_B, input_manifest_digest=DIGEST_C,
        workspace_root_ref="workspace://local/root-1", placement=placement, lease=lease, pool=pool,
    )


def task_trace(intent, *, call=None, thread=None, turn=None, attempt="attempt-1") -> TraceRef:
    return TraceRef(attempt_id=attempt, intent_id=intent, call_id=call, thread_id=thread, turn_id=turn)


def expected_binding(**overrides) -> TaskExecutionBinding:
    values = make_assignment().to_mapping()
    values.update(vm_goal_revision_id="goal-rev-1", vm_plan_revision_id="plan-rev-1", vm_task_id="task-1",
                  vm_attempt_id="attempt-1", vm_execution_spec_digest=DIGEST_A,
                  vm_runtime_intent_id="intent-1", vm_provider_call_id="call-1")
    values.update(overrides)
    return TaskExecutionBinding.from_mapping(values)


def attempt_detail_response() -> dict:
    """Shape after engine_port normalization (attempt.* lifted to the top level)."""
    attempt = {"attempt_id": "attempt-1", "task_id": "task-1", "execution_spec_digest": DIGEST_A,
               "attempt_no": 1, "kind": "worker", "status": "running", "binding": None, "failure_class": None}
    return {"project_id": "project-1", "plan_revision_id": "plan-rev-1", "task_ref": {"task_id": "task-1"},
            "attempt": attempt, "attempt_id": "attempt-1", "task_id": "task-1",
            "execution_spec_digest": DIGEST_A, "attempt_status": "running", "intents": []}


def status_response() -> dict:
    project = {"id": "project-1", "root": "/synthetic/project-1", "active_goal_revision_id": "goal-rev-1",
               "active_plan_revision_id": "plan-rev-1"}
    return {"project_id": "project-1", "project_root": "/synthetic/project-1", "control_state": "running",
            "project": project, "active_goal_revision_id": "goal-rev-1",
            "active_plan_revision_id": "plan-rev-1", "plan_revision_id": "plan-rev-1"}


def rows(records) -> list:
    return [(item["vm_attempt_id"], item["thread_id"], item["operation"]) for item in records]


# ----- fake VM trace module -------------------------------------------------

_MISSING = object()


@contextmanager
def installed_trace_module(value):
    """Install ``value`` as the VM trace module (None makes the import fail)."""
    saved = sys.modules.get(TRACE_MODULE, _MISSING)
    sys.modules[TRACE_MODULE] = value
    try:
        yield
    finally:
        if saved is _MISSING:
            sys.modules.pop(TRACE_MODULE, None)
        else:
            sys.modules[TRACE_MODULE] = saved


class FakeTraceModule:
    """Imitates ``flowmarshal.engine.operation_trace``; the scope lives in a ContextVar."""

    def __init__(self, *, with_reader: bool = True, reader: object = None):
        self._scope = contextvars.ContextVar("aik_review_operation_trace_scope", default=None)
        self._lock = threading.Lock()
        self.reader_threads: list = []
        self.module = types.ModuleType(TRACE_MODULE)
        if reader is not None:
            self.module.current_operation_trace_scope = reader
        elif with_reader:
            self.module.current_operation_trace_scope = self._current

    def _current(self):
        with self._lock:
            self.reader_threads.append(threading.get_ident())
        return self._scope.get()

    @contextmanager
    def operation_scope(self, *, attempt_id=None, intent_id=None, call_id=None, thread_id=None, turn_id=None):
        # Same field set as the VM OperationTraceScope; extra fields must be ignored.
        scope = SimpleNamespace(trace=object(), parent=None, deadline_monotonic_ns=None, deadline_at=None,
                                call_id=call_id, attempt_id=attempt_id, intent_id=intent_id,
                                thread_id=thread_id, turn_id=turn_id)
        token = self._scope.set(scope)
        try:
            yield scope
        finally:
            self._scope.reset(token)

    def install(self):
        return installed_trace_module(self.module)


# ----- fakes ----------------------------------------------------------------

class FakeReceipt:
    """Shaped like the VM RuntimeOperationReceipt (operation_id, payload, binding)."""

    def __init__(self, operation_id: str, thread_id: str | None):
        self.operation_id = operation_id
        self.payload = {} if thread_id is None else {"thread": {"id": thread_id}}
        self.binding = None if thread_id is None else SimpleNamespace(
            thread_id=thread_id, turn_id=None, host_id="host-1", bound_at="2029-06-01T00:00:00Z")


class FakeInner:
    """An inner runtime of one connection (stands in for a VM native runtime)."""

    def __init__(self, name: str, thread_ids=()):
        self.name = name
        self.thread_ids = list(thread_ids)
        self.calls: list = []
        self.failures: dict = {}
        self.hooks: dict = {}
        self.returned: list = []

    def ops(self) -> list:
        return [call[0] for call in self.calls]

    def _enter(self, operation: str, kwargs: dict) -> None:
        self.calls.append((operation, dict(kwargs)))
        hook = self.hooks.get(operation)
        if hook is not None:
            hook()
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    def _result(self, value):
        self.returned.append(value)
        return value

    def verify_execution_policy(self, cwd):
        self._enter("verify_execution_policy", {"cwd": cwd})
        return self._result(SimpleNamespace(kind="policy", connection=self.name))

    def list_models(self):
        self._enter("list_models", {})
        return self._result(SimpleNamespace(kind="models", connection=self.name))

    def create_thread(self, *, cwd, title, model, developer_instructions, ephemeral=False):
        self._enter("create_thread", dict(cwd=cwd, title=title, model=model,
                                          developer_instructions=developer_instructions, ephemeral=ephemeral))
        thread = self.thread_ids.pop(0) if self.thread_ids else f"thread-{self.name}-{len(self.calls)}"
        return self._result(FakeReceipt(f"create-{len(self.calls)}", thread))

    def start_turn(self, *, thread_id, cwd, prompt, model, effort, output_schema=None):
        self._enter("start_turn", dict(thread_id=thread_id, cwd=cwd, prompt=prompt, model=model, effort=effort,
                                       output_schema=output_schema))
        return self._result(FakeReceipt(f"start-{len(self.calls)}", None))

    def read(self, *, thread_id):
        self._enter("read", dict(thread_id=thread_id))
        return self._result(SimpleNamespace(kind="observation", thread_id=thread_id))

    def resume(self, *, thread_id, cwd):
        self._enter("resume", dict(thread_id=thread_id, cwd=cwd))
        return self._result(FakeReceipt(f"resume-{len(self.calls)}", None))

    def interrupt(self, *, thread_id, turn_id, timeout_seconds=5.0):
        self._enter("interrupt", dict(thread_id=thread_id, turn_id=turn_id, timeout_seconds=timeout_seconds))
        return self._result(FakeReceipt(f"interrupt-{len(self.calls)}", None))

    def read_stored(self, *, thread_id, turn_id=None, timeout_seconds=5.0):
        self._enter("read_stored", dict(thread_id=thread_id, turn_id=turn_id, timeout_seconds=timeout_seconds))
        return self._result(SimpleNamespace(kind="stored", thread_id=thread_id))

    def close(self, *, timeout_seconds=5.0):
        self._enter("close", dict(timeout_seconds=timeout_seconds))


class FakePort:
    """A VmApplicationPort fake without a ``hosted_runtime`` attribute."""

    def __init__(self):
        self.calls: list = []
        self.failures: dict = {}
        self.dispatch = None
        self.handshake_value = {
            "surface_revision": PINNED_SURFACE_REVISION, "engine_schema_revision": 4,
            "methods": dict(SIGNATURES), "extensions": [HOSTED_RUNTIME_EXTENSION],
        }
        self.responses = {
            "status": status_response(),
            "attempt_detail": attempt_detail_response(),
            "run_once": {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                         "runtime_job_id": "job-1", "runtime_job_kind": "worker_turn",
                         "runtime_job_status": "scheduled"},
            "observe": {"project_id": "project-1"},
            "cancel": {"project_id": "project-1"},
            "pause": {"project_id": "project-1"},
            "prepare": {"project_id": "project-1"},
            "revise": {"project_id": "project-1"},
            "authorize": {"project_id": "project-1"},
            "replan": {"project_id": "project-1"},
            "final_report": {"project_id": "project-1"},
            "model_binding_status": {"project_id": "project-1", "models": ["synthetic-model"]},
        }

    def names(self) -> list:
        return [call[0] for call in self.calls]

    def handshake(self):
        self.calls.append(("handshake", (), {}))
        return copy.deepcopy(self.handshake_value)

    def _do(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.failures:
            raise self.failures[name]
        if name == "run_once" and self.dispatch is not None:
            self.dispatch()
        return copy.deepcopy(self.responses[name])

    def prepare(self, project_id, *, source_request, candidate_count=None):
        return self._do("prepare", project_id, source_request=source_request, candidate_count=candidate_count)

    def revise(self, project_id, *, source_request, candidate_count=None):
        return self._do("revise", project_id, source_request=source_request, candidate_count=candidate_count)

    def authorize(self, project_id, *, source, selected_plan_revision_id=None):
        return self._do("authorize", project_id, source=source, selected_plan_revision_id=selected_plan_revision_id)

    def run_once(self, project_id, *, resume=False):
        return self._do("run_once", project_id, resume=resume)

    def observe(self, project_id):
        return self._do("observe", project_id)

    def pause(self, project_id, *, reason):
        return self._do("pause", project_id, reason=reason)

    def cancel(self, project_id, *, reason):
        return self._do("cancel", project_id, reason=reason)

    def status(self, project_id):
        return self._do("status", project_id)

    def replan(self, project_id, *, rationale):
        return self._do("replan", project_id, rationale=rationale)

    def final_report(self, project_id, *, goal_verdict_id=None):
        return self._do("final_report", project_id, goal_verdict_id=goal_verdict_id)

    def attempt_detail(self, attempt_id):
        return self._do("attempt_detail", attempt_id)

    def model_binding_status(self, project_id):
        return self._do("model_binding_status", project_id)


class FakeEngineApp:
    """Imitates the public EngineApplication façade with the pinned signatures."""

    def __init__(self, service, *, runtime=None, **kwargs):
        self.service = service
        self.runtime = runtime
        self.kwargs = kwargs
        self.calls: list = []
        self.dispatch = None

    def _record(self, name):
        self.calls.append(name)

    def prepare(self, project_id, *, source_request, candidate_count=None):
        self._record("prepare")
        return {"project_id": project_id}

    def revise(self, project_id, *, source_request, candidate_count=None):
        self._record("revise")
        return {"project_id": project_id}

    def authorize(self, project_id, *, source, operating_policy=None, capability=None, authorization_target=None,
                  selected_plan_revision_id=None):
        self._record("authorize")
        return {"project_id": project_id}

    def replan(self, project_id, *, rationale):
        self._record("replan")
        return {"project_id": project_id}

    def run_once(self, project_id, *, resume=False, proposal=None, goal_validation_step=None,
                 goal_validation_retry=None):
        self._record("run_once")
        if self.dispatch is not None:
            self.dispatch()
        return {"project_id": project_id, "task_id": "task-1", "attempt_id": "attempt-1",
                "runtime_job_id": "job-1", "runtime_job_kind": "worker_turn", "runtime_job_status": "scheduled"}

    def observe(self, project_id):
        self._record("observe")
        return {"runtime_job": {"job_id": "job-1", "status": "running", "attempt_id": "attempt-1",
                                "task_id": "task-1"}}

    def pause(self, project_id, *, reason="사용자 요청"):
        self._record("pause")
        return {"project_id": project_id}

    def cancel(self, project_id, *, reason="사용자 요청"):
        self._record("cancel")
        return {"project_id": project_id}

    def status(self, project_id):
        self._record("status")
        return {"project": {"id": project_id, "root": "/synthetic/project-1",
                            "active_goal_revision_id": "goal-rev-1", "active_plan_revision_id": "plan-rev-1"},
                "current_stage": "executing", "control_state": "running", "authorization_state": "granted",
                "tasks": []}

    def final_report(self, project_id, *, goal_verdict_id=None):
        self._record("final_report")
        return {"project_id": project_id}

    def attempt_detail(self, attempt_id):
        self._record("attempt_detail")
        return {"project_id": "project-1", "plan_revision_id": "plan-rev-1", "task_ref": {"task_id": "task-1"},
                "attempt": {"attempt_id": attempt_id, "task_id": "task-1", "execution_spec_digest": DIGEST_A,
                            "attempt_no": 1, "kind": "worker", "status": "running", "binding": None},
                "intents": []}

    def model_binding_status(self, project_id, *, inventory=None):
        self._record("model_binding_status")
        return {"project_id": project_id, "models": ["synthetic-model"]}


# ----- fixture --------------------------------------------------------------

class ReviewFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-hosted-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal_path = self.root / "state" / "bindings.sqlite3"
        self.cwd = self.root / "workspace"
        self.clock = [NOW]
        self.trace = [None]
        self.assignment = [make_assignment()]
        self.source_calls: list = []
        self.runtime_for_calls: list = []
        self.inners: dict = {}

    def runtime_for(self, connection_id):
        self.runtime_for_calls.append(connection_id)
        return self.inners.setdefault(connection_id, FakeInner(connection_id))

    def source(self, cwd, trace):
        self.source_calls.append((cwd, trace))
        return self.assignment[0]

    def open(self):
        """Store and runtime with an injected trace reader (``self.trace``)."""
        store = ExecutionBindingStore(self.journal_path)
        runtime = AikHostedRuntime(self.runtime_for, store, self.source, trace_reader=lambda: self.trace[0],
                                   now=lambda: self.clock[0])
        return store, runtime

    def open_default(self):
        """Store and runtime with the default VM trace reader."""
        store = ExecutionBindingStore(self.journal_path)
        runtime = AikHostedRuntime(self.runtime_for, store, self.source, now=lambda: self.clock[0])
        return store, runtime

    def restart(self):
        """Simulate a new process: fresh inner runtimes, store object and hosted runtime."""
        self.inners = {}
        self.runtime_for_calls = []
        return self.open()

    def inner(self, connection_id="connection-1", *thread_ids) -> FakeInner:
        inner = self.inners.setdefault(connection_id, FakeInner(connection_id))
        inner.thread_ids.extend(thread_ids)
        return inner

    def create(self, runtime, **overrides):
        arguments = dict(cwd=self.cwd, title="synthetic title", model="synthetic-model",
                         developer_instructions="synthetic instructions")
        arguments.update(overrides)
        return runtime.create_thread(**arguments)

    def start(self, runtime, thread="thread-1", **overrides):
        arguments = dict(thread_id=thread, cwd=self.cwd, prompt="synthetic prompt", model="synthetic-model",
                         effort="high")
        arguments.update(overrides)
        return runtime.start_turn(**arguments)

    def dispatch_attempt(self, runtime, *, thread="thread-1", connection="connection-1"):
        """What VM would do for a Worker turn: create then start, under its trace scope."""
        self.inner(connection, thread)
        self.trace[0] = task_trace("intent-1", call="call-1")
        self.create(runtime)
        self.trace[0] = task_trace("intent-2", call="call-2", thread=thread)
        self.start(runtime, thread)
        self.trace[0] = None

    def bridge(self, port, store=None):
        return VmBridge(port, surface_dict(), now=lambda: NOW, binding_store=store)

    def expect(self, code, function, *, subject=None, outcome=None) -> VmBridgeError:
        with self.assertRaises(VmBridgeError) as raised:
            function()
        error = raised.exception
        self.assertIs(error.code, code, str(error))
        if subject is not None:
            self.assertEqual(error.subject, subject)
        if outcome is not None:
            self.assertIs(error.outcome, outcome)
        self.assertNotIn(SECRET, str(error) + repr(error))
        return error


# ----- F1: default trace reader is fail-closed ------------------------------

class TraceSeamTests(ReviewFixture):
    def test_absent_module_reader_or_uncallable_reader_is_seam_unavailable(self) -> None:
        cases = (
            ("module_absent", None),
            ("reader_absent", FakeTraceModule(with_reader=False).module),
            ("reader_not_callable", FakeTraceModule(reader=5).module),
        )
        for label, module in cases:
            with self.subTest(case=label):
                with installed_trace_module(module):
                    self.expect(BridgeCode.SEAM_UNAVAILABLE, read_operation_trace, subject="trace_scope")

        def broken():
            raise RuntimeError(SECRET)

        with installed_trace_module(FakeTraceModule(reader=broken).module):
            self.expect(BridgeCode.SEAM_UNAVAILABLE, read_operation_trace, subject="trace_scope")

    def test_reader_returning_none_or_a_scope(self) -> None:
        trace = FakeTraceModule()
        with trace.install():
            # Outside a VM operation scope the reader returns None (preparation
            # call or no scope; this relies on the VM design assumption).
            self.assertIsNone(read_operation_trace())
            with trace.operation_scope(attempt_id="attempt-1", intent_id="intent-1", call_id="call-1"):
                self.assertEqual(read_operation_trace(),
                                 TraceRef(attempt_id="attempt-1", intent_id="intent-1", call_id="call-1"))
            self.assertIsNone(read_operation_trace())

    def test_missing_seam_never_turns_a_call_into_a_preparation_call(self) -> None:
        for label, module in (("module_absent", None), ("reader_absent", FakeTraceModule(with_reader=False).module)):
            with self.subTest(case=label):
                self.journal_path = self.root / label / "bindings.sqlite3"
                self.source_calls.clear()
                self.runtime_for_calls.clear()
                store, runtime = self.open_default()
                inner = self.inner("connection-1", "thread-1")
                with installed_trace_module(module):
                    self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: self.create(runtime), subject="trace_scope")
                    self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: runtime.list_models(), subject="trace_scope")
                self.assertEqual(inner.calls, [])
                self.assertEqual(self.source_calls, [])
                self.assertEqual(self.runtime_for_calls, [])
                self.assertEqual(store.effect_records(), ())
                self.assertIsNone(store.route_for_thread("thread-1"))
                self.assertEqual(store.inflight_effects(), ())

    def test_default_reader_reads_the_scope_on_the_dispatching_thread(self) -> None:
        store, runtime = self.open_default()
        inner = self.inner("connection-1", "thread-1", "thread-prep")
        trace = FakeTraceModule()
        errors: list = []
        worker_ident: list = []

        def supervisor():
            worker_ident.append(threading.get_ident())
            try:
                with trace.operation_scope(attempt_id="attempt-1", intent_id="intent-1", call_id="call-1"):
                    self.create(runtime)
                with trace.operation_scope(attempt_id="attempt-1", intent_id="intent-2", call_id="call-2",
                                           thread_id="thread-1"):
                    self.start(runtime)
            except BaseException as error:  # reported to the main thread
                errors.append(error)

        with trace.install():
            worker = threading.Thread(target=supervisor, name="fake-runtime-supervisor")
            worker.start()
            worker.join(JOIN_TIMEOUT)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(set(trace.reader_threads), set(worker_ident))
            self.assertNotIn(threading.get_ident(), trace.reader_threads)
            route = store.route_for_thread("thread-1")
            self.assertEqual((route.kind, route.vm_attempt_id), ("task", "attempt-1"))
            stored = store.read_attempt("attempt-1")
            self.assertEqual([(i.vm_intent_id, i.vm_call_id) for i in stored.intents],
                             [("intent-1", "call-1"), ("intent-2", "call-2")])
            # Without a scope (reader present, returning None) the call is a preparation call.
            self.create(runtime)
            self.assertEqual(store.route_for_thread("thread-prep").kind, "preparation")
        self.assertEqual(inner.ops(), ["create_thread", "start_turn", "create_thread"])


# ----- F2: interrupt while unresolved ---------------------------------------

class InterruptWhileUnresolvedTests(ReviewFixture):
    def test_unknown_target_can_still_be_interrupted_and_it_is_recorded(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        inner = self.inners["connection-1"]
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1", turn="turn-1")
        runtime.interrupt(thread_id="thread-1", turn_id="turn-1")
        self.assertIn("while_unresolved", store.effect_records()[-1])
        self.assertFalse(store.effect_records()[-1]["while_unresolved"])

        inner.failures["start_turn"] = RuntimeError(SECRET)
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.start(runtime), outcome=UNKNOWN)
        del inner.failures["start_turn"]
        self.assertIn("attempt-1", store.unknown_attempts)

        self.trace[0] = task_trace("intent-5", call="call-5", thread="thread-1", turn="turn-2")
        returned = runtime.interrupt(thread_id="thread-1", turn_id="turn-2", timeout_seconds=2.0)
        self.assertIs(returned, inner.returned[-1])
        self.assertEqual(inner.calls[-1], ("interrupt", dict(thread_id="thread-1", turn_id="turn-2",
                                                             timeout_seconds=2.0)))
        last = store.effect_records()[-1]
        self.assertEqual((last["operation"], last["vm_attempt_id"], last["connection_id"], last["while_unresolved"]),
                         ("interrupt", "attempt-1", "connection-1", True))

        # Other effects stay blocked.
        self.trace[0] = task_trace("intent-6", call="call-6", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime.resume(thread_id="thread-1", cwd=self.cwd))
        self.trace[0] = task_trace("intent-7", call="call-7", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: self.start(runtime))
        self.assertNotIn("resume", inner.ops())
        self.assertEqual(inner.ops().count("start_turn"), 2)

        # Identity checks are never skipped, not even while unresolved.
        self.assignment[0] = make_assignment(fencing=6)
        self.trace[0] = task_trace("intent-8", call="call-8", thread="thread-1", turn="turn-2")
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: runtime.interrupt(thread_id="thread-1", turn_id="turn-2"), subject="lease_fencing_token")
        self.assertEqual(inner.ops().count("interrupt"), 2)

    def test_interrupt_after_restart_reaches_the_inner_runtime_and_failure_stays_unknown(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        self.inners["connection-1"].failures["start_turn"] = TimeoutError(SECRET)
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.start(runtime), outcome=UNKNOWN)

        store2, runtime2 = self.restart()
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1", turn="turn-1")
        runtime2.interrupt(thread_id="thread-1", turn_id="turn-1")
        inner2 = self.inners["connection-1"]
        self.assertEqual(inner2.ops(), ["interrupt"])
        self.assertTrue(store2.effect_records()[-1]["while_unresolved"])

        inner2.failures["interrupt"] = RuntimeError(SECRET)
        self.trace[0] = task_trace("intent-5", call="call-5", thread="thread-1", turn="turn-1")
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: runtime2.interrupt(thread_id="thread-1", turn_id="turn-1"),
                    subject="interrupt", outcome=UNKNOWN)
        self.assertIn("attempt-1", store2.unknown_attempts)
        self.assertIn("thread-1", store2.unknown_threads)


# ----- F3: write-ahead in-flight records -----------------------------------

class InflightEffectTests(ReviewFixture):
    def test_inflight_row_exists_during_the_inner_call_and_is_removed_after_return(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1")  # dispatch_attempt supplies thread-1
        seen: dict = {}
        inner.hooks["create_thread"] = lambda: seen.setdefault("create", store.inflight_effects())
        inner.hooks["start_turn"] = lambda: seen.setdefault("start", store.inflight_effects())
        self.dispatch_attempt(runtime)
        self.assertEqual(rows(seen["create"]), [("attempt-1", None, "create_thread")])
        self.assertEqual(rows(seen["start"]), [("attempt-1", "thread-1", "start_turn")])
        self.assertTrue(all(type(item["id"]) is int for item in (*seen["create"], *seen["start"])))
        self.assertEqual(store.inflight_effects(), ())
        # A successful preparation create (no Attempt in the trace) leaves no in-flight row either.
        self.trace[0] = None
        self.inner("connection-1", "thread-prep")
        self.create(runtime)
        self.assertEqual(store.inflight_effects(), ())

    def test_inner_failure_keeps_the_block_across_restart_until_reconciled(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        self.inners["connection-1"].failures["start_turn"] = RuntimeError(SECRET)
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.start(runtime), outcome=UNKNOWN)
        self.assertEqual(rows(store.inflight_effects()), [("attempt-1", "thread-1", "start_turn")])

        store2, runtime2 = self.restart()
        self.assertEqual(rows(store2.inflight_effects()), [("attempt-1", "thread-1", "start_turn")])
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime2.resume(thread_id="thread-1", cwd=self.cwd))
        self.assertEqual(self.inners.get("connection-1", FakeInner("x")).ops(), [])

        runtime2.reconcile_thread("thread-1")
        runtime2.reconcile_attempt("attempt-1")
        self.assertEqual(store2.inflight_effects(), ())
        self.assertEqual((runtime2.unknown_attempts, runtime2.unknown_threads), (frozenset(), frozenset()))
        runtime2.resume(thread_id="thread-1", cwd=self.cwd)
        self.assertEqual(self.inners["connection-1"].ops(), ["resume"])
        self.assertEqual(store2.inflight_effects(), ())

    def test_process_death_during_the_inner_call_still_blocks_after_restart(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        self.inners["connection-1"].failures["start_turn"] = SimulatedCrash()
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        with self.assertRaises(SimulatedCrash):
            self.start(runtime)
        # No unknown mark could be written; only the write-ahead row remains.
        self.assertEqual(store.unknown_attempts, frozenset())
        self.assertEqual(store.unknown_threads, frozenset())
        self.assertEqual(rows(store.inflight_effects()), [("attempt-1", "thread-1", "start_turn")])

        store2, runtime2 = self.restart()
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime2.resume(thread_id="thread-1", cwd=self.cwd))
        self.trace[0] = task_trace("intent-5", call="call-5", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: self.start(runtime2))
        # The same Attempt is blocked even without a thread.
        self.inner("connection-1", "thread-2")
        self.trace[0] = task_trace("intent-6", call="call-6")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: self.create(runtime2))
        self.assertEqual(self.inners["connection-1"].ops(), [])

        runtime2.reconcile_thread("thread-1")
        runtime2.reconcile_attempt("attempt-1")
        self.assertEqual(store2.inflight_effects(), ())
        self.trace[0] = task_trace("intent-7", call="call-7", thread="thread-1")
        runtime2.resume(thread_id="thread-1", cwd=self.cwd)
        self.assertEqual(self.inners["connection-1"].ops(), ["resume"])

    def test_failed_thread_route_record_keeps_the_create_inflight_row(self) -> None:
        store, runtime = self.open()
        self.inner("connection-1", "thread-1")
        self.trace[0] = task_trace("intent-1", call="call-1")
        failure = VmBridgeError(BridgeCode.BINDING_STORE_FAILED, subject="binding_store")
        with mock.patch.object(store, "record_thread", side_effect=failure):
            self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.create(runtime), outcome=UNKNOWN)
        self.assertTrue(any(item["vm_attempt_id"] == "attempt-1" and item["operation"] == "create_thread"
                            for item in store.inflight_effects()))

    def test_orphan_preparation_row_blocks_nothing(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1")
        inner.failures["create_thread"] = SimulatedCrash()
        self.trace[0] = None
        with self.assertRaises(SimulatedCrash):
            self.create(runtime)
        self.assertEqual(rows(store.inflight_effects()), [(None, None, "create_thread")])
        del inner.failures["create_thread"]

        store2, runtime2 = self.restart()
        self.inner("connection-1", "thread-prep")
        self.create(runtime2)
        self.assertEqual(store2.route_for_thread("thread-prep").kind, "preparation")
        self.dispatch_attempt(runtime2)
        self.assertEqual(self.inners["connection-1"].ops(), ["create_thread", "create_thread", "start_turn"])
        self.assertEqual(rows(store2.inflight_effects()), [(None, None, "create_thread")])


# ----- F4: bridge unknown marks are durable ---------------------------------

class BridgeDurableUnknownTests(ReviewFixture):
    def test_failed_confirmation_blocks_the_next_effect_of_that_attempt(self) -> None:
        store, runtime = self.open()
        port = FakePort()
        port.dispatch = lambda: self.dispatch_attempt(runtime)
        port.failures["status"] = RuntimeError(SECRET)
        bridge = self.bridge(port, store)
        self.expect(BridgeCode.QUERY_FAILED,
                    lambda: bridge.run_once_hosted(HostedRunOnceRequest("run-1", "project-1")), outcome=UNKNOWN)
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        self.assertNotIn("project-1", bridge.unresolved_projects)
        self.assertIn("attempt-1", store.unknown_attempts)
        self.assertIn("attempt-1", ExecutionBindingStore(self.journal_path).unknown_attempts)
        inner = self.inners["connection-1"]
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: self.start(runtime))
        self.assertEqual(inner.ops(), ["create_thread", "start_turn"])

        store2, runtime2 = self.restart()
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime2.resume(thread_id="thread-1", cwd=self.cwd))
        self.assertEqual(self.inners.get("connection-1", FakeInner("x")).ops(), [])

    def test_unknown_job_phase_marks_the_attempt_durably_before_any_dispatch(self) -> None:
        store, runtime = self.open()
        port = FakePort()
        port.responses["run_once"] = {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                                      "runtime_job_id": "job-1", "runtime_job_status": "collector_lost"}
        receipt, confirmation = self.bridge(port, store).run_once_hosted(HostedRunOnceRequest("run-1", "project-1"))
        self.assertIsInstance(confirmation, HostedConfirmation)
        self.assertEqual(confirmation.state, "binding_pending")
        self.assertIn("attempt-1", store.unknown_attempts)
        inner = self.inner("connection-1", "thread-1")
        self.trace[0] = task_trace("intent-1", call="call-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: self.create(runtime))
        self.assertEqual(inner.calls, [])

    def test_failed_durable_mark_falls_back_to_the_project_guard(self) -> None:
        store, _ = self.open()
        port = FakePort()
        port.responses["run_once"] = {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                                      "runtime_job_id": "job-1", "runtime_job_status": "collector_lost"}
        bridge = self.bridge(port, store)
        failure = VmBridgeError(BridgeCode.BINDING_STORE_FAILED, subject="binding_store")
        with mock.patch.object(store, "mark_unknown", side_effect=failure):
            bridge.run_once_hosted(HostedRunOnceRequest("run-1", "project-1"))
        self.assertIn("project-1", bridge.unresolved_projects)
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        self.expect(BridgeCode.RECONCILIATION_REQUIRED,
                    lambda: bridge.run_once_hosted(HostedRunOnceRequest("run-2", "project-1")),
                    subject="vm_project_id")
        self.assertEqual(port.names().count("run_once"), 1)


# ----- F5: one journal for bridge and runtime -------------------------------

class StoreIdentityTests(ReviewFixture):
    def hosted_port(self, runtime) -> EngineApplicationPort:
        return EngineApplicationPort(FakeEngineApp("service-1", runtime=runtime),
                                     surface_revision=PINNED_SURFACE_REVISION, engine_schema_revision=4,
                                     error_types=(None, None), hosted_runtime=runtime)

    def test_bridge_store_must_be_the_runtime_journal(self) -> None:
        store, runtime = self.open()
        port = self.hosted_port(runtime)
        self.assertIs(port.hosted_runtime, runtime)
        other = ExecutionBindingStore(self.root / "other" / "bindings.sqlite3")
        self.expect(BridgeCode.INVALID_REQUEST, lambda: self.bridge(port, other), subject="binding_store")
        self.bridge(port, store)
        self.bridge(port, ExecutionBindingStore(self.journal_path))
        self.assertEqual(port.calls, ())

    def test_port_without_hosted_runtime_attribute_is_the_callers_responsibility(self) -> None:
        other = ExecutionBindingStore(self.root / "other" / "bindings.sqlite3")
        self.bridge(FakePort(), other)


# ----- F6: placement task cross-check ---------------------------------------

class PlacementTaskTests(ReviewFixture):
    def test_placement_task_must_equal_the_owner_task(self) -> None:
        store, runtime = self.open()
        self.assignment[0] = make_assignment(task_id="task-9")
        self.dispatch_attempt(runtime)
        self.assertEqual(store.read_attempt("attempt-1").placement_vm_task_id, "task-9")
        port = FakePort()
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: self.bridge(port, store).confirm_attempt("project-1", "attempt-1"),
                    subject="vm_task_id", outcome=KNOWN)
        self.assertIn("attempt_detail", port.names())
        self.assertIsNone(store.read_execution_binding("attempt-1"))

    def test_matching_placement_task_confirms(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        self.assertEqual(store.read_attempt("attempt-1").placement_vm_task_id, "task-1")
        confirmation = self.bridge(FakePort(), store).confirm_attempt("project-1", "attempt-1")
        self.assertEqual((confirmation.state, confirmation.binding_digest),
                         ("confirmed", expected_binding().digest()))


# ----- F7: header check before SQLite opens the file ------------------------

class JournalHeaderTests(ReviewFixture):
    def sqlite_file(self, path: Path, application_id: int = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(path))) as connection:
            connection.execute(f"PRAGMA application_id = {application_id}")
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('synthetic')")
            connection.commit()

    def rejected_unchanged(self, path: Path, *companions: Path) -> None:
        before = [(item, item.read_bytes()) for item in (path, *companions)]
        with self.assertRaises(InvalidDatabase):
            ExecutionBindingStore(path)
        for item, data in before:
            with self.subTest(file=item.name):
                self.assertEqual(item.read_bytes(), data)

    def test_foreign_or_damaged_files_are_rejected_without_changes(self) -> None:
        foreign = self.root / "a" / "bindings.sqlite3"
        self.sqlite_file(foreign)
        rollback = foreign.with_name(foreign.name + "-journal")
        rollback.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 504)
        self.rejected_unchanged(foreign, rollback)

        other_aik = self.root / "b" / "bindings.sqlite3"
        self.sqlite_file(other_aik, OTHER_AIK_APPLICATION_ID)
        self.rejected_unchanged(other_aik)

        own_id_wrong_schema = self.root / "c" / "bindings.sqlite3"
        self.sqlite_file(own_id_wrong_schema, BINDING_JOURNAL_APPLICATION_ID)
        self.rejected_unchanged(own_id_wrong_schema)

        garbage = self.root / "d" / "bindings.sqlite3"
        garbage.parent.mkdir(parents=True)
        garbage.write_bytes(b"not a database at all " * 10)
        self.rejected_unchanged(garbage)

        short = self.root / "e" / "bindings.sqlite3"
        short.parent.mkdir(parents=True)
        short.write_bytes(b"SQLite format 3\x00")
        self.rejected_unchanged(short)

    def test_empty_file_with_a_rollback_journal_is_rejected(self) -> None:
        empty = self.root / "f" / "bindings.sqlite3"
        empty.parent.mkdir(parents=True)
        empty.write_bytes(b"")
        rollback = empty.with_name(empty.name + "-journal")
        rollback.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 504)
        self.rejected_unchanged(empty, rollback)
        self.assertEqual(empty.stat().st_size, 0)

    def test_own_journal_reopens(self) -> None:
        store = ExecutionBindingStore(self.journal_path)
        store.mark_unknown(thread_id="thread-x")
        self.assertEqual(ExecutionBindingStore(self.journal_path).unknown_threads, frozenset({"thread-x"}))


# ----- F8: durable write failure before the effect --------------------------

class DurableWriteFailureTests(ReviewFixture):
    def test_record_effect_failure_never_calls_the_inner_runtime(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-1")
        self.trace[0] = task_trace("intent-1", call="call-1")
        with mock.patch.object(store._journal, "record_effect", side_effect=StorageBusy("synthetic busy")):
            self.expect(BridgeCode.BINDING_STORE_FAILED, lambda: self.create(runtime), outcome=KNOWN)
        self.assertEqual(inner.calls, [])
        self.assertIsNone(store.read_attempt("attempt-1"))
        self.assertEqual(store.effect_records(), ())
        self.assertEqual(store.inflight_effects(), ())

    def test_record_effect_failure_on_an_existing_thread_never_calls_the_inner_runtime(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        inner = self.inners["connection-1"]
        before = len(store.effect_records())
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        with mock.patch.object(store._journal, "record_effect", side_effect=StorageBusy("synthetic busy")):
            self.expect(BridgeCode.BINDING_STORE_FAILED, lambda: self.start(runtime), outcome=KNOWN)
        self.assertEqual(inner.ops(), ["create_thread", "start_turn"])
        self.assertEqual(len(store.effect_records()), before)
        self.assertEqual(store.inflight_effects(), ())


# ----- F9: asynchronous dispatch --------------------------------------------

class AsynchronousDispatchTests(ReviewFixture):
    def test_binding_pending_then_confirmed_after_supervisor_dispatch(self) -> None:
        store, runtime = self.open_default()
        self.inner("connection-1", "thread-1")
        trace = FakeTraceModule()
        app = FakeEngineApp("service-1", runtime=runtime)
        port = EngineApplicationPort(app, surface_revision=PINNED_SURFACE_REVISION, engine_schema_revision=4,
                                     error_types=(None, None), hosted_runtime=runtime)
        bridge = self.bridge(port, store)
        begin, created, proceed = threading.Event(), threading.Event(), threading.Event()
        errors: list = []
        workers: list = []

        def supervisor():
            try:
                if not begin.wait(JOIN_TIMEOUT):
                    raise TimeoutError("begin")
                with trace.operation_scope(attempt_id="attempt-1", intent_id="intent-1", call_id="call-1"):
                    self.create(app.runtime)
                created.set()
                if not proceed.wait(JOIN_TIMEOUT):
                    raise TimeoutError("proceed")
                with trace.operation_scope(attempt_id="attempt-1", intent_id="intent-2", call_id="call-2",
                                           thread_id="thread-1"):
                    self.start(app.runtime)
            except BaseException as error:  # reported to the main thread
                errors.append(error)
            finally:
                created.set()

        def dispatch():
            worker = threading.Thread(target=supervisor, name="fake-runtime-supervisor")
            workers.append(worker)
            worker.start()

        app.dispatch = dispatch
        with trace.install():
            receipt, confirmation = bridge.run_once_hosted(HostedRunOnceRequest("run-1", "project-1"))
            self.assertEqual(receipt.job.job_id, "job-1")
            self.assertEqual((confirmation.state, confirmation.binding_digest), ("binding_pending", None))
            self.assertIsNone(store.read_execution_binding("attempt-1"))

            begin.set()
            self.assertTrue(created.wait(JOIN_TIMEOUT))
            self.assertEqual(errors, [])
            # Only the create intent is bound so far; the first intent fixes the binding.
            partial = bridge.confirm_attempt("project-1", "attempt-1")
            self.assertEqual((partial.state, partial.binding_digest), ("confirmed", expected_binding().digest()))

            proceed.set()
            workers[0].join(JOIN_TIMEOUT)
            self.assertFalse(workers[0].is_alive())
            self.assertEqual(errors, [])
            final = bridge.confirm_attempt("project-1", "attempt-1")
            self.assertEqual(final.binding_digest, partial.binding_digest)
        self.assertEqual(store.read_execution_binding("attempt-1"),
                         {"vm_attempt_id": "attempt-1", "binding_digest": expected_binding().digest()})
        self.assertEqual(self.inners["connection-1"].ops(), ["create_thread", "start_turn"])
        self.assertTrue(trace.reader_threads)
        self.assertEqual({workers[0].ident}, set(trace.reader_threads))
        self.assertEqual(store.inflight_effects(), ())


if __name__ == "__main__":
    unittest.main()
