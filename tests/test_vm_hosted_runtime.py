"""AIK-031/008 hosted runtime tests (``aik-hosted-runtime-v1``) with fakes only.

The VM design seams are simulated here: an injected runtime that implements the
VM ``CodexRuntimePort``, an operation trace scope reader, and an application
port that exposes owner queries (``attempt_detail``/``status``). No VM
installation, provider, credential, network or subprocess is used. Every
journal lives in a disposable directory outside the repository.

Passing on Linux is development verification only. The installed VM, Windows
RuntimeJob, real models and AGS-governed dispatch remain NOT_RUN.
"""
from __future__ import annotations

from contextlib import closing
import copy
from datetime import datetime, timezone
import inspect
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import Lease, Placement, Pool  # noqa: E402
from aik_runtime.errors import InvalidDatabase, UnsafeStoragePath  # noqa: E402
from aik_vm_bridge.binding import TaskExecutionBinding  # noqa: E402
from aik_vm_bridge.bridge import (  # noqa: E402
    CancelRequest, HostedConfirmation, HostedRunOnceRequest, InventoryRequest, JobPhase, ObserveRequest,
    RunOnceRequest, VmBridge,
)
from aik_vm_bridge.engine_port import (  # noqa: E402
    EngineApplicationPort, EnginePortError, build_hosted_application,
)
from aik_vm_bridge.errors import BridgeCode, EffectOutcome, VmBridgeError  # noqa: E402
from aik_vm_bridge.hosted_runtime import (  # noqa: E402
    EFFECT_OPERATIONS, HOSTED_ASSIGNMENT_FIELDS, AikHostedRuntime, ExecutionBindingStore, HostedAssignment,
    TraceRef, read_operation_trace,
)
from aik_vm_bridge.surface import (  # noqa: E402
    BINDING_EXTENSION, HOSTED_RUNTIME_EXTENSION, PINNED_SURFACE_REVISION,
)


SECRET = "SYNTHETIC_SECRET_MUST_NOT_LEAK"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2029, 6, 1, tzinfo=timezone.utc)
AFTER_EXPIRY = datetime(2030, 6, 1, tzinfo=timezone.utc)
KNOWN = EffectOutcome.KNOWN_FAILURE
UNKNOWN = EffectOutcome.UNKNOWN
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

_P = inspect.Parameter
_E = inspect.Parameter.empty
_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KO = inspect.Parameter.KEYWORD_ONLY
# VM CodexRuntimePort (vibemarshal 32bb0f9), contract item 5.
EXPECTED_PORT_SIGNATURES = {
    "verify_execution_policy": (("self", _POK, _E), ("cwd", _POK, _E)),
    "list_models": (("self", _POK, _E),),
    "create_thread": (("self", _POK, _E), ("cwd", _KO, _E), ("title", _KO, _E), ("model", _KO, _E),
                      ("developer_instructions", _KO, _E), ("ephemeral", _KO, False)),
    "start_turn": (("self", _POK, _E), ("thread_id", _KO, _E), ("cwd", _KO, _E), ("prompt", _KO, _E),
                   ("model", _KO, _E), ("effort", _KO, _E), ("output_schema", _KO, None)),
    "read": (("self", _POK, _E), ("thread_id", _KO, _E)),
    "resume": (("self", _POK, _E), ("thread_id", _KO, _E), ("cwd", _KO, _E)),
    "interrupt": (("self", _POK, _E), ("thread_id", _KO, _E), ("turn_id", _KO, _E), ("timeout_seconds", _KO, 5.0)),
    "read_stored": (("self", _POK, _E), ("thread_id", _KO, _E), ("turn_id", _KO, None),
                    ("timeout_seconds", _KO, 5.0)),
    "close": (("self", _POK, _E), ("timeout_seconds", _KO, 5.0)),
}


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
                    route_revision: str = "3", fencing: int = 5, lease_fencing: int | None = None,
                    expires_at: str = "2030-01-01T00:00:00Z", lease_state: str = "active",
                    enabled: bool = True, placement_state: str = "assigned") -> HostedAssignment:
    pool = Pool("pool-1", "synthetic", connection_id, enabled=enabled)
    placement = Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "project-1", "task-1",
                          attempt_id, state=placement_state)
    lease = Lease("lease-1", "placement-1", "pool-1", "worker-1",
                  fencing if lease_fencing is None else lease_fencing, expires_at, state=lease_state)
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


def expected_binding(goal: str = "goal-rev-1", plan: str = "plan-rev-1", **overrides) -> TaskExecutionBinding:
    values = make_assignment().to_mapping()
    values.update(vm_goal_revision_id=goal, vm_plan_revision_id=plan, vm_task_id="task-1",
                  vm_attempt_id="attempt-1", vm_execution_spec_digest=DIGEST_A,
                  vm_runtime_intent_id="intent-1", vm_provider_call_id="call-1")
    values.update(overrides)
    return TaskExecutionBinding.from_mapping(values)


def attempt_detail_response(*, project="project-1", plan="plan-rev-1", digest=DIGEST_A) -> dict:
    """Shape after engine_port normalization (attempt.* lifted to the top level)."""
    attempt = {"attempt_id": "attempt-1", "task_id": "task-1", "attempt_no": 1, "kind": "worker",
               "status": "running", "binding": None, "failure_class": None}
    data = {"project_id": project, "plan_revision_id": plan, "task_ref": {"task_id": "task-1"},
            "attempt": attempt, "attempt_id": "attempt-1", "task_id": "task-1", "attempt_status": "running",
            "intents": []}
    if digest is not None:
        attempt["execution_spec_digest"] = digest
        data["execution_spec_digest"] = digest
    return data


