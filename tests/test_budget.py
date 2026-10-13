"""AIK-040 admission, reservation and settlement against disposable SQLite files.

Only synthetic pools, profiles and usage values are used. The clock is
injected. No provider is contacted and no credential is read. Passing on
Linux is development verification; Windows behaviour is NOT_RUN.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_budget import (  # noqa: E402
    UNKNOWN, AdmissionRequest, Amount, BudgetError, BudgetPolicy, BudgetStore, Usage, confirmed, estimated,
)
from aik_pool import QualificationLevel, RouteStatus  # noqa: E402
from aik_profiles import DirectorProfile, ExecutionChoice, RoleProfile  # noqa: E402
from aik_runtime import InvalidDatabase, RevisionConflict, RuntimeStore, UnsafeStoragePath  # noqa: E402


START = datetime(2030, 1, 1, tzinfo=timezone.utc)
CHOICE = ExecutionChoice("connection-1", "synthetic-model")
DIRECTOR = DirectorProfile("director-1", "workspace-1", CHOICE, 50)
WORKER = RoleProfile("worker-1", "workspace-1", "worker", CHOICE, 50)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def route_status(clock, *, pool_id="pool-1", model="synthetic-model", auth_kind="api_key", enabled=True,
                 max_parallel=4, capacity=4) -> RouteStatus:
    known = capacity is not None
    return RouteStatus(
        pool_id=pool_id, connection_id="connection-1", connection_revision=1, provider="synthetic",
        host="synthetic-host", auth_kind=auth_kind, model=model, tools=(), pool_enabled=enabled,
        configured_max_parallel=max_parallel, implementation_state="implemented", account_binding="bound",
        runtime_support="supported", account_isolation="isolated", governance="unknown",
        last_probe_at=iso(clock()), last_verified_at=iso(clock()), freshness="fresh", matrix_revision=1,
        qualification_matrix_revision=None, capacity_state="known" if known else "unknown",
        available_capacity=capacity, level=QualificationLevel.PROBE_VERIFIED,
        executable_now=bool(enabled and known and capacity > 0), placement_eligible=False,
    )


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-budget-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state" / "budget.sqlite3"
        self.clock = FakeClock()
        self.store = BudgetStore(self.path, clock=self.clock)

    @staticmethod
    def request(rid, *, pool_id="pool-1", max_seconds=60, **kwargs) -> AdmissionRequest:
        return AdmissionRequest(rid, pool_id, "synthetic-model", max_seconds, **kwargs)

    def admit(self, rid, profile=WORKER, route=None, **kwargs):
        return self.store.admit(self.request(rid, **kwargs), profile, route or route_status(self.clock))

    # ----------------------------------------------------------- concurrency
    def test_contending_admissions_take_the_last_scope_slot_only_once(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", max_concurrency=1))
        other = BudgetStore(self.path, clock=self.clock)
        barrier = threading.Barrier(2)
        results = []

        def run(store, rid):
            barrier.wait()
            results.append(store.admit(self.request(rid, scopes=("goal-1",)), WORKER, route_status(self.clock)))

        threads = [threading.Thread(target=run, args=(store, f"reservation-{index}"))
                   for index, store in enumerate((self.store, other))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(decision.status for decision in results), ["admitted", "wait"])
        waiting = next(decision for decision in results if decision.status == "wait")
        self.assertIn("CONCURRENCY_LIMIT@goal-1", waiting.reasons)
        self.assertIsNone(waiting.reservation_id)
        self.assertEqual(self.store.scope_status("goal-1").active, 1)

    def test_pool_configured_parallelism_limits_slots_until_settlement(self) -> None:
        route = lambda: route_status(self.clock, max_parallel=1, capacity=1)  # noqa: E731
        self.assertEqual(self.admit("r1", route=route()).status, "admitted")
        waiting = self.admit("r2", route=route())
        self.assertEqual(waiting.status, "wait")
        self.assertIn("CONCURRENCY_LIMIT@pool:pool-1", waiting.reasons)
        self.store.settle("r1", Usage())
        self.assertEqual(self.admit("r2", route=route()).status, "admitted")

    def test_route_state_is_used_and_unknown_capacity_is_not_treated_as_available(self) -> None:
        disabled = self.admit("r1", route=route_status(self.clock, enabled=False))
        self.assertEqual(disabled.status, "rejected")
        self.assertIn("POOL_DISABLED@pool:pool-1", disabled.reasons)
        unknown = self.admit("r2", route=route_status(self.clock, capacity=None))
        self.assertEqual((unknown.status, unknown.reasons), ("wait", ("CAPACITY_UNKNOWN@pool:pool-1",)))
        empty = self.admit("r3", route=route_status(self.clock, capacity=0))
        self.assertEqual(empty.reasons, ("CAPACITY_EXHAUSTED@pool:pool-1",))
        old_route = route_status(self.clock)
        self.clock.advance(400)
        stale = self.admit("r4", route=old_route)
        self.assertEqual(stale.reasons, ("ROUTE_STATUS_NOT_FRESH@pool:pool-1",))
        with self.assertRaises(BudgetError):
            self.admit("r5", route=route_status(self.clock, pool_id="pool-2"))
        missing = self.admit("r6", scopes=("goal-missing",))
        self.assertEqual(missing.status, "rejected")
        self.assertIn("SCOPE_POLICY_MISSING@goal-missing", missing.reasons)
        self.assertEqual(self.admit("r7").status, "admitted")
        with self.assertRaises(BudgetError):
            self.admit("r7")

    # --------------------------------------------------------- hard stops
    def test_director_calls_count_and_max_calls_is_a_hard_stop(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", max_calls=3))
        director = DirectorProfile("director-1", "workspace-1", CHOICE, 2)
        self.assertEqual(self.admit("r1", director, scopes=("goal-1",)).status, "admitted")
        self.store.settle("r1", Usage())
        self.assertEqual(self.admit("r2", director, scopes=("goal-1",)).status, "admitted")
        self.store.settle("r2", Usage())
        stopped = self.admit("r3", director, scopes=("goal-1",))
        self.assertEqual(stopped.status, "rejected")
        self.assertIn("CALL_LIMIT_REACHED@profile:director-1", stopped.reasons)
        self.assertEqual(stopped.role, "director")
        self.assertEqual(self.admit("r4", WORKER, scopes=("goal-1",)).status, "admitted")
        goal_stop = self.admit("r5", WORKER, scopes=("goal-1",))
        self.assertIn("CALL_LIMIT_REACHED@goal-1", goal_stop.reasons)
        status = self.store.scope_status("goal-1")
        self.assertEqual((status.calls, status.state), (3, "limit_reached"))
        self.assertEqual(self.store.profile_status(director).calls, 2)

    def test_absolute_deadline_is_a_deterministic_hard_stop(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", deadline=iso(START + timedelta(seconds=100))))
        too_long = self.admit("r1", scopes=("goal-1",), max_seconds=200)
        self.assertEqual(too_long.reasons, ("DEADLINE_INSUFFICIENT@goal-1",))
        self.assertEqual(self.admit("r2", scopes=("goal-1",), max_seconds=50).status, "admitted")
        self.clock.advance(100)
        passed = self.admit("r3", scopes=("goal-1",), max_seconds=1)
        self.assertEqual((passed.status, passed.reasons), ("rejected", ("DEADLINE_PASSED@goal-1",)))
        self.assertEqual(self.store.scope_status("goal-1").state, "limit_reached")
        self.assertTrue(self.store.reservation("r2").overdue)

    # ----------------------------------------------------------- cost holds
    def test_api_cost_is_reserved_and_requires_an_estimate(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", max_api_cost_cents=100))
        self.assertEqual(self.admit("r1", scopes=("goal-1",), api_cost_estimate_cents=60).status, "admitted")
        held = self.admit("r2", scopes=("goal-1",), api_cost_estimate_cents=60)
        self.assertEqual((held.status, held.reasons), ("wait", ("API_COST_HELD_BY_ACTIVE@goal-1",)))
        missing = self.admit("r3", scopes=("goal-1",))
        self.assertEqual(missing.reasons, ("API_COST_ESTIMATE_REQUIRED@goal-1",))
        excessive = self.admit("r4", scopes=("goal-1",), api_cost_estimate_cents=150)
        self.assertEqual(excessive.reasons, ("API_COST_INSUFFICIENT@goal-1",))
        self.store.settle("r1", Usage(api_cost_cents=confirmed(20)))
        self.assertEqual(self.admit("r2", scopes=("goal-1",), api_cost_estimate_cents=60).status, "admitted")

    def test_profile_budget_limit_applies_to_director_calls(self) -> None:
        director = DirectorProfile("director-1", "workspace-1", CHOICE, 10, budget_limit=50)
        over = self.admit("r1", director, api_cost_estimate_cents=60)
        self.assertEqual(over.reasons, ("API_COST_INSUFFICIENT@profile:director-1",))
        missing = self.admit("r2", director)
        self.assertEqual(missing.reasons, ("API_COST_ESTIMATE_REQUIRED@profile:director-1",))
        self.assertEqual(self.admit("r3", director, api_cost_estimate_cents=50).status, "admitted")

    # ------------------------------------------------------- 429 and quota
    def test_rate_limit_429_waits_until_retry_or_explicit_clear(self) -> None:
        self.store.record_rate_limit("pool-1", retry_after_seconds=30)
        waiting = self.admit("r1")
        self.assertEqual(waiting.status, "wait")
        self.assertEqual(waiting.reasons, ("RATE_LIMITED@pool:pool-1",))
        self.assertEqual(waiting.retry_at, iso(START + timedelta(seconds=30)))
        self.clock.advance(31)
        self.assertEqual(self.admit("r1").status, "admitted")
        self.store.record_rate_limit("pool-1")
        unknown = self.admit("r2")
        self.assertEqual((unknown.reasons, unknown.retry_at), (("RATE_LIMITED_RETRY_UNKNOWN@pool:pool-1",), None))
        self.clock.advance(3600)
        self.assertEqual(self.admit("r2").status, "wait")
        self.assertTrue(self.store.pool_status("pool-1").rate_limited)
        self.store.clear_rate_limit("pool-1")
        self.assertEqual(self.admit("r2").status, "admitted")

    def test_unknown_subscription_quota_remaining_blocks_and_is_never_derived_from_tokens(self) -> None:
        route = lambda: route_status(self.clock, auth_kind="native_subscription")  # noqa: E731
        self.assertEqual(self.admit("r1", route=route()).reasons, ("QUOTA_REMAINING_UNKNOWN@pool:pool-1",))
        self.store.record_quota("pool-1", "messages", None)
        self.assertEqual(self.admit("r1", route=route()).reasons, ("QUOTA_REMAINING_UNKNOWN@pool:pool-1",))
        resets = iso(START + timedelta(hours=1))
        self.store.record_quota("pool-1", "messages", 0, resets_at=resets)
        exhausted = self.admit("r1", route=route())
        self.assertEqual((exhausted.reasons, exhausted.retry_at), (("QUOTA_EXHAUSTED@pool:pool-1",), resets))
        self.store.record_quota("pool-1", "messages", 5, limit=40)
        self.assertEqual(self.admit("r1", route=route()).status, "admitted")
        self.store.settle("r1", Usage(input_tokens=confirmed(5000), output_tokens=confirmed(700),
                                      api_cost_cents=estimated(12)))
        quota = self.store.pool_status("pool-1").quota
        self.assertEqual([(item.unit, item.remaining) for item in quota], [("messages", 5)])
        profile = self.store.profile_status(WORKER)
        self.assertEqual((profile.quota_used, profile.quota_unreported_records), ((), 1))
        self.clock.advance(301)
        self.assertEqual(self.admit("r2", route=route()).reasons, ("QUOTA_OBSERVATION_STALE@pool:pool-1",))
        api_route = route_status(self.clock, pool_id="pool-2")
        self.assertEqual(self.store.admit(self.request("r3", pool_id="pool-2"), WORKER, api_route).status,
                         "admitted")

    # ----------------------------------------------------- unknown & late
    def test_missing_usage_values_stay_unknown_and_are_not_filled_with_zero(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", max_api_cost_cents=1000))
        self.admit("r1", scopes=("goal-1",), api_cost_estimate_cents=300)
        record = self.store.settle("r1", Usage(input_tokens=confirmed(120)))
        self.assertEqual(record.usage.api_cost_cents, UNKNOWN)
        status = self.store.scope_status("goal-1")
        self.assertEqual((status.api_cost.confirmed_cents, status.api_cost.estimated_cents), (0, 0))
        self.assertEqual((status.api_cost.unknown_held_cents, status.api_cost.unknown_records), (300, 1))
        self.assertEqual(status.api_cost.committed_cents, 300)
        self.assertEqual((status.tokens.observed, status.tokens.unknown_records), (120, 1))
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            stored = json.loads(connection.execute("SELECT document FROM usage_records").fetchone()[0])["usage"]
        self.assertEqual(stored["api_cost_cents"], {"basis": "unknown", "value": None})
        self.assertEqual(stored["output_tokens"], {"basis": "unknown", "value": None})
        self.assertEqual(stored["input_tokens"], {"basis": "confirmed", "value": 120})
        for bad in (lambda: Amount(0), lambda: Amount(-1, "confirmed"), lambda: Amount(True, "confirmed"),
                    lambda: Amount(None, "estimated"), lambda: Usage(quota_used=confirmed(3))):
            with self.assertRaises(BudgetError):
                bad()

    def test_delayed_billing_is_appended_without_changing_settled_state(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", max_api_cost_cents=60))
        self.admit("r1", scopes=("goal-1",), api_cost_estimate_cents=40)
        self.assertEqual(self.store.settle("r1", Usage(api_cost_cents=estimated(40))).kind, "settlement")
        late = self.store.settle("r1", Usage(api_cost_cents=confirmed(30)))
        self.assertEqual(late.kind, "late")
        self.assertEqual(self.store.reservation("r1").state, "settled")
        self.assertEqual([item.kind for item in self.store.usage_records("r1")], ["settlement", "late"])
        status = self.store.scope_status("goal-1")
        self.assertEqual((status.api_cost.estimated_cents, status.api_cost.confirmed_cents), (40, 30))
        self.assertEqual(status.state, "over_limit")
        stopped = self.admit("r2", scopes=("goal-1",), api_cost_estimate_cents=1)
        self.assertEqual((stopped.status, stopped.reasons), ("rejected", ("API_COST_LIMIT_REACHED@goal-1",)))

    def test_release_frees_the_slot_and_later_usage_is_a_late_record(self) -> None:
        self.admit("r1", api_cost_estimate_cents=10)
        self.assertEqual(self.store.release("r1").kind, "release")
        with self.assertRaises(BudgetError):
            self.store.release("r1")
        late = self.store.settle("r1", Usage(api_cost_cents=confirmed(3)))
        self.assertEqual((late.kind, self.store.reservation("r1").state), ("late", "released"))
        self.assertEqual(self.store.profile_status(WORKER).api_cost.confirmed_cents, 3)
        with self.assertRaises(BudgetError):
            self.store.settle("missing-reservation", Usage())

    def test_token_stop_is_best_effort_on_observed_tokens_only(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", token_stop=100))
        self.admit("r1", scopes=("goal-1",))
        self.store.settle("r1", Usage(input_tokens=confirmed(60)))
        self.assertEqual(self.admit("r2", scopes=("goal-1",)).status, "admitted")
        self.store.settle("r2", Usage(output_tokens=estimated(50)))
        stopped = self.admit("r3", scopes=("goal-1",))
        self.assertEqual(stopped.reasons, ("TOKEN_STOP_REACHED@goal-1",))
        tokens = self.store.scope_status("goal-1").tokens
        self.assertEqual((tokens.observed, tokens.unknown_records, tokens.policy), (110, 2, "best_effort_observed"))

    # --------------------------------------------------------------- storage
    def test_settlement_tables_are_append_only_at_the_database_level(self) -> None:
        self.admit("r1")
        self.store.settle("r1", Usage())
        self.store.record_quota("pool-1", "messages", 3)
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in ("UPDATE usage_records SET kind = 'late'", "DELETE FROM usage_records",
                              "DELETE FROM reservations", "UPDATE reservations SET state = 'reserved'",
                              "UPDATE decisions SET status = 'rejected'", "DELETE FROM observations",
                              "DELETE FROM decisions"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement)
        self.assertEqual(self.store.reservation("r1").state, "settled")

    def test_persistence_decisions_and_policy_revisions(self) -> None:
        self.store.set_policy(BudgetPolicy("goal-1", max_concurrency=1))
        with self.assertRaises(RevisionConflict):
            self.store.set_policy(BudgetPolicy("goal-1", max_concurrency=2))
        self.assertEqual(self.store.set_policy(BudgetPolicy("goal-1", max_concurrency=2), 1).revision, 2)
        self.admit("r1", scopes=("goal-1",))
        self.admit("r2", scopes=("goal-1",))
        self.admit("r3", scopes=("goal-1",))
        reopened = BudgetStore(self.path, clock=self.clock)
        self.assertEqual(reopened.policy("goal-1").policy.max_concurrency, 2)
        self.assertEqual(reopened.scope_status("goal-1").active, 2)
        self.assertEqual([item["status"] for item in reopened.decisions()], ["admitted", "admitted", "wait"])
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0x41494B42)

    def test_foreign_runtime_and_repository_destinations_are_rejected(self) -> None:
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        runtime = self.root / "runtime.sqlite3"
        RuntimeStore(runtime)
        for path in (foreign, runtime):
            with self.subTest(source=path.name):
                before = path.read_bytes()
                with self.assertRaises(InvalidDatabase):
                    BudgetStore(path, clock=self.clock)
                self.assertEqual(path.read_bytes(), before)
        inside = ROOT / "aik-budget-test.sqlite3"
        with self.assertRaises(UnsafeStoragePath):
            BudgetStore(inside, clock=self.clock)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()
