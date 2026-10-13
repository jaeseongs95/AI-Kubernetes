"""M2 integration: AIK-033~043 and AIK-085 modules connected in one flow.

Real module instances run on files in a temporary directory, and each output
is used directly as the next module's input:

RuntimeStore, BudgetStore, LeaseManager, PlacementPlanner/Journal,
LifecycleGate/GatedLeaseManager, ElasticityController, NativeDispatcher with
GuardedAdapter and DispatchJournal, WorkerObserver, DevTools, EvidenceRelay, IndependentChecker,
build_completion_view, ReadModel, LifecycleController, RecoveryReconciler.

Only the external boundary is fake: the provider adapter, VM responses (real
aik_vm_bridge.BridgeReceipt objects with synthetic payloads), the DevTools
authority, the pool registry/capability inventory, and the worker/effect/VM
ports. No provider, credential, network, VM or AGS installation is used.

The fake provider is a full ProviderAdapter behind the real GuardedAdapter, so
known and unknown AdapterFailure classification goes through NativeDispatcher.

Passing on Linux would be development verification only; Windows is NOT_RUN.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    STATUS_AUTHORIZED, AuthorityCode, AuthorityDenied, EffectKind, EffectOutcomeUnknown, EffectReceipt,
    ToolCallAuthorization, ToolSession, canonical_arguments,
)
from aik_authority.models import target_covers  # noqa: E402
from aik_budget import BudgetStore, confirmed  # noqa: E402
from aik_capability import UnknownResource  # noqa: E402
from aik_devtools import (  # noqa: E402
    DevToolPolicy, DevTools, Invocation, VerificationCommand, files_scope_target, tool_declarations,
    verification_scope_target,
)
from aik_dispatch import (  # noqa: E402
    STARTED_JOB_STATES, DispatchIdentity, DispatchJournal, DispatchOutcomeUnknown, DispatchRejected,
    NativeDispatcher,
)
from aik_elastic import ElasticityController  # noqa: E402
from aik_evidence.relay import EvidenceRelay  # noqa: E402
from aik_lease import HOLDING_STATES, LeaseManager  # noqa: E402
from aik_lifecycle import (  # noqa: E402
    GatedLeaseManager, LifecycleController, LifecycleError, LifecycleGate, WorkerRecord, WorkerRegistry,
    runtime_placement_lookup,
)
from aik_observer import WorkerObserver  # noqa: E402
from aik_observer.models import ProviderEvent  # noqa: E402
from aik_placement import ACTIVE_STATES, PlacementJournal, PlacementPlanner, ReadyTask  # noqa: E402
from aik_pool import QualificationLevel, RouteStatus, UnknownRoute  # noqa: E402
from aik_profiles import ExecutionChoice, RoleProfile  # noqa: E402
from aik_provider.contracts import (  # noqa: E402
    TERMINAL, AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind, AuthObservation, AuthState,
    ConnectionBinding, JobReceipt, JobRequest, JobState, Support, fault_from_provider,
)
from aik_provider.controller import GuardedAdapter  # noqa: E402
from aik_readmodel import ReadModel, SourceEvent, from_bridge_receipt, from_lease  # noqa: E402
from aik_recovery import EffectRef, RecoveryJournal, RecoveryReconciler  # noqa: E402
from aik_runtime import ExecutionProfile, Lease, Placement, Pool, RuntimeStore  # noqa: E402
from aik_validation import IndependentChecker, ValidationError, build_completion_view  # noqa: E402
from aik_vm_bridge.bridge import BridgeReceipt, VmRefs  # noqa: E402
from aik_vm_bridge.surface import Command  # noqa: E402
from aik_workspace import ProjectInfo, WorkspaceManager, WorkspaceRegistry, sha256_digest  # noqa: E402


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
TTL = 3600
IDLE = 30
POOL = "pool-1"
PROFILE = "profile-1"
CONNECTION = "connection-1"
MODEL = "synthetic-model"
ROUTE_ID = "route-1"
SCOPE = "os-user-synthetic-1"
WORKSPACE = "ws-dev"
INPUT_REVISION = "plan-rev-1"
PYTHON = os.path.abspath(sys.executable)
ORACLE = "hello 세계\n".encode("utf-8")
CHANGED = "changed oracle\n".encode("utf-8")
UNIT = (
    "import os, pathlib, sys\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
    "    handle.write('run\\n')\n"
    "print(os.getcwd())\n"
    "app = pathlib.Path('src/app.txt')\n"
    "actual = app.read_bytes() if app.exists() else b''\n"
    "sys.exit(0 if actual == pathlib.Path('tests/expected.txt').read_bytes() else 1)\n"
)
WORKER = RoleProfile("worker-1", "workspace-1", "worker", ExecutionChoice(CONNECTION, MODEL), 50)
RUNNING = JobState.__members__.get("RUNNING") or next(state for state in JobState if state not in TERMINAL)
SUCCEEDED = JobState.__members__.get("SUCCEEDED") or next(state for state in JobState if state in TERMINAL)
UNKNOWN_OUTCOME = fault_from_provider(outcome_unknown=True).outcome
KNOWN_FAULT = dict(http_status=400, native_code="synthetic_invalid_request")


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


STAMP = iso(START)


def _member(enum, *preferred):
    for wanted in preferred:
        for member in enum:
            if member.value == wanted or member.name.lower() == wanted:
                return member
    return next(iter(enum))


SUPPORTED = _member(Support, "supported", "implemented", "available")
ISOLATED = _member(AccountIsolation, "isolated", "dedicated", "per_connection")
SIGNED_IN = _member(AuthState, "signed_in", "authenticated", "ready", "valid", "active")
SIGNED_OUT = _member(AuthState, "signed_out", "unauthenticated")
BINDING = ConnectionBinding("synthetic", "synthetic-runtime", CONNECTION, 1, _member(AuthKind, "api_key"),
                            "secret://local/test-key", "0123456789abcdef" * 4)


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def ref_values(mapping, key) -> set:
    value = mapping.get(key)
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return set(value)


def error_code(error: BaseException) -> str:
    return str(getattr(error, "code", None) or error)


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_route() -> RouteStatus:
    return RouteStatus(
        pool_id=POOL, connection_id=CONNECTION, connection_revision=1, provider="synthetic",
        host="synthetic-host", auth_kind="api_key", model=MODEL, tools=(), pool_enabled=True,
        configured_max_parallel=8, implementation_state="implemented", account_binding="bound",
        runtime_support="supported", account_isolation="isolated", governance="approved",
        last_probe_at=STAMP, last_verified_at=STAMP, freshness="fresh", matrix_revision=7,
        qualification_matrix_revision=7, capacity_state="known", available_capacity=8,
        level=QualificationLevel.QUALIFIED, executable_now=True, placement_eligible=True,
    )


class FakeRegistry:
    matrix_revision = 7

    def __init__(self):
        self.routes = {}

    def put(self, route):
        self.routes[(route.pool_id, route.model)] = route

    def status(self, pool_id, model):
        try:
            return self.routes[(pool_id, model)]
        except KeyError:
            raise UnknownRoute("UNKNOWN_ROUTE") from None

    def operational_inventory(self, *, model=None):
        return tuple(route for _, route in sorted(self.routes.items())
                     if route.placement_eligible and (model is None or route.model == model))


class FakeInventory:
    def __init__(self):
        self.entries = {}
        self.revisions = {}

    def put(self, connection_id, model=MODEL, efforts=("low", "high"), tools=("shell", "edit"), revision=3):
        self.entries.setdefault(connection_id, {})[model] = (efforts, tools)
        self.revisions[connection_id] = revision

    def validate(self, connection_id, model, effort=None, tools=()):
        if connection_id not in self.entries:
            raise UnknownResource("UNKNOWN_RESOURCE")
        reasons = []
        entry = self.entries[connection_id].get(model)
        if entry is None:
            reasons.append("provider_observed:MODEL_NOT_LISTED")
        else:
            efforts, supported = entry
            if effort is not None and effort not in efforts:
                reasons.append("provider_observed:EFFORT_NOT_SUPPORTED")
            reasons += ["provider_observed:TOOL_NOT_SUPPORTED:" + tool for tool in tools if tool not in supported]
        return SimpleNamespace(accepted=not reasons, reasons=tuple(reasons),
                               resource_revision=self.revisions[connection_id])


class FakeProvider:
    """Synthetic provider boundary; it never contacts anything.

    mode: "ok", "known_failure" (provider confirmed no effect) or "unknown"
    (transport outcome unknown). Faults come from the real fault_from_provider.
    """

    def __init__(self) -> None:
        self.mode = "ok"
        self.start_calls = 0
        self.states = {}
        self.interrupts = []
        self.last_fault = None

    def describe(self, binding):
        # An API key connection: no host sign-in or sign-out (same shape as tests/test_dispatch.py).
        return AdapterCapabilities(binding.provider, binding.runtime, binding.auth_kind, ISOLATED,
                                   SUPPORTED, Support.UNSUPPORTED, Support.UNSUPPORTED, SUPPORTED, SUPPORTED,
                                   SUPPORTED)

    def auth_status(self, binding):
        return AuthObservation(binding.connection_id, binding.revision, binding.account_fingerprint, SIGNED_IN)

    def begin_sign_in(self, binding):
        return self.auth_status(binding)

    def sign_out(self, binding):
        return AuthObservation(binding.connection_id, binding.revision, binding.account_fingerprint, SIGNED_OUT)

    def interrupt_job(self, request, receipt):
        return self.interrupt(request, receipt)

    def start_job(self, request):
        self.start_calls += 1
        if self.mode == "known_failure":
            self.last_fault = fault_from_provider(**KNOWN_FAULT)
            raise AdapterFailure(self.last_fault)
        if self.mode == "unknown":
            self.last_fault = fault_from_provider(outcome_unknown=True)
            raise AdapterFailure(self.last_fault)
        job = "job-" + request.vm_attempt_id
        self.states[job] = RUNNING
        binding = request.binding
        return JobReceipt(request.request_id, job, binding.connection_id, binding.revision,
                          binding.account_fingerprint, RUNNING)

    def observe_job(self, request, receipt):
        return replace(receipt, state=self.states.get(receipt.provider_job_id, RUNNING))

    observe = observe_job

    def interrupt(self, request, receipt):
        self.interrupts.append(receipt.provider_job_id)
        return self.observe_job(request, receipt)


class FakeEventSource:
    def __init__(self, events):
        self.events = tuple(events)

    def read(self, after_sequence, limit):
        return tuple(event for event in self.events
                     if after_sequence is None or event.sequence > after_sequence)[:limit]


class FakeWorkerPort:
    def __init__(self):
        self.terminated = []
        self.cleaned = []

    def terminate(self, worker_id):
        self.terminated.append(worker_id)

    def cleanup(self, worker_id):
        self.cleaned.append(worker_id)


class FakeEffects:
    """Effect journal boundary for both LifecycleController and RecoveryReconciler."""

    def __init__(self):
        self.pending = {}
        self.refs = ()
        self.marked = []
        self.resent = []
        self.executed = []

    def unresolved(self, vm_attempt_id):
        return self.pending.get(vm_attempt_id, ())

    def inflight(self):
        return self.refs

    def mark_unknown(self, ref):
        self.marked.append(ref.tool_call_id)

    def resend_result(self, ref):
        self.resent.append(ref.tool_call_id)

    def execute(self, ref):  # A reconciler must never call this.
        self.executed.append(ref.tool_call_id)
        raise AssertionError("effect re-executed")


class FakeVm:
    def __init__(self, states):
        self.states = dict(states)

    def attempt_state(self, vm_attempt_id):
        if vm_attempt_id not in self.states:
            raise ConnectionError("synthetic VM unavailable")
        return self.states[vm_attempt_id]


class FakeProjects:
    def __init__(self):
        self.roots = {}

    def create_project(self, workspace_id, canonical_root, name):
        self.roots["vm-project-1"] = canonical_root
        return "vm-project-1"

    def lookup_project(self, project_id):
        return ProjectInfo(project_id, self.roots.get(project_id))


class FakeAuthority:
    """Mimics the AuthorityGateway contract (same fake as tests/test_devtools.py)."""

    def __init__(self, *, tools=None, targets=None):
        self.kinds = {declaration.tool_id: declaration.effect_kind for declaration in tool_declarations()}
        self.tools = frozenset(self.kinds if tools is None else tools)
        self.targets = tuple(targets or (files_scope_target(WORKSPACE), verification_scope_target(WORKSPACE)))
        self.requests, self.intents, self.receipts = [], [], []
        self.decisions, self.effects = {}, {}
        self.intent_failure = False

    def request(self, session, call):
        self.requests.append(call.tool_call_id)
        if call.tool_id not in self.tools:
            raise AuthorityDenied(AuthorityCode.TOOL_NOT_IN_AUTHORIZATION, subject="tool_id")
        if not any(target_covers(target, call.target) for target in self.targets):
            raise AuthorityDenied(AuthorityCode.TARGET_OUT_OF_SCOPE, subject="target")
        if call.input_revision != INPUT_REVISION:
            raise AuthorityDenied(AuthorityCode.STALE_INPUT_REVISION, subject="input_revision")
        if call.authorization_revision != 1:
            raise AuthorityDenied(AuthorityCode.STALE_AUTHORIZATION_REVISION, subject="authorization_revision")
        kind = self.kinds[call.tool_id]
        payload = json.dumps({"tool": call.tool_id, "target": call.target, "effect": call.effect_id,
                              "arguments": canonical_arguments(call.arguments)}, sort_keys=True)
        request_digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        if call.effect_id is not None and self.effects.get(call.effect_id) == "outcome_unknown":
            raise AuthorityDenied(AuthorityCode.RECONCILIATION_REQUIRED, subject="effect_id")
        previous = self.decisions.get(call.tool_call_id)
        if previous is not None:
            if previous.request_digest != request_digest or previous.session_id != session.session_id:
                raise AuthorityDenied(AuthorityCode.TOOL_CALL_CONFLICT, subject="tool_call_id")
            if kind is EffectKind.NON_IDEMPOTENT:
                raise AuthorityDenied(AuthorityCode.EFFECT_ALREADY_AUTHORIZED, subject="effect_id")
            return previous
        if kind is not EffectKind.READ_ONLY:
            if self.intent_failure:
                raise EffectOutcomeUnknown(AuthorityCode.INTENT_UNKNOWN, subject="record_intent")
            self.intents.append((call.effect_id, call.tool_call_id, request_digest))
            self.effects[call.effect_id] = "intent_recorded"
        decision = ToolCallAuthorization(
            STATUS_AUTHORIZED, call.tool_call_id, call.tool_id, kind, call.effect_id, request_digest,
            session.session_id, session.role, call.workspace_id, "vm-project-1", "vm-task-1", "vm-attempt-1",
            "vm-auth-1", 1, "lease-1", 1, 1, "sha256:" + "0" * 64, "2030-01-01T00:00:00Z",
        )
        self.decisions[call.tool_call_id] = decision
        return decision

    def forward_receipt(self, session, tool_call_id, status, *, receipt_ref=None):
        decision = self.decisions.get(tool_call_id)
        if decision is None or decision.session_id != session.session_id:
            raise AuthorityDenied(AuthorityCode.TOOL_CALL_UNKNOWN, subject="tool_call_id")
        self.receipts.append((tool_call_id, status, receipt_ref))
        self.effects[decision.effect_id] = "outcome_unknown" if status == "unknown" else "receipt_forwarded"
        return EffectReceipt(decision.effect_id, tool_call_id, status, "recorded", status == "unknown")

    def effect_state(self, effect_id):
        return self.effects.get(effect_id)


def ready(attempt: str, project: str) -> ReadyTask:
    return ReadyTask(vm_instance_id="vm-instance-1", vm_project_id=project, vm_task_id="task-" + attempt,
                     vm_attempt_id=attempt, vm_authorization_ref="vm-auth-" + attempt, role="worker",
                     model=MODEL, effort="high", tools=("shell",))


def vm_receipt(command, *, project, attempt=None, task=None, cursor=None, **payload):
    """A real aik_vm_bridge BridgeReceipt carrying a synthetic VM payload."""
    body = {"project_id": project, **payload}
    if attempt is not None:
        body["attempt_id"] = attempt
    refs = VmRefs(project_id=project, task_id=task if attempt is not None else None,
                  attempt_id=attempt, history_cursor=cursor)
    return BridgeReceipt(command=command, request_id="bridge-request-1", project_id=project, refs=refs,
                         payload=MappingProxyType(body))


class M2IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-m2-integration-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = self.base / "state"
        self.state.mkdir()
        self.clock = FakeClock()

        self.runtime = RuntimeStore(self.state / "runtime.sqlite3", timeout=10)
        self.budget = BudgetStore(self.state / "budget.sqlite3", clock=self.clock)
        self.leases = LeaseManager(self.runtime, self.budget, ttl_seconds=TTL, clock=self.clock)
        self.observer = WorkerObserver(self.state / "observer.sqlite3", budget=self.budget, clock=self.clock)
        with self.runtime.transaction() as tx:
            tx.save(Pool(POOL, "synthetic", CONNECTION, max_parallel=8, enabled=True), expected_revision=0)
            tx.save(ExecutionProfile(PROFILE, POOL, "worker", MODEL), expected_revision=0)

        self.registry = FakeRegistry()
        self.registry.put(make_route())
        self.inventory = FakeInventory()
        self.inventory.put(CONNECTION)
        self.placement_journal = PlacementJournal(self.state / "placement-journal.sqlite3", timeout=10)
        self.planner = PlacementPlanner(self.runtime, self.registry, self.inventory, self.placement_journal,
                                        clock=self.clock)
        self.gate = LifecycleGate(self.state / "lifecycle.sqlite3", clock=self.clock)
        self.workers = WorkerRegistry(self.state / "lifecycle.sqlite3", clock=self.clock)
        for project in ("project-1", "project-2"):
            self.workers.register(WorkerRecord("worker-" + project, "owned", POOL))
        self.gated = GatedLeaseManager(self.leases, self.gate, runtime_placement_lookup(self.runtime))
        self.elastic = ElasticityController(self.planner, self.gated, self.runtime, idle_seconds=IDLE,
                                            clock=self.clock)

        self.provider = FakeProvider()
        self.dispatch_journal = DispatchJournal(self.state / "dispatch.sqlite3", clock=self.clock)
        self.adapter = GuardedAdapter(self.provider, authorize_dispatch=lambda *args, **kwargs: True)
        self.dispatcher = NativeDispatcher(self.dispatch_journal, self.leases, self.adapter, self.observer,
                                           clock=self.clock)
        self.effects = FakeEffects()
        self.worker_port = FakeWorkerPort()
        self.managed = self.base / "managed"
        self.managed.mkdir()
        self.relay = EvidenceRelay(self.state / "evidence.sqlite3", managed_root=self.managed, clock=self.clock)
        self.lifecycle = LifecycleController(
            self.gate, self.leases, self.observer, self.workers, store=self.runtime, job_port=self.provider,
            worker_port=self.worker_port, effect_port=self.effects, evidence_port=self.relay, clock=self.clock,
        )

        folder = self.base / "작업 공간" / "프로젝트 A"
        folder.mkdir(parents=True)
        self.manager = WorkspaceManager(WorkspaceRegistry(self.state / "workspaces.json"), FakeProjects())
        self.manager.open_workspace(WORKSPACE, "개발 작업", folder)
        self.root = Path(self.manager.execution_context(WORKSPACE).canonical_root)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "expected.txt").write_bytes(ORACLE)
        (self.root / "src").mkdir()
        self.counter = self.base / "run-counter.txt"
        self.authority = FakeAuthority()
        self.tools = DevTools(authority=self.authority, workspaces=self.manager, policy=DevToolPolicy(WORKSPACE, (
            VerificationCommand("unit", (PYTHON, "-c", UNIT, str(self.counter)), timeout_seconds=30,
                                max_output_bytes=4096,
                                oracle_files=(("tests/expected.txt", sha256_digest(ORACLE)),)),
        )))
        self.checker = IndependentChecker(self.tools, self.state / "checks.sqlite3", clock=self.clock)
        self.worker_session = ToolSession("session-worker-1", "worker", WORKSPACE)
        self.reviewer_session = ToolSession("session-reviewer-1", "reviewer", WORKSPACE)

    # ----------------------------------------------------------------- helpers

    def reserve_options(self, task, placement):
        return dict(worker_id="worker-" + task.vm_project_id, route=self.registry.status(POOL, MODEL),
                    budget_profile=WORKER, max_seconds=60, api_cost_estimate_cents=1)

    def tick(self, *tasks):
        return self.elastic.tick(tasks, candidates=(PROFILE,), reserve_options=self.reserve_options)

    def all_records(self, model, store=None) -> list:
        store = store or self.runtime
        result, after = [], None
        while True:
            page = store.list(model, limit=1000, after_id=after)
            result.extend(page)
            if len(page) < 1000:
                return result
            after = page[-1].value.id

    def placements_for(self, attempt: str) -> list:
        return [entry for entry in self.all_records(Placement) if entry.value.vm_attempt_id == attempt]

    def runs(self) -> int:
        return self.counter.read_text(encoding="utf-8").count("run") if self.counter.exists() else 0

    def dispatch(self, lease: Lease, placement: Placement):
        """Real NativeDispatcher + GuardedAdapter over the lease/placement the elastic tick granted."""
        request_id = "request-" + placement.vm_attempt_id
        identity = DispatchIdentity(
            request_id=request_id, contract_digest=digest("contract:" + placement.vm_attempt_id),
            vm_project_id=placement.vm_project_id, vm_task_id=placement.vm_task_id,
            vm_attempt_id=placement.vm_attempt_id, lease_id=lease.id, fencing_token=lease.fencing_token,
            pool_id=lease.pool_id, route_id=ROUTE_ID, binding=BINDING, canonical_cwd=str(self.root),
            input_digest=digest("input:" + placement.vm_attempt_id), path="native",
        )
        request = JobRequest(BINDING, request_id, ROUTE_ID, placement.vm_attempt_id, MODEL)
        try:
            record = self.dispatcher.dispatch(identity, request, requested_model=MODEL, requested_effort="high")
        except (DispatchRejected, DispatchOutcomeUnknown):
            return self.dispatcher.record(request_id), request, None
        self.assertEqual(record.state, "sent_ack")
        record = self.dispatcher.confirm_started(request_id)
        receipt = self.observer_receipt(lease.id)
        self.assertIn(receipt.state, STARTED_JOB_STATES)
        return record, request, receipt

    def observer_receipt(self, lease_id: str) -> JobReceipt:
        """The provider receipt NativeDispatcher handed to WorkerObserver.start."""
        job = self.runtime.get(Lease, lease_id).value.provider_job_id
        self.assertIsNotNone(job)
        binding = BINDING
        return JobReceipt("request-" + job[len("job-"):], job, binding.connection_id, binding.revision,
                          binding.account_fingerprint, self.provider.states[job])

    def run_happy_path(self):
        """Scenario 1 flow; returns per-attempt context and the read model source events."""
        tasks = (ready("attempt-1", "project-1"), ready("attempt-2", "project-2"))
        result = self.tick(*tasks)
        self.assertEqual(result.waiting, ())
        granted = dict(result.granted)
        self.assertEqual(set(granted), {"attempt-1", "attempt-2"})

        flows, events = [], []
        cursors = {"vm": 0, "aik_runtime": 0, "dispatch": 0}

        def next_cursor(source):
            cursors[source] += 1
            return cursors[source]

        for number, task in enumerate(tasks, 1):
            attempt, project = task.vm_attempt_id, task.vm_project_id
            lease_id = granted[attempt]

            # Placement -> lease: identifiers flow unchanged; lease ID is the budget reservation ID.
            lease = self.runtime.get(Lease, lease_id)
            self.assertIsNotNone(lease)
            placement = self.runtime.get(Placement, lease.value.placement_id)
            self.assertIsNotNone(placement)
            self.assertEqual(placement.value.vm_attempt_id, attempt)
            self.assertEqual(placement.value.vm_project_id, project)
            self.assertEqual(placement.value.vm_task_id, task.vm_task_id)
            self.assertEqual(placement.value.profile_id, PROFILE)
            self.assertEqual((lease.value.pool_id, placement.value.pool_id), (POOL, POOL))
            self.assertEqual(lease.value.worker_id, "worker-" + project)
            self.assertEqual(lease.value.state, "reserved")
            self.assertIsNotNone(self.budget.reservation(lease_id))

            # Lease -> dispatch -> provider receipt.
            record, request, receipt = self.dispatch(lease.value, placement.value)
            self.assertEqual(record.state, "started")
            self.assertEqual(record.identity.lease_id, lease_id)
            self.assertEqual(record.identity.vm_attempt_id, attempt)
            self.assertEqual(record.provider_job_id, receipt.provider_job_id)
            active = self.runtime.get(Lease, lease_id)
            self.assertEqual(active.value.state, "active")
            self.assertEqual(active.value.provider_job_id, receipt.provider_job_id)

            # Provider events and terminal state -> observer -> budget settlement.
            source = FakeEventSource([ProviderEvent(1, f"usage-{number}", "usage",
                                                    {"input_tokens": 10 * number, "output_tokens": 5})])
            self.assertEqual(self.observer.ingest(lease_id, source).applied, 1)
            self.provider.states[receipt.provider_job_id] = SUCCEEDED
            observation = self.observer.poll(lease_id, self.provider)
            self.assertTrue(observation.terminal)
            self.assertEqual(observation.job_state, SUCCEEDED)
            self.assertEqual(observation.vm_attempt_id, attempt)
            self.assertEqual(observation.provider_job_id, receipt.provider_job_id)
            self.assertEqual(observation.usage.input_tokens, confirmed(10 * number))
            settlements = self.budget.usage_records(lease_id)
            self.assertTrue(settlements)
            self.assertEqual(settlements[0].kind, "settlement")

            # Worker output via DevTools -> evidence; digests must agree across modules.
            relative = "src/app.txt" if number == 1 else f"src/output-{number}.txt"
            content = ORACLE if number == 1 else f"attempt {number}\n".encode("utf-8")
            created = self.tools.create_file(
                self.worker_session, Invocation(f"call-worker-{number}", INPUT_REVISION, 1, f"effect-worker-{number}"),
                relative, content)
            artifact = self.relay.collect(f"callback-{number}", workspace_id=WORKSPACE, workspace_root=self.root,
                                          relative_path=relative, vm_attempt_id=attempt, lease_id=lease_id,
                                          max_bytes=4096)
            self.assertEqual(artifact.kind, "authoritative")
            self.assertEqual(artifact.sha256, sha256_digest(content))
            self.assertEqual(artifact.sha256, created.digest)
            self.assertEqual((artifact.vm_attempt_id, artifact.lease_id), (attempt, lease_id))
            self.assertTrue(self.relay.verify(artifact.artifact_id).intact)

            # Independent check through the fixed policy command.
            check = self.checker.run(attempt, worker_session_id=self.worker_session.session_id,
                                     reviewer_session=self.reviewer_session,
                                     invocation=Invocation(f"call-review-{number}", INPUT_REVISION, 1,
                                                           f"effect-review-{number}"),
                                     command_id="unit")
            self.assertTrue(check.passed)
            self.assertTrue(check.oracle_ok)
            self.assertEqual(check.vm_attempt_id, attempt)
            self.assertEqual(check.worker_session_id, self.worker_session.session_id)
            self.assertEqual(check.reviewer_session_id, self.reviewer_session.session_id)
            self.assertEqual(check.tool_call_id, f"call-review-{number}")

            # Completion view: AIK values never set the VM verdict flag.
            dispatch_record = self.dispatch_journal.record(record.identity.request_id)
            common = dict(
                vm_project_id=project, observation=observation, dispatch_record=dispatch_record, check=check,
                run_once_receipt=vm_receipt(Command.RUN_ONCE, project=project, attempt=attempt, task=task.vm_task_id),
                attempt_receipt=vm_receipt(Command.ATTEMPT_DETAIL, project=project, attempt=attempt,
                                           task=task.vm_task_id),
            )
            view = build_completion_view(attempt, final_report_receipt=vm_receipt(Command.FINAL_REPORT,
                                                                                  project=project), **common)
            self.assertEqual(view.vm_attempt_id, attempt)
            self.assertTrue(view.dispatch_ack)
            self.assertEqual(view.provider_state, str(SUCCEEDED))
            self.assertEqual(view.independent_check, check)
            self.assertEqual(view.check_verdict, "PASS")
            self.assertIsNone(view.gate)
            self.assertIsNone(view.goal_verdict_id)
            self.assertFalse(view.vm_verdict_recorded)
            verdict = build_completion_view(attempt, final_report_receipt=vm_receipt(
                Command.FINAL_REPORT, project=project, goal_verdict_id=f"vm-verdict-{number}"), **common)
            self.assertEqual(verdict.goal_verdict_id, f"vm-verdict-{number}")
            self.assertTrue(verdict.vm_verdict_recorded)

            # Read model source events from the real outputs.
            vm_event = from_bridge_receipt(vm_receipt(Command.RUN_ONCE, project=project, attempt=attempt,
                                                      task=task.vm_task_id, cursor=next_cursor("vm")), SCOPE)
            self.assertIsNotNone(vm_event)
            events.append(vm_event)
            events.append(from_lease(active, placement.value, SCOPE, cursor=next_cursor("aik_runtime")))
            for event in dispatch_record.events:
                refs = {"request_id": record.identity.request_id, "lease_id": lease_id, "vm_attempt_id": attempt}
                if event.state in ("sent_ack", "started"):
                    refs["provider_job_id"] = receipt.provider_job_id
                events.append(SourceEvent("dispatch", next_cursor("dispatch"), f"dispatch-{number}-{event.seq}",
                                          SCOPE, event.state, refs, {"state": event.state}))

            # Teardown through the elasticity controller: lease returned, placement released.
            self.elastic.finish(lease_id, lease.value.fencing_token, self.lifecycle,
                                required_artifacts=(artifact.artifact_id,))
            released = self.runtime.get(Lease, lease_id)
            self.assertEqual(released.value.state, "released")
            released_placement = self.runtime.get(Placement, placement.value.id)
            self.assertEqual(released_placement.value.state, "released")
            self.assertTrue(self.lifecycle.view(lease_id).teardown_complete)
            self.assertIn("worker-" + project, self.worker_port.terminated)
            events.append(from_lease(released, released_placement.value, SCOPE, cursor=next_cursor("aik_runtime")))

            flows.append(SimpleNamespace(attempt=attempt, project=project, lease_id=lease_id,
                                         placement_id=placement.value.id, request_id=record.identity.request_id,
                                         provider_job_id=receipt.provider_job_id,
                                         artifact_id=artifact.artifact_id))
        return flows, events

    # --------------------------------------------------------------- scenarios

    def test_1_happy_path_connects_every_module_output_to_the_next_input(self) -> None:
        flows, events = self.run_happy_path()
        model = ReadModel(self.state / "readmodel.sqlite3", user_scope=SCOPE, clock=self.clock)
        report = model.apply(events)
        self.assertEqual(report.conflicts, 0)
        for flow in flows:
            with self.subTest(attempt=flow.attempt):
                view = model.attempt_view(flow.attempt)
                self.assertTrue(view.found)
                self.assertFalse(view.authoritative)
                self.assertFalse(view.stale)
                self.assertEqual(ref_values(view.ids, "placement_id"), {flow.placement_id})
                self.assertEqual(ref_values(view.ids, "lease_id"), {flow.lease_id})
                self.assertEqual(ref_values(view.ids, "request_id"), {flow.request_id})
                self.assertEqual(ref_values(view.ids, "provider_job_id"), {flow.provider_job_id})
                self.assertEqual(ref_values(view.ids, "vm_project_id"), {flow.project})
        # Two VM projects do not leak into each other's attempt view.
        self.assertNotEqual(flows[0].lease_id, flows[1].lease_id)
        self.assertEqual(self.provider.start_calls, 2)

    def test_2_paused_project_gets_no_grant_and_no_active_placement_until_resume(self) -> None:
        task = ready("attempt-1", "project-1")
        self.gate.pause("project:project-1", "synthetic-maintenance")
        paused = self.tick(task)
        self.assertEqual(paused.granted, ())
        self.assertIn("attempt-1", [attempt for attempt, _ in paused.waiting])
        self.assertEqual([entry for entry in self.placements_for("attempt-1")
                          if entry.value.state in ACTIVE_STATES], [])
        self.assertEqual([entry for entry in self.all_records(Lease) if entry.value.state in HOLDING_STATES], [])

        self.gate.resume("project:project-1")
        resumed = self.tick(task)
        self.assertEqual(resumed.waiting, (), "resume must not report ATTEMPT_ALREADY_PLACED or other waits")
        self.assertEqual([attempt for attempt, _ in resumed.granted], ["attempt-1"])
        lease = self.runtime.get(Lease, dict(resumed.granted)["attempt-1"])
        self.assertEqual(lease.value.state, "reserved")

    def test_3_known_provider_failure_rejects_dispatch_and_only_a_new_attempt_is_placed(self) -> None:
        self.assertNotEqual(fault_from_provider(**KNOWN_FAULT).outcome, UNKNOWN_OUTCOME)
        first = self.tick(ready("attempt-1", "project-1"))
        lease_id = dict(first.granted)["attempt-1"]
        lease = self.runtime.get(Lease, lease_id)
        placement = self.runtime.get(Placement, lease.value.placement_id)

        self.provider.mode = "known_failure"
        record, _, receipt = self.dispatch(lease.value, placement.value)
        self.assertIsNone(receipt)
        self.assertEqual(record.state, "rejected")
        self.assertIsNone(record.provider_job_id)
        self.assertEqual(self.runtime.get(Lease, lease_id).value.state, "released")
        self.assertEqual(self.provider.start_calls, 1)

        current = self.runtime.get(Placement, placement.value.id)
        failed = self.planner.release_placement(placement.value.id, current.revision, "failed")
        self.assertEqual(failed.value.state, "failed")

        self.provider.mode = "ok"
        retry = self.tick(ready("attempt-1", "project-1"), ready("attempt-3", "project-1"))
        self.assertEqual([attempt for attempt, _ in retry.granted], ["attempt-3"])
        waiting = dict(retry.waiting)
        self.assertIn("attempt-1", waiting)
        self.assertTrue(any("ATTEMPT_ALREADY_PLACED" in str(reason) for reason in waiting["attempt-1"]))
        self.assertEqual(self.provider.start_calls, 1, "no module may resend the rejected Attempt")

    def test_4_unknown_dispatch_survives_restart_without_relaunch_or_release(self) -> None:
        granted = self.tick(ready("attempt-1", "project-1"))
        lease_id = dict(granted.granted)["attempt-1"]
        lease = self.runtime.get(Lease, lease_id)
        placement = self.runtime.get(Placement, lease.value.placement_id)
        self.provider.mode = "unknown"
        record, _, receipt = self.dispatch(lease.value, placement.value)
        self.assertIsNone(receipt)
        self.assertEqual(record.state, "send_unknown")
        self.assertIn(self.runtime.get(Lease, lease_id).value.state, HOLDING_STATES)
        self.assertIsNotNone(self.budget.reservation(lease_id))

        # Restart: new instances on the same files.
        runtime = RuntimeStore(self.state / "runtime.sqlite3", timeout=10)
        budget = BudgetStore(self.state / "budget.sqlite3", clock=self.clock)
        leases = LeaseManager(runtime, budget, ttl_seconds=TTL, clock=self.clock)
        observer = WorkerObserver(self.state / "observer.sqlite3", budget=budget, clock=self.clock)
        journal = DispatchJournal(self.state / "dispatch.sqlite3", clock=self.clock)
        self.assertEqual(journal.record(record.identity.request_id).state, "send_unknown")
        provider = FakeProvider()
        dispatcher = NativeDispatcher(journal, leases, GuardedAdapter(provider, authorize_dispatch=lambda *a, **k: True),
                                      observer, clock=self.clock)
        # A restarted dispatcher only converts `reserved`; send_unknown stays and nothing is resent.
        self.assertEqual(dispatcher.recover(), ())
        again = dispatcher.dispatch(record.identity, JobRequest(BINDING, record.identity.request_id, ROUTE_ID,
                                                                "attempt-1", MODEL), requested_model=MODEL)
        self.assertEqual(again.state, "send_unknown")
        self.assertEqual(provider.start_calls, 0)
        effects = FakeEffects()
        effect = EffectRef("attempt-1", "tool-call-unknown", "dev.create_file", False, False, "non_idempotent")
        effects.refs = (effect,)
        effects.pending["attempt-1"] = (effect,)
        reconciler = RecoveryReconciler(
            runtime, leases, observer, vm_port=FakeVm({"attempt-1": "active"}), job_port=provider,
            effect_port=effects, journal=RecoveryJournal(self.state / "recovery.sqlite3"), clock=self.clock,
        )
        report = reconciler.reconcile()
        lease_decisions = [d for d in report.decisions if d.subject_id == lease_id]
        self.assertTrue(lease_decisions, "the unknown lease must leave a journaled decision")
        for decision in lease_decisions:
            self.assertNotIn(decision.action, ("release_provider_terminal", "release_undispatched"))
        self.assertIn(effect.subject_id, [d.subject_id for d in report.decisions] + list(report.failures))
        self.assertEqual(provider.start_calls, 0)
        self.assertEqual(effects.executed, [])
        self.assertIn(runtime.get(Lease, lease_id).value.state, HOLDING_STATES)

        second = reconciler.reconcile()
        self.assertEqual(provider.start_calls, 0)
        self.assertEqual(effects.executed, [])
        self.assertNotIn(lease_id, [d.subject_id for d in second.decisions if d.action.startswith("release")])

        worker_port = FakeWorkerPort()
        lifecycle = LifecycleController(
            LifecycleGate(self.state / "lifecycle.sqlite3", clock=self.clock), leases, observer,
            WorkerRegistry(self.state / "lifecycle.sqlite3", clock=self.clock), store=runtime, job_port=provider,
            worker_port=worker_port, effect_port=effects,
            evidence_port=EvidenceRelay(self.state / "evidence.sqlite3", managed_root=self.managed,
                                        clock=self.clock),
            clock=self.clock,
        )
        try:
            view = lifecycle.teardown(lease_id, lease.value.fencing_token, required_artifacts=())
        except LifecycleError:
            view = None
        if view is not None:
            self.assertFalse(view.teardown_complete)
        self.assertIn(runtime.get(Lease, lease_id).value.state, HOLDING_STATES)
        self.assertEqual(worker_port.terminated, [])
        self.assertEqual(provider.start_calls, 0)

    def test_5_independence_violation_and_oracle_change_never_set_the_vm_verdict(self) -> None:
        attempt, project = "attempt-5", "project-1"
        self.tools.create_file(self.worker_session, Invocation("call-worker-5", INPUT_REVISION, 1, "effect-worker-5"),
                               "src/app.txt", ORACLE)
        before = self.runs()
        with self.assertRaises(ValidationError) as rejected:
            self.checker.run(attempt, worker_session_id=self.worker_session.session_id,
                             reviewer_session=ToolSession(self.worker_session.session_id, "reviewer", WORKSPACE),
                             invocation=Invocation("call-review-same", INPUT_REVISION, 1, "effect-review-same"),
                             command_id="unit")
        self.assertIn("NOT_INDEPENDENT", error_code(rejected.exception))
        self.assertEqual(self.runs(), before, "a non-independent check must not run the command")
        self.assertEqual(self.checker.history(attempt), ())

        # A worker with direct file access changes the pinned oracle and its output.
        (self.root / "tests" / "expected.txt").write_bytes(CHANGED)
        (self.root / "src" / "app.txt").write_bytes(CHANGED)
        oracle = self.checker.run(attempt, worker_session_id=self.worker_session.session_id,
                                  reviewer_session=self.reviewer_session,
                                  invocation=Invocation("call-review-oracle", INPUT_REVISION, 1,
                                                        "effect-review-oracle"),
                                  command_id="unit")
        self.assertFalse(oracle.oracle_ok)

        report = vm_receipt(Command.FINAL_REPORT, project=project)
        without_check = build_completion_view(attempt, vm_project_id=project, final_report_receipt=report)
        changed = build_completion_view(attempt, vm_project_id=project, check=oracle, final_report_receipt=report)
        self.assertEqual(changed.check_verdict, "ORACLE_CHANGED")
        self.assertFalse(without_check.vm_verdict_recorded)
        self.assertFalse(changed.vm_verdict_recorded)
        verdict_report = vm_receipt(Command.FINAL_REPORT, project=project, goal_verdict_id="vm-verdict-5")
        recorded = build_completion_view(attempt, vm_project_id=project, check=oracle,
                                         final_report_receipt=verdict_report)
        self.assertTrue(recorded.vm_verdict_recorded)
        self.assertEqual(recorded.goal_verdict_id, "vm-verdict-5")
        self.assertEqual(recorded.check_verdict, "ORACLE_CHANGED")

    def test_6_read_model_replay_in_reverse_with_duplicates_matches_forward_and_rebuild(self) -> None:
        _, events = self.run_happy_path()
        forward = ReadModel(self.state / "readmodel-forward.sqlite3", user_scope=SCOPE, clock=self.clock)
        forward.apply(events)
        expected = forward.projection_digest()

        replay = ReadModel(self.state / "readmodel-replay.sqlite3", user_scope=SCOPE, clock=self.clock)
        for event in reversed(events):
            replay.apply([event])
        duplicates = replay.apply(events)
        self.assertEqual(duplicates.duplicates, len(events))
        self.assertEqual(duplicates.conflicts, 0)
        self.assertEqual(replay.projection_digest(), expected)
        self.assertFalse(any(status.stale for status in replay.cursor_status().values()))
        self.assertEqual(replay.rebuild(), expected)
        self.assertEqual(replay.projection_digest(), expected)
        self.assertEqual(forward.rebuild(), expected)


if __name__ == "__main__":
    unittest.main()
