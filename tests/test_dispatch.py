"""AIK-035 dispatch skeleton against disposable SQLite files and real temporary workspaces.

Runtime, budget, lease and observer stores are the real modules on temporary
files. The provider is a synthetic ProviderAdapter wrapped by the real
GuardedAdapter; the model is a fake port; DevTools is real with a fake
authority (copied from tests/test_devtools.py). No provider, credential,
network, VM or AGS installation is used. Linux success is development
verification, not Windows support evidence (Windows is NOT_RUN).
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    STATUS_AUTHORIZED, AuthorityCode, AuthorityDenied, EffectKind, EffectOutcomeUnknown, EffectReceipt,
    ToolCallAuthorization, ToolSession, canonical_arguments,
)
from aik_authority.models import target_covers  # noqa: E402
from aik_budget import BudgetStore  # noqa: E402
from aik_devtools import (  # noqa: E402
    DevToolCode, DevToolPolicy, DevTools, EffectUnknown, VerificationCommand, files_scope_target,
    tool_declarations, verification_scope_target,
)
from aik_dispatch import (  # noqa: E402
    ApiToolLoop, DispatchError, DispatchIdentity, DispatchJournal, DispatchOutcomeUnknown, DispatchRejected,
    ModelToolCall, ModelTurn, NativeDispatcher, assistant_message,
)
from aik_lease import LeaseManager  # noqa: E402
from aik_observer.observer import WorkerObserver  # noqa: E402
from aik_pool import QualificationLevel, RouteStatus  # noqa: E402
from aik_profiles import ExecutionChoice, RoleProfile  # noqa: E402
from aik_provider.contracts import (  # noqa: E402
    TERMINAL, AccountIsolation, AdapterCapabilities, AuthKind, AuthObservation, AuthState, ConnectionBinding,
    JobReceipt, JobRequest, JobState, Support,
)
from aik_provider.controller import BoundJob, GuardedAdapter  # noqa: E402
from aik_runtime import ExecutionProfile, InvalidRecord, Lease, Placement, Pool, RuntimeStore  # noqa: E402
from aik_runtime.errors import UnsafeStoragePath  # noqa: E402
from aik_workspace import ProjectInfo, WorkspaceManager, WorkspaceRegistry, sha256_digest  # noqa: E402


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
TTL = 60
WORKER = RoleProfile("worker-1", "workspace-1", "worker", ExecutionChoice("connection-1", "synthetic-model"), 50)
SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_BE_STORED"
WORKSPACE = "ws-dev"
INPUT_REVISION = "plan-rev-1"
PYTHON = os.path.abspath(sys.executable)
CONTENT = "hello 세계\n"
USER = {"role": "user", "text": "create src/app.txt"}


def _member(enum, *preferred):
    """Pick an enum member by value/name preference; enum members are not part of the public stub."""
    for wanted in preferred:
        for member in enum:
            if member.value == wanted or member.name.lower() == wanted:
                return member
    return next(iter(enum))


SUPPORTED = _member(Support, "supported", "implemented", "available")
ISOLATED = _member(AccountIsolation, "isolated", "dedicated", "per_connection")
SIGNED_IN = _member(AuthState, "signed_in", "authenticated", "ready", "valid", "active")
SIGNED_OUT = _member(AuthState, "signed_out", "unauthenticated")
PENDING_JOB = _member(JobState, *(name for name in ("queued", "pending", "accepted", "submitted", "running")
                                  if any(member.value == name or member.name.lower() == name
                                         for member in JobState if member not in TERMINAL)))
BINDING = ConnectionBinding("synthetic", "synthetic-runtime", "connection-1", 1, _member(AuthKind, "api_key"),
                            "secret://local/test-key", "0123456789abcdef" * 4)
UNKNOWN_CODE = getattr(DevToolCode, "EFFECT_UNKNOWN", next(iter(DevToolCode)))


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class SimulatedCrash(BaseException):
    """Stands in for process death at a chosen point."""


def make_route(pool_id: str = "pool-1", **changes) -> RouteStatus:
    stamp = iso(START)
    values = dict(
        pool_id=pool_id, connection_id="connection-1", connection_revision=1, provider="synthetic",
        host="synthetic-host", auth_kind="api_key", model="synthetic-model", tools=(), pool_enabled=True,
        configured_max_parallel=8, implementation_state="implemented", account_binding="bound",
        runtime_support="supported", account_isolation="isolated", governance="unknown",
        last_probe_at=stamp, last_verified_at=stamp, freshness="fresh", matrix_revision=1,
        qualification_matrix_revision=None, capacity_state="known", available_capacity=8,
        level=QualificationLevel.PROBE_VERIFIED, executable_now=True, placement_eligible=False,
    )
    values.update(changes)
    return RouteStatus(**values)


def allow_dispatch(*args, **kwargs) -> bool:
    return True


class FakeProvider:
    """Synthetic ProviderAdapter; it counts calls and never contacts anything."""

    def __init__(self) -> None:
        self.start_calls = 0
        self.observe_calls = 0
        self.on_start = None
        self.start_error = None
        self.observe_state = JobState.SUCCEEDED

    def describe(self, binding):
        # An API key connection has no host sign-in or sign-out.
        return AdapterCapabilities(binding.provider, binding.runtime, binding.auth_kind, ISOLATED,
                                   SUPPORTED, Support.UNSUPPORTED, Support.UNSUPPORTED, SUPPORTED, SUPPORTED,
                                   SUPPORTED)

    def auth_status(self, binding):
        return AuthObservation(binding.connection_id, binding.revision, binding.account_fingerprint, SIGNED_IN)

    def begin_sign_in(self, binding):
        return self.auth_status(binding)

    def sign_out(self, binding):
        return AuthObservation(binding.connection_id, binding.revision, binding.account_fingerprint, SIGNED_OUT)

    def start_job(self, request):
        self.start_calls += 1
        if self.on_start is not None:
            self.on_start(request)
        if self.start_error is not None:
            raise self.start_error
        binding = request.binding
        return JobReceipt(request.request_id, "job-1", binding.connection_id, binding.revision,
                          binding.account_fingerprint, PENDING_JOB)

    def observe_job(self, request, receipt):
        self.observe_calls += 1
        return replace(receipt, state=self.observe_state)

    def interrupt_job(self, request, receipt):
        return replace(receipt, state=JobState.CANCELLED)


class NativeDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-dispatch-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.clock = FakeClock()
        self.store = RuntimeStore(self.base / "runtime.sqlite3")
        self.store.save(Pool("pool-1", "synthetic", "connection-1", max_parallel=4, enabled=True), expected_revision=0)
        self.store.save(ExecutionProfile("worker-1", "pool-1", "worker", "synthetic-model"), expected_revision=0)
        self.store.save(Placement("placement-1", "pool-1", "worker-1", "vm-instance-1", "vm-project-1",
                                  "vm-task-1", "vm-attempt-1", state="assigned"), expected_revision=0)
        self.budget = BudgetStore(self.base / "budget.sqlite3", clock=self.clock)
        self.leases = LeaseManager(self.store, self.budget, ttl_seconds=TTL, clock=self.clock)
        result = self.leases.reserve("placement-1", worker_id="worker-1", route=make_route(),
                                     budget_profile=WORKER, max_seconds=60)
        self.assertTrue(result.granted, result.reasons)
        self.lease = result.lease.value
        self.observer = WorkerObserver(self.base / "observer.sqlite3", budget=self.budget, clock=self.clock)
        self.journal = DispatchJournal(self.base / "dispatch.sqlite3", clock=self.clock)
        self.provider = FakeProvider()
        self.adapter = GuardedAdapter(self.provider, authorize_dispatch=allow_dispatch)
        self.dispatcher = self.make_dispatcher()

    def make_dispatcher(self) -> NativeDispatcher:
        return NativeDispatcher(self.journal, self.leases, self.adapter, self.observer, clock=self.clock)

    def identity(self, **changes) -> DispatchIdentity:
        values = dict(
            request_id="req-1", contract_digest="sha256:" + "1" * 64, vm_project_id="vm-project-1",
            vm_task_id="vm-task-1", vm_attempt_id="vm-attempt-1", lease_id=self.lease.id,
            fencing_token=self.lease.fencing_token, pool_id="pool-1", route_id="route-1", binding=BINDING,
            canonical_cwd=str(self.base / "workspace"), input_digest="sha256:" + "2" * 64, path="native",
        )
        values.update(changes)
        return DispatchIdentity(**values)

    @staticmethod
    def job_request(**changes) -> JobRequest:
        values = dict(binding=BINDING, request_id="req-1", route_id="route-1", vm_attempt_id="vm-attempt-1",
                      model="synthetic-model")
        values.update(changes)
        return JobRequest(**values)

    def stored_lease(self) -> Lease:
        return self.store.get(Lease, self.lease.id).value

    def states(self, request_id: str = "req-1") -> list:
        return [event.state for event in self.journal.record(request_id).events]

    def test_reserved_sent_ack_started_order_with_lease_intent_before_provider_call(self) -> None:
        seen = []

        def capture(request):
            lease = self.stored_lease()
            seen.append((lease.state, lease.provider_job_id, self.journal.record("req-1").state))

        self.provider.on_start = capture
        record = self.dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        self.assertEqual(seen, [("active", None, "reserved")])
        self.assertEqual(record.state, "sent_ack")
        self.assertEqual(record.provider_job_id, "job-1")
        self.assertEqual(self.stored_lease().provider_job_id, "job-1")
        started = self.dispatcher.confirm_started("req-1")
        self.assertEqual(started.state, "started")
        self.assertEqual(self.states(), ["reserved", "sent_ack", "started"])
        self.assertEqual(self.provider.observe_calls, 1)
        self.assertEqual(started.identity, self.identity())

    def test_identity_mismatch_is_rejected_before_any_effect(self) -> None:
        for changes in ({"vm_attempt_id": "vm-attempt-2"}, {"route_id": "route-2"}, {"request_id": "req-2"},
                        {"binding": replace(BINDING, connection_id="connection-2")}):
            with self.subTest(changed=tuple(changes)):
                with self.assertRaises(DispatchError) as raised:
                    self.dispatcher.dispatch(self.identity(), self.job_request(**changes),
                                             requested_model="synthetic-model")
                self.assertEqual(raised.exception.code, "IDENTITY_MISMATCH")
        self.assertEqual(self.provider.start_calls, 0)
        self.assertEqual(self.stored_lease().state, "reserved")
        self.assertIsNone(self.journal.find("req-1"))

    def test_duplicate_dispatch_does_not_call_provider_and_changed_identity_conflicts(self) -> None:
        first = self.dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        again = self.dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        self.assertEqual(again, first)
        self.assertEqual(self.provider.start_calls, 1)
        with self.assertRaises(DispatchError) as raised:
            self.dispatcher.dispatch(self.identity(canonical_cwd=str(self.base / "other")), self.job_request(),
                                     requested_model="synthetic-model")
        self.assertEqual(raised.exception.code, "CONFLICT")
        self.assertEqual(self.provider.start_calls, 1)

    def test_known_guard_failure_is_rejected_and_returns_the_lease_without_dispatch(self) -> None:
        self.adapter = GuardedAdapter(self.provider, authorize_dispatch=lambda request: False)
        dispatcher = self.make_dispatcher()
        with self.assertRaises(DispatchRejected):
            dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        self.assertEqual(self.provider.start_calls, 0)
        self.assertEqual(self.states(), ["reserved", "rejected"])
        lease = self.stored_lease()
        self.assertEqual((lease.state, lease.provider_job_id), ("released", None))
        self.assertNotEqual(self.budget.reservation(lease.id).state, "reserved")
        # The rejected request stays rejected; VM decides any retry with a new request.
        again = dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        self.assertEqual(again.state, "rejected")
        self.assertEqual(self.provider.start_calls, 0)

    def test_start_exception_records_send_unknown_without_resend_or_raw_message(self) -> None:
        self.provider.start_error = RuntimeError(SECRET_MARKER)
        with self.assertRaises(DispatchOutcomeUnknown):
            self.dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        record = self.journal.record("req-1")
        self.assertEqual(record.state, "send_unknown")
        self.assertEqual(self.provider.start_calls, 1)
        self.provider.start_error = None
        self.assertEqual(self.dispatcher.dispatch(self.identity(), self.job_request(),
                                                  requested_model="synthetic-model"), record)
        self.assertEqual(self.dispatcher.confirm_started("req-1").state, "send_unknown")
        self.assertEqual(self.provider.start_calls, 1)
        self.assertEqual(self.provider.observe_calls, 0)
        lease = self.stored_lease()
        self.assertEqual((lease.state, lease.provider_job_id), ("active", None))
        self.assertNotIn(SECRET_MARKER.encode("ascii"), self.journal.path.read_bytes())

    def test_crash_after_reserved_is_recovered_as_send_unknown_without_resend(self) -> None:
        with mock.patch.object(GuardedAdapter, "start_job", side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):
                self.dispatcher.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        self.assertEqual(self.journal.record("req-1").state, "reserved")
        restarted = self.make_dispatcher()
        recovered = restarted.recover()
        self.assertEqual([(entry.identity.request_id, entry.state) for entry in recovered],
                         [("req-1", "send_unknown")])
        self.assertEqual(restarted.recover(), ())
        restarted.dispatch(self.identity(), self.job_request(), requested_model="synthetic-model")
        self.assertEqual(self.provider.start_calls, 0)
        self.assertEqual(self.states(), ["reserved", "send_unknown"])

    def test_receipt_from_another_connection_is_not_accepted(self) -> None:
        request = self.job_request()
        foreign = JobReceipt("req-1", "job-foreign", "connection-2", 1, "fedcba9876543210" * 4, PENDING_JOB)
        with mock.patch.object(GuardedAdapter, "start_job", return_value=BoundJob(request, foreign)):
            with self.assertRaises(DispatchOutcomeUnknown):
                self.dispatcher.dispatch(self.identity(), request, requested_model="synthetic-model")
        self.assertEqual(self.states(), ["reserved", "send_unknown"])
        self.assertIsNone(self.stored_lease().provider_job_id)
        self.assertIsNone(self.journal.record("req-1").provider_job_id)

    def test_identity_provenance_is_optional_and_part_of_the_digest(self) -> None:
        plain = self.identity()
        self.assertIsNone(plain.observed_permission)
        self.assertIsNone(plain.trusted_same_user)
        observed = self.identity(observed_permission="same-user token, no elevation", trusted_same_user=True)
        self.assertNotEqual(plain.identity_digest, observed.identity_digest)
        self.assertEqual(DispatchIdentity.from_dict(json.loads(json.dumps(observed.to_dict()))), observed)
        for changes in ({"trusted_same_user": "yes"}, {"canonical_cwd": "relative/dir"}, {"path": "shell"},
                        {"contract_digest": "abc"}):
            with self.subTest(changed=tuple(changes)):
                with self.assertRaises(InvalidRecord):
                    self.identity(**changes)


class FakeProjects:
    def __init__(self):
        self.roots = {}

    def create_project(self, workspace_id, canonical_root, name):
        self.roots["vm-project-1"] = canonical_root
        return "vm-project-1"

    def lookup_project(self, project_id):
        return ProjectInfo(project_id, self.roots.get(project_id))


class FakeAuthority:
    """Mimics the AuthorityGateway contract; copied from tests/test_devtools.py."""

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
        digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        if call.effect_id is not None and self.effects.get(call.effect_id) == "outcome_unknown":
            raise AuthorityDenied(AuthorityCode.RECONCILIATION_REQUIRED, subject="effect_id")
        previous = self.decisions.get(call.tool_call_id)
        if previous is not None:
            if previous.request_digest != digest or previous.session_id != session.session_id:
                raise AuthorityDenied(AuthorityCode.TOOL_CALL_CONFLICT, subject="tool_call_id")
            if kind is EffectKind.NON_IDEMPOTENT:
                raise AuthorityDenied(AuthorityCode.EFFECT_ALREADY_AUTHORIZED, subject="effect_id")
            return previous
        if kind is not EffectKind.READ_ONLY:
            if self.intent_failure:
                raise EffectOutcomeUnknown(AuthorityCode.INTENT_UNKNOWN, subject="record_intent")
            self.intents.append((call.effect_id, call.tool_call_id, digest))
            self.effects[call.effect_id] = "intent_recorded"
        decision = ToolCallAuthorization(
            STATUS_AUTHORIZED, call.tool_call_id, call.tool_id, kind, call.effect_id, digest, session.session_id,
            session.role, call.workspace_id, "vm-project-1", "vm-task-1", "vm-attempt-1", "vm-auth-1", 1,
            "lease-1", 1, 1, "sha256:" + "0" * 64, "2030-01-01T00:00:00Z",
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


class FakeModel:
    """Synthetic model port: returns queued turns, then end_turn. No API is called."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    def next(self, request_id, messages):
        self.calls.append((request_id, messages))
        if not self.turns:
            return ModelTurn("end_turn", "done", ())
        return self.turns.pop(0)


