"""AIK-038 lifecycle tests with synthetic data only.

Real RuntimeStore, BudgetStore, LeaseManager and WorkerObserver run on files in
a temporary directory. Provider jobs, workers, tool effects and evidence are
fake ports. No provider, credential, network, VM or AGS installation is used.
Passing on Linux is development verification, not Windows support.
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
from aik_evidence.models import VerifyResult  # noqa: E402
from aik_lease import LeaseManager, LeaseResult, StaleLease, lease_id_for  # noqa: E402
from aik_lifecycle import (  # noqa: E402
    BindingEffectAdapter, GatedLeaseManager, LifecycleController, LifecycleError, LifecycleGate,
    WorkerRecord, WorkerRegistry, interrupt_supported, runtime_placement_lookup,
)
from aik_observer import WorkerObserver  # noqa: E402
from aik_provider.contracts import (  # noqa: E402
    AccountIsolation, AdapterCapabilities, AuthKind, ConnectionBinding, JobReceipt, JobRequest,
    JobState, Support, TERMINAL,
)
from aik_runtime import (  # noqa: E402
    ExecutionProfile, InvalidDatabase, Lease, Placement, Pool, RuntimeStore, UnsafeStoragePath,
)


NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
FINGERPRINT = "ab" * 32
SUPPORTED = Support.__members__.get("SUPPORTED")
UNSUPPORTED = next((member for member in Support if member.name != "SUPPORTED"), None)
RUNNING = JobState.__members__.get("RUNNING") or next(state for state in JobState if state not in TERMINAL)


def synthetic_binding() -> ConnectionBinding:
    for kind in AuthKind:
        for reference in ("secret://local/synthetic-ref", "host-session://synthetic/session-ref"):
            try:
                return ConnectionBinding("synthetic", "synthetic", "connection-1", 1, kind, reference, FINGERPRINT)
            except Exception:
                continue
    raise AssertionError("No synthetic connection binding could be constructed.")


def capabilities(interrupt, auth_kind) -> AdapterCapabilities:
    values = dict(provider="synthetic", runtime="synthetic", auth_kind=auth_kind,
                  isolation=next(iter(AccountIsolation)), auth_status=SUPPORTED, sign_in=SUPPORTED,
                  sign_out=SUPPORTED, job_start=SUPPORTED, job_observe=SUPPORTED, job_interrupt=interrupt)
    try:
        return AdapterCapabilities(**values)
    except Exception:
        # Disclosure fixture only; it is not a qualification of any adapter.
        forged = object.__new__(AdapterCapabilities)
        for name, value in {**values, "implementation_state": "implemented",
                            "qualification_status": "NOT_RUN"}.items():
            object.__setattr__(forged, name, value)
        return forged


class FakeJobPort:
    def __init__(self):
        self.states = {}
        self.interrupts = []
        self.interrupt_state = None
        self.interrupt_error = None

    def observe(self, request, receipt):
        return replace(receipt, state=self.states[receipt.provider_job_id])

    def interrupt(self, request, receipt):
        self.interrupts.append(receipt.provider_job_id)
        if self.interrupt_error is not None:
            raise self.interrupt_error
        return replace(receipt, state=self.interrupt_state or self.states[receipt.provider_job_id])


class SettlementSink:
    def __init__(self):
        self.calls = []

    def settle(self, reservation_id, usage):
        self.calls.append(reservation_id)
        return SimpleNamespace(seq=len(self.calls), reservation_id=reservation_id,
                               kind="settlement" if self.calls.count(reservation_id) == 1 else "late",
                               usage=usage, recorded_at="2026-10-13T09:00:00Z")


class FakeWorkerPort:
    def __init__(self):
        self.terminated = []
        self.cleaned = []
        self.terminate_error = None

    def terminate(self, worker_id):
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated.append(worker_id)

    def cleanup(self, worker_id):
        self.cleaned.append(worker_id)


class FakeEffectPort:
    def __init__(self):
        self.pending = {}
        self.error = False

    def unresolved(self, vm_attempt_id):
        if self.error:
            raise OSError("synthetic effect status failure")
        return self.pending.get(vm_attempt_id, ())


class FakeEvidencePort:
    def __init__(self):
        self.statuses = {}

    def verify(self, artifact_id):
        status = self.statuses[artifact_id]
        return VerifyResult(artifact_id, status, "sha256:" + "0" * 64,
                            None if status == "missing" else "sha256:" + "0" * 64)


class GrantingLeaseManager(LeaseManager):
    """Test double for the gate: grants by storing the lease; budget admission is not exercised."""

    def __init__(self, store, *args, on_reserve=None, **kwargs):
        super().__init__(store, *args, **kwargs)
        self.runtime = store
        self.calls = []
        self.on_reserve = on_reserve

    def reserve(self, placement_id, *, worker_id, **_):
        self.calls.append(placement_id)
        if self.on_reserve is not None:
            self.on_reserve()
        lease = Lease(lease_id_for(placement_id, 1), placement_id, "pool-1", worker_id, 1, "2030-01-01T00:00:00Z")
        stored = self.runtime.save(lease, expected_revision=0)
        return LeaseResult("granted", stored, (), None, lease.id)


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-lifecycle-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = RuntimeStore(self.root / "runtime.sqlite3")
        self.budget = BudgetStore(self.root / "budget.sqlite3")
        self.leases = LeaseManager(self.runtime, self.budget, ttl_seconds=3600, clock=lambda: NOW)
        self.gate = LifecycleGate(self.root / "lifecycle.sqlite3", clock=lambda: NOW)
        self.workers = WorkerRegistry(self.root / "lifecycle.sqlite3", clock=lambda: NOW)
        self.sink = SettlementSink()
        self.observer = WorkerObserver(self.root / "observer.sqlite3", budget=self.sink)
        self.jobs, self.worker_port = FakeJobPort(), FakeWorkerPort()
        self.effects, self.evidence = FakeEffectPort(), FakeEvidencePort()
        self.binding = synthetic_binding()
        self.controller = LifecycleController(
            self.gate, self.leases, self.observer, self.workers, store=self.runtime,
            job_port=self.jobs, worker_port=self.worker_port, effect_port=self.effects,
            evidence_port=self.evidence, clock=lambda: NOW,
        )
        with self.runtime.transaction() as tx:
            tx.save(Pool("pool-1", "synthetic", "connection-1", max_parallel=16, enabled=True), expected_revision=0)
            tx.save(ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model"), expected_revision=0)
        self.workers.register(WorkerRecord("worker-owned", "owned", "pool-1"))
        self.workers.register(WorkerRecord("worker-borrowed", "borrowed", "pool-1"))

    def caps(self, interrupt):
        return capabilities(interrupt, self.binding.auth_kind)

    def add_placement(self, number, project="vm-project-1"):
        placement_id = f"placement-{number}"
        self.runtime.save(Placement(placement_id, "pool-1", "profile-1", "vm-instance-1", project,
                                    f"vm-task-{number}", f"vm-attempt-{number}", state="assigned"),
                          expected_revision=0)
        return placement_id

    def seed(self, number, worker_id="worker-owned", *, dispatched=True):
        placement_id = self.add_placement(number)
        lease_id = lease_id_for(placement_id, 1)
        self.runtime.save(Lease(lease_id, placement_id, "pool-1", worker_id, 1, "2030-01-01T00:00:00Z"),
                          expected_revision=0)
        if not dispatched:
            return lease_id, f"vm-attempt-{number}", None
        job = f"job-{number}"
        self.leases.activate(lease_id, 1, job)
        request = JobRequest(self.binding, f"request-{number}", "route-1", f"vm-attempt-{number}", "synthetic-model")
        receipt = JobReceipt(f"request-{number}", job, "connection-1", 1, FINGERPRINT, RUNNING)
        self.jobs.states[job] = RUNNING
        self.observer.start(lease_id, vm_attempt_id=f"vm-attempt-{number}", provider_job_id=job,
                            requested_model="synthetic-model", request=request, receipt=receipt)
        return lease_id, f"vm-attempt-{number}", (request, receipt)

    def lease_state(self, lease_id):
        return self.runtime.get(Lease, lease_id).value.state


class GateTests(LifecycleTestCase):
    def gated(self, on_reserve=None):
        manager = GrantingLeaseManager(self.runtime, self.budget, ttl_seconds=3600, clock=lambda: NOW,
                                       on_reserve=on_reserve)
        return manager, GatedLeaseManager(manager, self.gate, runtime_placement_lookup(self.runtime))

    def test_pause_blocks_new_reservation_immediately_and_resume_allows_it(self):
        inner, gated = self.gated()
        first, second = self.add_placement(1), self.add_placement(2)
        self.gate.pause("global", "USER-STOP")
        result = gated.reserve(first, worker_id="worker-owned")
        self.assertEqual((result.status, result.lease, result.reasons), ("wait", None, ("SCOPE_PAUSED@global",)))
        self.assertEqual(inner.calls, [])
        self.assertIsNone(self.runtime.get(Lease, lease_id_for(first, 1)))
        self.gate.resume("global")
        self.assertTrue(gated.reserve(first, worker_id="worker-owned").granted)
        self.gate.drain("pool:pool-1", "MAINTENANCE")
        drained = gated.reserve(second, worker_id="worker-owned")
        self.assertEqual((drained.status, drained.reasons), ("wait", ("SCOPE_DRAINING@pool:pool-1",)))
        self.assertEqual(inner.calls, [first])

    def test_project_scope_blocks_only_that_project(self):
        inner, gated = self.gated()
        allowed, blocked = self.add_placement(1, "vm-project-1"), self.add_placement(2, "vm-project-2")
        self.gate.pause("project:vm-project-2", "USER-STOP")
        self.assertTrue(gated.reserve(allowed, worker_id="worker-owned").granted)
        self.assertEqual(gated.reserve(blocked, worker_id="worker-owned").reasons, ("SCOPE_PAUSED@project:vm-project-2",))

    def test_pause_racing_a_grant_releases_the_new_lease(self):
        _, gated = self.gated(on_reserve=lambda: self.gate.pause("global", "USER-STOP"))
        placement = self.add_placement(1)
        result = gated.reserve(placement, worker_id="worker-owned")
        self.assertEqual(result.status, "wait")
        self.assertEqual(result.lease_id, lease_id_for(placement, 1))
        self.assertEqual(self.lease_state(result.lease_id), "released")

    def test_unreadable_placement_lookup_fails_closed(self):
        inner = GrantingLeaseManager(self.runtime, self.budget, ttl_seconds=3600, clock=lambda: NOW)

        def broken(_):
            raise OSError("synthetic lookup failure")

        gated = GatedLeaseManager(inner, self.gate, broken)
        self.assertEqual(gated.reserve(self.add_placement(1), worker_id="worker-owned").reasons, ("GATE_UNAVAILABLE",))
        self.assertEqual(inner.calls, [])

    def test_gate_state_persists_events_are_append_only_and_foreign_files_are_kept(self):
        self.gate.pause("pool:pool-1", "USER-STOP")
        reopened = LifecycleGate(self.root / "lifecycle.sqlite3")
        self.assertEqual(reopened.state("pool:pool-1").state, "paused")
        self.assertEqual([event.action for event in reopened.events()], ["pause"])
        with closing(sqlite3.connect(self.root / "lifecycle.sqlite3")) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE gate_events SET action='resume'")
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InvalidDatabase):
            LifecycleGate(foreign)
        self.assertEqual(foreign.read_bytes(), before)
        with self.assertRaises(UnsafeStoragePath):
            LifecycleGate(self.root / "backlog.sqlite3")
        for scope in ("pool:", "tenant:x", "", None):
            with self.assertRaises(LifecycleError):
                self.gate.pause(scope, "USER-STOP")

    def test_worker_ownership_is_immutable(self):
        with self.assertRaises(LifecycleError) as raised:
            self.workers.register(WorkerRecord("worker-owned", "borrowed", "pool-1"))
        self.assertEqual(raised.exception.code, "WORKER_CONFLICT")
        self.assertEqual(self.workers.get("worker-owned").ownership, "owned")


class CancelTests(LifecycleTestCase):
    def test_interrupt_answer_is_not_termination_and_confirmation_is_observed_later(self):
        lease_id, _, (request, receipt) = self.seed(1)
        self.jobs.interrupt_state = JobState.CANCELLED  # the answer claims cancellation
        self.assertTrue(interrupt_supported(self.caps(SUPPORTED)))
        view = self.controller.cancel(lease_id, 1, capabilities=self.caps(SUPPORTED), request=request, receipt=receipt)
        self.assertEqual(self.jobs.interrupts, ["job-1"])
        self.assertEqual(view.kinds, ("cancel_requested", "cancel_interrupt_sent", "lease_draining"))
        self.assertEqual(view.events[1].detail["reported_state"], str(JobState.CANCELLED))
        self.assertIsNone(view.cancel_outcome)
        self.assertEqual(self.lease_state(lease_id), "draining")
        still = self.controller.settle_cancel(lease_id, 1)  # provider still reports running
        self.assertIsNone(still.cancel_outcome)
        self.assertEqual(self.lease_state(lease_id), "draining")
        self.assertEqual(self.sink.calls, [])
        self.jobs.states["job-1"] = JobState.CANCELLED
        confirmed = self.controller.settle_cancel(lease_id, 1)
        self.assertEqual(confirmed.cancel_outcome, "cancel_confirmed")
        self.assertNotIn("late_success", confirmed.kinds)
        self.assertEqual(self.lease_state(lease_id), "released")
        self.assertEqual(self.sink.calls, [lease_id])
        self.controller.settle_cancel(lease_id, 1)
        self.assertEqual(self.sink.calls, [lease_id])

    def test_late_success_after_cancel_is_recorded_separately(self):
        lease_id, _, (request, receipt) = self.seed(1)
        self.controller.cancel(lease_id, 1, capabilities=self.caps(SUPPORTED), request=request, receipt=receipt)
        self.jobs.states["job-1"] = JobState.SUCCEEDED
        view = self.controller.settle_cancel(lease_id, 1)
        self.assertEqual(view.cancel_outcome, "late_success")
        self.assertNotIn("cancel_confirmed", view.kinds)
        self.assertEqual(self.lease_state(lease_id), "released")

    def test_unsupported_cancel_leaves_job_running_and_lease_draining(self):
        lease_id, _, (request, receipt) = self.seed(1)
        self.assertFalse(interrupt_supported(self.caps(UNSUPPORTED)))
        view = self.controller.cancel(lease_id, 1, capabilities=self.caps(UNSUPPORTED), request=request, receipt=receipt)
        self.assertEqual(self.jobs.interrupts, [])
        self.assertEqual(view.kinds, ("cancel_requested", "cancel_unsupported", "lease_draining"))
        self.controller.settle_cancel(lease_id, 1)
        self.assertEqual(self.lease_state(lease_id), "draining")

    def test_failed_interrupt_is_unknown_and_lease_still_only_drains(self):
        lease_id, _, (request, receipt) = self.seed(1)
        self.jobs.interrupt_error = TimeoutError("synthetic")
        view = self.controller.cancel(lease_id, 1, capabilities=self.caps(SUPPORTED), request=request, receipt=receipt)
        self.assertIn("cancel_interrupt_unknown", view.kinds)
        self.assertEqual(self.lease_state(lease_id), "draining")

    def test_result_just_before_cancel_is_not_interrupted(self):
        lease_id, _, (request, receipt) = self.seed(1)
        self.jobs.states["job-1"] = JobState.SUCCEEDED
        self.observer.poll(lease_id, self.jobs)
        view = self.controller.cancel(lease_id, 1, capabilities=self.caps(SUPPORTED), request=request, receipt=receipt)
        self.assertEqual(self.jobs.interrupts, [])
        self.assertEqual(view.kinds, ("cancel_requested", "late_success", "lease_released"))
        self.assertEqual(self.lease_state(lease_id), "released")

    def test_cancel_before_dispatch_releases_without_provider_call(self):
        lease_id, _, _ = self.seed(1, dispatched=False)
        view = self.controller.cancel(lease_id, 1, capabilities=self.caps(SUPPORTED))
        self.assertEqual(view.kinds, ("cancel_requested", "cancel_before_dispatch", "lease_released"))
        self.assertEqual(self.jobs.interrupts, [])
        self.assertEqual(self.lease_state(lease_id), "released")

    def test_stale_fencing_token_is_rejected_without_events(self):
        lease_id, _, (request, receipt) = self.seed(1)
        with self.assertRaises(StaleLease):
            self.controller.cancel(lease_id, 2, capabilities=self.caps(SUPPORTED), request=request, receipt=receipt)
        self.assertEqual(self.controller.view(lease_id).events, ())
        self.assertEqual(self.lease_state(lease_id), "active")


class TeardownTests(LifecycleTestCase):
    def test_teardown_waits_for_effects_evidence_and_termination_then_terminates_owned_worker(self):
        lease_id, attempt, _ = self.seed(1)
        self.effects.pending[attempt] = (("EFFECT_IN_FLIGHT", "7"),)
        view = self.controller.teardown(lease_id, 1, required_artifacts=("artifact-1",))
        self.assertEqual(view.stage, "awaiting_effect_resolution")
        self.effects.pending[attempt] = ()
        self.evidence.statuses["artifact-1"] = "tampered"
        self.assertEqual(self.controller.teardown(lease_id, 1, required_artifacts=("artifact-1",)).stage,
                         "awaiting_evidence_handoff")
        self.evidence.statuses["artifact-1"] = "intact"
        self.assertEqual(self.controller.teardown(lease_id, 1, required_artifacts=("artifact-1",)).stage,
                         "awaiting_provider_terminal")
        self.assertEqual((self.worker_port.terminated, self.worker_port.cleaned), ([], []))
        self.assertEqual(self.lease_state(lease_id), "active")
        self.jobs.states["job-1"] = JobState.SUCCEEDED
        self.observer.poll(lease_id, self.jobs)
        done = self.controller.teardown(lease_id, 1, required_artifacts=("artifact-1",))
        self.assertTrue(done.teardown_complete)
        self.assertEqual(self.worker_port.terminated, ["worker-owned"])
        self.assertEqual(self.worker_port.cleaned, ["worker-owned"])
        self.assertEqual(self.lease_state(lease_id), "released")

    def test_borrowed_host_is_never_terminated_only_returned(self):
        lease_id, _, _ = self.seed(1, "worker-borrowed")
        self.jobs.states["job-1"] = JobState.SUCCEEDED
        self.observer.poll(lease_id, self.jobs)
        view = self.controller.teardown(lease_id, 1, required_artifacts=())
        self.assertTrue(view.teardown_complete)
        self.assertIn("worker_returned", view.kinds)
        self.assertEqual(self.worker_port.terminated, [])
        self.assertEqual(self.lease_state(lease_id), "released")

    def test_unresolved_effect_after_cancel_confirmation_blocks_completion_and_cleanup(self):
        lease_id, attempt, (request, receipt) = self.seed(1)
        self.controller.cancel(lease_id, 1, capabilities=self.caps(SUPPORTED), request=request, receipt=receipt)
        self.jobs.states["job-1"] = JobState.CANCELLED
        self.controller.settle_cancel(lease_id, 1)
        self.assertEqual(self.lease_state(lease_id), "released")
        self.effects.pending[attempt] = (("EFFECT_INTENT_RECORDED", "effect-1"),)  # late receipt pending
        view = self.controller.teardown(lease_id, 1, required_artifacts=())
        self.assertFalse(view.teardown_complete)
        self.assertEqual(self.worker_port.cleaned, [])
        self.effects.error = True
        self.assertEqual(self.controller.teardown(lease_id, 1, required_artifacts=()).events[-1].detail["code"],
                         "EFFECT_STATUS_UNAVAILABLE")
        self.assertEqual(self.worker_port.cleaned, [])

    def test_missing_artifact_and_shared_worker_block_cleanup(self):
        lease_id, _, _ = self.seed(1)
        self.seed(2)  # the same owned worker still holds another lease
        view = self.controller.teardown(lease_id, 1, required_artifacts=("artifact-absent",))
        self.assertEqual(view.events[-1].detail["artifacts"], ("artifact-absent",))
        self.jobs.states["job-1"] = JobState.SUCCEEDED
        self.observer.poll(lease_id, self.jobs)
        shared = self.controller.teardown(lease_id, 1, required_artifacts=())
        self.assertEqual(shared.stage, "worker_in_use")
        self.assertEqual((self.worker_port.terminated, self.worker_port.cleaned), ([], []))


class AdapterTests(unittest.TestCase):
    def test_binding_effect_adapter_reports_inflight_unknown_and_pending_receipts(self):
        binding = SimpleNamespace(
            unknown_attempts=frozenset({"vm-attempt-9"}), unknown_threads=frozenset({"thread-x"}),
            inflight_effects=lambda: ({"id": 3, "vm_attempt_id": "vm-attempt-1", "thread_id": None,
                                       "operation": "start_turn"},),
            route_for_thread=lambda thread: SimpleNamespace(vm_attempt_id="vm-attempt-2"),
        )
        journal = SimpleNamespace(read_effect=lambda effect: {"effect_id": effect,
                                                              "state": "intent_recorded" if effect == "e1" else "succeeded"})
        adapter = BindingEffectAdapter(binding, effect_journal=journal, effect_ids=lambda attempt: ("e1", "e2"))
        self.assertEqual(adapter.unresolved("vm-attempt-1"),
                         (("EFFECT_IN_FLIGHT", "3"), ("EFFECT_INTENT_RECORDED", "e1")))
        self.assertIn(("ATTEMPT_UNKNOWN", "vm-attempt-9"), adapter.unresolved("vm-attempt-9"))


if __name__ == "__main__":
    unittest.main()
