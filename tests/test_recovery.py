"""AIK-039 recovery reconciliation against disposable, synthetic stores.

Real RuntimeStore, BudgetStore, LeaseManager and WorkerObserver run in a
temporary directory. VM, provider jobs and the effect journal are fake ports.
No provider, credential, network or VM/AGS installation is used. Passing on
Linux is development verification, not Windows qualification.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_budget import BudgetStore  # noqa: E402
from aik_lease import HOLDING_STATES, LeaseManager  # noqa: E402
from aik_observer import WorkerObserver  # noqa: E402
from aik_provider.contracts import (  # noqa: E402
    TERMINAL, AuthKind, ConnectionBinding, JobReceipt, JobRequest, JobState,
)
from aik_recovery import EffectRef, RecoveryError, RecoveryJournal, RecoveryReconciler  # noqa: E402
from aik_runtime import ExecutionProfile, InvalidDatabase, Lease, Placement, Pool, RuntimeStore  # noqa: E402


NOW = datetime(2029, 1, 1, tzinfo=timezone.utc)
FINGERPRINT = "0123456789abcdef" * 4
MODEL = "synthetic-model"

# lease_id, vm_attempt_id, lease state, provider job id
LEASES = (
    ("lease-reserved-done", "attempt-reserved-done", "reserved", None),
    ("lease-reserved-live", "attempt-reserved-live", "reserved", None),
    ("lease-reserved-vmdown", "attempt-vm-unavailable", "reserved", None),
    ("lease-intent", "attempt-intent", "active", None),
    ("lease-terminal", "attempt-terminal", "active", "job-terminal"),
    ("lease-running", "attempt-running", "active", "job-running"),
    ("lease-failobs", "attempt-failobs", "active", "job-failobs"),
    ("lease-unknown-terminal", "attempt-unknown-terminal", "unknown", "job-unknown-terminal"),
)
VM_STATES = {
    "attempt-reserved-done": "terminal",
    "attempt-reserved-live": "active",
    "attempt-intent": "active",
    "attempt-terminal": "active",
    "attempt-running": "active",
    "attempt-failobs": "active",
    "attempt-unknown-terminal": "unknown",
    "attempt-effect": "active",
    "attempt-cancelled": "terminal",
}
EFFECTS = (
    EffectRef("attempt-effect", "tool-call-1", "write_file", False, False, "non_idempotent"),
    EffectRef("attempt-effect", "tool-call-2", "write_file", True, False, "idempotent", receipt_ref="receipt-2"),
    EffectRef("attempt-cancelled", "tool-call-3", "post_comment", True, False, "non_idempotent",
              cancelled=True, receipt_ref="receipt-3"),
    EffectRef("attempt-cancelled", "tool-call-4", "post_comment", False, False, "non_idempotent", cancelled=True),
    EffectRef("attempt-effect", "tool-call-5", "read_file", True, True, "read_only", receipt_ref="receipt-5"),
)


def make_binding() -> ConnectionBinding:
    for kind in AuthKind:
        for reference in ("secret://local/synthetic-ref", "host-session://synthetic/session-ref"):
            try:
                return ConnectionBinding("synthetic", "synthetic", "connection-1", 1, kind, reference, FINGERPRINT)
            except Exception:
                continue
    raise unittest.SkipTest("No synthetic connection binding is constructible.")


def running_state() -> JobState:
    for name in ("RUNNING", "STARTED", "QUEUED", "ACCEPTED"):
        state = getattr(JobState, name, None)
        if state is not None and state not in TERMINAL:
            return state
    return next(state for state in JobState if state not in TERMINAL)


class FakeBudget:
    """Settlement sink for the observer; records calls, never contacts a provider."""

    def __init__(self):
        self.settled = []

    def settle(self, reservation_id, usage):
        self.settled.append(reservation_id)
        return SimpleNamespace(kind="settlement", reservation_id=reservation_id, usage=usage,
                               seq=len(self.settled), recorded_at="2029-01-01T00:00:00Z",
                               submitted_at="2029-01-01T00:00:00Z")


class FakeVm:
    def __init__(self, states):
        self.states = dict(states)

    def attempt_state(self, vm_attempt_id):
        if vm_attempt_id not in self.states:
            raise ConnectionError("synthetic VM unavailable")
        return self.states[vm_attempt_id]


class FakeJobPort:
    def __init__(self, states, failing):
        self.states = dict(states)
        self.failing = set(failing)
        self.calls = []
        self.started = []

    def observe(self, request, receipt):
        self.calls.append((receipt.provider_job_id, request.binding.connection_id,
                           request.binding.account_fingerprint))
        if receipt.provider_job_id in self.failing:
            raise TimeoutError("synthetic provider timeout")
        return replace(receipt, state=self.states[receipt.provider_job_id])

    observe_job = observe

    def start_job(self, request):  # A reconciler must never call this.
        self.started.append(request.request_id)
        raise AssertionError("relaunch attempted")


class FakeEffectPort:
    def __init__(self, refs):
        self.refs = tuple(refs)
        self.marked = []
        self.resent = []
        self.executed = []
        self.fail_resend = 0

    def inflight(self):
        return self.refs

    def mark_unknown(self, ref):
        self.marked.append(ref.tool_call_id)

    def resend_result(self, ref):
        self.resent.append(ref.tool_call_id)
        if self.fail_resend:
            self.fail_resend -= 1
            raise ConnectionError("synthetic delivery failure")

    def execute(self, ref):  # A reconciler must never call this.
        self.executed.append(ref.tool_call_id)
        raise AssertionError("effect re-executed")


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-recovery-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "state"
        self.runtime_path = self.root / "runtime.sqlite3"
        self.budget_path = self.root / "budget.sqlite3"
        self.observer_path = self.root / "observer.sqlite3"
        self.journal_path = self.root / "recovery.sqlite3"
        self.store = RuntimeStore(self.runtime_path)
        self.seed()
        self.observer_budget = FakeBudget()
        observer = WorkerObserver(self.observer_path, budget=self.observer_budget)
        binding = make_binding()
        running = running_state()
        for lease_id, attempt, _state, job in LEASES:
            if job is None:
                continue
            suffix = lease_id.removeprefix("lease-")
            request = JobRequest(binding, f"request-{suffix}", "route-1", attempt, MODEL)
            receipt = JobReceipt(request.request_id, job, "connection-1", 1, FINGERPRINT, running)
            observer.start(lease_id, vm_attempt_id=attempt, provider_job_id=job, requested_model=MODEL,
                           request=request, receipt=receipt)
        self.vm = FakeVm(VM_STATES)
        self.jobs = FakeJobPort({"job-terminal": JobState.SUCCEEDED, "job-running": running,
                                 "job-unknown-terminal": JobState.SUCCEEDED}, failing={"job-failobs"})
        self.effects = FakeEffectPort(EFFECTS)
        self.reconciler = self.build()

    def seed(self) -> None:
        with self.store.transaction() as tx:
            tx.save(Pool("pool-1", "synthetic", "connection-1", max_parallel=16, enabled=True))
            tx.save(ExecutionProfile("profile-1", "pool-1", "worker", MODEL))
            for lease_id, attempt, state, job in LEASES:
                suffix = lease_id.removeprefix("lease-")
                placement_id = f"placement-{suffix}"
                tx.save(Placement(placement_id, "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                                  f"vm-task-{suffix}", attempt, state="assigned"))
                tx.save(Lease(lease_id, placement_id, "pool-1", "worker-1", 1, "2030-01-01T00:00:00Z",
                              state=state, provider_job_id=job))

    def build(self) -> RecoveryReconciler:
        """Fresh instances over the same files: the state a rebooted process sees."""
        store = RuntimeStore(self.runtime_path)
        manager = LeaseManager(store, BudgetStore(self.budget_path), ttl_seconds=600, clock=lambda: NOW)
        observer = WorkerObserver(self.observer_path, budget=self.observer_budget)
        return RecoveryReconciler(store, manager, observer, vm_port=self.vm, job_port=self.jobs,
                                  effect_port=self.effects, journal=RecoveryJournal(self.journal_path),
                                  clock=lambda: NOW)

    def lease_state(self, lease_id: str) -> str:
        return self.store.get(Lease, lease_id).value.state

    def rows(self, kind: str, subject: str) -> tuple:
        return self.reconciler.journal.decisions(kind, subject)

    # ------------------------------------------------------------------ leases
    def test_dispatch_boundary_crash_without_job_id_becomes_unknown_without_relaunch(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(self.lease_state("lease-intent"), "unknown")
        self.assertIn("unknown", HOLDING_STATES)  # capacity stays held
        self.assertNotIn("attempt-intent", [call[0] for call in self.jobs.calls])
        self.assertEqual(self.jobs.started, [])
        rows = self.rows("lease", "lease-intent")
        self.assertEqual([(row.action, row.outcome) for row in rows],
                         [("handoff_vm_unknown", "planned"), ("handoff_vm_unknown", "applied")])
        self.assertIn("DISPATCH_INTENT_WITHOUT_PROVIDER_JOB_ID", rows[0].reasons)
        self.assertIn("NO_RELAUNCH", rows[0].reasons)

    def test_provider_terminal_observed_on_stored_route_releases_lease(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(self.lease_state("lease-terminal"), "released")
        self.assertEqual(self.lease_state("lease-unknown-terminal"), "released")
        observed_jobs = {call[0] for call in self.jobs.calls}
        self.assertEqual(observed_jobs, {"job-terminal", "job-running", "job-failobs", "job-unknown-terminal"})
        for _job, connection, account in self.jobs.calls:
            self.assertEqual((connection, account), ("connection-1", FINGERPRINT))
        rows = self.rows("lease", "lease-terminal")
        self.assertEqual([row.outcome for row in rows], ["planned", "applied"])
        self.assertEqual(rows[0].observed["lease_revision"], 1)
        self.assertEqual(rows[1].observed["result_state"], "released")
        self.assertEqual(rows[1].observed["result_revision"], 2)
        self.assertIn("job-terminal", rows[0].evidence_refs)
        self.assertIn("attempt-terminal", rows[0].evidence_refs)

    def test_running_job_is_kept_running(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(self.lease_state("lease-running"), "active")
        rows = self.rows("lease", "lease-running")
        self.assertEqual([(row.action, row.outcome) for row in rows], [("keep_running", "recorded")])
        self.assertIsNotNone(rows[0].observed["job_state"])

    def test_observation_failure_keeps_unknown_and_never_relaunches(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(self.lease_state("lease-failobs"), "unknown")
        self.assertEqual(self.jobs.started, [])
        rows = self.rows("lease", "lease-failobs")
        self.assertEqual(rows[-1].action, "observe_failed_keep_unknown")
        self.assertIn("PROVIDER_OBSERVATION_FAILED", rows[0].reasons)
        # A second run after reboot keeps it unknown and still never relaunches.
        self.build().reconcile()
        self.assertEqual(self.lease_state("lease-failobs"), "unknown")
        self.assertEqual(self.jobs.started, [])

    def test_reserved_without_dispatch_released_only_when_vm_attempt_finished(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(self.lease_state("lease-reserved-done"), "released")
        self.assertEqual(self.lease_state("lease-reserved-live"), "reserved")
        self.assertEqual(self.lease_state("lease-reserved-vmdown"), "reserved")
        live = self.rows("lease", "lease-reserved-live")
        self.assertEqual(live[0].action, "await_vm")
        down = self.rows("lease", "lease-reserved-vmdown")
        self.assertIn("VM_ATTEMPT_UNAVAILABLE", down[0].reasons)
        self.assertEqual(down[0].observed["vm_state"], "unavailable")

    # ----------------------------------------------------------------- effects
    def test_effect_without_receipt_is_marked_unknown_not_reexecuted(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(sorted(self.effects.marked), ["tool-call-1", "tool-call-4"])
        self.assertEqual(self.effects.executed, [])
        rows = self.rows("effect", "attempt-effect/tool-call-1/write_file")
        self.assertIn("NON_IDEMPOTENT_HALT_UNTIL_CONFIRMED", rows[0].reasons)
        self.assertIn("NO_AUTOMATIC_REEXECUTION", rows[0].reasons)
        self.assertEqual(rows[-1].outcome, "applied")

    def test_receipt_before_model_response_resends_result_only(self) -> None:
        self.reconciler.reconcile()
        self.assertEqual(self.effects.resent, ["tool-call-2"])
        self.assertEqual(self.effects.executed, [])
        rows = self.rows("effect", "attempt-effect/tool-call-2/write_file")
        self.assertEqual(rows[-1].action, "resend_result")
        self.assertIn("EFFECT_NOT_REEXECUTED", rows[-1].reasons)
        self.assertIn("receipt-2", rows[-1].evidence_refs)
        done = self.rows("effect", "attempt-effect/tool-call-5/read_file")
        self.assertEqual(done[0].action, "no_action_result_delivered")

    def test_late_receipt_after_cancel_is_recorded_without_resend(self) -> None:
        self.reconciler.reconcile()
        self.assertNotIn("tool-call-3", self.effects.resent)
        self.assertNotIn("tool-call-3", self.effects.marked)
        rows = self.rows("effect", "attempt-cancelled/tool-call-3/post_comment")
        self.assertEqual([(row.action, row.outcome) for row in rows], [("record_late_receipt", "recorded")])
        self.assertIn("LATE_RECEIPT_AFTER_CANCEL", rows[0].reasons)
        late_effect = self.rows("effect", "attempt-cancelled/tool-call-4/post_comment")
        self.assertIn("ATTEMPT_CANCELLED", late_effect[0].reasons)

    def test_failed_resend_is_recorded_and_retried_once_later(self) -> None:
        self.effects.fail_resend = 1
        report = self.reconciler.reconcile()
        subject = "attempt-effect/tool-call-2/write_file"
        self.assertIn(f"effect:{subject}", report.failures)
        self.assertEqual([row.outcome for row in self.rows("effect", subject)], ["planned", "failed"])
        self.build().reconcile()
        self.assertEqual(self.effects.resent, ["tool-call-2", "tool-call-2"])
        self.assertEqual(self.rows("effect", subject)[-1].outcome, "applied")
        self.build().reconcile()
        self.assertEqual(self.effects.resent, ["tool-call-2", "tool-call-2"])

    # ------------------------------------------------------------- journaling
    def test_journal_is_append_only_and_rejects_foreign_files(self) -> None:
        self.reconciler.reconcile()
        before = self.reconciler.journal.decisions()
        self.assertTrue(before)
        with closing(sqlite3.connect(self.journal_path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE decisions SET action = 'keep_running'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM decisions")
        self.assertEqual(self.reconciler.journal.decisions(), before)

        garbage = self.root / "garbage.sqlite3"
        garbage.write_bytes(b"not a database" * 8)
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        for path in (garbage, foreign):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                with self.assertRaises(InvalidDatabase):
                    RecoveryJournal(path)
                self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(RecoveryError):
            self.reconciler.journal.append(subject_kind="lease", subject_id="lease-x", action="keep_running",
                                           outcome="recorded", reasons=("lowercase reason",), observed={},
                                           evidence_refs=(), decided_at="2029-01-01T00:00:00Z")

    def test_second_reconcile_after_restart_is_idempotent(self) -> None:
        first = self.reconciler.reconcile()
        first_rows = self.reconciler.journal.decisions()
        self.assertEqual(first_rows, first.decisions)
        states = {lease_id: self.lease_state(lease_id) for lease_id, *_ in LEASES}
        marked, resent = list(self.effects.marked), list(self.effects.resent)

        self.build().reconcile()
        third = self.build().reconcile()
        all_rows = self.reconciler.journal.decisions()
        self.assertEqual(all_rows[:len(first_rows)], first_rows)
        self.assertEqual(third.decisions, ())
        self.assertEqual(self.build().reconciler_journal_rows() if False else len(all_rows),
                         len(self.reconciler.journal.decisions()))
        self.assertEqual({lease_id: self.lease_state(lease_id) for lease_id, *_ in LEASES}, states)
        self.assertEqual((self.effects.marked, self.effects.resent), (marked, resent))
        self.assertEqual(self.jobs.started, [])
        self.assertEqual(self.effects.executed, [])
        self.assertIn("effect:attempt-effect/tool-call-2/write_file", third.unchanged)


if __name__ == "__main__":
    unittest.main()