class CrashOn:
    def __init__(self, number: int):
        self.number = number
        self.count = 0

    def __call__(self) -> bool:
        self.count += 1
        if self.count == self.number:
            raise SimulatedCrash()
        return True


def create_turn(*calls) -> ModelTurn:
    calls = calls or (ModelToolCall("call-1", "create_file", {"path": "src/app.txt", "content": CONTENT}),)
    return ModelTurn("tool_use", "working", tuple(calls))


class ApiToolLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-dispatch-api-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        folder = self.base / "작업 공간" / "프로젝트 A"
        folder.mkdir(parents=True)
        self.manager = WorkspaceManager(WorkspaceRegistry(self.base / "state" / "workspaces.json"), FakeProjects())
        self.manager.open_workspace(WORKSPACE, "개발 작업", folder)
        self.root = Path(self.manager.execution_context(WORKSPACE).canonical_root)
        (self.root / "src").mkdir()
        self.session = ToolSession("session-worker-1", "worker", WORKSPACE)
        self.authority = FakeAuthority()
        self.tools = DevTools(authority=self.authority, workspaces=self.manager, policy=DevToolPolicy(WORKSPACE, (
            VerificationCommand("unit", (PYTHON, "-c", "import sys; sys.exit(0)"), timeout_seconds=30),
        ), analysis_only_paths=("fixtures",)))
        self.journal = DispatchJournal(self.base / "dispatch.sqlite3")
        self.identity = DispatchIdentity(
            "req-api", "sha256:" + "1" * 64, "vm-project-1", "vm-task-1", "vm-attempt-1", "lease-1", 1,
            "pool-1", "route-1", BINDING, str(self.root), "sha256:" + "2" * 64, "api",
        )

    def loop(self, model, **changes) -> ApiToolLoop:
        values = dict(max_model_calls=8, max_tool_calls=8, budget_ok=lambda: True)
        values.update(changes)
        return ApiToolLoop(model, self.tools, self.journal, **values)

    def run_loop(self, loop, messages=(USER,)):
        return loop.run(self.identity, self.session, input_revision=INPUT_REVISION, authorization_revision=1,
                        messages=messages)

    def resume_loop(self, loop, messages):
        return loop.resume(self.identity, self.session, input_revision=INPUT_REVISION, authorization_revision=1,
                           messages=messages)

    def tool_states(self, call_id: str = "call-1") -> list:
        return [event.state for event in self.journal.tool_events("req-api") if event.tool_call_id == call_id]

    def test_create_file_result_returns_to_the_model_then_end_turn(self) -> None:
        model = FakeModel(create_turn())
        result = self.run_loop(self.loop(model))
        self.assertEqual((result.stop_reason, result.model_calls, result.tool_calls), ("end_turn", 2, 1))
        self.assertEqual(result.unresolved_tool_calls, ())
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), CONTENT.encode("utf-8"))
        returned = model.calls[1][1][-1]
        self.assertEqual((returned["role"], returned["tool_call_id"], returned["result"]["ok"]),
                         ("tool", "call-1", True))
        self.assertEqual(self.tool_states(), ["intent", "receipt", "returned"])
        intent = self.journal.tool_events("req-api")[0].data
        self.assertNotIn(CONTENT, json.dumps(dict(intent), ensure_ascii=False))
        self.assertEqual(intent["effect_id"], "effect-call-1")

    def test_model_and_tool_limits_budget_and_cancel_stop_the_loop(self) -> None:
        model = FakeModel(create_turn())
        result = self.run_loop(self.loop(model, max_model_calls=1))
        self.assertEqual((result.stop_reason, result.model_calls), ("max_model_calls", 1))
        self.assertEqual(self.tool_states(), ["intent", "receipt"])
        self.assertEqual(result.unresolved_tool_calls, ())

    def test_max_tool_calls_stops_before_the_next_effect(self) -> None:
        turn = create_turn(ModelToolCall("call-1", "create_file", {"path": "src/a.txt", "content": "a"}),
                           ModelToolCall("call-2", "create_file", {"path": "src/b.txt", "content": "b"}))
        result = self.run_loop(self.loop(FakeModel(turn), max_tool_calls=1))
        self.assertEqual((result.stop_reason, result.tool_calls), ("max_tool_calls", 1))
        self.assertTrue((self.root / "src" / "a.txt").exists())
        self.assertFalse((self.root / "src" / "b.txt").exists())
        self.assertEqual(self.tool_states("call-2"), [])

    def test_budget_and_cancel_stop_before_any_model_call(self) -> None:
        model = FakeModel(create_turn())
        self.assertEqual(self.run_loop(self.loop(model, budget_ok=lambda: False)).stop_reason, "budget_stop")
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.run_loop(self.loop(model, cancel=cancel)).stop_reason, "cancelled")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.authority.requests, [])

    def test_unsupported_tool_and_bad_arguments_return_errors_without_effect(self) -> None:
        turn = create_turn(ModelToolCall("call-1", "delete_file", {"path": "src/app.txt"}),
                           ModelToolCall("call-2", "create_file", {"path": "src/app.txt"}))
        model = FakeModel(turn)
        result = self.run_loop(self.loop(model))
        self.assertEqual(result.stop_reason, "end_turn")
        results = [message["result"] for message in model.calls[1][1] if message.get("role") == "tool"]
        self.assertEqual(results, [{"ok": False, "error": "TOOL_NOT_ALLOWED"},
                                   {"ok": False, "error": "INVALID_ARGUMENTS"}])
        self.assertEqual(self.authority.requests, [])
        self.assertFalse((self.root / "src" / "app.txt").exists())
        self.assertEqual(self.tool_states("call-1"), ["intent", "denied", "returned"])
        self.assertEqual(self.journal.tool_events("req-api")[0].data["tool"], "unsupported")

    def test_crash_after_intent_resumes_as_effect_unknown_without_execution(self) -> None:
        with mock.patch.object(DevTools, "create_file", side_effect=SimulatedCrash()):
            with self.assertRaises(SimulatedCrash):
                self.run_loop(self.loop(FakeModel(create_turn())))
        self.assertEqual(self.tool_states(), ["intent"])
        model = FakeModel()
        result = self.resume_loop(self.loop(model), (USER, assistant_message(create_turn())))
        self.assertEqual(result.stop_reason, "effect_unknown")
        self.assertEqual(result.unresolved_tool_calls, ("call-1",))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.authority.intents, [])
        self.assertFalse((self.root / "src" / "app.txt").exists())
        self.assertEqual(self.tool_states(), ["intent", "effect_unknown"])

    def test_crash_after_receipt_replays_the_same_result_without_a_second_effect(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.run_loop(self.loop(FakeModel(create_turn()), budget_ok=CrashOn(3)))
        self.assertEqual(self.tool_states(), ["intent", "receipt"])
        self.assertEqual(len(self.authority.intents), 1)
        model = FakeModel()
        result = self.resume_loop(self.loop(model), (USER, assistant_message(create_turn())))
        self.assertEqual((result.stop_reason, result.model_calls, result.tool_calls), ("end_turn", 2, 1))
        resent = model.calls[0][1][-1]
        self.assertEqual((resent["tool_call_id"], resent["result"]["ok"]), ("call-1", True))
        self.assertEqual(resent["result"]["result"]["digest"], sha256_digest(CONTENT.encode("utf-8")))
        self.assertEqual(len(self.authority.intents), 1)
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), CONTENT.encode("utf-8"))
        self.assertEqual(self.tool_states(), ["intent", "receipt", "returned"])

    def test_effect_unknown_stops_without_retry(self) -> None:
        model = FakeModel(create_turn())
        with mock.patch.object(DevTools, "create_file", side_effect=EffectUnknown(UNKNOWN_CODE)):
            result = self.run_loop(self.loop(model))
        self.assertEqual(result.stop_reason, "effect_unknown")
        self.assertEqual(result.unresolved_tool_calls, ("call-1",))
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.tool_states(), ["intent", "effect_unknown"])
        again = self.resume_loop(self.loop(FakeModel()), (USER, assistant_message(create_turn())))
        self.assertEqual(again.stop_reason, "effect_unknown")
        self.assertEqual(self.authority.requests, [])


class JournalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-dispatch-journal-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def identity(self) -> DispatchIdentity:
        return DispatchIdentity("req-1", "sha256:" + "1" * 64, "vm-project-1", "vm-task-1", "vm-attempt-1",
                                "lease-1", 1, "pool-1", "route-1", BINDING, str(self.base),
                                "sha256:" + "2" * 64, "native")

    def test_all_three_tables_are_append_only(self) -> None:
        journal = DispatchJournal(self.base / "dispatch.sqlite3")
        journal.reserve(self.identity(), {"lease_id": "lease-1"})
        journal.tool_event("req-1", "call-1", "intent", {"tool": "read_file"})
        with self.assertRaises(DispatchError):
            journal.tool_event("req-1", "call-1", "intent", {"tool": "read_file"})
        with closing(sqlite3.connect(journal.path)) as connection:
            for table in ("dispatches", "dispatch_events", "tool_events"):
                for statement in (f'UPDATE "{table}" SET "request_id" = "request_id"', f'DELETE FROM "{table}"'):
                    with self.subTest(statement=statement):
                        with self.assertRaises(sqlite3.DatabaseError):
                            connection.execute(statement)
        self.assertEqual(journal.record("req-1").state, "reserved")
        self.assertEqual(len(journal.tool_events("req-1")), 1)

    def test_foreign_database_and_repository_paths_are_rejected(self) -> None:
        foreign = self.base / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(DispatchError) as raised:
            DispatchJournal(foreign)
        self.assertEqual(raised.exception.code, "INVALID_JOURNAL")
        self.assertEqual(foreign.read_bytes(), before)
        inside = ROOT / "aik-dispatch-should-not-exist.sqlite3"
        with self.assertRaises(UnsafeStoragePath):
            DispatchJournal(inside)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