def status_response(goal="goal-rev-1", plan="plan-rev-1") -> dict:
    project = {"id": "project-1", "root": "/synthetic/project-1"}
    data = {"project_id": "project-1", "project_root": "/synthetic/project-1", "control_state": "running",
            "project": project}
    if goal is not None:
        project["active_goal_revision_id"] = goal
        data["active_goal_revision_id"] = goal
    if plan is not None:
        project["active_plan_revision_id"] = plan
        data["active_plan_revision_id"] = plan
        data["plan_revision_id"] = plan
    return data


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
        self.receipt_override = None

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
        if self.receipt_override is not None:
            return self._result(self.receipt_override)
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
    """A VmApplicationPort fake; ``dispatch`` simulates VM calling the injected runtime."""

    def __init__(self, *, extensions=(HOSTED_RUNTIME_EXTENSION,)):
        self.calls: list = []
        self.failures: dict = {}
        self.dispatch = None
        self.handshake_value = {
            "surface_revision": PINNED_SURFACE_REVISION, "engine_schema_revision": 4,
            "methods": dict(SIGNATURES), "extensions": list(extensions),
        }
        self.responses = {
            "prepare": {"project_id": "project-1"},
            "revise": {"project_id": "project-1"},
            "authorize": {"project_id": "project-1", "authorization_id": "authorization-1",
                          "plan_revision_id": "plan-rev-1"},
            "replan": {"project_id": "project-1"},
            "pause": {"project_id": "project-1", "state": "paused"},
            "cancel": {"project_id": "project-1", "state": "cancel_requested"},
            "status": status_response(),
            "final_report": {"project_id": "project-1"},
            "attempt_detail": attempt_detail_response(),
            "run_once": {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                         "runtime_job_id": "job-1", "runtime_job_kind": "worker_turn",
                         "runtime_job_status": "scheduled"},
            "observe": {"project_id": "project-1", "attempt_id": "attempt-1",
                        "job": {"job_id": "job-1", "status": "consumed"}},
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


class FakeBoundPort(FakePort):
    """The pre-existing (assumed) upstream binding extension path."""

    def __init__(self):
        super().__init__(extensions=(BINDING_EXTENSION,))
        self.persisted: dict = {}

    def bind_execution(self, binding):
        self.calls.append(("bind_execution", (dict(binding),), {}))
        digest = TaskExecutionBinding.from_mapping(dict(binding)).digest()
        self.persisted[binding["vm_attempt_id"]] = digest
        return {"binding_digest": digest}

    def read_execution_binding(self, vm_attempt_id):
        self.calls.append(("read_execution_binding", (vm_attempt_id,), {}))
        digest = self.persisted.get(vm_attempt_id)
        return None if digest is None else {"binding_digest": digest}


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
        return {"runtime_job": {"job_id": "job-1", "status": "consumed", "attempt_id": "attempt-1",
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

class HostedFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-hosted-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal_path = self.root / "state" / "bindings.sqlite3"
        self.cwd = self.root / "workspace"
        self.clock = [NOW]
        self.trace = [None]
        self.assignment = [make_assignment()]
        self.source_calls: list = []
        self.runtime_for_calls: list = []
        self.runtime_failures: set = set()
        self.inners: dict = {}

    # injected collaborators
    def runtime_for(self, connection_id):
        self.runtime_for_calls.append(connection_id)
        if connection_id in self.runtime_failures:
            raise RuntimeError(SECRET)
        return self.inners.setdefault(connection_id, FakeInner(connection_id))

    def source(self, cwd, trace):
        self.source_calls.append((cwd, trace))
        value = self.assignment[0]
        if isinstance(value, Exception):
            raise value
        return value

    def open(self):
        store = ExecutionBindingStore(self.journal_path)
        runtime = AikHostedRuntime(self.runtime_for, store, self.source, trace_reader=lambda: self.trace[0],
                                   now=lambda: self.clock[0])
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
        """What VM would do inside run_once: create then start, under its trace scope."""
        self.inner(connection, thread)
        self.trace[0] = task_trace("intent-1", call="call-1")
        self.create(runtime)
        self.trace[0] = task_trace("intent-2", call="call-2", thread=thread)
        self.start(runtime, thread)
        self.trace[0] = None

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

    def tamper(self, table: str, column: str, old: str, new: str) -> None:
        """Bypass the immutability trigger to simulate external damage (schema kept identical)."""
        trigger = f"{table}_no_update"
        with closing(sqlite3.connect(str(self.journal_path), isolation_level=None)) as connection:
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                                     (trigger,)).fetchone()[0]
            connection.execute(f'DROP TRIGGER "{trigger}"')
            changed = connection.execute(f'UPDATE "{table}" SET "{column}" = replace("{column}", ?, ?)',
                                         (old, new)).rowcount
            connection.execute(sql)
        self.assertGreater(changed, 0)


# ----- hosted runtime -------------------------------------------------------

class HostedRuntimeTests(HostedFixture):
    def test_case13_every_method_signature_equals_the_vm_runtime_port(self) -> None:
        for name, expected in EXPECTED_PORT_SIGNATURES.items():
            with self.subTest(method=name):
                function = getattr(AikHostedRuntime, name)
                observed = tuple((p.name, p.kind, p.default)
                                 for p in inspect.signature(function).parameters.values())
                self.assertEqual(observed, expected)
        self.assertEqual(set(EFFECT_OPERATIONS), {"create_thread", "start_turn", "resume", "interrupt"})

    def test_case1_create_then_start_records_attempt_and_intent_before_the_inner_call(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-1")
        seen = []

        def at_create():
            stored = store.read_attempt("attempt-1")
            seen.append(("create", None if stored is None else [i.vm_intent_id for i in stored.intents],
                         store.route_for_thread("thread-1")))

        def at_start():
            stored = store.read_attempt("attempt-1")
            seen.append(("start", [i.vm_intent_id for i in stored.intents]))

        inner.hooks["create_thread"] = at_create
        inner.hooks["start_turn"] = at_start
        # A VM-like scope object with extra fields the runtime must ignore.
        self.trace[0] = SimpleNamespace(trace=object(), parent=None, deadline_monotonic_ns=1, deadline_at=None,
                                        call_id="call-1", attempt_id="attempt-1", intent_id="intent-1",
                                        thread_id=None, turn_id=None)
        receipt = self.create(runtime, title=SECRET, developer_instructions=SECRET)
        self.assertIs(receipt, inner.returned[-1])
        self.assertEqual(seen[0], ("create", ["intent-1"], None))
        self.assertEqual(inner.calls[0], ("create_thread", dict(cwd=self.cwd, title=SECRET, model="synthetic-model",
                                                                developer_instructions=SECRET, ephemeral=False)))
        route = store.route_for_thread("thread-1")
        self.assertEqual((route.connection_id, route.kind, route.vm_attempt_id), ("connection-1", "task", "attempt-1"))

        self.trace[0] = task_trace("intent-2", call="call-2", thread="thread-1")
        started = self.start(runtime, prompt=SECRET)
        self.assertIs(started, inner.returned[-1])
        self.assertEqual(seen[1], ("start", ["intent-1", "intent-2"]))
        self.assertEqual(inner.calls[1], ("start_turn", dict(thread_id="thread-1", cwd=self.cwd, prompt=SECRET,
                                                             model="synthetic-model", effort="high",
                                                             output_schema=None)))
        stored = store.read_attempt("attempt-1")
        self.assertEqual(stored.vm_project_id, "project-1")
        self.assertEqual(stored.assignment_digest, make_assignment().assignment_digest())
        self.assertEqual((stored.first_intent().vm_intent_id, stored.first_intent().vm_call_id),
                         ("intent-1", "call-1"))
        self.assertFalse(stored.unknown)
        records = store.effect_records()
        self.assertEqual([(r["operation"], r["vm_attempt_id"], r["vm_intent_id"], r["connection_id"],
                           r["lease_usable"]) for r in records],
                         [("create_thread", "attempt-1", "intent-1", "connection-1", True),
                          ("start_turn", "attempt-1", "intent-2", "connection-1", True)])
        # Prompts, titles and instructions are never journaled.
        self.assertNotIn(SECRET.encode("ascii"), self.journal_path.read_bytes())
        # No owner binding exists until VM owner confirmation.
        self.assertIsNone(store.read_execution_binding("attempt-1"))

    def test_case2_missing_assignment_or_unusable_lease_never_calls_the_inner_runtime(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-1")
        cases = (
            (None, BridgeCode.SEAM_UNAVAILABLE, "assignment"),
            (RuntimeError(SECRET), BridgeCode.SEAM_UNAVAILABLE, "assignment_source"),
            (make_assignment(expires_at="2029-01-01T00:00:00Z"), BridgeCode.LEASE_NOT_USABLE, "lease_expired"),
            (make_assignment(enabled=False), BridgeCode.LEASE_NOT_USABLE, "pool_enabled"),
            (make_assignment(lease_state="released"), BridgeCode.LEASE_NOT_USABLE, "lease_state"),
            (make_assignment(placement_state="released"), BridgeCode.LEASE_NOT_USABLE, "placement_state"),
            (make_assignment(lease_fencing=6), BridgeCode.BINDING_MISMATCH, "lease_fencing_token"),
            (make_assignment(attempt_id="attempt-9"), BridgeCode.BINDING_MISMATCH, "vm_attempt_id"),
        )
        self.trace[0] = task_trace("intent-1", call="call-1")
        for value, code, subject in cases:
            with self.subTest(subject=subject):
                self.assignment[0] = value
                self.expect(code, lambda: self.create(runtime), subject=subject, outcome=KNOWN)
        forged = make_assignment()
        object.__setattr__(forged, "lease_fencing_token", 0)
        self.assignment[0] = forged
        self.expect(BridgeCode.INVALID_REQUEST, lambda: self.create(runtime))
        self.assertEqual(inner.calls, [])
        self.assertEqual(store.effect_records(), ())
        self.assertIsNone(store.read_attempt("attempt-1"))
        self.assertEqual(runtime.unknown_attempts, frozenset())

    def test_case2_durable_binding_failure_never_calls_the_inner_runtime(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-1")
        self.journal_path.unlink()
        self.trace[0] = task_trace("intent-1", call="call-1")
        self.expect(BridgeCode.BINDING_STORE_FAILED, lambda: self.create(runtime), outcome=KNOWN)
        self.assertEqual(inner.calls, [])

    def test_case3_another_assignment_for_the_same_attempt_is_a_mismatch_without_effect(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-1", "thread-2")
        self.trace[0] = task_trace("intent-1", call="call-1")
        self.create(runtime)
        self.trace[0] = task_trace("intent-3", call="call-3")
        self.assignment[0] = make_assignment(route_revision="4")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.create(runtime), subject="assignment", outcome=KNOWN)
        # A rotated account cannot take over the Attempt either.
        self.assignment[0] = make_assignment("connection-2")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.create(runtime), subject="assignment")
        self.assertEqual(inner.ops(), ["create_thread"])
        self.assertNotIn("connection-2", self.inners)
        self.assertEqual([i.vm_intent_id for i in store.read_attempt("attempt-1").intents], ["intent-1"])
        self.assertEqual(len(store.effect_records()), 1)

    def test_case4_restart_keeps_threads_on_the_original_connection_despite_rotation(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        store2, runtime2 = self.restart()
        self.assignment[0] = make_assignment("connection-2")  # current selection rotated
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        runtime2.resume(thread_id="thread-1", cwd=self.cwd)
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.start(runtime2)
        self.trace[0] = task_trace("intent-5", call="call-5", thread="thread-1", turn="turn-1")
        runtime2.interrupt(thread_id="thread-1", turn_id="turn-1")
        self.trace[0] = None
        observed = runtime2.read(thread_id="thread-1")
        stored = runtime2.read_stored(thread_id="thread-1")
        inner = self.inners["connection-1"]
        self.assertIs(observed, inner.returned[-2])
        self.assertIs(stored, inner.returned[-1])
        self.assertEqual(inner.ops(), ["resume", "start_turn", "interrupt", "read", "read_stored"])
        self.assertEqual(inner.calls[2][1], dict(thread_id="thread-1", turn_id="turn-1", timeout_seconds=5.0))
        self.assertEqual(inner.calls[4][1], dict(thread_id="thread-1", turn_id=None, timeout_seconds=5.0))
        self.assertNotIn("connection-2", self.inners)
        self.assertEqual(set(self.runtime_for_calls), {"connection-1"})
        self.assertTrue(all(r["connection_id"] == "connection-1" for r in store2.effect_records()))
        self.assertEqual([i.vm_intent_id for i in store2.read_attempt("attempt-1").intents],
                         ["intent-1", "intent-2", "intent-3", "intent-4", "intent-5"])

    def test_case4_unavailable_original_connection_is_refused_without_switching_accounts(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        store2, runtime2 = self.restart()
        self.assignment[0] = make_assignment("connection-2")
        self.runtime_failures.add("connection-1")
        before = len(store2.effect_records())
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: runtime2.resume(thread_id="thread-1", cwd=self.cwd),
                    subject="connection_runtime")
        self.trace[0] = None
        self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: runtime2.read(thread_id="thread-1"),
                    subject="connection_runtime")
        self.assertNotIn("connection-2", self.inners)
        self.assertNotIn("connection-2", self.runtime_for_calls)
        self.assertEqual(len(store2.effect_records()), before)

    def test_interrupt_tolerates_an_unusable_lease_but_resume_does_not(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        inner = self.inners["connection-1"]
        self.clock[0] = AFTER_EXPIRY
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        self.expect(BridgeCode.LEASE_NOT_USABLE, lambda: runtime.resume(thread_id="thread-1", cwd=self.cwd),
                    subject="lease_expired")
        self.expect(BridgeCode.LEASE_NOT_USABLE, lambda: self.start(runtime), subject="lease_expired")
        self.assertNotIn("resume", inner.ops())
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1", turn="turn-1")
        runtime.interrupt(thread_id="thread-1", turn_id="turn-1", timeout_seconds=2.0)
        self.assertEqual(inner.calls[-1], ("interrupt", dict(thread_id="thread-1", turn_id="turn-1",
                                                             timeout_seconds=2.0)))
        last = store.effect_records()[-1]
        self.assertEqual((last["operation"], last["lease_usable"], last["connection_id"]),
                         ("interrupt", False, "connection-1"))
        # A released lease is tolerated for interrupt as well.
        self.clock[0] = NOW
        self.assignment[0] = make_assignment(lease_state="released")
        self.trace[0] = task_trace("intent-5", call="call-5", thread="thread-1", turn="turn-1")
        runtime.interrupt(thread_id="thread-1", turn_id="turn-1")
        self.assertFalse(store.effect_records()[-1]["lease_usable"])
        # A different placement/lease/fencing is never tolerated, not even for interrupt.
        self.assignment[0] = make_assignment(fencing=6)
        self.trace[0] = task_trace("intent-6", call="call-6", thread="thread-1", turn="turn-1")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: runtime.interrupt(thread_id="thread-1", turn_id="turn-1"),
                    subject="lease_fencing_token")
        self.assertEqual(inner.ops().count("interrupt"), 2)

    def test_existing_thread_requires_its_route_attempt_and_thread_identity(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        inner = self.inners["connection-1"]
        self.trace[0] = task_trace("intent-9", call="call-9", thread="thread-unknown")
        self.expect(BridgeCode.THREAD_ROUTE_UNKNOWN, lambda: self.start(runtime, "thread-unknown"),
                    subject="thread_id")
        self.trace[0] = task_trace("intent-9", call="call-9", thread="thread-1", attempt="attempt-2")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.start(runtime), subject="vm_attempt_id")
        self.trace[0] = task_trace("intent-9", call="call-9", thread="thread-other")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.start(runtime), subject="thread_id")
        self.trace[0] = None
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.start(runtime), subject="vm_attempt_id")
        self.assertEqual(inner.ops().count("start_turn"), 1)

    def test_case5_same_thread_id_on_two_connections_is_rejected_on_lookup(self) -> None:
        store, runtime = self.open()
        first = self.inner("connection-1", "thread-dup")
        second = self.inner("connection-2", "thread-dup")
        self.create(runtime)  # preparation call (no trace)
        self.assignment[0] = make_assignment("connection-2")
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.create(runtime), subject="thread_id_collision",
                    outcome=UNKNOWN)
        self.assertIn("thread-dup", runtime.unknown_threads)
        self.expect(BridgeCode.THREAD_ID_COLLISION, lambda: store.route_for_thread("thread-dup"))
        self.expect(BridgeCode.THREAD_ID_COLLISION, lambda: runtime.read(thread_id="thread-dup"))
        self.expect(BridgeCode.THREAD_ID_COLLISION, lambda: self.start(runtime, "thread-dup"))
        self.assertEqual(first.ops(), ["create_thread"])
        self.assertEqual(second.ops(), ["create_thread"])
        store2, runtime2 = self.restart()
        self.expect(BridgeCode.THREAD_ID_COLLISION, lambda: store2.route_for_thread("thread-dup"))
        self.assertIn("thread-dup", store2.unknown_threads)

    def test_case6_inner_failure_is_unknown_and_blocks_until_explicit_reconciliation(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        inner = self.inners["connection-1"]
        inner.failures["start_turn"] = RuntimeError(SECRET)
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        error = self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.start(runtime), subject="start_turn",
                            outcome=UNKNOWN)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        del inner.failures["start_turn"]
        self.assertIn("attempt-1", runtime.unknown_attempts)
        self.assertIn("thread-1", runtime.unknown_threads)
        self.assertIn("attempt-1", store.unknown_attempts)
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime.resume(thread_id="thread-1", cwd=self.cwd))
        self.trace[0] = task_trace("intent-4", call="call-4")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: self.create(runtime), subject="vm_attempt_id")
        self.assertEqual(inner.ops().count("start_turn"), 2)
        self.assertNotIn("resume", inner.ops())

        # The block is durable: a new process is blocked by the journal.
        store2, runtime2 = self.restart()
        self.trace[0] = task_trace("intent-4", call="call-4", thread="thread-1")
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime2.resume(thread_id="thread-1", cwd=self.cwd))
        self.assertEqual(self.inners.get("connection-1", FakeInner("x")).ops(), [])
        self.assertTrue(runtime2.reconcile_thread("thread-1"))
        self.expect(BridgeCode.RECONCILIATION_REQUIRED, lambda: runtime2.resume(thread_id="thread-1", cwd=self.cwd),
                    subject="vm_attempt_id")
        self.assertTrue(runtime2.reconcile_attempt("attempt-1"))
        self.assertEqual((runtime2.unknown_attempts, runtime2.unknown_threads), (frozenset(), frozenset()))
        runtime2.resume(thread_id="thread-1", cwd=self.cwd)
        self.assertEqual(self.inners["connection-1"].ops(), ["resume"])

    def test_case6_create_failure_or_unreadable_thread_id_marks_the_attempt_unknown(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1")
        inner.failures["create_thread"] = TimeoutError(SECRET)
        self.trace[0] = task_trace("intent-1", call="call-1")
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.create(runtime), subject="create_thread",
                    outcome=UNKNOWN)
        self.assertIn("attempt-1", store.unknown_attempts)

        self.journal_path = self.root / "state" / "other.sqlite3"
        store2, runtime2 = self.restart()
        inner2 = self.inner("connection-1")
        inner2.receipt_override = SimpleNamespace(operation_id="op-1", payload={}, binding=None)
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: self.create(runtime2), subject="thread_id", outcome=UNKNOWN)
        self.assertIn("attempt-1", store2.unknown_attempts)
        self.assertEqual(inner2.ops(), ["create_thread"])

    def test_case7_preparation_calls_record_only_a_thread_route(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-prep", "thread-prep-2")
        self.trace[0] = None
        self.create(runtime)
        route = store.route_for_thread("thread-prep")
        self.assertEqual((route.kind, route.vm_attempt_id, route.connection_id),
                         ("preparation", None, "connection-1"))
        # A planning-role scope without an Attempt is also a preparation call.
        self.trace[0] = SimpleNamespace(attempt_id=None, intent_id=None, call_id="call-plan", thread_id=None,
                                        turn_id=None)
        self.create(runtime)
        self.assertEqual(store.route_for_thread("thread-prep-2").kind, "preparation")
        self.assertIsNone(store.read_attempt("attempt-1"))
        self.assertIsNone(store.read_execution_binding("attempt-1"))
        self.assertTrue(all(r["vm_attempt_id"] is None and r["vm_intent_id"] is None
                            for r in store.effect_records()))
        self.trace[0] = None
        self.start(runtime, "thread-prep")
        self.assertEqual(inner.ops(), ["create_thread", "create_thread", "start_turn"])
        self.trace[0] = task_trace("intent-1", call="call-1", thread="thread-prep")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.start(runtime, "thread-prep"), subject="vm_attempt_id")
        self.assertIsNone(store.read_attempt("attempt-1"))

    def test_case8_attempt_without_intent_is_refused_before_any_effect(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1", "thread-1")
        self.trace[0] = TraceRef(attempt_id="attempt-1", call_id="call-1")
        self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: self.create(runtime), subject="trace_intent_id")
        self.expect(BridgeCode.SEAM_UNAVAILABLE,
                    lambda: store.bind_effect(TraceRef(attempt_id="attempt-1"), make_assignment(), "create_thread"),
                    subject="trace_intent_id")
        self.assertEqual(inner.calls, [])
        self.assertEqual(store.effect_records(), ())

    def test_trace_reader_failure_or_malformed_scope_is_refused(self) -> None:
        store = ExecutionBindingStore(self.journal_path)

        def broken():
            raise RuntimeError(SECRET)

        runtime = AikHostedRuntime(self.runtime_for, store, self.source, trace_reader=broken,
                                   now=lambda: self.clock[0])
        self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: self.create(runtime), subject="trace_scope")
        self.trace[0] = SimpleNamespace(attempt_id="bad attempt", intent_id="intent-1", call_id=None,
                                        thread_id=None, turn_id=None)
        _, runtime2 = self.open()
        self.expect(BridgeCode.INVALID_RESPONSE, lambda: self.create(runtime2), subject="trace_scope")
        self.assertEqual(self.inners, {})

    def test_default_trace_reader_outside_vm_dispatch_reports_no_scope(self) -> None:
        # Without the VM package, or outside a VM operation scope, there is no scope.
        self.assertIsNone(read_operation_trace())
        for name in ("hosted_runtime.py", "engine_port.py"):
            text = (ROOT / "src/aik_vm_bridge" / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertNotIn("import flowmarshal", text)
                self.assertNotIn("from flowmarshal", text)

    def test_queries_delegate_to_the_assigned_connection_without_effect_records(self) -> None:
        store, runtime = self.open()
        inner = self.inner("connection-1")
        self.trace[0] = task_trace("intent-1")
        policy = runtime.verify_execution_policy(str(self.cwd))
        models = runtime.list_models()
        self.assertIs(policy, inner.returned[0])
        self.assertIs(models, inner.returned[1])
        self.assertEqual(inner.calls, [("verify_execution_policy", {"cwd": str(self.cwd)}), ("list_models", {})])
        self.assertEqual(self.source_calls[0], (self.cwd, task_trace("intent-1")))
        self.assertEqual(self.source_calls[1], (None, task_trace("intent-1")))
        self.assertEqual(store.effect_records(), ())
        self.assignment[0] = None
        self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: runtime.list_models(), subject="assignment")

    def test_query_failure_is_known_and_adds_no_unknown_marks(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        self.inners["connection-1"].failures["read"] = OSError(SECRET)
        self.expect(BridgeCode.QUERY_FAILED, lambda: runtime.read(thread_id="thread-1"), subject="read",
                    outcome=KNOWN)
        self.assertEqual((runtime.unknown_attempts, runtime.unknown_threads), (frozenset(), frozenset()))

    def test_close_closes_every_inner_runtime_and_reports_a_failure_last(self) -> None:
        store, runtime = self.open()
        first = self.inner("connection-1")
        second = self.inner("connection-2")
        first.failures["close"] = RuntimeError(SECRET)
        runtime.verify_execution_policy(self.cwd)
        self.assignment[0] = make_assignment("connection-2")
        runtime.verify_execution_policy(self.cwd)
        self.expect(BridgeCode.OUTCOME_UNKNOWN, lambda: runtime.close(timeout_seconds=1.5), subject="close")
        self.assertEqual(first.calls[-1], ("close", {"timeout_seconds": 1.5}))
        self.assertEqual(second.calls[-1], ("close", {"timeout_seconds": 1.5}))
        self.expect(BridgeCode.SEAM_UNAVAILABLE, lambda: runtime.list_models(), subject="hosted_runtime_closed")

    def test_assignment_digest_excludes_objects_and_fields_are_validated(self) -> None:
        base = make_assignment()
        self.assertEqual(tuple(base.to_mapping()), HOSTED_ASSIGNMENT_FIELDS)
        self.assertEqual(base.assignment_digest(),
                         make_assignment(expires_at="2031-01-01T00:00:00Z").assignment_digest())
        self.assertNotEqual(base.assignment_digest(), make_assignment(route_revision="4").assignment_digest())
        values = {name: getattr(base, name) for name in (*HOSTED_ASSIGNMENT_FIELDS, "placement", "lease", "pool")}
        for name, value in (("auth_kind", "password"), ("support_matrix_digest", "sha256:XYZ"),
                            ("lease_fencing_token", 0), ("workspace_root_ref", "workspace://local/../x"),
                            ("lease", object())):
            with self.subTest(field=name):
                self.expect(BridgeCode.INVALID_REQUEST, lambda: HostedAssignment(**{**values, name: value}))


# ----- durable journal ------------------------------------------------------

class BindingStoreTests(HostedFixture):
    def bind(self, store, intent="intent-1", call="call-1", operation="create_thread", assignment=None, **trace):
        store.bind_effect(task_trace(intent, call=call, **trace), assignment or make_assignment(), operation)

    def test_records_survive_reopening(self) -> None:
        store = ExecutionBindingStore(self.journal_path)
        self.bind(store)
        store.record_thread(connection_id="connection-1", thread_id="thread-1", kind="task",
                            vm_attempt_id="attempt-1", assignment=make_assignment())
        store.confirm_owner(expected_binding())
        store.mark_unknown(thread_id="thread-x")
        reopened = ExecutionBindingStore(str(self.journal_path))
        self.assertEqual(reopened.read_attempt("attempt-1"), store.read_attempt("attempt-1"))
        self.assertEqual(reopened.route_for_thread("thread-1"), store.route_for_thread("thread-1"))
        self.assertEqual(reopened.read_execution_binding("attempt-1"),
                         {"vm_attempt_id": "attempt-1", "binding_digest": expected_binding().digest()})
        self.assertEqual(reopened.unknown_threads, frozenset({"thread-x"}))
        self.assertEqual(len(reopened.effect_records()), 1)

    def test_intent_rerecord_is_idempotent_and_conflicts_fail_closed(self) -> None:
        store = ExecutionBindingStore(self.journal_path)
        self.bind(store)
        self.bind(store)
        self.assertEqual(len(store.read_attempt("attempt-1").intents), 1)
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.bind(store, call="call-9"),
                    subject="vm_runtime_intent_id")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: self.bind(store, operation="start_turn"),
                    subject="vm_runtime_intent_id")
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: self.bind(store, intent="intent-2", assignment=make_assignment(route_revision="4")),
                    subject="assignment")
        self.assertEqual(len(store.read_attempt("attempt-1").intents), 1)
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: store.record_thread(connection_id="connection-1", thread_id="thread-1", kind="task",
                                                vm_attempt_id="attempt-1",
                                                assignment=make_assignment(route_revision="4")),
                    subject="assignment")

    def test_owner_confirmation_must_match_the_stored_assignment_and_first_intent(self) -> None:
        store = ExecutionBindingStore(self.journal_path)
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: store.confirm_owner(expected_binding()),
                    subject="attempt_binding")
        self.bind(store)
        self.bind(store, intent="intent-2", call="call-2", operation="start_turn")
        for override, subject in (({"route_revision": "4"}, "route_revision"),
                                  ({"connection_id": "connection-2"}, "connection_id"),
                                  ({"vm_runtime_intent_id": "intent-2"}, "vm_runtime_intent_id"),
                                  ({"vm_provider_call_id": "call-2"}, "vm_provider_call_id")):
            with self.subTest(subject=subject):
                self.expect(BridgeCode.BINDING_MISMATCH, lambda: store.confirm_owner(expected_binding(**override)),
                            subject=subject)
        self.assertIsNone(store.read_execution_binding("attempt-1"))
        digest = store.confirm_owner(expected_binding())
        self.assertEqual(store.confirm_owner(expected_binding()), digest)
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: store.confirm_owner(expected_binding(goal="goal-rev-2")),
                    subject="persisted_binding")
        self.assertEqual(store.read_execution_binding("attempt-1")["binding_digest"], digest)

    def test_immutable_rows_cannot_be_updated_or_deleted_directly(self) -> None:
        store = ExecutionBindingStore(self.journal_path)
        self.bind(store)
        with closing(sqlite3.connect(str(self.journal_path), isolation_level=None)) as connection:
            for statement in ('UPDATE "attempt_bindings" SET "vm_project_id" = \'project-2\'',
                              'DELETE FROM "intent_bindings"', 'DELETE FROM "effect_log"'):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.Error):
                        connection.execute(statement)
        self.assertEqual(store.read_attempt("attempt-1").vm_project_id, "project-1")

    def test_tampered_records_fail_closed_on_read_and_before_any_effect(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        store.confirm_owner(expected_binding())
        self.tamper("owner_confirmations", "binding_json", '"lease_fencing_token":5', '"lease_fencing_token":6')
        self.expect(BridgeCode.BINDING_STORE_FAILED, lambda: store.read_execution_binding("attempt-1"),
                    subject="binding_json")
        self.tamper("attempt_bindings", "assignment_json", '"route_revision":"3"', '"route_revision":"9"')
        self.expect(BridgeCode.BINDING_STORE_FAILED, lambda: store.read_attempt("attempt-1"),
                    subject="assignment_json")
        inner = self.inners["connection-1"]
        before = inner.ops()
        self.trace[0] = task_trace("intent-3", call="call-3", thread="thread-1")
        self.expect(BridgeCode.BINDING_STORE_FAILED, lambda: self.start(runtime))
        self.assertEqual(inner.ops(), before)

    def test_foreign_or_unsafe_journal_paths_are_rejected_without_changes(self) -> None:
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(str(foreign))) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('synthetic')")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InvalidDatabase):
            ExecutionBindingStore(foreign)
        self.assertEqual(foreign.read_bytes(), before)
        ledger = self.root / "backlog.sqlite3"
        with self.assertRaises(UnsafeStoragePath):
            ExecutionBindingStore(ledger)
        self.assertFalse(ledger.exists())
        project = self.root / "source-project"
        (project / ".git").mkdir(parents=True)
        inside = project / "data" / "bindings.sqlite3"
        with self.assertRaises(UnsafeStoragePath):
            ExecutionBindingStore(inside)
        self.assertFalse(inside.parent.exists())


