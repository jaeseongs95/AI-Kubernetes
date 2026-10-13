"""AIK-033 placement against disposable SQLite files and synthetic inventories.

Pools and profiles are stored in a real RuntimeStore. The pool registry and
capability inventory are fakes with the same public methods. No provider,
credential, network, VM or AGS installation is used. Passing on Linux is
development verification; Windows behaviour is NOT_RUN.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_capability import UnknownResource  # noqa: E402
from aik_placement import (  # noqa: E402
    ParallelQualification, PlacementError, PlacementJournal, PlacementPlanner, ReadyTask,
    compute_digest, decision_document,
)
from aik_pool import QualificationLevel, RouteStatus, UnknownRoute  # noqa: E402
from aik_runtime import (  # noqa: E402
    ExecutionProfile, InvalidDatabase, Placement, Pool, RevisionConflict, RuntimeStore, UnsafeStoragePath,
)


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
STAMP = "2030-01-01T00:00:00Z"
RUNNABLE = ("probe_verified", "candidate_for_qualification", "qualified")


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


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-placement-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime_path = self.root / "state" / "runtime.sqlite3"
        self.journal_path = self.root / "state" / "placement-journal.sqlite3"
        self.store = RuntimeStore(self.runtime_path, timeout=10)
        self.journal = PlacementJournal(self.journal_path, timeout=10)
        with self.store.transaction() as tx:
            tx.save(Pool("pool-a", "synthetic", "connection-pool-a", max_parallel=3, enabled=True))
            tx.save(Pool("pool-b", "synthetic", "connection-pool-b", max_parallel=1, enabled=True))
            tx.save(Pool("pool-off", "synthetic", "connection-pool-off", max_parallel=1, enabled=False))
            tx.save(ExecutionProfile("profile-a", "pool-a", "worker", "model-x"))
            tx.save(ExecutionProfile("profile-b", "pool-b", "worker", "model-x", effort="high"))
            tx.save(ExecutionProfile("profile-review", "pool-a", "reviewer", "model-x"))
            tx.save(ExecutionProfile("profile-off", "pool-off", "worker", "model-x"))
        self.registry = FakeRegistry()
        self.registry.put(make_route("pool-a", capacity=3, max_parallel=3))
        self.registry.put(make_route("pool-b", capacity=1, max_parallel=1))
        self.registry.put(make_route("pool-off", capacity=1, max_parallel=1))
        self.inventory = FakeInventory()
        for pool in ("pool-a", "pool-b", "pool-off"):
            self.inventory.put("connection-" + pool)
        self.planner = self.make_planner(self.store, self.journal)

    def make_planner(self, store, journal):
        return PlacementPlanner(store, self.registry, self.inventory, journal, clock=lambda: START)

    def check(self, decision, profile_id):
        return next(check for check in decision.checks if check.profile_id == profile_id)

    def active_on(self, pool_id):
        return [entry for entry in self.store.list(Placement, limit=1000)
                if entry.value.pool_id == pool_id and entry.value.state in ("pending", "assigned")]

    def test_first_eligible_profile_is_placed_with_vm_ids_and_recorded(self):
        decision = self.planner.plan(task(), ("profile-a", "profile-b"))
        self.assertEqual(decision.status, "placed")
        self.assertEqual((decision.profile_id, decision.pool_id), ("profile-a", "pool-a"))
        placement = decision.placement.value
        self.assertEqual(placement.state, "assigned")
        self.assertEqual((placement.vm_instance_id, placement.vm_project_id, placement.vm_task_id,
                          placement.vm_attempt_id), ("vm-instance-1", "project-1", "task-attempt-1", "attempt-1"))
        self.assertEqual(self.store.get(Placement, placement.id), decision.placement)
        self.assertEqual(dict(decision.inventory_revision), {"matrix_revision": 7, "resource_revision": 3})
        self.assertEqual(decision.task.vm_authorization_ref, "vm-auth-attempt-1")
        self.assertEqual(decision.digest, compute_digest(decision_document(decision)))
        records = self.journal.decisions()
        self.assertEqual([record["decision_id"] for record in records], [decision.decision_id])
        self.assertEqual(records[0]["digest"], decision.digest)
        self.assertEqual(records[0]["task"]["vm_authorization_ref"], "vm-auth-attempt-1")

    def test_preference_order_is_kept_and_each_rejection_is_recorded(self):
        del self.registry.routes[("pool-a", "model-x")]
        decision = self.planner.plan(task(), ("profile-missing", "profile-a", "profile-b"))
        self.assertEqual(decision.status, "placed")
        self.assertEqual(decision.profile_id, "profile-b")
        self.assertEqual(self.check(decision, "profile-missing").reasons, ("PROFILE_NOT_FOUND",))
        self.assertIn("ROUTE_NOT_REGISTERED", self.check(decision, "profile-a").reasons)
        self.assertEqual([check.profile_id for check in decision.checks],
                         ["profile-missing", "profile-a", "profile-b"])

    def test_role_model_effort_and_disabled_pool_mismatches_reject(self):
        decision = self.planner.plan(task(effort="low"), ("profile-review", "profile-b", "profile-off"))
        self.assertEqual(decision.status, "rejected")
        self.assertIsNone(decision.placement)
        self.assertIn("ROLE_MISMATCH", self.check(decision, "profile-review").reasons)
        self.assertIn("EFFORT_MISMATCH", self.check(decision, "profile-b").reasons)
        self.assertIn("POOL_DISABLED", self.check(decision, "profile-off").reasons)
        model = self.planner.plan(task(attempt="attempt-2", model="model-y"), ("profile-a",))
        self.assertIn("MODEL_MISMATCH", model.reasons)
        self.assertEqual(self.store.list(Placement), ())
        self.assertEqual([record["status"] for record in self.journal.decisions()], ["rejected", "rejected"])

    def test_unsupported_tool_and_unknown_resource_keep_capability_reasons(self):
        decision = self.planner.plan(task(tools=("shell", "browser")), ("profile-a",))
        self.assertEqual(decision.status, "rejected")
        self.assertIn("CAPABILITY:provider_observed:TOOL_NOT_SUPPORTED:browser", decision.reasons)
        del self.inventory.entries["connection-pool-a"]
        unknown = self.planner.plan(task(), ("profile-a",))
        self.assertIn("CAPABILITY_RESOURCE_UNKNOWN", unknown.reasons)
        self.assertEqual(self.store.list(Placement), ())

    def test_capacity_unknown_exhausted_and_stale_routes_wait(self):
        for route, code in ((make_route("pool-a", capacity=None, max_parallel=3), "CAPACITY_UNKNOWN"),
                            (make_route("pool-a", capacity=0, max_parallel=3), "CAPACITY_EXHAUSTED"),
                            (make_route("pool-a", freshness="stale", max_parallel=3), "ROUTE_NOT_FRESH")):
            with self.subTest(code=code):
                self.registry.put(route)
                decision = self.planner.plan(task(), ("profile-a",))
                self.assertEqual(decision.status, "wait")
                self.assertIn(code, decision.reasons)
        self.assertEqual(self.store.list(Placement), ())

    def test_pool_parallel_limit_waits_until_release(self):
        first = self.planner.plan(task(), ("profile-b",))
        self.assertEqual(first.status, "placed")
        second = self.planner.plan(task(attempt="attempt-2", project="project-2"), ("profile-b",))
        self.assertEqual(second.status, "wait")
        self.assertIn("POOL_PARALLEL_LIMIT", second.reasons)
        released = self.planner.release_placement(first.placement.value.id, first.placement.revision)
        self.assertEqual((released.value.state, released.revision), ("released", 2))
        retried = self.planner.plan(task(attempt="attempt-2", project="project-2"), ("profile-b",))
        self.assertEqual(retried.status, "placed")
        self.assertEqual(len(self.active_on("pool-b")), 1)

    def test_same_project_is_serial_unless_a_current_parallel_basis_is_given(self):
        self.assertEqual(self.planner.plan(task(), ("profile-a",)).status, "placed")
        serial = self.planner.plan(task(attempt="attempt-2"), ("profile-a",))
        self.assertEqual(serial.status, "wait")
        self.assertIn("SAME_PROJECT_SERIAL", serial.reasons)
        independent = self.planner.plan(task(attempt="attempt-3", project="project-2"), ("profile-a",))
        self.assertEqual(independent.status, "placed")
        stale = self.planner.plan(task(attempt="attempt-2"), ("profile-a",),
                                  parallel=ParallelQualification("parallel-evidence-1", 2))
        self.assertEqual(stale.status, "rejected")
        self.assertIn("PARALLEL_BASIS_STALE", stale.reasons)
        basis = ParallelQualification("parallel-evidence-1", 3)
        parallel = self.planner.plan(task(attempt="attempt-2"), ("profile-a",), parallel=basis)
        self.assertEqual(parallel.status, "placed")
        self.assertIn("SAME_PROJECT_PARALLEL_QUALIFIED", parallel.reasons)
        self.assertEqual(parallel.parallel_basis, basis)
        self.assertEqual(self.journal.decisions()[-1]["parallel_basis"],
                         {"evidence_ref": "parallel-evidence-1", "inventory_revision": 3})

    def test_parallel_basis_requires_a_qualified_route(self):
        self.registry.put(make_route("pool-a", level=QualificationLevel.CANDIDATE_FOR_QUALIFICATION,
                                     capacity=3, max_parallel=3))
        route = ("pool-a", "model-x")
        first = self.planner.plan(task(purpose="qualification", candidate_route=route), ("profile-a",))
        self.assertEqual(first.status, "placed")
        second = self.planner.plan(task(attempt="attempt-2", purpose="qualification", candidate_route=route),
                                   ("profile-a",), parallel=ParallelQualification("parallel-evidence-1", 3))
        self.assertEqual(second.status, "rejected")
        self.assertIn("PARALLEL_ROUTE_NOT_QUALIFIED", second.reasons)

    def test_candidate_route_is_only_used_by_explicit_qualification(self):
        self.registry.put(make_route("pool-a", level=QualificationLevel.CANDIDATE_FOR_QUALIFICATION,
                                     capacity=3, max_parallel=3))
        self.assertNotIn("pool-a", [route.pool_id for route in self.registry.operational_inventory(model="model-x")])
        operation = self.planner.plan(task(), ("profile-a",))
        self.assertEqual(operation.status, "rejected")
        self.assertIn("ROUTE_NOT_QUALIFIED", operation.reasons)
        mixed = self.planner.plan(task(attempt="attempt-2", candidate_route=("pool-a", "model-x")), ("profile-a",))
        self.assertEqual(mixed.reasons, ("CANDIDATE_ROUTE_NOT_OPERATIONAL",))
        missing = self.planner.plan(task(attempt="attempt-3", purpose="qualification"), ("profile-a",))
        self.assertEqual(missing.reasons, ("QUALIFICATION_ROUTE_REQUIRED",))
        qualification = self.planner.plan(
            task(attempt="attempt-4", project="project-4", purpose="qualification",
                 candidate_route=("pool-a", "model-x")), ("profile-b", "profile-a"))
        self.assertEqual(qualification.status, "placed")
        self.assertEqual(qualification.pool_id, "pool-a")
        self.assertIn("NOT_CANDIDATE_ROUTE", self.check(qualification, "profile-b").reasons)
        self.registry.put(make_route("pool-a", level=QualificationLevel.PROBE_VERIFIED, capacity=3, max_parallel=3))
        not_candidate = self.planner.plan(
            task(attempt="attempt-5", project="project-5", purpose="qualification",
                 candidate_route=("pool-a", "model-x")), ("profile-a",))
        self.assertIn("ROUTE_NOT_CANDIDATE", not_candidate.reasons)

    def test_same_attempt_is_idempotent_and_conflicting_reuse_is_rejected(self):
        first = self.planner.plan(task(), ("profile-a",))
        again = self.planner.plan(task(), ("profile-a", "profile-b"))
        self.assertEqual(again.status, "placed")
        self.assertEqual(again.placement, first.placement)
        self.assertEqual(again.reasons, ("IDEMPOTENT_EXISTING_PLACEMENT",))
        self.assertEqual(len(self.store.list(Placement)), 1)
        other = self.planner.plan(task(vm_task_id="task-other"), ("profile-a",))
        self.assertEqual(other.reasons, ("ATTEMPT_ALREADY_PLACED",))
        self.planner.release_placement(first.placement.value.id, 1, "failed")
        ended = self.planner.plan(task(), ("profile-a",))
        self.assertEqual(ended.reasons, ("ATTEMPT_ALREADY_PLACED",))
        self.assertEqual(len(self.store.list(Placement)), 1)

    def test_release_checks_revision_state_and_existence(self):
        placed = self.planner.plan(task(), ("profile-a",))
        identifier = placed.placement.value.id
        with self.assertRaises(RevisionConflict):
            self.planner.release_placement(identifier, 5)
        with self.assertRaises(PlacementError):
            self.planner.release_placement(identifier, 1, "assigned")
        self.assertEqual(self.planner.release_placement(identifier, 1, "unknown").value.state, "unknown")
        with self.assertRaises(PlacementError):
            self.planner.release_placement(identifier, 2, "unknown")
        self.assertEqual(self.planner.release_placement(identifier, 2).value.state, "released")
        with self.assertRaises(PlacementError):
            self.planner.release_placement(identifier, 3)
        with self.assertRaises(PlacementError):
            self.planner.release_placement("placement-missing", 1)

    def test_unknown_placement_keeps_capacity_and_serial_slot(self):
        placed = self.planner.plan(task(), ("profile-a",))
        self.planner.release_placement(placed.placement.value.id, 1, "unknown")
        same_project = self.planner.plan(task(vm_task_id="task-2", vm_attempt_id="attempt-2"), ("profile-a",))
        self.assertEqual(same_project.status, "wait")
        self.assertIn("SAME_PROJECT_SERIAL", same_project.reasons)
        self.planner.release_placement(placed.placement.value.id, 2)
        after = self.planner.plan(task(vm_task_id="task-2", vm_attempt_id="attempt-2"), ("profile-a",))
        self.assertEqual(after.status, "placed")

    def test_invalid_task_and_candidates_are_refused(self):
        for overrides in ({"role": "admin"}, {"tools": ("shell", "SHELL")}, {"purpose": "retry"},
                          {"candidate_route": ("pool-a",)}, {"vm_attempt_id": "../x"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(PlacementError):
                    task(**overrides)
        for candidates in ((), ["profile-a"], ("profile-a", "profile-a")):
            with self.assertRaises(PlacementError):
                self.planner.plan(task(), candidates)

    def test_journal_is_append_only_paginated_and_rejects_foreign_files(self):
        decisions = [self.planner.plan(task(attempt=f"attempt-{n}", project=f"project-{n}", effort="low"),
                                       ("profile-b",)) for n in range(3)]
        records = self.journal.decisions(limit=2)
        rest = self.journal.decisions(after_seq=records[-1]["seq"])
        self.assertEqual([record["decision_id"] for record in records + rest],
                         [decision.decision_id for decision in decisions])
        # closing(): a sqlite3 context manager only commits; Windows cannot delete an open file.
        with closing(sqlite3.connect(self.journal_path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE decisions SET status = 'placed'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM decisions")
        self.assertEqual(len(self.journal.decisions()), 3)
        with self.assertRaises(InvalidDatabase):
            PlacementJournal(self.runtime_path)
        with self.assertRaises(UnsafeStoragePath):
            PlacementJournal(ROOT / "src" / "placement-journal.sqlite3")
        forged = decisions[0]
        object.__setattr__(forged, "status", "placed")
        with self.assertRaises(PlacementError):
            self.journal.append(forged)

    def test_concurrent_planners_on_two_connections_do_not_exceed_capacity(self):
        stores = [RuntimeStore(self.runtime_path, timeout=10) for _ in range(2)]
        journals = [PlacementJournal(self.journal_path, timeout=10) for _ in range(2)]
        planners = [self.make_planner(store, journal) for store, journal in zip(stores, journals)]
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run(planner, ready):
            try:
                barrier.wait(timeout=10)
                results.append(planner.plan(ready, ("profile-b",)).status)
            except BaseException as error:  # reported below
                errors.append(error)

        threads = [threading.Thread(target=run, args=(planner, task(attempt=f"attempt-{n}", project=f"project-{n}")))
                   for n, planner in enumerate(planners)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["placed", "wait"])
        self.assertEqual(len(self.active_on("pool-b")), 1)
        self.assertEqual(len(self.journal.decisions()), 2)


if __name__ == "__main__":
    unittest.main()
