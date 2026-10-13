"""AIK-042 validation bridge with synthetic data only.

DevTools runs real child processes on a temporary workspace through a fake
authority (same fake as the AIK-085 tests). VM responses are real
aik_vm_bridge BridgeReceipt objects carrying synthetic payloads, read through
the real governance projection; one test patches the projection only to make
it fail. No provider, credential, network, VM or AGS installation is used. Passing on Linux is development verification, not Windows support.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    STATUS_AUTHORIZED, AuthorityCode, AuthorityDenied, EffectKind, EffectOutcomeUnknown, EffectReceipt,
    ToolCallAuthorization, ToolSession, canonical_arguments,
)
from aik_authority.models import target_covers  # noqa: E402
from aik_devtools import (  # noqa: E402
    DevToolPolicy, DevTools, Invocation, VerificationCommand, files_scope_target, tool_declarations,
    verification_scope_target,
)
from aik_provider.contracts import TERMINAL, JobState  # noqa: E402
from aik_runtime import UnsafeStoragePath  # noqa: E402
from aik_validation import (  # noqa: E402
    CheckJournal, CheckRecord, CompletionView, IndependentChecker, ValidationError, build_completion_view,
)
from aik_validation import completion as completion_module  # noqa: E402
from aik_vm_bridge.bridge import BridgeReceipt, VmRefs  # noqa: E402
from aik_vm_bridge.governance import GatePhase, GateState  # noqa: E402
from aik_vm_bridge.surface import Command  # noqa: E402
from aik_workspace import ProjectInfo, WorkspaceManager, WorkspaceRegistry, sha256_digest  # noqa: E402


WORKSPACE = "ws-dev"
INPUT_REVISION = "plan-rev-1"
ATTEMPT = "vm-attempt-1"
START = datetime(2030, 1, 1, tzinfo=timezone.utc)
PYTHON = os.path.abspath(sys.executable)
ORACLE = "hello 세계\n".encode("utf-8")
UNIT = (
    "import os, pathlib, sys\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
    "    handle.write('run\\n')\n"
    "print(os.getcwd())\n"
    "app = pathlib.Path('src/app.txt')\n"
    "actual = app.read_bytes() if app.exists() else b''\n"
    "sys.exit(0 if actual == pathlib.Path('tests/expected.txt').read_bytes() else 1)\n"
)
SUCCEEDED = JobState.__members__.get("SUCCEEDED") or next(state for state in JobState if state in TERMINAL)


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


PROJECT = "vm-project-1"


def receipt(command, *, attempt=ATTEMPT, project=PROJECT, **payload):
    """A real aik_vm_bridge BridgeReceipt with a synthetic VM payload."""
    body = {"project_id": project, **payload}
    if attempt is not None:
        body["attempt_id"] = attempt
    refs = VmRefs(project_id=project, task_id="vm-task-1" if attempt is not None else None, attempt_id=attempt)
    return BridgeReceipt(command=command, request_id="request-1", project_id=project, refs=refs,
                         payload=MappingProxyType(body))


def run_once(**payload):
    return receipt(Command.RUN_ONCE, **payload)


def attempt_detail(**payload):
    return receipt(Command.ATTEMPT_DETAIL, **payload)


def final_report(**payload):
    return receipt(Command.FINAL_REPORT, attempt=None, **payload)


def record(verdict="PASS", attempt=ATTEMPT, tool_call_id="call-synthetic"):
    passed, oracle_ok = {"PASS": (True, True), "FAIL": (False, True), "ORACLE_CHANGED": (True, False)}[verdict]
    return CheckRecord(
        check_id="check-" + tool_call_id, vm_attempt_id=attempt, command_id="unit",
        definition_digest="sha256:" + "1" * 64, passed=passed, oracle_ok=oracle_ok,
        exit_code=0 if passed else 1, output_digest="sha256:" + "2" * 64, reviewer_role="reviewer",
        reviewer_session_id="session-reviewer-1", worker_session_id="session-worker-1",
        tool_call_id=tool_call_id, recorded_at="2030-01-01T00:00:00Z",
    )



class CompletionViewTests(unittest.TestCase):
    def setUp(self):
        self.observation = SimpleNamespace(vm_attempt_id=ATTEMPT, job_state=SUCCEEDED)
        self.dispatch = SimpleNamespace(identity=SimpleNamespace(vm_attempt_id=ATTEMPT), state="sent_ack")

    def build(self, **inputs):
        return build_completion_view(ATTEMPT, **inputs)

    def test_provider_success_ack_claim_and_check_pass_never_record_a_verdict(self):
        view = self.build(observation=self.observation, dispatch_record=self.dispatch,
                          worker_claim="done", check=record("PASS"))
        self.assertIsNotNone(view.provider_state)
        self.assertTrue(view.dispatch_ack)
        self.assertEqual(view.worker_claim, "done")
        self.assertEqual(view.check_verdict, "PASS")
        self.assertIsNone(view.goal_verdict_id)
        self.assertEqual(view.completion_authority, "vm")
        self.assertFalse(view.vm_verdict_recorded)
        recorded = self.build(vm_project_id=PROJECT, observation=self.observation, dispatch_record=self.dispatch,
                              worker_claim="done", final_report_receipt=final_report(goal_verdict_id="goal-verdict-1"))
        self.assertTrue(recorded.vm_verdict_recorded)
        self.assertEqual(recorded.goal_verdict_id, "goal-verdict-1")

    def test_insufficient_evidence_audit_failure_and_pass_are_distinct_fields(self):
        missing = self.build(observation=self.observation, worker_claim="done",
                             attempt_receipt=attempt_detail(validation_result_id="vm-validation-1"))
        self.assertIsNone(missing.independent_check)
        self.assertIsNone(missing.check_verdict)
        self.assertEqual(missing.vm_validation_result_id, "vm-validation-1")
        self.assertIsNone(missing.gate)
        self.assertFalse(missing.vm_verdict_recorded)

        audit = self.build(check=record("PASS"), run_once_receipt=run_once(
            action="blocked", failure_class="tests_failed", suggested_repair_action="fix_tests",
            blocker_code="GOVERNANCE_GATE_FAILED", detail="governance gate: GOVERNANCE_TIMEOUT: synthetic"))
        self.assertEqual(audit.gate.state, GateState.FAILED)
        self.assertEqual(audit.gate.phase, GatePhase.BEFORE_COMPLETION)
        self.assertEqual(audit.check_verdict, "PASS")
        self.assertEqual(audit.rework_reasons,
                         ("tests_failed", "fix_tests", "GOVERNANCE_GATE_FAILED", "GOVERNANCE_TIMEOUT"))
        self.assertFalse(audit.vm_verdict_recorded)

        passing = self.build(check=record("PASS"), run_once_receipt=run_once(action="dispatched"),
                             attempt_receipt=attempt_detail(validation_result_id="vm-validation-2"))
        self.assertIsNone(passing.gate)
        self.assertEqual(passing.rework_reasons, ())
        self.assertEqual(passing.vm_validation_result_id, "vm-validation-2")
        self.assertFalse(passing.vm_verdict_recorded)
        self.assertEqual(passing.provenance["worker_claim"], "model_reported")
        self.assertEqual(passing.provenance["provider_state"], "provider_observed")
        self.assertEqual(passing.provenance["goal_verdict_id"], "vm")
        self.assertEqual(passing.provenance["independent_check"], "local_derived")

    def test_attempt_and_project_mismatch_and_conflicts_are_rejected(self):
        other = "vm-attempt-other"
        cases = (
            dict(observation=SimpleNamespace(vm_attempt_id=other, job_state=SUCCEEDED)),
            dict(dispatch_record=SimpleNamespace(identity=SimpleNamespace(vm_attempt_id=other), state="started")),
            dict(check=record("PASS", attempt=other)),
            dict(run_once_receipt=run_once(attempt=other)),
            dict(attempt_receipt=attempt_detail(attempt=other, validation_result_id="v-1")),
        )
        for inputs in cases:
            with self.subTest(input=next(iter(inputs))):
                with self.assertRaises(ValidationError) as rejected:
                    self.build(**inputs)
                self.assertEqual(rejected.exception.code, "ATTEMPT_MISMATCH")
        # The final report is project-level: it needs the project and must name the same one.
        with self.assertRaises(ValidationError) as rejected:
            self.build(final_report_receipt=final_report(goal_verdict_id="goal-verdict-1"))
        self.assertEqual(rejected.exception.code, "PROJECT_REQUIRED")
        with self.assertRaises(ValidationError) as rejected:
            self.build(vm_project_id="vm-project-2", final_report_receipt=final_report(goal_verdict_id="g-1"))
        self.assertEqual(rejected.exception.code, "PROJECT_MISMATCH")
        with self.assertRaises(ValidationError) as rejected:
            self.build(vm_project_id="vm-project-2", run_once_receipt=run_once())
        self.assertEqual(rejected.exception.code, "PROJECT_MISMATCH")
        # Only real BridgeReceipts of the expected command are read.
        with self.assertRaises(ValidationError) as rejected:
            self.build(run_once_receipt=SimpleNamespace(payload={"attempt_id": ATTEMPT}))
        self.assertEqual(rejected.exception.code, "UNREADABLE_RUN_ONCE_RECEIPT")
        with self.assertRaises(ValidationError) as rejected:
            self.build(attempt_receipt=run_once())
        self.assertEqual(rejected.exception.code, "UNREADABLE_ATTEMPT_RECEIPT")
        with self.assertRaises(ValidationError) as rejected:
            self.build(run_once_receipt=run_once(validation_result_id="v-1"),
                       attempt_receipt=attempt_detail(validation_result_id="v-2"))
        self.assertEqual(rejected.exception.code, "VALIDATION_RESULT_CONFLICT")
        with mock.patch.object(completion_module, "project_run_once", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(ValidationError) as rejected:
                self.build(run_once_receipt=run_once())
        self.assertEqual(rejected.exception.code, "GATE_PROJECTION_FAILED")

    def test_unacknowledged_dispatch_and_raw_view_values(self):
        for state in ("reserved", "send_unknown", "rejected", None):
            with self.subTest(state=state):
                view = self.build(dispatch_record=SimpleNamespace(
                    identity=SimpleNamespace(vm_attempt_id=ATTEMPT), state=state))
                self.assertFalse(view.dispatch_ack)
        with self.assertRaises(ValidationError):
            self.build(dispatch_record=SimpleNamespace(identity=SimpleNamespace(vm_attempt_id=ATTEMPT),
                                                       state="invented"))
        view = CompletionView(ATTEMPT, None, False, "done", None, None, None, None, ())
        self.assertFalse(view.vm_verdict_recorded)


class IndependentCheckerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aik-validation-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        folder = self.base / "작업 공간" / "프로젝트 A"
        folder.mkdir(parents=True)
        self.manager = WorkspaceManager(WorkspaceRegistry(self.base / "state" / "workspaces.json"), FakeProjects())
        self.manager.open_workspace(WORKSPACE, "개발 작업", folder)
        self.root = Path(self.manager.execution_context(WORKSPACE).canonical_root)
        (self.root / "tests").mkdir()
        (self.root / "src").mkdir()
        (self.root / "tests" / "expected.txt").write_bytes(ORACLE)
        self.counter = self.base / "run-counter.txt"
        self.authority = FakeAuthority()
        self.journal_path = self.base / "state" / "validation.sqlite3"
        self.checker = IndependentChecker(self.tools(), self.journal_path, clock=lambda: START)
        self.reviewer = ToolSession("session-reviewer-1", "reviewer", WORKSPACE)

    def policy(self):
        return DevToolPolicy(WORKSPACE, (
            VerificationCommand("unit", (PYTHON, "-c", UNIT, str(self.counter)), timeout_seconds=30,
                                max_output_bytes=4096,
                                oracle_files=(("tests/expected.txt", sha256_digest(ORACLE)),)),
        ))

    def tools(self):
        return DevTools(authority=self.authority, workspaces=self.manager, policy=self.policy())

    @staticmethod
    def inv(number):
        return Invocation(f"call-{number}", INPUT_REVISION, 1, f"effect-{number}")

    def runs(self):
        return self.counter.read_text(encoding="utf-8").count("run") if self.counter.exists() else 0

    def run_check(self, number, session=None):
        return self.checker.run(ATTEMPT, worker_session_id="session-worker-1",
                                reviewer_session=session or self.reviewer, invocation=self.inv(number),
                                command_id="unit")

    def test_self_review_or_worker_role_is_rejected_before_running(self):
        for session in (ToolSession("session-worker-1", "reviewer", WORKSPACE),
                        ToolSession("SESSION-WORKER-1", "reviewer", WORKSPACE),
                        ToolSession("session-other", "worker", WORKSPACE)):
            with self.subTest(session=session.session_id, role=session.role):
                with self.assertRaises(ValidationError) as rejected:
                    self.run_check(1, session)
                self.assertEqual(rejected.exception.code, "NOT_INDEPENDENT")
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.authority.requests, [])
        self.assertEqual(self.checker.history(ATTEMPT), ())

    def test_intended_failure_then_fixed_bytes_pass_and_failure_is_kept(self):
        first = self.run_check(1)
        self.assertEqual(first.verdict, "FAIL")
        self.assertTrue(first.oracle_ok)
        self.assertEqual(first.reviewer_role, "reviewer")
        self.assertEqual(first.worker_session_id, "session-worker-1")
        self.assertEqual(first.provenance, "local_derived")
        # The worker fixes the actual file bytes; the oracle is unchanged.
        (self.root / "src" / "app.txt").write_bytes(ORACLE)
        second = self.run_check(2)
        self.assertEqual(second.verdict, "PASS")
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(self.runs(), 2)
        self.assertEqual(self.checker.history(ATTEMPT), (first, second))
        reopened = CheckJournal(self.journal_path)
        self.assertEqual(reopened.history(ATTEMPT), (first, second))
        view = build_completion_view(ATTEMPT, worker_claim="done", check=second)
        self.assertFalse(view.vm_verdict_recorded)

    def test_changed_oracle_is_reported_even_when_the_command_exits_zero(self):
        weakened = b"weakened\n"
        (self.root / "tests" / "expected.txt").write_bytes(weakened)
        (self.root / "src" / "app.txt").write_bytes(weakened)
        result = self.run_check(1)
        self.assertFalse(result.oracle_ok)
        self.assertEqual(result.verdict, "ORACLE_CHANGED")

    def test_journal_is_append_only_and_rejects_foreign_files(self):
        stored = self.checker.journal.append(record("FAIL", tool_call_id="call-a"))
        self.assertEqual(self.checker.journal.append(replace(stored)), stored)
        with self.assertRaises(ValidationError) as rejected:
            self.checker.journal.append(record("PASS", tool_call_id="call-a"))
        self.assertEqual(rejected.exception.code, "TOOL_CALL_CONFLICT")
        with closing(sqlite3.connect(self.journal_path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE checks SET verdict='PASS'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM checks")
            connection.rollback()
        self.assertEqual(self.checker.history(ATTEMPT), (stored,))
        foreign = self.base / "state" / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(ValidationError) as rejected:
            CheckJournal(foreign)
        self.assertEqual(rejected.exception.code, "INVALID_JOURNAL")
        self.assertEqual(foreign.read_bytes(), before)
        with self.assertRaises(UnsafeStoragePath):
            CheckJournal("relative-validation.sqlite3")

    def test_forged_records_are_revalidated(self):
        forged = record("PASS", tool_call_id="call-forged")
        object.__setattr__(forged, "worker_session_id", "session-reviewer-1")
        with self.assertRaises(ValidationError) as rejected:
            self.checker.journal.append(forged)
        self.assertEqual(rejected.exception.code, "NOT_INDEPENDENT")
        self.assertEqual(self.checker.history(ATTEMPT), ())


if __name__ == "__main__":
    unittest.main()