# ----- bridge hosted mode ---------------------------------------------------

class BridgeHostedTests(HostedFixture):
    def bridge(self, port, store=None):
        return VmBridge(port, surface_dict(), now=lambda: NOW, binding_store=store)

    def test_case9_run_once_hosted_confirms_the_owner_binding_and_returns_its_digest(self) -> None:
        store, runtime = self.open()
        port = FakePort()
        port.dispatch = lambda: self.dispatch_attempt(runtime)
        receipt, confirmation = self.bridge(port, store).run_once_hosted(HostedRunOnceRequest("run-1", "project-1"))
        self.assertEqual((receipt.job.phase, receipt.job.job_id), (JobPhase.PENDING, "job-1"))
        self.assertEqual(port.calls[1], ("run_once", ("project-1",), {"resume": False}))
        names = port.names()
        self.assertEqual(names[:2], ["handshake", "run_once"])
        self.assertCountEqual(names[2:], ["attempt_detail", "status"])
        digest = expected_binding().digest()
        self.assertIsInstance(confirmation, HostedConfirmation)
        self.assertEqual((confirmation.state, confirmation.vm_project_id, confirmation.vm_attempt_id,
                          confirmation.vm_task_id, confirmation.binding_digest),
                         ("confirmed", "project-1", "attempt-1", "task-1", digest))
        self.assertEqual(store.read_execution_binding("attempt-1"),
                         {"vm_attempt_id": "attempt-1", "binding_digest": digest})

    def test_case9_plan_revision_disagreement_is_a_contract_mismatch(self) -> None:
        store, runtime = self.open()
        port = FakePort()
        port.dispatch = lambda: self.dispatch_attempt(runtime)
        port.responses["status"] = status_response(plan="plan-rev-2")
        bridge = self.bridge(port, store)
        error = self.expect(BridgeCode.CONTRACT_MISMATCH,
                            lambda: bridge.run_once_hosted(HostedRunOnceRequest("run-1", "project-1")),
                            outcome=UNKNOWN)
        self.assertIsNone(error.__context__)
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        self.assertIsNone(store.read_execution_binding("attempt-1"))
        for status in (status_response(plan="plan-rev-2"), status_response(goal=None), status_response(plan=None)):
            with self.subTest(status=sorted(status)):
                port.responses["status"] = status
                self.expect(BridgeCode.CONTRACT_MISMATCH,
                            lambda: self.bridge(port, store).confirm_attempt("project-1", "attempt-1"))
        self.assertIsNone(store.read_execution_binding("attempt-1"))
        port.responses["status"] = status_response()
        confirmed = self.bridge(port, store).confirm_attempt("project-1", "attempt-1")
        self.assertEqual(confirmed.state, "confirmed")

    def test_case9_without_an_effect_binding_the_attempt_is_binding_pending(self) -> None:
        store, _ = self.open()
        port = FakePort()
        receipt, confirmation = self.bridge(port, store).run_once_hosted(HostedRunOnceRequest("run-1", "project-1"))
        self.assertEqual((confirmation.state, confirmation.vm_attempt_id, confirmation.vm_task_id,
                          confirmation.binding_digest), ("binding_pending", "attempt-1", None, None))
        self.assertEqual(port.names(), ["handshake", "run_once"])
        direct = self.bridge(port, store).confirm_attempt("project-1", "attempt-1")
        self.assertEqual(direct.state, "binding_pending")
        self.assertEqual(port.names().count("attempt_detail"), 0)
        port.responses["run_once"] = {"project_id": "project-1", "runtime_job_id": "job-2",
                                      "runtime_job_status": "scheduled"}
        receipt, confirmation = self.bridge(port, store).run_once_hosted(HostedRunOnceRequest("run-2", "project-1"))
        self.assertIsNone(confirmation)

    def test_case9_owner_query_failures_stop_confirmation(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        cases = (
            (attempt_detail_response(digest=None), BridgeCode.INVALID_RESPONSE),
            (attempt_detail_response(project="project-2"), BridgeCode.CONTRACT_MISMATCH),
        )
        for detail, code in cases:
            with self.subTest(code=code):
                port = FakePort()
                port.responses["attempt_detail"] = detail
                self.expect(code, lambda: self.bridge(port, store).confirm_attempt("project-1", "attempt-1"))
        port = FakePort()
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: self.bridge(port, store).confirm_attempt("project-2", "attempt-1"))
        self.assertIsNone(store.read_execution_binding("attempt-1"))

    def test_case9_confirmation_is_immutable_idempotent_and_claims_request_ids(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        port = FakePort()
        bridge = self.bridge(port, store)
        first = bridge.confirm_attempt("project-1", "attempt-1", request_id="confirm-1")
        again = bridge.confirm_attempt("project-1", "attempt-1")
        self.assertEqual(first.binding_digest, again.binding_digest)
        self.expect(BridgeCode.DUPLICATE_REQUEST,
                    lambda: bridge.confirm_attempt("project-1", "attempt-1", request_id="confirm-1"))
        port.responses["status"] = status_response(goal="goal-rev-2")
        self.expect(BridgeCode.BINDING_MISMATCH, lambda: bridge.confirm_attempt("project-1", "attempt-1"),
                    outcome=KNOWN)
        self.assertEqual(store.read_execution_binding("attempt-1")["binding_digest"], first.binding_digest)

    def test_case9_hosted_mode_requires_both_the_store_and_the_declared_extension(self) -> None:
        store, _ = self.open()
        port = FakePort()
        self.expect(BridgeCode.SEAM_UNAVAILABLE,
                    lambda: self.bridge(port).run_once_hosted(HostedRunOnceRequest("run-1", "project-1")))
        undeclared = FakePort(extensions=())
        self.expect(BridgeCode.SEAM_UNAVAILABLE,
                    lambda: self.bridge(undeclared, store).run_once_hosted(HostedRunOnceRequest("run-1", "project-1")))
        self.assertNotIn("run_once", port.names() + undeclared.names())

    def test_case9_unclear_run_once_blocks_the_project(self) -> None:
        store, _ = self.open()
        port = FakePort()
        port.failures["run_once"] = TimeoutError(SECRET)
        bridge = self.bridge(port, store)
        self.expect(BridgeCode.OUTCOME_UNKNOWN,
                    lambda: bridge.run_once_hosted(HostedRunOnceRequest("run-1", "project-1")), outcome=UNKNOWN)
        self.assertIn("project-1", bridge.unresolved_projects)
        del port.failures["run_once"]
        self.expect(BridgeCode.RECONCILIATION_REQUIRED,
                    lambda: bridge.run_once_hosted(HostedRunOnceRequest("run-2", "project-1")),
                    subject="vm_project_id")
        self.assertEqual(port.names().count("run_once"), 1)

    def test_case10_observe_cancel_and_inventory_use_only_the_confirmed_binding(self) -> None:
        store, runtime = self.open()
        self.dispatch_attempt(runtime)
        port = FakePort()
        bridge = self.bridge(port, store)
        binding = expected_binding()
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: bridge.observe(ObserveRequest("observe-1", "project-1", binding)),
                    subject="persisted_binding")
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: bridge.cancel(CancelRequest("cancel-1", "project-1", "stop", binding)),
                    subject="persisted_binding")
        self.assertNotIn("observe", port.names())
        self.assertNotIn("cancel", port.names())
        bridge.confirm_attempt("project-1", "attempt-1")
        observed = bridge.observe(ObserveRequest("observe-2", "project-1", binding))
        self.assertEqual((observed.binding_digest, observed.job.core_consumed), (binding.digest(), True))
        inventory = bridge.inventory(InventoryRequest("inventory-1", binding))
        self.assertEqual(inventory.payload["models"], ["synthetic-model"])
        rotated = expected_binding(connection_revision="8")
        self.expect(BridgeCode.BINDING_MISMATCH,
                    lambda: bridge.cancel(CancelRequest("cancel-2", "project-1", "stop", rotated)),
                    subject="persisted_binding")
        cancelled = bridge.cancel(CancelRequest("cancel-3", "project-1", "synthetic stop", binding))
        self.assertEqual(port.calls[-1], ("cancel", ("project-1",), {"reason": "synthetic stop"}))
        self.assertEqual(cancelled.binding_digest, binding.digest())
        # The confirmation is durable: a new process and bridge still observe with it.
        store2, _ = self.restart()
        port2 = FakePort()
        again = self.bridge(port2, store2).observe(ObserveRequest("observe-3", "project-1", binding))
        self.assertEqual(again.binding_digest, binding.digest())

    def test_end_to_end_through_engine_application_port_with_hosted_runtime(self) -> None:
        store, runtime = self.open()
        app = FakeEngineApp("service-1", runtime=runtime)
        app.dispatch = lambda: self.dispatch_attempt(app.runtime)
        port = EngineApplicationPort(app, surface_revision=PINNED_SURFACE_REVISION, engine_schema_revision=4,
                                     error_types=(None, None), hosted_runtime=runtime)
        bridge = self.bridge(port, store)
        receipt, confirmation = bridge.run_once_hosted(HostedRunOnceRequest("run-1", "project-1"))
        self.assertEqual(receipt.job.job_id, "job-1")
        self.assertEqual((confirmation.state, confirmation.binding_digest), ("confirmed", expected_binding().digest()))
        self.assertEqual(port.read_execution_binding("attempt-1"), store.read_execution_binding("attempt-1"))
        self.assertIn("run_once", app.calls)
        self.assertEqual(self.inners["connection-1"].ops(), ["create_thread", "start_turn"])


