"""AIK-026 controller loop against a fake VM application port.

The real aik_vm_bridge.VmBridge is used over a fake EngineApplication port that
owns the plan DAG, readiness, validation and GoalVerdict (as VM does). Only
synthetic IDs are used: no provider, credential, network or VM installation.
Linux success is development verification, not Windows support.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    AuthorityCode, AuthorityDenied, EffectKind, EffectOutcomeUnknown, ToolDeclaration, ToolPolicy,
)
from aik_controller import (  # noqa: E402
    Action, Controller, GatewayCommandAuthority, InvalidControllerConfig, Memory, RunPolicy, TaskView,
    VmView, decide, interpret_final_report,
)
from aik_provider.contracts import AuthKind  # noqa: E402
from aik_runtime import Lease, Placement, Pool  # noqa: E402
from aik_vm_bridge import (  # noqa: E402
    AuthorizeRequest, Command, EffectOutcome, RunOnceRequest, TaskExecutionBinding, VmBridge, VmBridgeError,
)


PROJECT = "project-1"
REVISION = "surface-rev-test"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
DIGEST = "sha256:" + "0" * 64
AUTH_KIND = next(iter(AuthKind)).value
DEFINITE = next(outcome for outcome in EffectOutcome if outcome is not EffectOutcome.UNKNOWN)
SIGNATURES = {
    "prepare": "self, project_id, *, source_request, candidate_count=None",
    "revise": "self, project_id, *, source_request, candidate_count=None",
    "authorize": "self, project_id, *, source, selected_plan_revision_id=None",
    "run_once": "self, project_id, *, resume=False",
    "observe": "self, project_id",
    "pause": "self, project_id, *, reason",
    "cancel": "self, project_id, *, reason",
    "status": "self, project_id",
    "replan": "self, project_id, *, rationale",
    "final_report": "self, project_id, *, goal_verdict_id=None",
    "attempt_detail": "self, attempt_id",
    "model_binding_status": "self, project_id",
}
SURFACE = {
    "format": "aik-upstream-surface-v1",
    "revision": REVISION,
    "classes": [{"name": "EngineApplication",
                 "methods": [{"name": name, "signature": sig} for name, sig in SIGNATURES.items()]}],
}


class FakeVm:
    """Synthetic VM: owns DAG, readiness, independent validation and GoalVerdict."""

    def __init__(self, *, failing_attempts=(), replan_allowed=True, stuck=False, run_once_faults=None,
                 final_evidence=("evidence-goal-test-1",), blocked_reason=None, verdict_outcome="passed"):
        self.failing_attempts = set(failing_attempts)
        self.replan_allowed = replan_allowed
        self.stuck = stuck
        self.run_once_faults = dict(run_once_faults or {})
        self.final_evidence = tuple(final_evidence)
        self.blocked_reason = blocked_reason
        self.verdict_outcome = verdict_outcome
        self.goal_state = "draft"
        self.tasks: dict = {}
        self.history = 0
        self.plan_revision = None
        self.goal_verdict_id = None
        self.bindings: dict = {}
        self.bound_attempt = None
        self.calls: list = []
        self.run_counts = Counter()

    def _log(self, name, **details):
        self.calls.append((name, details))

    def names(self):
        return [name for name, _ in self.calls]

    def handshake(self):
        return {"surface_revision": REVISION, "engine_schema_revision": 4, "methods": dict(SIGNATURES),
                "extensions": ["aik-task-execution-binding-v1"]}

    @staticmethod
    def _new_task(deps):
        return {"state": "pending", "deps": tuple(deps), "attempt": 0, "attempt_id": None, "inputs": (),
                "output": None, "failure_id": None, "evidence": ()}

    def _make_ready(self, task_id):
        task = self.tasks[task_id]
        task["attempt"] += 1
        task["attempt_id"] = f"attempt-{task_id}-{task['attempt']}"
        task["state"] = "ready"

    def _advance(self):
        if self.goal_state != "active":
            return
        for task_id, task in self.tasks.items():
            if task["state"] == "pending" and all(self.tasks[d]["state"] == "done" for d in task["deps"]):
                task["inputs"] = tuple(self.tasks[d]["output"] for d in task["deps"])
                self._make_ready(task_id)
        if "goal-test" not in self.tasks and self.tasks and all(t["state"] == "done" for t in self.tasks.values()):
            deps = tuple(self.tasks)
            self.tasks["goal-test"] = self._new_task(deps)
            self.tasks["goal-test"]["inputs"] = tuple(self.tasks[d]["output"] for d in deps)
            self._make_ready("goal-test")
        goal_test = self.tasks.get("goal-test")
        if goal_test is not None and goal_test["state"] == "done":
            self.goal_state = "goal_verdict_ready"
            self.goal_verdict_id = "goal-verdict-1"

    def _payload(self, project_id):
        return {
            "project_id": project_id, "goal_state": self.goal_state, "plan_revision_id": self.plan_revision,
            "history_cursor": self.history, "goal_verdict_id": self.goal_verdict_id,
            "blocked_reason": self.blocked_reason,
            "tasks": [{"task_id": task_id, "state": t["state"], "attempt_id": t["attempt_id"],
                       "inputs": list(t["inputs"]), "evidence": list(t["evidence"]), "failure_id": t["failure_id"],
                       "replan_allowed": t["failure_id"] is not None and self.replan_allowed}
                      for task_id, t in self.tasks.items()],
        }

    def _task_of(self, attempt):
        for task_id, task in self.tasks.items():
            if task["attempt_id"] == attempt:
                return task_id
        raise LookupError("unknown attempt")

    # ---- public façade ----
    def prepare(self, project_id, *, source_request, candidate_count=None):
        self._log("prepare", source_request=source_request)
        self.plan_revision = "plan-1"
        self.goal_state = "awaiting_authorization"
        for task_id, deps in (("task-a", ()), ("task-b", ()), ("task-c", ("task-a", "task-b"))):
            self.tasks[task_id] = self._new_task(deps)
        self.history += 1
        return {"project_id": project_id, "goal_id": "goal-1", "plan_revision_id": "plan-1"}

    def revise(self, project_id, *, source_request, candidate_count=None):
        self._log("revise")
        return {"project_id": project_id}

    def authorize(self, project_id, *, source, selected_plan_revision_id=None):
        self._log("authorize", source=source)
        self.goal_state = "blocked" if self.blocked_reason else "active"
        self._advance()
        self.history += 1
        return {"project_id": project_id, "authorization_id": "authorization-1",
                "plan_revision_id": self.plan_revision}

    def run_once(self, project_id, *, resume=False):
        attempt = self.bound_attempt
        self._log("run_once", attempt=attempt)
        self.run_counts[attempt] += 1
        task_id = self._task_of(attempt)
        fault = self.run_once_faults.get(attempt)
        if fault == "before":
            raise RuntimeError("synthetic transport loss before start")
        self.tasks[task_id]["state"] = "running"
        self.history += 1
        if fault == "after":
            raise RuntimeError("synthetic transport loss after start")
        return {"project_id": project_id, "task_id": task_id, "attempt_id": attempt,
                "job": {"status": "running", "job_id": f"job-{attempt}"}}

    def observe(self, project_id):
        self._log("observe")
        if not self.stuck:
            for task_id, task in self.tasks.items():
                if task["state"] == "running":
                    if task["attempt_id"] in self.failing_attempts:
                        task["state"] = "validation_failed"
                        task["failure_id"] = f"failure-{task['attempt_id']}"
                    else:
                        task["state"] = "done"
                        task["output"] = f"artifact-{task_id}-{task['attempt']}"
                        task["evidence"] = (f"evidence-{task['attempt_id']}",)
                    self.history += 1
            self._advance()
        return self._payload(project_id)

    def pause(self, project_id, *, reason):
        self._log("pause")
        return {"project_id": project_id}

    def cancel(self, project_id, *, reason):
        self._log("cancel")
        return {"project_id": project_id}

    def status(self, project_id):
        self._log("status")
        return self._payload(project_id)

    def replan(self, project_id, *, rationale):
        self._log("replan", rationale=rationale)
        for task_id, task in self.tasks.items():
            if task["state"] == "validation_failed" and self.replan_allowed:
                task["failure_id"] = None
                self._make_ready(task_id)
        self.plan_revision = "plan-2"
        self.history += 1
        return {"project_id": project_id, "plan_revision_id": "plan-2"}

    def final_report(self, project_id, *, goal_verdict_id=None):
        self._log("final_report", goal_verdict_id=goal_verdict_id)
        return {"project_id": project_id,
                "goal_verdict": {"verdict_id": self.goal_verdict_id, "outcome": self.verdict_outcome},
                "evidence": list(self.final_evidence),
                "deliverables": [self.tasks["task-c"]["output"]]}

    def attempt_detail(self, attempt_id):
        return {"attempt_id": attempt_id, "task_id": self._task_of(attempt_id), "project_id": PROJECT,
                "goal_revision_id": "goal-rev-1", "plan_revision_id": "plan-1",
                "execution_spec_digest": DIGEST}

    def model_binding_status(self, project_id):
        return {"project_id": project_id}

    def bind_execution(self, binding):
        digest = TaskExecutionBinding.from_mapping(binding).digest()
        self.bindings[binding["vm_attempt_id"]] = digest
        self.bound_attempt = binding["vm_attempt_id"]
        return {"binding_digest": digest}

    def read_execution_binding(self, vm_attempt_id):
        digest = self.bindings.get(vm_attempt_id)
        return None if digest is None else {"binding_digest": digest}


def make_run_request(project_id, task, request_id):
    attempt = task.attempt_id
    binding = TaskExecutionBinding(
        vm_instance_id="vm-instance-1", vm_project_id=project_id, vm_goal_revision_id="goal-rev-1",
        vm_plan_revision_id="plan-1", vm_task_id=task.task_id, vm_attempt_id=attempt,
        vm_execution_spec_digest=DIGEST, vm_runtime_intent_id=f"intent-{attempt}",
        vm_provider_call_id=f"call-{attempt}", cluster_id="cluster-1", route_id="route-1",
        route_revision="route-rev-1", connection_id="connection-1", connection_revision="connection-rev-1",
        provider="synthetic", auth_kind=AUTH_KIND, provider_account_fingerprint="account-fp-1",
        pool_id="pool-1", placement_id=f"placement-{attempt}", lease_id=f"lease-{attempt}",
        lease_fencing_token=1, support_matrix_digest=DIGEST, input_manifest_digest=DIGEST,
        workspace_root_ref="workspace://ws-1/root",
    )
    placement = Placement(f"placement-{attempt}", "pool-1", "profile-1", "vm-instance-1", project_id,
                          task.task_id, attempt, state="assigned")
    lease = Lease(f"lease-{attempt}", f"placement-{attempt}", "pool-1", "worker-1", 1,
                  "2099-01-01T00:00:00Z", state="active")
    pool = Pool("pool-1", "synthetic", "connection-1", enabled=True)
    return RunOnceRequest(request_id, binding, placement, lease, pool)


class FakeDelegation:
    def __init__(self):
        self.calls = []
        self.released = []

    def prepare_run(self, project_id, task, request_id):
        self.calls.append((task.task_id, task.attempt_id, task.inputs))
        return make_run_request(project_id, task, request_id)

    def release(self, request):
        self.released.append(request.request_id)


class MismatchDelegation(FakeDelegation):
    """Returns a reserved request bound to a different attempt than VM reported."""

    def prepare_run(self, project_id, task, request_id):
        self.calls.append((task.task_id, task.attempt_id, task.inputs))
        return make_run_request(project_id, replace(task, attempt_id="attempt-other-1"), request_id)


class FailingDelegation(FakeDelegation):
    def prepare_run(self, project_id, task, request_id):
        self.calls.append((task.task_id, task.attempt_id, task.inputs))
        raise RuntimeError("synthetic delegation failure")


class NoCapacityDelegation(FakeDelegation):
    def prepare_run(self, project_id, task, request_id):
        self.calls.append((task.task_id, task.attempt_id, task.inputs))
        return None


class FakeAuthority:
    def __init__(self, deny=None, unknown_once=(), fail=()):
        self.calls = []
        self.deny = dict(deny or {})
        self.unknown = set(unknown_once)
        self.fail = set(fail)

    def check(self, command, *, project_id, request_id, task_id=None):
        command = Command(command)
        self.calls.append((command, task_id))
        if command in self.fail:
            raise RuntimeError("synthetic authority infrastructure failure")
        if command in self.deny:
            raise AuthorityDenied(self.deny[command], subject="tool_id")
        if command in self.unknown:
            self.unknown.discard(command)
            raise EffectOutcomeUnknown(AuthorityCode.INTENT_UNKNOWN, subject="record_intent")


class FakeWake:
    def __init__(self):
        self.polls = 0

    def poll(self):
        self.polls += 1


class FailingWake:
    def __init__(self):
        self.polls = 0

    def poll(self):
        self.polls += 1
        raise RuntimeError("synthetic inbox wake failure")


class StepClock:
    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        value = self.now
        self.now += timedelta(minutes=1)
        return value


class SyntheticBridgeError(VmBridgeError):
    """A classified bridge failure with explicit outcome; never raw VM text."""

    outcome = None
    code = None
    vm_code = None

    def __init__(self, outcome, code="VM_REJECTED", vm_code=None):
        Exception.__init__(self, "synthetic bridge failure")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "code", SimpleNamespace(value=code))
        object.__setattr__(self, "vm_code", vm_code)


class FaultyBridge:
    """Delegates to a real VmBridge, raising queued faults for selected commands."""

    def __init__(self, inner, faults):
        self._inner = inner
        self._faults = {name: list(errors) for name, errors in faults.items()}

    def __getattr__(self, name):
        method = getattr(self._inner, name)
        queue = self._faults.get(name)
        if queue is None:
            return method

        def wrapped(request):
            if queue:
                raise queue.pop(0)
            return method(request)
        return wrapped


class StubBridge:
    """Minimal bridge with a fixed VM status payload (no VmBridge involved)."""

    def __init__(self, payload, replan_error):
        self.payload = payload
        self.replan_error = replan_error
        self.calls = []

    def _receipt(self, name):
        self.calls.append(name)
        return SimpleNamespace(payload=self.payload)

    def prepare(self, request):
        return self._receipt("prepare")

    def revise(self, request):
        return self._receipt("revise")

    def observe(self, request):
        return self._receipt("observe")

    def pause(self, request):
        return self._receipt("pause")

    def cancel(self, request):
        return self._receipt("cancel")

    def final_report(self, request):
        return self._receipt("final_report")

    def status(self, request):
        return self._receipt("status")

    def replan(self, request):
        self.calls.append("replan")
        raise self.replan_error

    def run_once(self, request):
        self.calls.append("run_once")
        return SimpleNamespace(job=None)


class ControllerLoopTests(unittest.TestCase):
    def build(self, vm, *, authority=None, clock=None, faults=None, delegation=None, wake=None, **policy):
        self.bridge = VmBridge(vm, SURFACE, now=lambda: NOW)
        bridge = self.bridge if faults is None else FaultyBridge(self.bridge, faults)
        self.delegation = delegation or FakeDelegation()
        self.authority = authority or FakeAuthority()
        self.wake = wake or FakeWake()
        self.user_requests = 0
        controller = Controller(bridge, project_id=PROJECT, delegation=self.delegation,
                                authority=self.authority, wake=self.wake, policy=RunPolicy(**policy),
                                clock=clock, sleep=lambda seconds: None, run_id="run-test")
        return controller

    def user_authorizes(self):
        # The user's approval path (GUI -> VM); the controller never authorizes.
        self.user_requests += 1
        self.bridge.authorize(AuthorizeRequest(f"user-authorize-{self.user_requests}", PROJECT, "user"))

    def started(self, vm, **kwargs):
        controller = self.build(vm, **kwargs)
        controller.start_goal("Combine synthetic inputs A and B into report C.")
        self.user_authorizes()
        return controller

    def test_goal_to_report_with_ab_inputs_rework_goal_test_and_handoff(self) -> None:
        vm = FakeVm(failing_attempts={"attempt-task-c-1"})
        controller = self.build(vm)
        controller.start_goal("Combine synthetic inputs A and B into report C.")
        first = controller.run()
        self.assertEqual(first.outcome, "awaiting_authorization")
        self.assertEqual(first.handoff, "authorize")
        self.assertFalse(first.goal_complete)
        self.assertNotIn("authorize", vm.names())

        controller.note_model_reported()
        self.user_authorizes()
        report = controller.run()

        self.assertEqual(report.outcome, "completed")
        self.assertTrue(report.goal_complete)
        self.assertEqual(report.handoff, "review_result")
        self.assertEqual(report.verdict_outcome, "passed")
        self.assertEqual(report.goal_verdict_id, "goal-verdict-1")
        self.assertEqual(report.evidence_refs, ("evidence-goal-test-1",))
        self.assertEqual(report.deliverable_refs, ("artifact-task-c-2",))
        self.assertTrue(report.model_reported)

        delegated = self.delegation.calls
        self.assertEqual([call[0] for call in delegated], ["task-a", "task-b", "task-c", "task-c", "goal-test"])
        self.assertEqual(delegated[2], ("task-c", "attempt-task-c-1", ("artifact-task-a-1", "artifact-task-b-1")))
        self.assertEqual(delegated[3][1], "attempt-task-c-2")
        self.assertEqual(delegated[4][2], ("artifact-task-a-1", "artifact-task-b-1", "artifact-task-c-2"))

        replans = [details for name, details in vm.calls if name == "replan"]
        self.assertEqual(len(replans), 1)
        self.assertIn("failure-attempt-task-c-1", replans[0]["rationale"])
        finals = [details for name, details in vm.calls if name == "final_report"]
        self.assertEqual(finals, [{"goal_verdict_id": "goal-verdict-1"}])
        self.assertEqual(vm.names().count("authorize"), 1)
        self.assertEqual(
            self.authority.calls,
            [(Command.PREPARE, None), (Command.RUN_ONCE, "task-a"), (Command.RUN_ONCE, "task-b"),
             (Command.RUN_ONCE, "task-c"), (Command.REPLAN, "task-c"), (Command.RUN_ONCE, "task-c"),
             (Command.RUN_ONCE, "goal-test")],
        )
        self.assertEqual(self.wake.polls, first.ticks + report.ticks)
        self.assertTrue(all(count == 1 for count in vm.run_counts.values()))

    def test_model_reported_without_vm_verdict_is_not_completion(self) -> None:
        vm = FakeVm(stuck=True)
        controller = self.started(vm, max_ticks=6, max_no_progress=100)
        controller.note_model_reported()
        report = controller.run()
        self.assertEqual(report.outcome, "max_ticks")
        self.assertFalse(report.goal_complete)
        self.assertTrue(report.model_reported)
        self.assertNotIn("final_report", vm.names())

    def test_final_report_without_evidence_is_unverified(self) -> None:
        controller = self.started(FakeVm(final_evidence=()))
        report = controller.run()
        self.assertEqual(report.outcome, "report_unverified")
        self.assertEqual(report.reason, "EVIDENCE_MISSING")
        self.assertFalse(report.goal_complete)
        self.assertEqual(report.handoff, "inspect_report")

    def test_failed_goal_verdict_is_reported_not_completed(self) -> None:
        report = self.started(FakeVm(verdict_outcome="failed")).run()
        self.assertEqual(report.outcome, "goal_failed")
        self.assertEqual(report.verdict_outcome, "failed")
        self.assertFalse(report.goal_complete)

    def test_validation_failure_without_vm_rework_stops_for_review(self) -> None:
        vm = FakeVm(failing_attempts={"attempt-task-c-1"}, replan_allowed=False)
        report = self.started(vm).run()
        self.assertEqual(report.outcome, "validation_failed")
        self.assertEqual(report.handoff, "review_failure")
        self.assertNotIn("replan", vm.names())

    def test_vm_blocked_goal_hands_off_with_vm_reason(self) -> None:
        vm = FakeVm(blocked_reason="WORKER_POOL_EMPTY")
        report = self.started(vm).run()
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(report.reason, "WORKER_POOL_EMPTY")
        self.assertEqual(report.handoff, "resolve_blocker")
        self.assertEqual(vm.run_counts, Counter())

    def test_unknown_run_once_is_observed_not_relaunched_when_vm_shows_it_started(self) -> None:
        vm = FakeVm(run_once_faults={"attempt-task-a-1": "after"})
        report = self.started(vm).run()
        self.assertEqual(report.outcome, "completed")
        self.assertEqual(vm.run_counts["attempt-task-a-1"], 1)
        self.assertIn("OUTCOME_UNKNOWN", [record.result for record in report.actions if record.action == "run_once"])

    def test_unknown_run_once_without_vm_evidence_stops_for_reconciliation(self) -> None:
        vm = FakeVm(run_once_faults={"attempt-task-a-1": "before"})
        report = self.started(vm, max_no_progress=3).run()
        self.assertEqual(report.outcome, "reconciliation_required")
        self.assertEqual(report.unresolved_task_ids, ("task-a",))
        self.assertEqual(sum(vm.run_counts.values()), 1)
        self.assertEqual(len(self.delegation.calls), 1)

    def test_authority_escalation_denial_is_not_retried_or_sent(self) -> None:
        authority = FakeAuthority(deny={Command.RUN_ONCE: AuthorityCode.TOOL_NOT_IN_AUTHORIZATION})
        vm = FakeVm()
        report = self.started(vm, authority=authority).run()
        self.assertEqual(report.outcome, "escalation_required")
        self.assertEqual(report.handoff, "request_authorization")
        self.assertEqual(vm.run_counts, Counter())
        self.assertEqual([c for c in authority.calls if c[0] is Command.RUN_ONCE], [(Command.RUN_ONCE, "task-a")])
        self.assertEqual(len(self.delegation.released), 1)

    def test_plain_authority_denial_stops(self) -> None:
        authority = FakeAuthority(deny={Command.RUN_ONCE: AuthorityCode.LEASE_INACTIVE})
        report = self.started(FakeVm(), authority=authority).run()
        self.assertEqual(report.outcome, "authority_denied")
        self.assertEqual(report.reason, "LEASE_INACTIVE")

    def test_authority_outcome_unknown_is_not_relaunched(self) -> None:
        authority = FakeAuthority(unknown_once={Command.RUN_ONCE})
        vm = FakeVm()
        report = self.started(vm, authority=authority, max_no_progress=3).run()
        self.assertEqual(report.outcome, "reconciliation_required")
        self.assertEqual(vm.run_counts, Counter())
        self.assertEqual(len([c for c in authority.calls if c[0] is Command.RUN_ONCE]), 1)

    def test_no_progress_limit_returns_control(self) -> None:
        vm = FakeVm(stuck=True)
        report = self.started(vm, max_no_progress=3).run()
        self.assertEqual(report.outcome, "no_progress")
        self.assertEqual(report.unresolved_task_ids, ())
        self.assertEqual(sum(vm.run_counts.values()), 2)
        self.assertFalse(report.goal_complete)

    def test_absolute_deadline_stops_the_loop(self) -> None:
        clock = StepClock()
        controller = self.started(FakeVm(stuck=True), clock=clock, max_no_progress=100, poll_interval_seconds=0)
        report = controller.run(deadline=NOW + timedelta(minutes=4))
        self.assertEqual(report.outcome, "deadline_exceeded")
        self.assertEqual(report.ticks, 4)

    def test_naive_clock_with_deadline_fails_closed(self) -> None:
        controller = self.started(FakeVm(), clock=lambda: datetime(2030, 1, 1))
        report = controller.run(deadline=NOW)
        self.assertEqual(report.outcome, "clock_unavailable")
        self.assertEqual(report.ticks, 0)

    def test_denied_prepare_is_not_sent(self) -> None:
        vm = FakeVm()
        controller = self.build(vm, authority=FakeAuthority(deny={Command.PREPARE: AuthorityCode.POLICY_UNKNOWN}))
        with self.assertRaises(AuthorityDenied):
            controller.start_goal("synthetic goal")
        self.assertNotIn("prepare", vm.names())

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(InvalidControllerConfig):
            RunPolicy(max_ticks=0)
        with self.assertRaises(InvalidControllerConfig):
            RunPolicy(deadline=datetime(2030, 1, 1))
        with self.assertRaises(InvalidControllerConfig):
            Controller(object(), project_id=PROJECT, delegation=FakeDelegation(), authority=FakeAuthority())

    # ---- F-1: replan denial / rejection must not consume the failure ----
    def test_denied_replan_is_not_marked_and_is_sent_after_permission_is_fixed(self) -> None:
        authority = FakeAuthority(deny={Command.REPLAN: AuthorityCode.LEASE_INACTIVE})
        vm = FakeVm(failing_attempts={"attempt-task-c-1"})
        controller = self.started(vm, authority=authority)
        first = controller.run()
        self.assertEqual(first.outcome, "authority_denied")
        self.assertEqual(first.reason, "LEASE_INACTIVE")
        self.assertNotIn("replan", vm.names())
        authority.deny.clear()
        second = controller.run()
        self.assertEqual(second.outcome, "completed")
        self.assertEqual(vm.names().count("replan"), 1)

    def test_definite_vm_rejection_of_replan_permits_a_later_replan(self) -> None:
        vm = FakeVm(failing_attempts={"attempt-task-c-1"})
        controller = self.started(vm, faults={"replan": [SyntheticBridgeError(DEFINITE, vm_code="PLAN_LOCKED")]})
        first = controller.run()
        self.assertEqual(first.outcome, "vm_rejected")
        self.assertEqual(first.vm_code, "PLAN_LOCKED")
        self.assertEqual(first.unresolved_task_ids, ())
        second = controller.run()
        self.assertEqual(second.outcome, "completed")
        self.assertEqual(vm.names().count("replan"), 1)

    def test_authority_failure_on_replan_stops_as_unavailable(self) -> None:
        vm = FakeVm(failing_attempts={"attempt-task-c-1"})
        report = self.started(vm, authority=FakeAuthority(fail={Command.REPLAN})).run()
        self.assertEqual(report.outcome, "authority_denied")
        self.assertEqual(report.reason, "AUTHORITY_UNAVAILABLE")
        self.assertNotIn("replan", vm.names())

    # ---- F-3: unclear replan blocks other effects until VM shows its state ----
    def test_unclear_replan_blocks_other_delegation_until_vm_shows_it(self) -> None:
        payload = {"goal_state": "active", "plan_revision_id": "plan-1", "history_cursor": 1, "tasks": [
            {"task_id": "task-x", "state": "validation_failed", "attempt_id": "attempt-x-1",
             "failure_id": "failure-x-1", "replan_allowed": True},
            {"task_id": "task-y", "state": "ready", "attempt_id": "attempt-y-1"},
        ]}
        bridge = StubBridge(payload, RuntimeError("synthetic transport loss"))
        controller = Controller(bridge, project_id=PROJECT, delegation=FakeDelegation(), authority=FakeAuthority(),
                                policy=RunPolicy(max_no_progress=3), sleep=lambda seconds: None, run_id="run-stub")
        report = controller.run()
        self.assertEqual(bridge.calls.count("replan"), 1)
        self.assertNotIn("run_once", bridge.calls)
        self.assertEqual(report.outcome, "reconciliation_required")
        self.assertEqual(report.unresolved_task_ids, ("task-x",))

        bridge.payload = {"goal_state": "active", "plan_revision_id": "plan-2", "history_cursor": 2, "tasks": [
            {"task_id": "task-x", "state": "ready", "attempt_id": "attempt-x-2"},
            {"task_id": "task-y", "state": "ready", "attempt_id": "attempt-y-1"},
        ]}
        controller.run()
        self.assertEqual(bridge.calls.count("replan"), 1)
        self.assertEqual(bridge.calls.count("run_once"), 1)

    # ---- F-4 ----
    def test_mismatched_delegation_request_is_released(self) -> None:
        vm = FakeVm()
        report = self.started(vm, delegation=MismatchDelegation()).run()
        self.assertEqual(report.outcome, "delegation_failed")
        self.assertEqual(report.reason, "DELEGATION_MISMATCH")
        self.assertEqual(len(self.delegation.released), 1)
        self.assertEqual(vm.run_counts, Counter())

    # ---- F-6 ----
    def test_replan_limit_is_per_run_and_a_new_run_continues(self) -> None:
        vm = FakeVm(failing_attempts={"attempt-task-c-1", "attempt-task-c-2"})
        controller = self.started(vm, max_replans=1)
        first = controller.run()
        self.assertEqual(first.outcome, "validation_failed")
        self.assertEqual(first.reason, "REPLAN_LIMIT")
        self.assertEqual(vm.names().count("replan"), 1)
        second = controller.run()
        self.assertEqual(second.outcome, "completed")
        self.assertEqual(vm.names().count("replan"), 2)
        replans = [details["rationale"] for name, details in vm.calls if name == "replan"]
        self.assertIn("failure-attempt-task-c-1", replans[0])
        self.assertIn("failure-attempt-task-c-2", replans[1])

    # ---- F-8 ----
    def test_wake_failure_is_recorded_in_the_report(self) -> None:
        wake = FailingWake()
        controller = self.started(FakeVm(), wake=wake)
        report = controller.run()
        self.assertEqual(report.outcome, "completed")
        wake_records = [record for record in report.actions if record.action == "wake"]
        self.assertEqual(len(wake_records), report.ticks)
        self.assertTrue(all(record.result == "WAKE_FAILED" for record in wake_records))
        self.assertEqual(controller.wake_failures, report.ticks)

    # ---- F-9: refusal and failure branches ----
    def test_definite_run_once_rejection_stops_with_vm_code_and_releases(self) -> None:
        vm = FakeVm()
        controller = self.started(vm, faults={"run_once": [SyntheticBridgeError(DEFINITE, vm_code="ATTEMPT_STALE")]})
        report = controller.run()
        self.assertEqual(report.outcome, "vm_rejected")
        self.assertEqual(report.vm_code, "ATTEMPT_STALE")
        self.assertEqual(report.handoff, "inspect_vm")
        self.assertEqual(len(self.delegation.released), 1)
        self.assertEqual(report.unresolved_task_ids, ())
        self.assertEqual(vm.run_counts, Counter())

    def test_delegation_failure_stops(self) -> None:
        vm = FakeVm()
        report = self.started(vm, delegation=FailingDelegation()).run()
        self.assertEqual(report.outcome, "delegation_failed")
        self.assertEqual(report.reason, "DELEGATION_FAILED")
        self.assertEqual(report.handoff, "check_capacity")
        self.assertEqual(vm.run_counts, Counter())

    def test_no_capacity_waits_without_sending(self) -> None:
        vm = FakeVm()
        report = self.started(vm, delegation=NoCapacityDelegation(), max_no_progress=3).run()
        self.assertEqual(report.outcome, "no_progress")
        self.assertIn("NO_CAPACITY", [record.result for record in report.actions if record.action == "run_once"])
        self.assertEqual(vm.run_counts, Counter())
        self.assertEqual([c for c in self.authority.calls if c[0] is Command.RUN_ONCE], [])

    def test_status_failure_observes_without_effects(self) -> None:
        vm = FakeVm()
        faults = {"status": [RuntimeError("synthetic status failure") for _ in range(3)]}
        report = self.started(vm, faults=faults, max_no_progress=3).run()
        self.assertEqual(report.outcome, "no_progress")
        statuses = [record.result for record in report.actions if record.action == "status"]
        self.assertEqual(statuses, ["STATUS_FAILED"] * 3)
        self.assertEqual(vm.run_counts, Counter())

    def test_final_report_failure_waits_then_reports(self) -> None:
        controller = self.started(FakeVm(), faults={"final_report": [RuntimeError("synthetic report failure")]})
        report = controller.run()
        self.assertEqual(report.outcome, "completed")
        finals = [record.result for record in report.actions if record.action == "final_report"]
        self.assertEqual(finals, ["REPORT_FAILED", "VM_GOAL_VERDICT"])

    def test_vm_paused_or_cancelled_stops_the_loop(self) -> None:
        for state, handoff in (("paused", "resume"), ("cancelled", "review_result")):
            with self.subTest(state=state):
                vm = FakeVm()
                controller = self.started(vm)
                vm.goal_state = state
                report = controller.run()
                self.assertEqual(report.outcome, state)
                self.assertEqual(report.handoff, handoff)
                self.assertEqual(vm.run_counts, Counter())

    def test_revise_pause_and_cancel_require_authority(self) -> None:
        cases = (
            (Command.REVISE, lambda c: c.revise_goal("Revise the synthetic goal.")),
            (Command.PAUSE, lambda c: c.pause("USER_REQUESTED")),
            (Command.CANCEL, lambda c: c.cancel("USER_REQUESTED")),
        )
        for command, call in cases:
            with self.subTest(command=command.value):
                vm = FakeVm()
                controller = self.build(vm, authority=FakeAuthority(deny={command: AuthorityCode.LEASE_INACTIVE}))
                with self.assertRaises(AuthorityDenied):
                    call(controller)
                self.assertNotIn(command.value, vm.names())
                self.assertEqual(self.authority.calls, [(command, None)])


class DecisionTests(unittest.TestCase):
    def test_pure_decisions_by_vm_state(self) -> None:
        policy = RunPolicy()
        self.assertIs(decide(None, Memory(), policy).action, Action.OBSERVE)
        unknown = VmView("active", (TaskView("t1", "unknown"), TaskView("t2", "ready", "a2")))
        self.assertEqual(decide(unknown, Memory(), policy).reason, "VM_STATE_UNKNOWN")
        ready = VmView("active", (TaskView("t1", "ready", "a1"),))
        self.assertIs(decide(ready, Memory(), policy).action, Action.RUN_ONCE)
        self.assertIs(decide(ready, Memory(unresolved=frozenset({"t1"})), policy).action, Action.OBSERVE)
        failed = VmView("active", (TaskView("t1", "validation_failed", "a1", failure_id="f1", replan_allowed=True),))
        self.assertIs(decide(failed, Memory(), policy).action, Action.REPLAN)
        self.assertEqual(decide(failed, Memory(replanned=frozenset({"f1"}), replans=1), policy).reason,
                         "REWORK_REQUESTED")
        self.assertEqual(decide(failed, Memory(replans=3), policy).outcome, "validation_failed")
        self.assertIs(decide(VmView("goal_verdict_ready", goal_verdict_id="v1"), Memory(), policy).action,
                      Action.FINAL_REPORT)
        self.assertIs(decide(VmView("goal_verdict_ready"), Memory(), policy).action, Action.OBSERVE)
        self.assertEqual(decide(VmView("awaiting_authorization"), Memory(), policy).outcome, "awaiting_authorization")

    def test_status_payload_parsing_fails_closed(self) -> None:
        self.assertEqual(VmView.from_payload({"goal_state": "mystery"}).goal_state, "unknown")
        self.assertEqual(VmView.from_payload({"goal_state": "active", "tasks": [{"task_id": "bad id"}]}).goal_state,
                         "unknown")
        view = VmView.from_payload({"goal_state": "active", "tasks": [{"task_id": "t1", "state": "odd"}]})
        self.assertEqual(view.tasks[0].state, "unknown")
        self.assertEqual(VmView.from_payload(None).goal_state, "unknown")

    def test_final_report_interpretation_requires_matching_verdict_and_evidence(self) -> None:
        good = {"goal_verdict": {"verdict_id": "v1", "outcome": "passed"}, "evidence": ["e1"]}
        self.assertEqual(interpret_final_report(good, "v1")[0], "completed")
        self.assertEqual(interpret_final_report(good, "v2")[4], "GOAL_VERDICT_MISMATCH")
        self.assertEqual(interpret_final_report({"model_said": "done"}, "v1")[0], "report_unverified")

    def test_invalid_deliverables_are_reported_unverified(self) -> None:
        payload = {"goal_verdict": {"verdict_id": "v1", "outcome": "passed"}, "evidence": ["e1"],
                   "deliverables": ["bad id/../x"]}
        outcome, verdict, evidence, deliverables, reason = interpret_final_report(payload, "v1")
        self.assertEqual(outcome, "report_unverified")
        self.assertEqual(reason, "DELIVERABLES_INVALID")
        self.assertIsNone(verdict)
        self.assertEqual(deliverables, ())

    def test_unrecognized_verdict_outcome_is_unverified(self) -> None:
        payload = {"goal_verdict": {"verdict_id": "v1", "outcome": "maybe"}, "evidence": ["e1"]}
        result = interpret_final_report(payload, "v1")
        self.assertEqual(result[0], "report_unverified")
        self.assertEqual(result[4], "GOAL_VERDICT_UNRECOGNIZED")


class FakeGateway:
    def __init__(self, policy, result="authorized"):
        self._policy = policy
        self.result = result
        self.calls = []

    def policy(self, workspace_id):
        return (self._policy, 1) if workspace_id == self._policy.workspace_id else None

    def request(self, session, call):
        self.calls.append((session, call))
        return self.result


class GatewayAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ToolPolicy("ws-1", (ToolDeclaration("vm.run_once", EffectKind.NON_IDEMPOTENT),
                                          ToolDeclaration("vm.pause", EffectKind.IDEMPOTENT)),
                                 director_tools=("vm.run_once", "vm.pause"))
        self.gateway = FakeGateway(self.policy)
        self.session = object()
        self.adapter = GatewayCommandAuthority(
            self.gateway, self.session, workspace_id="ws-1", input_revision="plan-1", authorization_revision=1,
            tool_ids={Command.RUN_ONCE: "vm.run_once", Command.PAUSE: "vm.pause", Command.CANCEL: "vm.cancel"},
        )

    def adapter_for(self, gateway, **kwargs):
        return GatewayCommandAuthority(gateway, self.session, workspace_id=kwargs.pop("workspace_id", "ws-1"),
                                       input_revision="plan-1", authorization_revision=1,
                                       tool_ids={Command.RUN_ONCE: "vm.run_once"}, **kwargs)

    def test_command_maps_to_scoped_tool_call(self) -> None:
        self.adapter.check(Command.RUN_ONCE, project_id=PROJECT, request_id="run-test-000001", task_id="task-a")
        session, call = self.gateway.calls[0]
        self.assertIs(session, self.session)
        self.assertEqual(call.tool_id, "vm.run_once")
        self.assertEqual(call.effect_id, "run-test-000001")
        self.assertEqual(call.target, "vm://project-1/task-a")
        self.assertEqual(call.input_revision, "plan-1")

    def test_unmapped_or_undeclared_commands_are_denied(self) -> None:
        for command in (Command.REPLAN, Command.CANCEL):
            with self.subTest(command=command.value):
                with self.assertRaises(AuthorityDenied) as raised:
                    self.adapter.check(command, project_id=PROJECT, request_id="run-test-000002")
                self.assertEqual(raised.exception.code, AuthorityCode.TOOL_NOT_DECLARED)
        self.assertEqual(self.gateway.calls, [])

    def test_authorize_cannot_be_mapped(self) -> None:
        with self.assertRaises(InvalidControllerConfig):
            GatewayCommandAuthority(self.gateway, self.session, workspace_id="ws-1", input_revision="plan-1",
                                    authorization_revision=1, tool_ids={Command.AUTHORIZE: "vm.approve_plan"})

    # ---- F-2: a non-authorizing gateway return value is a denial ----
    def test_gateway_denial_return_values_are_not_permission(self) -> None:
        results = (None, False, "denied", SimpleNamespace(allowed=False), SimpleNamespace(outcome="denied"),
                   SimpleNamespace(denied=True))
        for result in results:
            with self.subTest(result=repr(result)):
                adapter = self.adapter_for(FakeGateway(self.policy, result))
                with self.assertRaises(AuthorityDenied) as raised:
                    adapter.check(Command.RUN_ONCE, project_id=PROJECT, request_id="run-test-000010",
                                  task_id="task-a")
                self.assertEqual(raised.exception.code, AuthorityCode.INVALID_REQUEST)

    def test_decision_check_pins_the_gateway_contract(self) -> None:
        rejecting = self.adapter_for(FakeGateway(self.policy), decision_check=lambda decision: False)
        with self.assertRaises(AuthorityDenied):
            rejecting.check(Command.RUN_ONCE, project_id=PROJECT, request_id="run-test-000011", task_id="task-a")
        accepting = self.adapter_for(FakeGateway(self.policy),
                                     decision_check=lambda decision: decision == "authorized")
        accepting.check(Command.RUN_ONCE, project_id=PROJECT, request_id="run-test-000012", task_id="task-a")
        allowed = self.adapter_for(FakeGateway(self.policy, SimpleNamespace(allowed=True)))
        allowed.check(Command.RUN_ONCE, project_id=PROJECT, request_id="run-test-000013", task_id="task-a")

    # ---- F-9: policy unknown and read-only tools ----
    def test_unknown_workspace_policy_is_denied(self) -> None:
        gateway = FakeGateway(self.policy)
        adapter = self.adapter_for(gateway, workspace_id="ws-other")
        with self.assertRaises(AuthorityDenied) as raised:
            adapter.check(Command.RUN_ONCE, project_id=PROJECT, request_id="run-test-000020", task_id="task-a")
        self.assertEqual(raised.exception.code, AuthorityCode.POLICY_UNKNOWN)
        self.assertEqual(gateway.calls, [])

    def test_read_only_tool_has_no_effect_id(self) -> None:
        policy = ToolPolicy("ws-1", (ToolDeclaration("vm.pause", EffectKind.READ_ONLY),),
                            director_tools=("vm.pause",))
        gateway = FakeGateway(policy)
        adapter = GatewayCommandAuthority(gateway, self.session, workspace_id="ws-1", input_revision="plan-1",
                                          authorization_revision=1, tool_ids={Command.PAUSE: "vm.pause"})
        adapter.check(Command.PAUSE, project_id=PROJECT, request_id="run-test-000021")
        _, call = gateway.calls[0]
        self.assertIsNone(call.effect_id)
        self.assertEqual(call.target, "vm://project-1")


if __name__ == "__main__":
    unittest.main()
