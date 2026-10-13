"""AIK-041 elasticity controller against disposable SQLite files and synthetic inventories.

Real RuntimeStore, BudgetStore, PlacementPlanner/Journal, LifecycleGate and
GatedLeaseManager run in a temporary directory. The lease manager is the real
LeaseManager with only reserve() replaced by a granting double, as in the
lifecycle tests: budget admission is not exercised. The pool registry,
capability inventory and provider/worker/effect/evidence ports are fakes. No
provider, credential, network, VM or AGS installation is used. Passing on Linux
is development verification only; Windows behaviour is NOT_RUN.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_budget import BudgetStore  # noqa: E402
from aik_capability import UnknownResource  # noqa: E402
from aik_elastic import (  # noqa: E402
    CAPACITY_EXHAUSTED, CONTROL_RESERVE, ElasticError, ElasticityController, TickResult,
)
from aik_evidence.models import VerifyResult  # noqa: E402
from aik_lease import HOLDING_STATES, LeaseManager, LeaseResult, lease_id_for  # noqa: E402
from aik_lifecycle import (  # noqa: E402
    GatedLeaseManager, LifecycleController, LifecycleError, LifecycleGate, WorkerRecord,
    WorkerRegistry, runtime_placement_lookup,
)
from aik_observer import WorkerObserver  # noqa: E402
from aik_placement import PlacementJournal, PlacementPlanner, ReadyTask  # noqa: E402
from aik_pool import QualificationLevel, RouteStatus, UnknownRoute  # noqa: E402
from aik_provider.contracts import (  # noqa: E402
    AuthKind, ConnectionBinding, JobReceipt, JobRequest, JobState, TERMINAL,
)
from aik_runtime import ExecutionProfile, Lease, Placement, Pool, RuntimeStore  # noqa: E402


START = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
STAMP = "2026-10-13T09:00:00Z"
IDLE = 30
FINGERPRINT = "ab" * 32
RUNNABLE = ("probe_verified", "candidate_for_qualification", "qualified")
RUNNING = JobState.__members__.get("RUNNING") or next(state for state in JobState if state not in TERMINAL)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make_route(pool_id, model="model-x", *, level=QualificationLevel.QUALIFIED, capacity=2,
               freshness="fresh", max_parallel=2):
    executable = str(level) in RUNNABLE and freshness == "fresh" and capacity is not None and capacity > 0
    return RouteStatus(
        pool_id=pool_id, connection_id="connection-" + pool_id, connection_revision=1, provider="synthetic",
        host="synthetic-host", auth_kind="api_key", model=model, tools=(), pool_enabled=True,
        configured_max_parallel=max_parallel, implementation_state="implemented", account_binding="bound",
        runtime_support="supported", account_isolation="isolated", governance="approved",
        last_probe_at=STAMP, last_verified_at=STAMP, freshness=freshness, matrix_revision=7,
        qualification_matrix_revision=7 if level == QualificationLevel.QUALIFIED else None,
        capacity_state="known" if capacity is not None else "unknown", available_capacity=capacity,
        level=level, executable_now=executable,
        placement_eligible=executable and level == QualificationLevel.QUALIFIED,
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

    def put(self, connection_id, model="model-x", efforts=("low", "high"), tools=("shell", "edit"), revision=3):
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


def task(attempt="attempt-1", project="project-1", **overrides):
    values = dict(vm_instance_id="vm-instance-1", vm_project_id=project, vm_task_id="task-" + attempt,
                  vm_attempt_id=attempt, vm_authorization_ref="vm-auth-" + attempt, role="worker",
                  model="model-x", effort="high", tools=("shell",))
    values.update(overrides)
    return ReadyTask(**values)


class GrantingLeaseManager(LeaseManager):
    """Real LeaseManager whose reserve() stores the lease directly (no budget admission)."""

    def __init__(self, store, budget, *, clock):
        super().__init__(store, budget, ttl_seconds=3600, clock=clock)
        self._test_runtime = store
        self._test_clock = clock
        self.calls = []

    def reserve(self, placement_id, *, worker_id, **_):
        self.calls.append(placement_id)
        placement = self._test_runtime.get(Placement, placement_id).value
        expires = iso(self._test_clock() + timedelta(seconds=3600))
        lease = Lease(lease_id_for(placement_id, 1), placement_id, placement.pool_id, worker_id, 1, expires)
        stored = self._test_runtime.save(lease, expected_revision=0)
        return LeaseResult("granted", stored, (), None, lease.id)


def synthetic_binding() -> ConnectionBinding:
    for kind in AuthKind:
        for reference in ("secret://local/synthetic-ref", "host-session://synthetic/session-ref"):
            try:
                return ConnectionBinding("synthetic", "synthetic", "connection-1", 1, kind, reference, FINGERPRINT)
            except Exception:
                continue
    raise AssertionError("No synthetic connection binding could be constructed.")


class FakeJobPort:
    def __init__(self):
        self.states = {}
        self.interrupts = []

    def observe(self, request, receipt):
        return replace(receipt, state=self.states[receipt.provider_job_id])

    def interrupt(self, request, receipt):
        self.interrupts.append(receipt.provider_job_id)
        return replace(receipt, state=self.states[receipt.provider_job_id])


class SettlementSink:
    def __init__(self):
        self.calls = []

    def settle(self, reservation_id, usage):
        self.calls.append(reservation_id)
        return SimpleNamespace(seq=len(self.calls), reservation_id=reservation_id,
                               kind="settlement" if self.calls.count(reservation_id) == 1 else "late",
                               usage=usage, recorded_at=STAMP)


class FakeWorkerPort:
    def __init__(self):
        self.terminated = []
        self.cleaned = []

    def terminate(self, worker_id):
        self.terminated.append(worker_id)

    def cleanup(self, worker_id):
        self.cleaned.append(worker_id)


class FakeEffectPort:
    def __init__(self):
        self.pending = {}

    def unresolved(self, vm_attempt_id):
        return self.pending.get(vm_attempt_id, ())


class FakeEvidencePort:
    def __init__(self):
        self.statuses = {}

    def verify(self, artifact_id):
        status = self.statuses[artifact_id]
        return VerifyResult(artifact_id, status, "sha256:" + "0" * 64,
                            None if status == "missing" else "sha256:" + "0" * 64)


class ElasticityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aik-elastic-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = self.root / "state"
        self.clock = FakeClock()
        self.runtime = RuntimeStore(state / "runtime.sqlite3", timeout=10)
        self.journal = PlacementJournal(state / "placement-journal.sqlite3", timeout=10)
        self.budget = BudgetStore(state / "budget.sqlite3")
        self.gate = LifecycleGate(state / "lifecycle.sqlite3", clock=self.clock)
        with self.runtime.transaction() as tx:
            tx.save(Pool("pool-a", "synthetic", "connection-pool-a", max_parallel=2, enabled=True))
            tx.save(Pool("pool-solo", "synthetic", "connection-pool-solo", max_parallel=1, enabled=True))
            tx.save(ExecutionProfile("profile-a", "pool-a", "worker", "model-x"))
            tx.save(ExecutionProfile("profile-solo", "pool-solo", "worker", "model-x"))
        self.registry = FakeRegistry()
        self.registry.put(make_route("pool-a", capacity=2, max_parallel=2))
        self.registry.put(make_route("pool-solo", capacity=1, max_parallel=1))
        self.inventory = FakeInventory()
        for pool in ("pool-a", "pool-solo"):
            self.inventory.put("connection-" + pool)
        self.planner = PlacementPlanner(self.runtime, self.registry, self.inventory, self.journal, clock=self.clock)
        self.manager = GrantingLeaseManager(self.runtime, self.budget, clock=self.clock)
        self.gated = GatedLeaseManager(self.manager, self.gate, runtime_placement_lookup(self.runtime))

    def controller(self, reserve=None):
        return ElasticityController(self.planner, self.gated, self.runtime, control_reserve=reserve,
                                    idle_seconds=IDLE, clock=self.clock)

    @staticmethod
    def tick(controller, tasks, profiles=("profile-a",)) -> TickResult:
        return controller.tick(tasks, candidates=profiles, reserve_options={"worker_id": "worker-owned"})

    def active_placements(self, attempt):
        return [entry for entry in self.runtime.list(Placement, limit=1000)
                if entry.value.vm_attempt_id == attempt and entry.value.state in ("pending", "assigned")]

    def lease_state(self, lease_id):
        return self.runtime.get(Lease, lease_id).value.state

    def test_same_project_runs_serially(self):
        result = self.tick(self.controller(), [task("attempt-1"), task("attempt-2")])
        self.assertEqual([attempt for attempt, _ in result.granted], ["attempt-1"])
        waiting = dict(result.waiting)
        self.assertEqual(list(waiting), ["attempt-2"])
        self.assertTrue(waiting["attempt-2"])
        self.assertNotIn(CONTROL_RESERVE, waiting["attempt-2"])
        self.assertNotIn(CAPACITY_EXHAUSTED, waiting["attempt-2"])
        self.assertEqual(len(self.manager.calls), 1)
        self.assertEqual(len(self.runtime.list(Lease)), 1)

    def test_different_projects_run_in_bounded_parallel(self):
        tasks = [task("attempt-1", "project-1"), task("attempt-2", "project-2"), task("attempt-3", "project-3")]
        result = self.tick(self.controller(), tasks)
        self.assertEqual([attempt for attempt, _ in result.granted], ["attempt-1", "attempt-2"])
        self.assertEqual(result.waiting, (("attempt-3", (CAPACITY_EXHAUSTED,)),))
        self.assertEqual(len(self.manager.calls), 2)
        self.assertEqual(self.active_placements("attempt-3"), [])
        held = [entry for entry in self.runtime.list(Lease) if entry.value.state in HOLDING_STATES]
        self.assertEqual(len(held), 2)

    def test_control_reserve_keeps_slots_for_control_paths(self):
        controller = self.controller({"pool-a": 1, "pool-solo": 1})
        result = self.tick(controller, [task("attempt-1", "project-1"), task("attempt-2", "project-2")])
        self.assertEqual([attempt for attempt, _ in result.granted], ["attempt-1"])
        self.assertEqual(result.waiting, (("attempt-2", (CONTROL_RESERVE,)),))
        solo = self.tick(controller, [task("attempt-3", "project-3")], profiles=("profile-solo",))
        self.assertEqual(solo.granted, ())
        self.assertEqual(solo.waiting, (("attempt-3", (CONTROL_RESERVE,)),))
        self.assertEqual(len(self.manager.calls), 1)
        self.assertEqual(self.active_placements("attempt-3"), [])

    def test_paused_scope_waits_and_resume_grants(self):
        controller = self.controller()
        self.gate.pause("project:project-1", "synthetic-maintenance")
        paused = self.tick(controller, [task("attempt-1")])
        self.assertEqual(paused.granted, ())
        (attempt, reasons), = paused.waiting
        self.assertEqual(attempt, "attempt-1")
        self.assertTrue(any("paused" in reason.lower() for reason in reasons), reasons)
        self.assertEqual(self.runtime.list(Lease), ())
        self.assertEqual(self.active_placements("attempt-1"), [])
        self.gate.resume("project:project-1")
        resumed = self.tick(controller, [task("attempt-1")])
        self.assertEqual([attempt for attempt, _ in resumed.granted], ["attempt-1"])
        self.assertEqual(resumed.waiting, ())

    def test_idle_reclaim_returns_only_undispatched_reserved_leases(self):
        controller = self.controller()
        result = self.tick(controller, [task("attempt-1", "project-1"), task("attempt-2", "project-2")])
        leases = dict(result.granted)
        self.gated.activate(leases["attempt-1"], 1, "job-1")
        self.assertEqual(controller.reclaim_idle(), ())
        self.clock.advance(IDLE + 1)
        self.assertEqual(controller.reclaim_idle(), (leases["attempt-2"],))
        self.assertEqual(self.lease_state(leases["attempt-1"]), "active")
        self.assertNotIn(self.lease_state(leases["attempt-2"]), HOLDING_STATES)
        self.assertEqual(self.active_placements("attempt-2"), [])
        again = self.tick(controller, [task("attempt-3", "project-2")])
        self.assertEqual([attempt for attempt, _ in again.granted], ["attempt-3"])
        self.assertEqual(again.reclaimed, ())

    def test_finish_delegates_teardown_and_keeps_resources_with_unresolved_effects(self):
        state = self.root / "state"
        workers = WorkerRegistry(state / "lifecycle.sqlite3", clock=self.clock)
        workers.register(WorkerRecord("worker-owned", "owned", "pool-a"))
        observer = WorkerObserver(state / "observer.sqlite3", budget=SettlementSink())
        jobs, worker_port = FakeJobPort(), FakeWorkerPort()
        effects, evidence = FakeEffectPort(), FakeEvidencePort()
        lifecycle = LifecycleController(
            self.gate, self.manager, observer, workers, store=self.runtime, job_port=jobs,
            worker_port=worker_port, effect_port=effects, evidence_port=evidence, clock=self.clock,
        )
        controller = self.controller()
        (_, lease_id), = self.tick(controller, [task("attempt-1")]).granted
        self.gated.activate(lease_id, 1, "job-1")
        binding = synthetic_binding()
        request = JobRequest(binding, "request-1", "route-1", "attempt-1", "synthetic-model")
        receipt = JobReceipt("request-1", "job-1", "connection-1", 1, FINGERPRINT, RUNNING)
        jobs.states["job-1"] = RUNNING
        observer.start(lease_id, vm_attempt_id="attempt-1", provider_job_id="job-1",
                       requested_model="synthetic-model", request=request, receipt=receipt)
        effects.pending["attempt-1"] = ("effect-open",)
        try:
            view = controller.finish(lease_id, 1, lifecycle, required_artifacts=())
            self.assertFalse(view.teardown_complete)
        except LifecycleError:
            pass
        self.assertEqual(self.lease_state(lease_id), "active")
        self.assertEqual(worker_port.terminated, [])
        self.assertEqual(len(self.active_placements("attempt-1")), 1)
        with self.assertRaises(ElasticError):
            controller.finish(lease_id, 1, object(), required_artifacts=())

    def test_duplicate_attempts_and_invalid_configuration(self):
        result = self.tick(self.controller(), [task("attempt-1"), task("attempt-1")])
        self.assertEqual(len(result.granted), 1)
        self.assertEqual(result.waiting, ())
        with self.assertRaises(ElasticError):
            ElasticityController(self.planner, self.manager, self.runtime, idle_seconds=IDLE)
        with self.assertRaises(ElasticError):
            self.controller({"pool-a": -1})
        with self.assertRaises(ElasticError):
            ElasticityController(self.planner, self.gated, self.runtime, idle_seconds=float("nan"))
        with self.assertRaises(ElasticError):
            self.controller().tick(["not-a-task"], candidates=("profile-a",), reserve_options={})


if __name__ == "__main__":
    unittest.main()