# ----- engine port and regressions -----------------------------------------

class EnginePortHostedTests(HostedFixture):
    def test_case11_hosted_port_declares_the_extension_and_never_bind_execution(self) -> None:
        store, runtime = self.open()
        port = EngineApplicationPort(FakeEngineApp("service-1"), surface_revision=PINNED_SURFACE_REVISION,
                                     engine_schema_revision=4, error_types=(None, None), hosted_runtime=runtime)
        handshake = port.handshake()
        self.assertIn(HOSTED_RUNTIME_EXTENSION, handshake["extensions"])
        self.assertNotIn(BINDING_EXTENSION, handshake["extensions"])
        self.assertFalse(hasattr(port, "bind_execution"))
        self.assertFalse(port.upstream_binding_methods_present)
        self.assertIsNone(port.read_execution_binding("attempt-1"))
        self.dispatch_attempt(runtime)
        store.confirm_owner(expected_binding())
        self.assertEqual(port.read_execution_binding("attempt-1"),
                         {"vm_attempt_id": "attempt-1", "binding_digest": expected_binding().digest()})

        plain = EngineApplicationPort(FakeEngineApp("service-1"), surface_revision=PINNED_SURFACE_REVISION,
                                      engine_schema_revision=4, error_types=(None, None))
        self.assertEqual(plain.handshake()["extensions"], [])
        self.assertFalse(hasattr(plain, "bind_execution"))
        self.assertFalse(plain.upstream_binding_methods_present)
        reader = getattr(plain, "read_execution_binding", None)
        if callable(reader):
            with self.assertRaises(EnginePortError) as raised:
                reader("attempt-1")
            self.assertEqual(raised.exception.code, "ENGINE_METHOD_ABSENT")

    def test_case11_build_hosted_application_injects_the_runtime_into_the_factory(self) -> None:
        _, runtime = self.open()
        recorded = {}

        def factory(service, *, runtime, **kwargs):
            recorded.update(service=service, runtime=runtime, kwargs=kwargs)
            return FakeEngineApp(service, runtime=runtime, **kwargs)

        port = build_hosted_application(factory, "service-1", runtime, clock="synthetic")
        self.assertIsInstance(port, EngineApplicationPort)
        self.assertEqual(recorded, {"service": "service-1", "runtime": runtime, "kwargs": {"clock": "synthetic"}})
        self.assertIn(HOSTED_RUNTIME_EXTENSION, port.handshake()["extensions"])
        self.assertFalse(hasattr(port, "bind_execution"))
        self.assertIsNone(port.read_execution_binding("attempt-1"))

    def test_case12_extension_path_is_unchanged_when_a_binding_store_is_supplied(self) -> None:
        store, _ = self.open()
        port = FakeBoundPort()
        port.responses["attempt_detail"] = {"project_id": "project-1", "attempt_id": "attempt-1",
                                            "task_id": "task-1", "goal_revision_id": "goal-rev-1",
                                            "plan_revision_id": "plan-rev-1", "execution_spec_digest": DIGEST_A}
        port.responses["run_once"] = {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                                      "job": {"job_id": "job-1", "status": "running"}}
        assignment = make_assignment()
        bridge = VmBridge(port, surface_dict(), now=lambda: NOW, binding_store=store)
        receipt = bridge.run_once(RunOnceRequest("run-1", expected_binding(), assignment.placement,
                                                 assignment.lease, assignment.pool))
        self.assertEqual(port.names(), ["handshake", "attempt_detail", "read_execution_binding",
                                        "bind_execution", "run_once"])
        self.assertEqual(receipt.binding_digest, expected_binding().digest())
        self.assertIsNone(store.read_execution_binding("attempt-1"))
        self.assertEqual(store.effect_records(), ())

    def test_case12_hosted_declaration_alone_does_not_enable_the_extension_path(self) -> None:
        port = FakePort()
        assignment = make_assignment()
        bridge = VmBridge(port, surface_dict(), now=lambda: NOW)
        self.expect(BridgeCode.SEAM_UNAVAILABLE,
                    lambda: bridge.run_once(RunOnceRequest("run-1", expected_binding(), assignment.placement,
                                                           assignment.lease, assignment.pool)),
                    subject="aik-bound-runtime")
        self.assertEqual(port.names(), ["handshake"])
        self.expect(BridgeCode.SEAM_UNAVAILABLE,
                    lambda: bridge.observe(ObserveRequest("observe-1", "project-1", expected_binding())))
        self.assertNotIn("observe", port.names())


if __name__ == "__main__":
    unittest.main()
