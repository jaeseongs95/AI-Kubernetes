"""AIK-019 ResourcePool registry tests with a shared synthetic fake adapter.

No provider is called and no credential value is used. Most qualification
attempts are rejection cases. The qualified transition and its invalidation
are exercised only with an in-memory synthetic verifier (``lambda e: True``)
and a record labeled ``evidence_kind="live"`` inside this process. That is a
unit test of the registry logic; it is NOT live evidence, is never written
anywhere, and does not claim that any real route is qualified.
Running this on Linux is development verification only.
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_pool import (  # noqa: E402
    Freshness, Governance, PoolRegistryError, QualificationEvidence,
    QualificationLevel, QualificationRejected, ResourcePoolRegistry, UnknownRoute,
)
from aik_provider import (  # noqa: E402
    AccountIsolation, AdapterCapabilities, AuthKind, AuthObservation, AuthState,
    ConnectionBinding, Support,
)
from aik_runtime import Pool  # noqa: E402


MODEL = "synthetic-model"
SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"
SUB = ConnectionBinding("synthetic", "fake-host", "conn-sub", 1, AuthKind.NATIVE_SUBSCRIPTION,
                        "host-session://synthetic/session-a", "a" * 64)
API = ConnectionBinding("synthetic", "fake-host", "conn-api", 1, AuthKind.API_KEY,
                        "secret://local/api-key-ref", "b" * 64)
SUB2 = ConnectionBinding("synthetic", "fake-host", "conn-sub-2", 1, AuthKind.NATIVE_SUBSCRIPTION,
                         "host-session://synthetic/session-b", "c" * 64)
POOL_SUB = Pool("pool-sub", "synthetic", "conn-sub", max_parallel=2, enabled=True)
POOL_API = Pool("pool-api", "synthetic", "conn-api", max_parallel=4, enabled=True)
POOL_SUB2 = Pool("pool-sub-2", "synthetic", "conn-sub-2", max_parallel=1, enabled=True)


def api_binding(revision: int, *, runtime: str = "fake-host", fingerprint: str = "b" * 64) -> ConnectionBinding:
    return ConnectionBinding("synthetic", runtime, "conn-api", revision, AuthKind.API_KEY,
                             "secret://local/api-key-ref", fingerprint)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.fail = False

    def __call__(self) -> datetime:
        if self.fail:
            raise RuntimeError(SECRET_MARKER)
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeProvider:
    """Shared synthetic fake adapter: only read-only calls answer; effects fail."""

    def __init__(self) -> None:
        self.state: dict[str, dict] = {}
        self.effects: list[str] = []

    def configure(self, binding: ConnectionBinding, *, auth=AuthState.AUTHENTICATED, fingerprint=None,
                  isolation=AccountIsolation.ISOLATED, implementation="implemented",
                  job_start=Support.SUPPORTED, describe_error=None, auth_error=None) -> None:
        self.state[binding.connection_id] = {
            "auth": auth, "fingerprint": fingerprint, "isolation": isolation,
            "implementation": implementation, "job_start": job_start, "describe_error": describe_error,
            "auth_error": auth_error,
        }

    def describe(self, binding):
        state = self.state[binding.connection_id]
        if state["describe_error"] is not None:
            raise state["describe_error"]
        native = binding.auth_kind is AuthKind.NATIVE_SUBSCRIPTION
        return AdapterCapabilities(
            binding.provider, binding.runtime, binding.auth_kind, state["isolation"],
            Support.SUPPORTED, Support.SUPPORTED if native else Support.UNSUPPORTED, Support.SUPPORTED,
            state["job_start"], Support.SUPPORTED, Support.SUPPORTED,
            implementation_state=state["implementation"],
        )

    def auth_status(self, binding):
        state = self.state[binding.connection_id]
        if state["auth_error"] is not None:
            raise state["auth_error"]
        authenticated = state["auth"] is AuthState.AUTHENTICATED
        fingerprint = (state["fingerprint"] or binding.account_fingerprint) if authenticated else None
        return AuthObservation(binding.connection_id, binding.revision, fingerprint, state["auth"])

    def _effect(self, name):
        self.effects.append(name)
        raise AssertionError("registry must not cause provider effects")

    def begin_sign_in(self, binding):
        return self._effect("begin_sign_in")

    def sign_out(self, binding):
        return self._effect("sign_out")

    def start_job(self, request):
        return self._effect("start_job")

    def observe_job(self, request, receipt):
        return self._effect("observe_job")

    def interrupt_job(self, request, receipt):
        return self._effect("interrupt_job")


class ResourcePoolTests(unittest.TestCase):
    def make_registry(self, **options) -> ResourcePoolRegistry:
        self.clock = FakeClock()
        self.provider = FakeProvider()
        self.capacity: dict[str, object] = {"pool-sub": 2, "pool-api": 3, "pool-sub-2": 1}

        def capacity_probe(pool_id, connection_id, model):
            value = self.capacity.get(pool_id)
            if isinstance(value, Exception):
                raise value
            return value

        registry = ResourcePoolRegistry(matrix_revision=1, ttl_seconds=60, clock=self.clock,
                                        capacity_probe=capacity_probe, **options)
        for binding in (SUB, API, SUB2):
            self.provider.configure(binding)
        registry.register(POOL_SUB, SUB, self.provider, MODEL, tools=("shell", "editor"))
        registry.register(POOL_API, API, self.provider, MODEL, tools=("editor",))
        provider = self.provider
        self.addCleanup(lambda: self.assertEqual(provider.effects, []))
        return registry

    def evidence(self, binding, pool_id, **overrides) -> QualificationEvidence:
        values = dict(pool_id=pool_id, model=MODEL, connection_id=binding.connection_id,
                      connection_revision=binding.revision, account_fingerprint=binding.account_fingerprint,
                      runtime=binding.runtime, matrix_revision=1, evidence_ref="evidence-synthetic-1",
                      evidence_kind="synthetic_fixture", verdict="PASS")
        values.update(overrides)
        return QualificationEvidence(**values)

    def qualified_registry(self) -> ResourcePoolRegistry:
        """Logic-only qualified route: in-memory synthetic verifier, never live evidence."""
        registry = self.make_registry(qualification_verifier=lambda evidence: True)
        registry.probe("pool-api", MODEL)
        registry.set_governance("pool-api", MODEL, Governance.APPROVED)
        status = registry.record_qualification(self.evidence(API, "pool-api", evidence_kind="live"))
        self.assertEqual(status.level, QualificationLevel.QUALIFIED)
        return registry

    def inventory_ids(self, registry) -> list[str]:
        return [status.pool_id for status in registry.operational_inventory()]

    # 1. pool_id/connection_id separation, separate subscription/API routes for one model
    def test_same_model_subscription_and_api_routes_are_separately_queryable(self) -> None:
        registry = self.make_registry()
        routes = registry.routes(model=MODEL)
        self.assertEqual([(r.pool_id, r.connection_id, r.auth_kind) for r in routes], [
            ("pool-api", "conn-api", "api_key"), ("pool-sub", "conn-sub", "native_subscription")])
        self.assertEqual(registry.routes(connection_id="conn-sub")[0].pool_id, "pool-sub")
        self.assertEqual(registry.routes(pool_id="pool-api")[0].connection_id, "conn-api")
        self.assertEqual(registry.routes(model="other-model"), ())
        self.assertEqual(registry.routes(pool_id="pool-sub")[0].tools, ("shell", "editor"))
        self.assertEqual(routes[0].host, "fake-host")

    # 2. separate fields, no executable advertisement from implementation alone
    def test_status_exposes_independent_fields_and_registration_is_not_executable(self) -> None:
        registry = self.make_registry()
        names = {field.name for field in fields(type(registry.status("pool-sub", MODEL)))}
        for name in ("implementation_state", "account_binding", "runtime_support", "governance",
                     "last_verified_at", "matrix_revision", "level", "executable_now"):
            self.assertIn(name, names)
        status = registry.status("pool-sub", MODEL)
        self.assertEqual(status.level, QualificationLevel.UNKNOWN)
        self.assertEqual(status.freshness, Freshness.UNKNOWN.value)
        self.assertIsNone(status.last_verified_at)
        self.assertFalse(status.executable_now)

    def test_implemented_without_authentication_is_not_executable(self) -> None:
        registry = self.make_registry()
        self.provider.configure(SUB, auth=AuthState.UNAUTHENTICATED)
        status = registry.probe("pool-sub", MODEL)
        self.assertEqual(status.implementation_state, "implemented")
        self.assertEqual(status.account_binding, "unauthenticated")
        self.assertEqual(status.runtime_support, "supported")
        self.assertEqual(status.level, QualificationLevel.IMPLEMENTED)
        self.assertFalse(status.executable_now)

    def test_pending_login_and_unknown_native_isolation_are_not_executable(self) -> None:
        registry = self.make_registry()
        self.provider.configure(SUB, auth=AuthState.PENDING)
        self.assertEqual(registry.probe("pool-sub", MODEL).account_binding, "pending")
        self.provider.configure(SUB, isolation=AccountIsolation.UNKNOWN)
        status = registry.probe("pool-sub", MODEL)
        self.assertEqual((status.account_binding, status.runtime_support), ("bound", "unknown"))
        self.assertEqual(status.level, QualificationLevel.IMPLEMENTED)
        self.assertFalse(status.executable_now)

    def test_not_implemented_adapter_is_reported_as_such(self) -> None:
        registry = self.make_registry()
        self.provider.configure(API, implementation="not_implemented")
        status = registry.probe("pool-api", MODEL)
        self.assertEqual(status.level, QualificationLevel.NOT_IMPLEMENTED)
        self.assertFalse(status.executable_now)

    def test_fresh_probe_and_governance_reach_candidate_but_not_operational_inventory(self) -> None:
        registry = self.make_registry()
        status = registry.probe("pool-api", MODEL)
        self.assertEqual(status.level, QualificationLevel.PROBE_VERIFIED)
        self.assertTrue(status.executable_now)
        self.assertFalse(status.placement_eligible)
        self.assertEqual(status.last_verified_at, "2030-01-01T00:00:00Z")
        status = registry.set_governance("pool-api", MODEL, Governance.APPROVED)
        self.assertEqual(status.level, QualificationLevel.CANDIDATE_FOR_QUALIFICATION)
        self.assertEqual(registry.operational_inventory(), ())
        status = registry.set_governance("pool-api", MODEL, Governance.REJECTED)
        self.assertEqual(status.level, QualificationLevel.PROBE_VERIFIED)

    def test_disabled_pool_is_not_executable_even_when_probe_verified(self) -> None:
        registry = self.make_registry()
        registry.update_pool(Pool("pool-api", "synthetic", "conn-api", max_parallel=4, enabled=False))
        status = registry.probe("pool-api", MODEL)
        self.assertEqual(status.level, QualificationLevel.PROBE_VERIFIED)
        self.assertFalse(status.executable_now)

    def test_unsupported_selected_route_does_not_affect_the_other_route(self) -> None:
        registry = self.make_registry()
        self.provider.configure(SUB, job_start=Support.UNSUPPORTED)
        sub = registry.probe("pool-sub", MODEL)
        api = registry.probe("pool-api", MODEL)
        self.assertEqual((sub.runtime_support, sub.level), ("unsupported", QualificationLevel.IMPLEMENTED))
        self.assertFalse(sub.executable_now)
        self.assertEqual((api.runtime_support, api.level), ("supported", QualificationLevel.PROBE_VERIFIED))
        self.assertTrue(api.executable_now)

    # multi-account and external login change
    def test_external_account_change_requires_revalidation_without_affecting_other_accounts(self) -> None:
        registry = self.make_registry()
        registry.register(POOL_SUB2, SUB2, self.provider, MODEL)
        for pool_id in ("pool-sub", "pool-sub-2"):
            self.assertEqual(registry.probe(pool_id, MODEL).level, QualificationLevel.PROBE_VERIFIED)
        self.provider.configure(SUB, fingerprint="d" * 64)  # external login to another account
        changed = registry.probe("pool-sub", MODEL)
        self.assertEqual(changed.account_binding, "account_changed")
        self.assertEqual(changed.level, QualificationLevel.IMPLEMENTED)
        self.assertFalse(changed.executable_now)
        self.assertEqual(changed.last_verified_at, "2030-01-01T00:00:00Z")
        other = registry.probe("pool-sub-2", MODEL)
        self.assertEqual((other.account_binding, other.level), ("bound", QualificationLevel.PROBE_VERIFIED))

        # The new account is recorded as a new binding revision and must be probed again.
        rebound = ConnectionBinding("synthetic", "fake-host", "conn-sub", 2, AuthKind.NATIVE_SUBSCRIPTION,
                                    "host-session://synthetic/session-a", "d" * 64)
        self.provider.configure(rebound, fingerprint="d" * 64)
        (reset,) = registry.update_binding(rebound)
        self.assertEqual((reset.level, reset.connection_revision), (QualificationLevel.UNKNOWN, 2))
        self.assertIsNone(reset.last_verified_at)
        self.assertEqual(registry.probe("pool-sub", MODEL).level, QualificationLevel.PROBE_VERIFIED)
        with self.assertRaises(PoolRegistryError):
            registry.update_binding(rebound)  # the revision does not increase

    def test_probe_failure_is_unknown_and_does_not_leak_provider_text(self) -> None:
        registry = self.make_registry()
        self.provider.configure(SUB, describe_error=RuntimeError(SECRET_MARKER))
        status = registry.probe("pool-sub", MODEL)
        self.assertEqual((status.implementation_state, status.account_binding, status.runtime_support),
                         ("unknown", "unknown", "unknown"))
        self.assertEqual(status.level, QualificationLevel.UNKNOWN)
        self.assertIsNone(status.last_verified_at)
        self.assertEqual(status.last_probe_at, "2030-01-01T00:00:00Z")
        self.assertFalse(status.executable_now)
        self.assertNotIn(SECRET_MARKER, repr(status))

    # 3. TTL, stale, capacity
    def test_ttl_expiry_marks_stale_and_refresh_reprobes(self) -> None:
        registry = self.make_registry()
        self.assertEqual(len(registry.refresh()), 2)
        self.assertEqual(registry.refresh(), ())
        self.clock.advance(60)
        self.assertEqual(registry.status("pool-api", MODEL).freshness, "fresh")
        self.clock.advance(1)
        status = registry.status("pool-api", MODEL)
        self.assertEqual(status.freshness, "stale")
        self.assertEqual((status.account_binding, status.runtime_support, status.capacity_state),
                         ("stale", "stale", "stale"))
        self.assertIsNone(status.available_capacity)
        # F6: a stale implementation claim is not reported; the level is unknown.
        self.assertEqual(status.level, QualificationLevel.UNKNOWN)
        self.assertFalse(status.executable_now)
        refreshed = registry.refresh()
        self.assertEqual({s.freshness for s in refreshed}, {"fresh"})
        self.assertEqual(registry.status("pool-api", MODEL).last_verified_at, "2030-01-01T00:01:01Z")

    def test_clock_regression_is_stale_not_fresh(self) -> None:
        registry = self.make_registry()
        registry.probe("pool-api", MODEL)
        self.clock.advance(-5)
        status = registry.status("pool-api", MODEL)
        self.assertEqual(status.freshness, "stale")
        self.assertFalse(status.executable_now)

    def test_unverifiable_capacity_is_unknown_never_zero_or_unlimited(self) -> None:
        registry = self.make_registry()
        for value in (None, -1, True, 3, "2", 1.0, math.inf, RuntimeError(SECRET_MARKER)):
            with self.subTest(value=repr(value)):
                self.capacity["pool-sub"] = value
                status = registry.probe("pool-sub", MODEL)
                self.assertEqual(status.capacity_state, "unknown")
                self.assertIsNone(status.available_capacity)
                self.assertEqual(status.level, QualificationLevel.PROBE_VERIFIED)
                self.assertFalse(status.executable_now)
                self.assertNotIn(SECRET_MARKER, repr(status))
        self.capacity["pool-sub"] = 0
        status = registry.probe("pool-sub", MODEL)
        self.assertEqual((status.capacity_state, status.available_capacity), ("known", 0))
        self.assertFalse(status.executable_now)
        self.capacity["pool-sub"] = 2
        self.assertEqual(registry.probe("pool-sub", MODEL).available_capacity, 2)

    def test_no_capacity_probe_means_unknown_capacity(self) -> None:
        registry = ResourcePoolRegistry(matrix_revision=1, clock=FakeClock())
        provider = FakeProvider()
        provider.configure(API)
        registry.register(POOL_API, API, provider, MODEL)
        status = registry.probe("pool-api", MODEL)
        self.assertEqual((status.capacity_state, status.available_capacity), ("unknown", None))
        self.assertFalse(status.executable_now)

    # 4. qualified only from verified live evidence (rejection cases)
    def test_qualification_is_rejected_without_verified_live_evidence(self) -> None:
        cases = (
            ("no verifier", None, {"evidence_kind": "live"}),
            ("synthetic kind", lambda evidence: True, {}),
            ("not pass", lambda evidence: True, {"evidence_kind": "live", "verdict": "FAIL"}),
            ("truthy non-True", lambda evidence: 1, {"evidence_kind": "live"}),
            ("verifier raises", lambda evidence: 1 / 0, {"evidence_kind": "live"}),
            ("matrix mismatch", lambda evidence: True, {"evidence_kind": "live", "matrix_revision": 2}),
            ("old account", lambda evidence: True, {"evidence_kind": "live", "account_fingerprint": "e" * 64}),
        )
        for name, verifier, overrides in cases:
            with self.subTest(case=name):
                registry = self.make_registry(qualification_verifier=verifier)
                registry.probe("pool-api", MODEL)
                registry.set_governance("pool-api", MODEL, Governance.APPROVED)
                with self.assertRaises(QualificationRejected):
                    registry.record_qualification(self.evidence(API, "pool-api", **overrides))
                status = registry.status("pool-api", MODEL)
                self.assertEqual(status.level, QualificationLevel.CANDIDATE_FOR_QUALIFICATION)
                self.assertIsNone(status.qualification_matrix_revision)
                self.assertEqual(registry.operational_inventory(), ())

    def test_qualification_requires_a_fresh_candidate_route(self) -> None:
        registry = self.make_registry(qualification_verifier=lambda evidence: True)
        registry.probe("pool-api", MODEL)  # probe_verified, governance unknown
        with self.assertRaises(QualificationRejected):
            registry.record_qualification(self.evidence(API, "pool-api", evidence_kind="live",
                                                        verdict="FAIL"))
        with self.assertRaises(QualificationRejected):
            registry.record_qualification(object())
        self.assertNotEqual(registry.status("pool-api", MODEL).level, QualificationLevel.QUALIFIED)

    def test_matrix_revision_is_exposed_and_cannot_regress(self) -> None:
        registry = self.make_registry()
        self.assertEqual(registry.matrix_revision, 1)
        self.assertEqual(registry.status("pool-api", MODEL).matrix_revision, 1)
        registry.set_matrix_revision(2)
        self.assertEqual(registry.status("pool-api", MODEL).matrix_revision, 2)
        for invalid in (1, 0, True, "3"):
            with self.subTest(revision=invalid):
                with self.assertRaises(PoolRegistryError):
                    registry.set_matrix_revision(invalid)

    # registration and input validation
    def test_registration_rejects_inconsistent_or_duplicate_routes(self) -> None:
        registry = self.make_registry()
        with self.assertRaises(PoolRegistryError):
            registry.register(POOL_SUB, API, self.provider, "model-x")  # connection mismatch
        with self.assertRaises(PoolRegistryError):
            registry.register(Pool("pool-x", "other", "conn-api"), API, self.provider, MODEL)
        with self.assertRaises(PoolRegistryError):
            registry.register(POOL_API, API, self.provider, MODEL)  # duplicate route
        with self.assertRaises(PoolRegistryError):
            registry.register({"id": "pool-raw"}, API, self.provider, MODEL)
        with self.assertRaises(PoolRegistryError):
            registry.register(Pool("pool-new", "synthetic", "conn-new"), API, self.provider, MODEL)
        with self.assertRaises(PoolRegistryError):
            registry.register(POOL_API, API, self.provider, "bad model")
        with self.assertRaises(PoolRegistryError):
            registry.register(POOL_API, API, self.provider, "model-y", tools=("../escape",))
        with self.assertRaises(PoolRegistryError):
            registry.register(POOL_API, API, object(), "model-z")
        newer_api = ConnectionBinding("synthetic", "fake-host", "conn-api", 2, AuthKind.API_KEY,
                                      "secret://local/api-key-ref", "b" * 64)
        with self.assertRaises(PoolRegistryError):
            registry.register(Pool("pool-api-2", "synthetic", "conn-api"), newer_api, self.provider, MODEL)
        with self.assertRaises(UnknownRoute):
            registry.probe("pool-missing", MODEL)
        with self.assertRaises(PoolRegistryError):
            ResourcePoolRegistry(matrix_revision=0)
        with self.assertRaises(PoolRegistryError):
            ResourcePoolRegistry(matrix_revision=1, ttl_seconds=0)

    def test_naive_clock_is_rejected(self) -> None:
        registry = ResourcePoolRegistry(matrix_revision=1, clock=lambda: datetime(2030, 1, 1))
        provider = FakeProvider()
        provider.configure(API)
        with self.assertRaises(PoolRegistryError):
            registry.register(POOL_API, API, provider, MODEL)

    # ----- review regressions: F1 ----------------------------------------
    def test_rejected_governance_is_never_executable_now(self) -> None:
        registry = self.make_registry()
        unknown = registry.probe("pool-api", MODEL)
        self.assertEqual(unknown.governance, "unknown")
        self.assertTrue(unknown.executable_now)  # technical availability only
        self.assertFalse(unknown.placement_eligible)
        rejected = registry.set_governance("pool-api", MODEL, Governance.REJECTED)
        self.assertEqual(rejected.level, QualificationLevel.PROBE_VERIFIED)
        self.assertFalse(rejected.executable_now)
        self.assertFalse(rejected.placement_eligible)
        self.assertFalse(registry.probe("pool-api", MODEL).executable_now)
        self.assertFalse(any(s.executable_now for s in registry.routes(pool_id="pool-api")))

    # ----- review regressions: F2 (logic only, synthetic verifier) --------
    def test_verified_live_record_reaches_qualified_and_operational_inventory(self) -> None:
        registry = self.qualified_registry()
        status = registry.status("pool-api", MODEL)
        self.assertTrue(status.executable_now)
        self.assertTrue(status.placement_eligible)
        self.assertEqual(status.qualification_matrix_revision, 1)
        self.assertEqual(self.inventory_ids(registry), ["pool-api"])
        self.assertEqual(registry.operational_inventory(model="other-model"), ())

    def test_qualified_route_with_rejected_governance_leaves_inventory(self) -> None:
        registry = self.qualified_registry()
        status = registry.set_governance("pool-api", MODEL, Governance.REJECTED)
        self.assertNotEqual(status.level, QualificationLevel.QUALIFIED)
        self.assertFalse(status.executable_now)
        self.assertEqual(registry.operational_inventory(), ())

    def test_unsupported_route_does_not_override_another_qualified_route(self) -> None:
        registry = self.qualified_registry()
        self.provider.configure(SUB, job_start=Support.UNSUPPORTED)
        sub = registry.probe("pool-sub", MODEL)
        self.assertEqual(sub.runtime_support, "unsupported")
        self.assertFalse(sub.executable_now)
        api = registry.status("pool-api", MODEL)
        self.assertEqual(api.level, QualificationLevel.QUALIFIED)
        self.assertTrue(api.placement_eligible)
        self.assertEqual(self.inventory_ids(registry), ["pool-api"])

    def test_account_change_discards_qualification_until_requalified(self) -> None:
        registry = self.qualified_registry()
        self.provider.configure(API, fingerprint="e" * 64)
        changed = registry.probe("pool-api", MODEL)
        self.assertEqual(changed.account_binding, "account_changed")
        self.assertFalse(changed.placement_eligible)
        self.provider.configure(API)
        restored = registry.probe("pool-api", MODEL)
        self.assertEqual(restored.level, QualificationLevel.CANDIDATE_FOR_QUALIFICATION)
        self.assertIsNone(restored.qualification_matrix_revision)
        self.assertEqual(registry.operational_inventory(), ())

    def test_stale_qualified_route_leaves_inventory_and_returns_after_fresh_probe(self) -> None:
        registry = self.qualified_registry()
        self.clock.advance(61)
        stale = registry.status("pool-api", MODEL)
        self.assertEqual(stale.freshness, "stale")
        self.assertNotEqual(stale.level, QualificationLevel.QUALIFIED)
        self.assertFalse(stale.placement_eligible)
        self.assertEqual(registry.operational_inventory(), ())
        registry.refresh()
        self.assertEqual(registry.status("pool-api", MODEL).level, QualificationLevel.QUALIFIED)
        self.assertEqual(self.inventory_ids(registry), ["pool-api"])

    def test_matrix_bump_invalidates_qualified_route(self) -> None:
        registry = self.qualified_registry()
        registry.set_matrix_revision(2)
        status = registry.status("pool-api", MODEL)
        self.assertNotEqual(status.level, QualificationLevel.QUALIFIED)
        self.assertIsNone(status.qualification_matrix_revision)
        self.assertFalse(status.placement_eligible)
        self.assertEqual(registry.operational_inventory(), ())

    def test_binding_update_invalidates_qualified_route(self) -> None:
        registry = self.qualified_registry()
        (reset,) = registry.update_binding(api_binding(2))
        self.assertEqual(reset.level, QualificationLevel.UNKNOWN)
        status = registry.probe("pool-api", MODEL)
        self.assertNotEqual(status.level, QualificationLevel.QUALIFIED)
        self.assertIsNone(status.qualification_matrix_revision)
        self.assertEqual(registry.operational_inventory(), ())

    # ----- review regressions: F3 ----------------------------------------
    def test_binding_update_and_matrix_bump_revoke_approval_but_keep_rejection(self) -> None:
        registry = self.make_registry()
        registry.probe("pool-api", MODEL)
        registry.set_governance("pool-api", MODEL, Governance.APPROVED)
        (reset,) = registry.update_binding(api_binding(2))
        self.assertEqual(reset.governance, "unknown")
        self.assertEqual(registry.probe("pool-api", MODEL).level, QualificationLevel.PROBE_VERIFIED)

        approved = registry.set_governance("pool-api", MODEL, Governance.APPROVED)
        self.assertEqual(approved.level, QualificationLevel.CANDIDATE_FOR_QUALIFICATION)
        registry.set_matrix_revision(2)
        status = registry.status("pool-api", MODEL)
        self.assertEqual(status.governance, "unknown")
        self.assertEqual(status.level, QualificationLevel.PROBE_VERIFIED)

        registry.set_governance("pool-api", MODEL, Governance.REJECTED)
        registry.update_binding(api_binding(3))
        registry.set_matrix_revision(3)
        status = registry.probe("pool-api", MODEL)
        self.assertEqual(status.governance, "rejected")
        self.assertFalse(status.executable_now)

    # ----- review regressions: F4 ----------------------------------------
    def test_clock_failure_leaves_registry_state_unchanged(self) -> None:
        registry = self.make_registry()
        self.clock.fail = True
        failures = (
            lambda: registry.register(POOL_SUB2, SUB2, self.provider, MODEL),
            lambda: registry.update_binding(api_binding(2)),
            lambda: registry.update_pool(Pool("pool-api", "synthetic", "conn-api", max_parallel=4, enabled=False)),
            lambda: registry.set_governance("pool-api", MODEL, Governance.APPROVED),
        )
        for operation in failures:
            with self.assertRaises(PoolRegistryError) as caught:
                operation()
            self.assertEqual(str(caught.exception), "CLOCK_UNAVAILABLE")
            self.assertNotIn(SECRET_MARKER, str(caught.exception))
        self.clock.fail = False
        self.assertEqual(registry.routes(pool_id="pool-sub-2"), ())
        status = registry.status("pool-api", MODEL)
        self.assertEqual((status.connection_revision, status.pool_enabled, status.governance), (1, True, "unknown"))
        # Every operation succeeds on retry instead of reporting a phantom change.
        self.assertEqual(registry.register(POOL_SUB2, SUB2, self.provider, MODEL).pool_id, "pool-sub-2")
        (updated,) = registry.update_binding(api_binding(2))
        self.assertEqual(updated.connection_revision, 2)
        (disabled,) = registry.update_pool(Pool("pool-api", "synthetic", "conn-api", max_parallel=4, enabled=False))
        self.assertFalse(disabled.pool_enabled)
        self.assertEqual(registry.set_governance("pool-api", MODEL, Governance.APPROVED).governance, "approved")

    # ----- review regressions: F5 ----------------------------------------
    def test_last_verified_is_not_set_by_unbound_or_unsupported_observations(self) -> None:
        registry = self.make_registry()
        for auth in (AuthState.UNAUTHENTICATED, AuthState.PENDING):
            with self.subTest(auth=auth.value):
                self.provider.configure(SUB, auth=auth)
                self.assertIsNone(registry.probe("pool-sub", MODEL).last_verified_at)
        self.provider.configure(SUB, isolation=AccountIsolation.UNKNOWN)
        status = registry.probe("pool-sub", MODEL)
        self.assertEqual((status.account_binding, status.runtime_support), ("bound", "unknown"))
        self.assertIsNone(status.last_verified_at)
        self.provider.configure(SUB)
        self.clock.advance(5)
        self.assertEqual(registry.probe("pool-sub", MODEL).last_verified_at, "2030-01-01T00:00:05Z")

    # ----- review regressions: F6 ----------------------------------------
    def test_stale_route_does_not_expose_old_implementation_state(self) -> None:
        registry = self.make_registry()
        registry.probe("pool-api", MODEL)
        self.clock.advance(61)
        status = registry.status("pool-api", MODEL)
        self.assertEqual(status.implementation_state, "stale")
        self.assertEqual(status.level, QualificationLevel.UNKNOWN)
        self.assertEqual(status.last_probe_at, "2030-01-01T00:00:00Z")
        self.assertFalse(status.executable_now)

    # ----- review regressions: F7 (pins the documented conditions) --------
    def test_documented_binding_equality_and_implementation_condition(self) -> None:
        registry = self.make_registry()
        other_account = api_binding(1, fingerprint="f" * 64)
        with self.assertRaises(PoolRegistryError) as caught:
            registry.register(Pool("pool-api-2", "synthetic", "conn-api", max_parallel=4, enabled=True),
                              other_account, self.provider, MODEL)
        self.assertEqual(str(caught.exception), "CONNECTION_REVISION_CONFLICT")
        self.provider.configure(API, implementation="not_implemented")
        status = registry.probe("pool-api", MODEL)
        self.assertEqual((status.account_binding, status.runtime_support, status.capacity_state),
                         ("bound", "supported", "known"))
        self.assertFalse(status.executable_now)

    # ----- review regressions: F8 ----------------------------------------
    def test_runtime_loss_discards_qualification_until_requalified(self) -> None:
        registry = self.qualified_registry()
        self.provider.configure(API, job_start=Support.UNSUPPORTED)
        lost = registry.probe("pool-api", MODEL)
        self.assertEqual((lost.account_binding, lost.runtime_support), ("bound", "unsupported"))
        self.assertFalse(lost.placement_eligible)
        self.provider.configure(API)
        restored = registry.probe("pool-api", MODEL)
        self.assertEqual(restored.level, QualificationLevel.CANDIDATE_FOR_QUALIFICATION)
        self.assertIsNone(restored.qualification_matrix_revision)
        self.assertEqual(registry.operational_inventory(), ())

    # ----- review regressions: F9 (rejection-path coverage) ---------------
    def test_binding_and_pool_update_rejections_use_fixed_codes(self) -> None:
        registry = self.make_registry()
        missing = ConnectionBinding("synthetic", "fake-host", "conn-missing", 2, AuthKind.API_KEY,
                                    "secret://local/api-key-ref", "b" * 64)
        with self.assertRaises(UnknownRoute) as caught:
            registry.update_binding(missing)
        self.assertEqual(str(caught.exception), "UNKNOWN_CONNECTION")
        with self.assertRaises(PoolRegistryError) as caught:
            registry.update_binding(api_binding(2, runtime="other-host"))
        self.assertEqual(str(caught.exception), "BINDING_IDENTITY_CHANGED")
        with self.assertRaises(UnknownRoute) as caught:
            registry.update_pool(Pool("pool-missing", "synthetic", "conn-api"))
        self.assertEqual(str(caught.exception), "UNKNOWN_POOL")
        with self.assertRaises(PoolRegistryError) as caught:
            registry.update_pool(Pool("pool-api", "synthetic", "conn-other"))
        self.assertEqual(str(caught.exception), "POOL_IDENTITY_CHANGED")
        status = registry.status("pool-api", MODEL)
        self.assertEqual((status.connection_revision, status.host, status.configured_max_parallel),
                         (1, "fake-host", 4))

    def test_refresh_modes_callbacks_and_clock_failure(self) -> None:
        registry = self.make_registry()
        self.assertEqual(len(registry.refresh()), 2)
        self.assertEqual(len(registry.refresh(stale_only=False)), 2)
        self.assertEqual(registry.refresh(), ())
        with self.assertRaises(PoolRegistryError) as caught:
            registry.refresh(stale_only=1)
        self.assertEqual(str(caught.exception), "INVALID_REFRESH_MODE")
        for name in ("clock", "capacity_probe", "qualification_verifier"):
            with self.subTest(callback=name):
                with self.assertRaises(PoolRegistryError) as caught:
                    ResourcePoolRegistry(matrix_revision=1, **{name: 42})
                self.assertEqual(str(caught.exception), "INVALID_CALLBACK")

        def broken_clock():
            raise RuntimeError(SECRET_MARKER)

        broken = ResourcePoolRegistry(matrix_revision=1, clock=broken_clock)
        with self.assertRaises(PoolRegistryError) as caught:
            broken.routes()
        self.assertEqual(str(caught.exception), "CLOCK_UNAVAILABLE")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_lowering_max_parallel_below_observed_capacity_makes_capacity_unknown(self) -> None:
        registry = self.make_registry()
        self.assertEqual(registry.probe("pool-api", MODEL).available_capacity, 3)
        (status,) = registry.update_pool(Pool("pool-api", "synthetic", "conn-api", max_parallel=2, enabled=True))
        self.assertEqual(status.configured_max_parallel, 2)
        self.assertEqual((status.capacity_state, status.available_capacity), ("unknown", None))
        self.assertFalse(status.executable_now)
        reprobed = registry.probe("pool-api", MODEL)
        self.assertEqual((reprobed.capacity_state, reprobed.available_capacity), ("unknown", None))

    def test_auth_status_failure_and_other_auth_states_are_not_bound_and_do_not_leak(self) -> None:
        registry = self.make_registry()
        self.provider.configure(SUB, auth_error=RuntimeError(SECRET_MARKER))
        status = registry.probe("pool-sub", MODEL)
        self.assertNotEqual(status.account_binding, "bound")
        self.assertEqual(status.level, QualificationLevel.IMPLEMENTED)
        self.assertIsNone(status.last_verified_at)
        self.assertFalse(status.executable_now)
        self.assertNotIn(SECRET_MARKER, repr(status))
        for state in AuthState:
            if state is AuthState.AUTHENTICATED:
                continue
            with self.subTest(state=state.value):
                self.provider.configure(SUB, auth=state)
                status = registry.probe("pool-sub", MODEL)
                self.assertIn(status.account_binding, {state.value, "unauthenticated", "unknown"})
                self.assertNotEqual(status.account_binding, "bound")
                self.assertEqual(status.level, QualificationLevel.IMPLEMENTED)
                self.assertIsNone(status.last_verified_at)
                self.assertFalse(status.executable_now)


if __name__ == "__main__":
    unittest.main()
