"""AIK-034 lease manager against disposable runtime and budget SQLite files.

All pools, placements, profiles and routes are synthetic. The clock is
injected. No provider is contacted, no credential is read, and no VM/AGS
installation is used. Passing on Linux is development verification; Windows
behaviour is NOT_RUN.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_budget import BudgetStore, Usage, confirmed  # noqa: E402
from aik_lease import (  # noqa: E402
    InvalidTransition, LeaseError, LeaseExpired, LeaseManager, StaleLease, lease_id_for,
)
from aik_pool import QualificationLevel, RouteStatus  # noqa: E402
from aik_profiles import ExecutionChoice, RoleProfile  # noqa: E402
from aik_runtime import ExecutionProfile, Lease, Placement, Pool, RuntimeStore  # noqa: E402
from aik_runtime.models import identifier  # noqa: E402


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
TTL = 60
WORKER = RoleProfile("worker-1", "workspace-1", "worker", ExecutionChoice("connection-1", "synthetic-model"), 50)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


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


class LeaseManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-lease-test-")
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name) / "runtime"
        self.runtime_path = directory / "runtime.sqlite3"
        self.budget_path = directory / "budget.sqlite3"
        self.clock = FakeClock()
        self.store = RuntimeStore(self.runtime_path, timeout=20)
        self.budget = self.new_budget()
        self.manager = LeaseManager(self.store, self.budget, ttl_seconds=TTL, clock=self.clock)

    def new_budget(self) -> BudgetStore:
        return BudgetStore(self.budget_path, clock=self.clock, timeout=20, route_max_age_seconds=3600)

    def seed(self, *, max_parallel: int = 1, placements=("placement-1",)) -> None:
        with self.store.transaction() as tx:
            tx.save(Pool("pool-1", "synthetic", "connection-1", max_parallel=max_parallel, enabled=True))
            tx.save(ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model"))
            for number, placement_id in enumerate(placements, 1):
                tx.save(Placement(placement_id, "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                                  f"vm-task-{number}", f"vm-attempt-{number}", state="assigned"))

    def reserve(self, placement_id: str = "placement-1", manager: LeaseManager | None = None, **options):
        options.setdefault("route", make_route())
        return (manager or self.manager).reserve(placement_id, worker_id="worker-1", budget_profile=WORKER,
                                                 max_seconds=60, **options)

    def lease(self, lease_id: str) -> Lease:
        return self.store.get(Lease, lease_id).value

    def budget_state(self, lease_id: str) -> str | None:
        reservation = self.budget.reservation(lease_id)
        return None if reservation is None else reservation.state

    # ------------------------------------------------------------------ tests
    def test_grant_links_lease_to_budget_reservation_and_vm_ids(self) -> None:
        self.seed()
        result = self.reserve()
        self.assertEqual(result.status, "granted")
        lease = result.lease.value
        self.assertEqual(lease.id, "lease-placement-1-1")
        self.assertEqual((lease.placement_id, lease.pool_id, lease.worker_id), ("placement-1", "pool-1", "worker-1"))
        self.assertEqual((lease.fencing_token, lease.state, lease.provider_job_id), (1, "reserved", None))
        self.assertEqual(lease.expires_at, iso(START + timedelta(seconds=TTL)))
        self.assertEqual(self.store.get(Lease, lease.id), result.lease)
        self.assertEqual(self.budget_state(lease.id), "reserved")
        placement = self.store.get(Placement, lease.placement_id).value
        self.assertEqual((placement.vm_task_id, placement.vm_attempt_id), ("vm-task-1", "vm-attempt-1"))

    def test_concurrent_reserves_from_separate_stores_never_exceed_pool_capacity(self) -> None:
        placements = tuple(f"placement-{n}" for n in range(1, 7))
        self.seed(max_parallel=2, placements=placements)
        managers = [LeaseManager(RuntimeStore(self.runtime_path, timeout=20), self.new_budget(),
                                 ttl_seconds=TTL, clock=self.clock) for _ in placements]
        barrier = threading.Barrier(len(placements))
        results, errors = [], []

        def run(manager: LeaseManager, placement_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                results.append(self.reserve(placement_id, manager=manager))
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(repr(error))

        threads = [threading.Thread(target=run, args=pair) for pair in zip(managers, placements)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(placements))
        granted = [result for result in results if result.status == "granted"]
        waiting = [result for result in results if result.status == "wait"]
        self.assertEqual(len(granted), 2)
        self.assertEqual(len(waiting), 4)
        self.assertTrue(all(result.reasons == ("POOL_CAPACITY",) for result in waiting))
        holding = [entry for entry in self.store.list(Lease, limit=1000) if entry.value.state == "reserved"]
        self.assertEqual(len(holding), 2)
        self.assertEqual(self.budget.pool_status("pool-1").active, 2)

    def test_duplicate_holder_is_rejected_and_next_lease_gets_next_token(self) -> None:
        self.seed(max_parallel=2)
        first = self.reserve()
        duplicate = self.reserve()
        self.assertEqual((duplicate.status, duplicate.reasons), ("rejected", ("LEASE_ALREADY_HELD",)))
        self.manager.release(first.lease.value.id, 1, dispatched=False, provider_terminal=False)
        self.assertEqual(self.budget_state(first.lease.value.id), "released")
        second = self.reserve()
        self.assertEqual(second.status, "granted")
        self.assertEqual((second.lease.value.id, second.lease.value.fencing_token), ("lease-placement-1-2", 2))

    def test_stale_and_superseded_owner_updates_are_rejected(self) -> None:
        self.seed()
        first = self.reserve()
        first_id = first.lease.value.id
        with self.assertRaises(StaleLease):
            self.manager.activate(first_id, 2, "job-forged")
        with self.assertRaises(StaleLease):
            self.manager.release(first_id, 7, dispatched=False, provider_terminal=False)
        self.manager.release(first_id, 1, dispatched=False, provider_terminal=False)
        second = self.reserve()
        for call in (lambda: self.manager.renew(first_id, 1),
                     lambda: self.manager.activate(first_id, 1, "job-stale"),
                     lambda: self.manager.release(first_id, 1, dispatched=False, provider_terminal=False)):
            with self.assertRaises(StaleLease):
                call()
        self.assertEqual(self.lease(first_id).state, "released")
        active = self.manager.activate(second.lease.value.id, 2, "job-current")
        self.assertEqual((active.value.state, active.value.provider_job_id), ("active", "job-current"))

    def test_ttl_expiry_before_dispatch_releases_budget_hold(self) -> None:
        self.seed()
        lease_id = self.reserve().lease.value.id
        self.assertEqual(self.manager.expire_due(), ())
        self.clock.advance(TTL)
        changed = self.manager.expire_due()
        self.assertEqual([(entry.value.id, entry.value.state) for entry in changed], [(lease_id, "expired")])
        self.assertEqual(self.budget_state(lease_id), "released")
        again = self.reserve()
        self.assertEqual((again.status, again.lease.value.fencing_token), ("granted", 2))

    def test_expiry_with_provider_job_is_unknown_and_blocks_relaunch(self) -> None:
        self.seed(placements=("placement-1", "placement-2"))
        lease_id = self.reserve().lease.value.id
        self.manager.activate(lease_id, 1, "job-1")
        self.clock.advance(TTL + 1)
        changed = self.manager.expire_due()
        self.assertEqual([entry.value.state for entry in changed], ["unknown"])
        self.assertEqual(self.budget_state(lease_id), "reserved")
        relaunch = self.reserve("placement-1")
        self.assertEqual((relaunch.status, relaunch.reasons, relaunch.lease_id),
                         ("rejected", ("PREVIOUS_LEASE_UNKNOWN",), None))
        other = self.reserve("placement-2")
        self.assertEqual((other.status, other.reasons), ("wait", ("POOL_CAPACITY",)))
        with self.assertRaises(InvalidTransition):
            self.manager.renew(lease_id, 1)
        still = self.manager.resolve_unknown(lease_id, 1, provider_terminal=False)
        self.assertEqual((still.value.state, still.revision), ("unknown", 3))
        resolved = self.manager.resolve_unknown(lease_id, 1, provider_terminal=True)
        self.assertEqual(resolved.value.state, "released")
        # Settlement is not performed by the lease manager.
        self.assertEqual(self.budget_state(lease_id), "reserved")
        self.assertEqual(self.budget.usage_records(lease_id), ())
        self.budget.settle(lease_id, Usage(api_cost_cents=confirmed(5)))
        again = self.reserve("placement-1")
        self.assertEqual((again.status, again.lease.value.fencing_token), ("granted", 2))

    def test_dispatch_intent_before_receipt_survives_crash_as_unknown(self) -> None:
        self.seed()
        lease_id = self.reserve().lease.value.id
        intent = self.manager.activate(lease_id, 1)  # written before the provider call
        self.assertEqual((intent.value.state, intent.value.provider_job_id), ("active", None))
        # Crash between the provider call and its receipt: expiry must not treat it as "no dispatch".
        self.clock.advance(TTL + 1)
        changed = self.manager.expire_due()
        self.assertEqual([entry.value.state for entry in changed], ["unknown"])
        self.assertEqual(self.budget_state(lease_id), "reserved")

    def test_receipt_fills_the_job_id_of_an_active_intent(self) -> None:
        self.seed()
        lease_id = self.reserve().lease.value.id
        self.manager.activate(lease_id, 1)
        filled = self.manager.activate(lease_id, 1, "job-1")
        self.assertEqual((filled.value.state, filled.value.provider_job_id), ("active", "job-1"))
        with self.assertRaises(InvalidTransition):
            self.manager.activate(lease_id, 1, "job-2")
        draining = self.manager.release(lease_id, 1, dispatched=True, provider_terminal=False)
        self.assertEqual(draining.value.state, "draining")

    def test_release_branches_distinguish_dispatch_and_provider_termination(self) -> None:
        self.seed(placements=("placement-1", "placement-2"))
        first = self.reserve().lease.value.id
        released = self.manager.release(first, 1, dispatched=False, provider_terminal=False)
        self.assertEqual(released.value.state, "released")
        self.assertEqual(self.budget_state(first), "released")

        second = self.reserve().lease.value.id
        self.manager.activate(second, 2, "job-2")
        with self.assertRaises(InvalidTransition):
            self.manager.release(second, 2, dispatched=False, provider_terminal=False)
        draining = self.manager.release(second, 2, dispatched=True, provider_terminal=False)
        self.assertEqual(draining.value.state, "draining")
        blocked = self.reserve("placement-2")
        self.assertEqual((blocked.status, blocked.reasons), ("wait", ("POOL_CAPACITY",)))
        unchanged = self.manager.release(second, 2, dispatched=True, provider_terminal=False)
        self.assertEqual(unchanged, draining)
        done = self.manager.release(second, 2, dispatched=True, provider_terminal=True)
        self.assertEqual(done.value.state, "released")
        self.assertEqual(self.budget_state(second), "reserved")
        self.assertEqual(self.budget.usage_records(second), ())

        third = self.reserve("placement-2")
        self.assertEqual(third.status, "granted")
        with self.assertRaises(InvalidTransition):
            self.manager.release(third.lease.value.id, 1, dispatched=True, provider_terminal=True)
        with self.assertRaises(LeaseError):
            self.manager.release(third.lease.value.id, 1, dispatched="no", provider_terminal=True)
        self.assertEqual(self.lease(third.lease.value.id).state, "reserved")

    def test_renew_extends_only_before_expiry(self) -> None:
        self.seed()
        lease_id = self.reserve().lease.value.id
        self.clock.advance(30)
        renewed = self.manager.renew(lease_id, 1)
        self.assertEqual((renewed.value.expires_at, renewed.revision), (iso(START + timedelta(seconds=90)), 2))
        self.clock.advance(60)
        with self.assertRaises(LeaseExpired):
            self.manager.renew(lease_id, 1)
        with self.assertRaises(LeaseExpired):
            self.manager.activate(lease_id, 1, "job-late")
        self.assertEqual(self.lease(lease_id).state, "reserved")

    def test_budget_wait_and_rejection_mark_lease_failed(self) -> None:
        self.seed()
        self.budget.record_rate_limit("pool-1", 120)
        waited = self.reserve()
        self.assertEqual(waited.status, "wait")
        self.assertIn("RATE_LIMITED@pool:pool-1", waited.reasons)
        self.assertEqual(waited.retry_at, iso(START + timedelta(seconds=120)))
        self.assertIsNone(waited.lease)
        self.assertEqual(self.lease(waited.lease_id).state, "failed")
        self.assertIsNone(self.budget.reservation(waited.lease_id))

        self.budget.clear_rate_limit("pool-1")
        rejected = self.reserve(scopes=("goal-missing",))
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("SCOPE_POLICY_MISSING@goal-missing", rejected.reasons)
        self.assertEqual(self.lease(rejected.lease_id).state, "failed")

        granted = self.reserve()
        self.assertEqual((granted.status, granted.lease.value.id), ("granted", "lease-placement-1-3"))

    def test_budget_exception_marks_lease_failed_and_reconcile_releases_orphan_hold(self) -> None:
        self.seed()
        with mock.patch.object(self.budget, "admit", side_effect=RuntimeError("synthetic budget failure")):
            with self.assertRaises(RuntimeError):
                self.reserve()
        self.assertEqual(self.lease("lease-placement-1-1").state, "failed")
        self.assertIsNone(self.budget.reservation("lease-placement-1-1"))

        real_admit = self.budget.admit

        def admit_then_fail(*args, **kwargs):
            real_admit(*args, **kwargs)
            raise RuntimeError("synthetic failure after budget commit")

        with mock.patch.object(self.budget, "admit", side_effect=admit_then_fail):
            with self.assertRaises(RuntimeError):
                self.reserve()
        self.assertEqual(self.lease("lease-placement-1-2").state, "failed")
        self.assertEqual(self.budget_state("lease-placement-1-2"), "reserved")
        report = self.manager.reconcile_reservations()
        self.assertEqual(report.failed_leases, ())
        self.assertEqual(report.released_reservations, ("lease-placement-1-2",))
        self.assertEqual(self.budget_state("lease-placement-1-2"), "released")

    def test_reconcile_fails_lease_orphaned_by_crash_before_budget_admission(self) -> None:
        self.seed()
        # A crashed process stored its lease but never reached the budget step.
        self.store.save(Lease("lease-orphan", "placement-1", "pool-1", "worker-crashed", 1,
                              iso(START + timedelta(seconds=TTL))), expected_revision=0)
        blocked = self.reserve()
        self.assertEqual(blocked.reasons, ("LEASE_ALREADY_HELD",))
        report = self.manager.reconcile_reservations()
        self.assertEqual(report.failed_leases, ("lease-orphan",))
        self.assertEqual(self.lease("lease-orphan").state, "failed")
        granted = self.reserve()
        self.assertEqual(granted.lease.value.id, "lease-placement-1-2")
        again = self.manager.reconcile_reservations()
        self.assertEqual((again.failed_leases, again.released_reservations), ((), ()))
        self.assertEqual(self.lease(granted.lease.value.id).state, "reserved")

    def test_expiry_during_budget_admission_does_not_grant(self) -> None:
        self.seed()
        real_admit = self.budget.admit

        def expire_first(*args, **kwargs):
            self.clock.advance(TTL + 1)
            self.manager.expire_due()
            return real_admit(*args, **kwargs)

        with mock.patch.object(self.budget, "admit", side_effect=expire_first):
            result = self.reserve()
        self.assertEqual((result.status, result.reasons), ("rejected", ("LEASE_LOST_BEFORE_GRANT",)))
        self.assertEqual(self.lease(result.lease_id).state, "expired")
        self.assertEqual(self.budget_state(result.lease_id), "released")

    def test_placement_and_route_preconditions_write_nothing(self) -> None:
        self.seed()
        with self.store.transaction() as tx:
            tx.save(Placement("placement-pending", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                              "vm-task-9", "vm-attempt-9"))
        self.assertEqual(self.reserve("placement-pending").reasons, ("PLACEMENT_NOT_ASSIGNED",))
        self.assertEqual(self.reserve("placement-absent").reasons, ("PLACEMENT_UNKNOWN",))
        mismatch = self.reserve("placement-1", route=make_route("pool-2"))
        self.assertEqual((mismatch.status, mismatch.reasons), ("rejected", ("ROUTE_POOL_MISMATCH",)))
        self.assertEqual(self.store.list(Lease), ())
        self.assertEqual(self.budget.decisions(), ())
        with self.assertRaises(LeaseError):
            self.manager.reserve("placement-1", worker_id="worker-1", route=object(),
                                 budget_profile=WORKER, max_seconds=60)
        with self.assertRaises(LeaseError):
            self.manager.reserve("placement-1", worker_id="worker-1", route=make_route(),
                                 budget_profile="not-a-profile", max_seconds=60)
        self.assertEqual(self.store.list(Lease), ())

    def test_lease_id_format_is_deterministic_and_bounded(self) -> None:
        self.assertEqual(lease_id_for("placement-1", 3), "lease-placement-1-3")
        long_id = lease_id_for("p" * 128, 7)
        self.assertTrue(long_id.startswith("lease:"))
        identifier(long_id)
        self.assertEqual(long_id, lease_id_for("p" * 128, 7))
        self.assertNotEqual(long_id, lease_id_for("p" * 128, 8))
        with self.assertRaises(LeaseError):
            LeaseManager(self.store, self.budget, ttl_seconds=0)


    def test_mark_unknown_holds_only_a_dispatched_live_lease(self) -> None:
        self.seed()
        lease = self.reserve().lease.value
        with self.assertRaises(InvalidTransition) as raised:
            self.manager.mark_unknown(lease.id, lease.fencing_token)
        self.assertEqual(str(raised.exception), "NO_DISPATCH_RECORDED")
        activated = self.manager.activate(lease.id, lease.fencing_token)
        with self.assertRaises(InvalidTransition) as raised:
            self.manager.mark_unknown(lease.id, lease.fencing_token, expected_revision=activated.revision - 1)
        self.assertEqual(str(raised.exception), "LEASE_CHANGED")
        with self.assertRaises(StaleLease):
            self.manager.mark_unknown(lease.id, lease.fencing_token + 1)
        marked = self.manager.mark_unknown(lease.id, lease.fencing_token, expected_revision=activated.revision)
        self.assertEqual(marked.value.state, "unknown")
        self.assertEqual(self.budget_state(lease.id), "reserved")
        again = self.manager.mark_unknown(lease.id, lease.fencing_token)
        self.assertEqual(again.revision, marked.revision)
        released = self.manager.resolve_unknown(lease.id, lease.fencing_token, provider_terminal=True)
        self.assertEqual(released.value.state, "released")
        with self.assertRaises(InvalidTransition) as raised:
            self.manager.mark_unknown(lease.id, lease.fencing_token)
        self.assertEqual(str(raised.exception), "LEASE_NOT_LIVE")

if __name__ == "__main__":
    unittest.main()
