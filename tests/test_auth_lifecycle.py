"""AIK-016 auth lifecycle tests with synthetic bindings, a fake clock and fake adapters.

No provider, credential or network is used. The fake vault is test-only.
Linux results are development verification, not Windows support evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_auth_lifecycle import (  # noqa: E402
    AuthEventKind, AuthLifecycle, AuthLifecycleError, AuthState, LockPolicy, ManualLockSignal,
    RunStatus, windows_session_locked,
)
from aik_connection import (  # noqa: E402
    AuthenticationRequired, CredentialRefUnavailable, UnknownConnection, VaultCredentialRefChecker,
)
from aik_pool.registry import ResourcePoolRegistry  # noqa: E402
from aik_provider.contracts import (  # noqa: E402
    AccountIsolation, AdapterCapabilities, AuthKind, AuthObservation, AuthState as ProviderAuthState,
    ConnectionBinding, JobReceipt, JobState, Support,
)
from aik_runtime import Pool, RevisionConflict  # noqa: E402
from aik_vault import FakeVaultBackend  # noqa: E402


FP_A = "a" * 64
FP_B = "b" * 64
SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"


class FakeClock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeAdapter:
    """Only describe/auth_status may be called; effect methods are recorded and refused."""

    def __init__(self):
        self.state = ProviderAuthState.AUTHENTICATED
        self.fingerprint = FP_A
        self.effects = []

    def describe(self, binding):
        native = binding.auth_kind is AuthKind.NATIVE_SUBSCRIPTION
        return AdapterCapabilities(
            provider=binding.provider, runtime=binding.runtime, auth_kind=binding.auth_kind,
            isolation=AccountIsolation.ISOLATED, auth_status=Support.SUPPORTED,
            sign_in=Support.SUPPORTED if native else Support.UNSUPPORTED, sign_out=Support.UNSUPPORTED,
            job_start=Support.SUPPORTED, job_observe=Support.SUPPORTED, job_interrupt=Support.SUPPORTED,
        )

    def auth_status(self, binding):
        fingerprint = self.fingerprint if self.state is ProviderAuthState.AUTHENTICATED else None
        return AuthObservation(binding.connection_id, binding.revision, fingerprint, self.state)

    def _effect(self, name):
        self.effects.append(name)
        raise AssertionError("effect methods must not be called")

    def begin_sign_in(self, binding):
        self._effect("begin_sign_in")

    def sign_out(self, binding):
        self._effect("sign_out")

    def start_job(self, request):
        self._effect("start_job")

    def observe_job(self, request, receipt):
        self._effect("observe_job")

    def interrupt_job(self, request, receipt):
        self._effect("interrupt_job")


def session_binding(revision=1, cid="conn-session", fingerprint=FP_A,
                    ref="host-session://synthetic/session-a"):
    return ConnectionBinding("synthetic", "synthetic-host", cid, revision,
                             AuthKind.NATIVE_SUBSCRIPTION, ref, fingerprint)


def api_binding(ref, revision=1, cid="conn-api", fingerprint=FP_A):
    return ConnectionBinding("synthetic", "synthetic-api", cid, revision, AuthKind.API_KEY, ref, fingerprint)


def authenticated(binding):
    return AuthObservation(binding.connection_id, binding.revision, binding.account_fingerprint,
                           ProviderAuthState.AUTHENTICATED)


def receipt(run_id, binding, state):
    return JobReceipt(run_id, "job-" + run_id, binding.connection_id, binding.revision,
                      binding.account_fingerprint, state)


class AuthLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-auth-lifecycle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()

    def make_vault(self):
        vault = FakeVaultBackend(self.root / "vault", key=bytes(range(32)))
        return vault, VaultCredentialRefChecker(vault)

    def make_registry(self, binding, adapter):
        registry = ResourcePoolRegistry(matrix_revision=1, clock=self.clock, capacity_probe=lambda *_: 1)
        registry.register(Pool("pool-session", "synthetic", binding.connection_id, max_parallel=1, enabled=True),
                          binding, adapter, "synthetic-model")
        return registry

    def valid_session(self, life, binding=None):
        binding = binding or session_binding()
        life.register(binding)
        snap = life.revalidate(binding.connection_id, 1, observation=authenticated(binding))
        self.assertIs(snap.state, AuthState.VALID)
        return binding

    # ----------------------------------------------------------------- 기준 1
    def test_registration_is_unknown_until_registry_revalidation(self):
        adapter = FakeAdapter()
        binding = session_binding()
        registry = self.make_registry(binding, adapter)
        life = AuthLifecycle(clock=self.clock, registry=registry)
        snap = life.register(binding)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertFalse(snap.placeable)
        self.assertEqual(life.placement_candidates(require_qualified=False), ())
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(binding.connection_id, "run-early")
        snap = life.revalidate(binding.connection_id, 1)
        self.assertIs(snap.state, AuthState.VALID)
        candidates = life.placement_candidates(require_qualified=False)
        self.assertEqual([c.pool_id for c in candidates], ["pool-session"])
        # No live qualification exists, so automatic placement still sees nothing.
        self.assertEqual(life.placement_candidates(), ())
        self.assertEqual(adapter.effects, [])

    def test_clock_expiry_excludes_new_runs_until_reauth_and_revalidation(self):
        adapter = FakeAdapter()
        binding = session_binding()
        registry = self.make_registry(binding, adapter)
        life = AuthLifecycle(clock=self.clock, registry=registry)
        life.register(binding, expires_at=self.clock.now + timedelta(seconds=60))
        life.revalidate(binding.connection_id, 1)
        self.assertEqual(len(life.placement_candidates(require_qualified=False)), 1)

        self.clock.advance(120)
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(binding.connection_id, "run-after-expiry")
        snap = life.snapshot(binding.connection_id)
        self.assertIs(snap.state, AuthState.EXPIRED)
        self.assertEqual(life.placement_candidates(require_qualified=False), ())
        expired = [e for e in life.events() if e.kind is AuthEventKind.EXPIRED]
        self.assertTrue(expired and expired[-1].reauth_required)

        # The host still looks authenticated, but the binding has expired.
        snap = life.revalidate(binding.connection_id, snap.revision)
        self.assertIs(snap.state, AuthState.EXPIRED)

        renewed = session_binding(revision=2)
        snap = life.reauthenticate(binding.connection_id, renewed, snap.revision,
                                   expires_at=self.clock.now + timedelta(hours=1))
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual(registry.status("pool-session", "synthetic-model").connection_revision, 2)
        self.assertEqual(registry.status("pool-session", "synthetic-model").freshness, "unknown")
        self.assertEqual(life.placement_candidates(require_qualified=False), ())
        snap = life.revalidate(binding.connection_id, snap.revision)
        self.assertIs(snap.state, AuthState.VALID)
        candidates = life.placement_candidates(require_qualified=False)
        self.assertEqual([c.connection_revision for c in candidates], [2])
        self.assertEqual(life.begin_run(binding.connection_id, "run-renewed").binding.revision, 2)
        self.assertEqual(adapter.effects, [])

    def test_host_reported_expiry_lock_and_account_change_block_placement(self):
        adapter = FakeAdapter()
        binding = session_binding()
        registry = self.make_registry(binding, adapter)
        life = AuthLifecycle(clock=self.clock, registry=registry)
        life.register(binding)
        cid = binding.connection_id
        cases = (
            (ProviderAuthState.EXPIRED, FP_A, AuthState.EXPIRED, "probe:expired"),
            (ProviderAuthState.LOCKED, FP_A, AuthState.LOCKED, "probe:locked"),
            (ProviderAuthState.AUTHENTICATED, FP_B, AuthState.UNKNOWN, "probe:account_changed"),
        )
        for provider_state, fingerprint, expected, reason in cases:
            with self.subTest(provider_state=provider_state.value, fingerprint=fingerprint[:1]):
                adapter.state, adapter.fingerprint = provider_state, fingerprint
                snap = life.revalidate(cid, life.snapshot(cid).revision)
                self.assertIs(snap.state, expected)
                self.assertEqual(life.events()[-1].kind, AuthEventKind.REVALIDATION_FAILED)
                self.assertEqual(life.events()[-1].reason, reason)
                self.assertEqual(life.placement_candidates(require_qualified=False), ())
                with self.assertRaises(AuthenticationRequired):
                    life.begin_run(cid, "run-blocked")
        adapter.state, adapter.fingerprint = ProviderAuthState.AUTHENTICATED, FP_A
        self.assertIs(life.revalidate(cid, life.snapshot(cid).revision).state, AuthState.VALID)

    def test_stale_or_foreign_observations_never_validate(self):
        life = AuthLifecycle(clock=self.clock)
        binding = session_binding()
        life.register(binding)
        stale = AuthObservation(binding.connection_id, 2, FP_A, ProviderAuthState.AUTHENTICATED)
        snap = life.revalidate(binding.connection_id, 1, observation=stale)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual(life.events()[-1].reason, "stale_observation")
        foreign = AuthObservation(binding.connection_id, 1, FP_B, ProviderAuthState.AUTHENTICATED)
        snap = life.revalidate(binding.connection_id, snap.revision, observation=foreign)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual(life.events()[-1].reason, "account_changed")
        with self.assertRaises(AuthLifecycleError) as raised:
            life.revalidate(binding.connection_id, snap.revision)
        self.assertEqual(raised.exception.code, "NO_REVALIDATION_SOURCE")

    def test_probe_exception_withdraws_previous_validation(self):
        # F6: a failed probe must not keep the earlier valid state.
        adapter = FakeAdapter()
        binding = session_binding()
        registry = self.make_registry(binding, adapter)
        life = AuthLifecycle(clock=self.clock, registry=registry)
        life.register(binding)
        cid = binding.connection_id
        snap = life.revalidate(cid, 1)
        self.assertIs(snap.state, AuthState.VALID)
        failure = RuntimeError("synthetic probe failure " + SECRET_MARKER)
        with mock.patch.object(ResourcePoolRegistry, "probe", side_effect=failure):
            snap = life.revalidate(cid, snap.revision)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual((life.events()[-1].kind, life.events()[-1].reason),
                         (AuthEventKind.REVALIDATION_FAILED, "probe_failed"))
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(cid, "run-after-probe-failure")
        self.assertNotIn(SECRET_MARKER, repr(life.events()))
        self.assertEqual(adapter.effects, [])

    def test_forged_observation_is_rejected_with_fixed_code(self):
        # F8: observations are rebuilt like bindings; a forged state gives a fixed code.
        life = AuthLifecycle(clock=self.clock)
        binding = session_binding()
        life.register(binding)
        forged = authenticated(binding)
        object.__setattr__(forged, "state", "authenticated")
        with self.assertRaises(AuthLifecycleError) as raised:
            life.revalidate(binding.connection_id, 1, observation=forged)
        self.assertEqual(raised.exception.code, "INVALID_OBSERVATION")
        snap = life.snapshot(binding.connection_id)
        self.assertEqual((snap.state, snap.revision), (AuthState.UNKNOWN, 1))

    def test_auth_valid_alias_does_not_imply_qualified_placement(self):
        # F10: the snapshot only describes authentication, not qualification.
        adapter = FakeAdapter()
        binding = session_binding()
        registry = self.make_registry(binding, adapter)
        life = AuthLifecycle(clock=self.clock, registry=registry)
        life.register(binding)
        snap = life.revalidate(binding.connection_id, 1)
        self.assertTrue(snap.auth_valid)
        self.assertEqual(snap.auth_valid, snap.placeable)
        self.assertEqual(life.placement_candidates(), ())

    def test_reported_expiry_blocks_new_runs_and_reauthentication_restores(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        cid = binding.connection_id
        life.begin_run(cid, "run-before-expiry")
        snap = life.mark_expired(cid, 2)
        self.assertIs(snap.state, AuthState.EXPIRED)
        event = life.events()[-1]
        self.assertEqual((event.kind, event.reason), (AuthEventKind.EXPIRED, "reported"))
        self.assertTrue(event.reauth_required)
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(cid, "run-after-expiry")
        # A reported expiry does not settle running work.
        self.assertIs(life.run("run-before-expiry").status, RunStatus.ACTIVE)
        with self.assertRaises(RevisionConflict):
            life.mark_expired(cid, 2)
        renewed = session_binding(revision=2)
        snap = life.reauthenticate(cid, renewed, snap.revision)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        record = life.run("run-before-expiry")
        self.assertEqual((record.status, record.reclaim_reason), (RunStatus.RECLAIM_REQUIRED, "reauthenticated"))
        snap = life.revalidate(cid, snap.revision, observation=authenticated(renewed))
        self.assertIs(snap.state, AuthState.VALID)
        self.assertEqual(life.begin_run(cid, "run-renewed").binding.revision, 2)
        logout = life.force_logout(cid, snap.revision)
        with self.assertRaises(AuthLifecycleError) as raised:
            life.mark_expired(cid, logout.revision)
        self.assertEqual(raised.exception.code, "REAUTH_REQUIRED")

    # ----------------------------------------------------------------- 기준 2
    def test_rotation_new_runs_use_new_reference_and_old_runs_require_reclaim(self):
        vault, checker = self.make_vault()
        ref1 = vault.put("synthetic-api-key-version-1")
        life = AuthLifecycle(clock=self.clock, credential_checker=checker)
        b1 = api_binding(ref1)
        life.register(b1)
        life.revalidate("conn-api", 1, observation=authenticated(b1))
        old = life.begin_run("conn-api", "run-old")
        self.assertEqual((old.binding.credential_ref, old.credential_version), (ref1, 1))

        self.assertIs(life.begin_rotation("conn-api", 2).state, AuthState.ROTATING)
        with self.assertRaises(AuthenticationRequired):
            life.begin_run("conn-api", "run-during-rotation")
        ref2 = vault.put("synthetic-api-key-version-2")
        b2 = api_binding(ref2, revision=2)
        with self.assertRaises(AuthLifecycleError) as raised:
            life.complete_rotation("conn-api", api_binding(ref2, revision=2, fingerprint=FP_B), 3)
        self.assertEqual(raised.exception.code, "ACCOUNT_CHANGED")
        snap = life.complete_rotation("conn-api", b2, 3)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual((snap.credential_version, snap.credential_ref), (2, ref2))
        old_record = life.run("run-old")
        self.assertIs(old_record.status, RunStatus.RECLAIM_REQUIRED)
        self.assertEqual((old_record.reclaim_reason, old_record.credential_ref), ("rotated", ref1))
        with self.assertRaises(AuthenticationRequired):
            life.begin_run("conn-api", "run-before-revalidation")

        # An observation of the old binding cannot validate the new reference.
        snap = life.revalidate("conn-api", snap.revision, observation=authenticated(b1))
        self.assertIs(snap.state, AuthState.UNKNOWN)
        snap = life.revalidate("conn-api", snap.revision, observation=authenticated(b2))
        self.assertIs(snap.state, AuthState.VALID)
        new = life.begin_run("conn-api", "run-new")
        self.assertEqual((new.binding.credential_ref, new.credential_version, new.binding.revision), (ref2, 2, 2))

        with self.assertRaises(AuthLifecycleError) as raised:
            life.record_observation(receipt("run-old", b2, JobState.SUCCEEDED))
        self.assertEqual(raised.exception.code, "RECEIPT_MISMATCH")
        self.assertIs(life.run("run-old").status, RunStatus.RECLAIM_REQUIRED)
        closed = life.record_observation(receipt("run-old", b1, JobState.SUCCEEDED))
        self.assertEqual((closed.status, closed.outcome), (RunStatus.CLOSED, "succeeded"))
        self.assertIs(life.run("run-new").status, RunStatus.ACTIVE)

    def test_refs_in_use_keeps_old_reference_until_reclaim_closes(self):
        # F3: callers can see which old reference is still needed for reclaim.
        vault, checker = self.make_vault()
        ref1 = vault.put("synthetic-api-key-version-1")
        life = AuthLifecycle(clock=self.clock, credential_checker=checker)
        b1 = api_binding(ref1)
        life.register(b1)
        self.assertEqual(life.refs_in_use(), (ref1,))
        life.revalidate("conn-api", 1, observation=authenticated(b1))
        life.begin_run("conn-api", "run-old")
        life.begin_rotation("conn-api", 2)
        ref2 = vault.put("synthetic-api-key-version-2")
        b2 = api_binding(ref2, revision=2)
        snap = life.complete_rotation("conn-api", b2, 3)
        self.assertEqual(set(life.refs_in_use()), {ref1, ref2})
        pending = life.runs(status=RunStatus.RECLAIM_REQUIRED)
        self.assertEqual([(r.run_id, r.credential_ref) for r in pending], [("run-old", ref1)])
        life.record_observation(receipt("run-old", b1, JobState.CANCELLED))
        self.assertEqual(life.refs_in_use(), (ref2,))
        life.delete_connection("conn-api", snap.revision)
        self.assertEqual(life.refs_in_use(), ())

    def test_job_receipt_is_bound_to_first_observed_job(self):
        # F4: a receipt of another job with the same binding cannot close the run.
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        cid = binding.connection_id
        life.begin_run(cid, "run-r1")
        running = life.record_observation(receipt("run-r1", binding, JobState.RUNNING))
        self.assertIs(running.status, RunStatus.ACTIVE)
        foreign = JobReceipt("run-r1", "job-other", cid, binding.revision, binding.account_fingerprint,
                             JobState.SUCCEEDED)
        with self.assertRaises(AuthLifecycleError) as raised:
            life.record_observation(foreign)
        self.assertEqual(raised.exception.code, "RECEIPT_MISMATCH")
        record = life.run("run-r1")
        self.assertEqual((record.status, record.outcome), (RunStatus.ACTIVE, None))
        closed = life.record_observation(receipt("run-r1", binding, JobState.SUCCEEDED))
        self.assertEqual((closed.status, closed.outcome), (RunStatus.CLOSED, "succeeded"))

    def test_clock_failure_during_terminal_observation_leaves_run_open(self):
        # F5: the clock is read before the run is closed.
        failing = {"on": False}

        def flaky_clock():
            if failing["on"]:
                raise OSError("synthetic clock failure")
            return self.clock()

        life = AuthLifecycle(clock=flaky_clock)
        binding = self.valid_session(life)
        life.begin_run(binding.connection_id, "run-clock")
        failing["on"] = True
        with self.assertRaises(AuthLifecycleError) as raised:
            life.record_observation(receipt("run-clock", binding, JobState.FAILED))
        self.assertEqual(raised.exception.code, "CLOCK_UNAVAILABLE")
        failing["on"] = False
        record = life.run("run-clock")
        self.assertEqual((record.status, record.outcome), (RunStatus.ACTIVE, None))
        self.assertNotIn(AuthEventKind.RUN_CLOSED, [e.kind for e in life.events()])
        closed = life.record_observation(receipt("run-clock", binding, JobState.FAILED))
        self.assertEqual((closed.status, closed.outcome), (RunStatus.CLOSED, "failed"))
        self.assertEqual(life.events()[-1].kind, AuthEventKind.RUN_CLOSED)

    def test_rotation_survives_clock_expiry_and_lock_until_completed(self):
        vault, checker = self.make_vault()
        ref1 = vault.put("synthetic-api-key-version-1")
        signal = ManualLockSignal()
        life = AuthLifecycle(clock=self.clock, credential_checker=checker, vault_lock_source=signal,
                             policy=LockPolicy(vault_lock_blocks=True))
        b1 = api_binding(ref1)
        life.register(b1, expires_at=self.clock.now + timedelta(seconds=60))
        life.revalidate("conn-api", 1, observation=authenticated(b1))
        snap = life.begin_rotation("conn-api", 2)
        self.clock.advance(120)
        snap = life.snapshot("conn-api")
        self.assertIs(snap.state, AuthState.ROTATING)
        signal.lock()
        snap = life.snapshot("conn-api")
        self.assertEqual((snap.state, snap.lock_reasons), (AuthState.LOCKED, ("vault",)))
        ref2 = vault.put("synthetic-api-key-version-2")
        b2 = api_binding(ref2, revision=2)
        with self.assertRaises(AuthLifecycleError) as raised:
            life.complete_rotation("conn-api", b2, snap.revision)
        self.assertEqual(raised.exception.code, "LOCKED")
        signal.unlock()
        snap = life.snapshot("conn-api")
        self.assertIs(snap.state, AuthState.ROTATING)
        snap = life.complete_rotation("conn-api", b2, snap.revision,
                                      expires_at=self.clock.now + timedelta(hours=1))
        self.assertEqual((snap.state, snap.credential_version), (AuthState.UNKNOWN, 2))
        snap = life.revalidate("conn-api", snap.revision, observation=authenticated(b2))
        self.assertIs(snap.state, AuthState.VALID)

    def test_concurrent_rotation_is_serialized_by_revision(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt():
            barrier.wait()
            try:
                life.begin_rotation(binding.connection_id, 2)
                outcomes.append("ok")
            except RevisionConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        snap = life.snapshot(binding.connection_id)
        self.assertEqual((snap.state, snap.revision), (AuthState.ROTATING, 3))
        with self.assertRaises(RevisionConflict):
            life.complete_rotation(binding.connection_id, session_binding(revision=2), 2)

    def test_delete_races_rotation_and_runs_stay_reclaimable_after_deletion(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        cid = binding.connection_id
        life.begin_run(cid, "run-active")
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(operation):
            barrier.wait()
            try:
                operation()
                outcomes.append("ok")
            except RevisionConflict:
                outcomes.append("conflict")

        operations = (lambda: life.delete_connection(cid, 2), lambda: life.begin_rotation(cid, 2))
        threads = [threading.Thread(target=attempt, args=(op,)) for op in operations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        snap = life.snapshot(cid)
        if not snap.deleted:
            snap = life.delete_connection(cid, snap.revision)
        self.assertTrue(snap.deleted)
        self.assertIs(snap.state, AuthState.REVOKED)
        with self.assertRaises(UnknownConnection):
            life.begin_run(cid, "run-after-delete")
        with self.assertRaises(UnknownConnection):
            life.begin_rotation(cid, snap.revision)
        with self.assertRaises(RevisionConflict):
            life.reauthenticate(cid, session_binding(revision=2), 2)
        record = life.run("run-active")
        self.assertEqual((record.status, record.reclaim_reason), (RunStatus.RECLAIM_REQUIRED, "connection_deleted"))
        closed = life.record_observation(receipt("run-active", binding, JobState.FAILED))
        self.assertEqual((closed.status, closed.outcome), (RunStatus.CLOSED, "failed"))
        with self.assertRaises(AuthLifecycleError) as raised:
            life.register(binding)
        self.assertEqual(raised.exception.code, "DUPLICATE_CONNECTION")

    def test_reauthenticate_on_deleted_connection_with_current_revision_is_unknown(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        snap = life.delete_connection(binding.connection_id, 2)
        with self.assertRaises(UnknownConnection):
            life.reauthenticate(binding.connection_id, session_binding(revision=2), snap.revision)
        self.assertTrue(life.snapshot(binding.connection_id).deleted)

    # ----------------------------------------------------------------- 기준 3
    def test_vault_lock_policy_blocks_api_connections_and_requires_revalidation(self):
        vault, checker = self.make_vault()
        ref = vault.put("synthetic-api-key")
        signal = ManualLockSignal()
        life = AuthLifecycle(clock=self.clock, credential_checker=checker, vault_lock_source=signal,
                             policy=LockPolicy(vault_lock_blocks=True))
        api = api_binding(ref)
        life.register(api)
        life.revalidate("conn-api", 1, observation=authenticated(api))
        session = self.valid_session(life)

        signal.lock()
        with self.assertRaises(AuthenticationRequired):
            life.begin_run("conn-api", "run-locked")
        snap = life.snapshot("conn-api")
        self.assertEqual((snap.state, snap.lock_reasons), (AuthState.LOCKED, ("vault",)))
        self.assertEqual(life.begin_run(session.connection_id, "run-session").binding.revision, 1)
        for operation in (lambda: life.revalidate("conn-api", snap.revision, observation=authenticated(api)),
                          lambda: life.begin_rotation("conn-api", snap.revision)):
            with self.assertRaises(AuthLifecycleError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "LOCKED")

        signal.unlock()
        snap = life.snapshot("conn-api")
        self.assertIs(snap.state, AuthState.UNKNOWN)
        with self.assertRaises(AuthenticationRequired):
            life.begin_run("conn-api", "run-after-unlock")
        snap = life.revalidate("conn-api", snap.revision, observation=authenticated(api))
        self.assertIs(snap.state, AuthState.VALID)
        self.assertEqual(life.begin_run("conn-api", "run-revalidated").credential_version, 1)
        kinds = [e.kind for e in life.events() if e.connection_id == "conn-api"]
        self.assertIn(AuthEventKind.LOCKED, kinds)
        self.assertIn(AuthEventKind.UNLOCKED, kinds)

    def test_lock_and_unlock_between_polls_invalidates_validation_and_reclaims(self):
        # F1: a lock that starts and ends between two polls must not be missed.
        vault, checker = self.make_vault()
        ref = vault.put("synthetic-api-key")
        signal = ManualLockSignal()
        life = AuthLifecycle(clock=self.clock, credential_checker=checker, vault_lock_source=signal,
                             policy=LockPolicy(vault_lock_blocks=True, reclaim_running_on_lock=True))
        api = api_binding(ref)
        life.register(api)
        life.revalidate("conn-api", 1, observation=authenticated(api))
        session = self.valid_session(life)
        life.begin_run("conn-api", "run-a")

        signal.lock()
        signal.unlock()
        self.assertEqual(signal.generation, 1)
        snap = life.snapshot("conn-api")
        self.assertEqual((snap.state, snap.lock_reasons), (AuthState.UNKNOWN, ()))
        with self.assertRaises(AuthenticationRequired):
            life.begin_run("conn-api", "run-b")
        record = life.run("run-a")
        self.assertEqual((record.status, record.reclaim_reason, record.outcome),
                         (RunStatus.RECLAIM_REQUIRED, "lock:vault", None))
        locked = [e for e in life.events() if e.kind is AuthEventKind.LOCKED and e.connection_id == "conn-api"]
        self.assertEqual([e.reason for e in locked], ["lock_missed:vault"])
        # The vault lock does not apply to host-session connections.
        self.assertIs(life.snapshot(session.connection_id).state, AuthState.VALID)
        # The pulse is consumed once; a later poll without a new lock changes nothing.
        self.assertEqual(life.snapshot("conn-api").revision, snap.revision)
        snap = life.revalidate("conn-api", snap.revision, observation=authenticated(api))
        self.assertIs(snap.state, AuthState.VALID)

    def test_default_lock_policy_keeps_running_work_active(self):
        vault, checker = self.make_vault()
        ref = vault.put("synthetic-api-key")
        signal = ManualLockSignal()
        life = AuthLifecycle(clock=self.clock, credential_checker=checker, vault_lock_source=signal,
                             policy=LockPolicy(vault_lock_blocks=True))
        api = api_binding(ref)
        life.register(api)
        life.revalidate("conn-api", 1, observation=authenticated(api))
        life.begin_run("conn-api", "run-a")
        signal.lock()
        self.assertIs(life.snapshot("conn-api").state, AuthState.LOCKED)
        record = life.run("run-a")
        self.assertEqual((record.status, record.reclaim_reason), (RunStatus.ACTIVE, None))
        signal.unlock()
        signal.lock()
        signal.unlock()
        self.assertIs(life.snapshot("conn-api").state, AuthState.UNKNOWN)
        self.assertIs(life.run("run-a").status, RunStatus.ACTIVE)
        self.assertNotIn(AuthEventKind.RECLAIM_REQUIRED, [e.kind for e in life.events()])

    def test_session_lock_signal_failures_are_fail_closed_and_policy_can_reclaim(self):
        class Signal:
            mode = "unlocked"

            def __call__(self):
                if self.mode == "raise":
                    raise OSError("synthetic signal failure")
                if self.mode == "garbage":
                    return "locked?"
                return self.mode == "locked"

        signal = Signal()
        life = AuthLifecycle(clock=self.clock, session_lock_source=signal,
                             policy=LockPolicy(session_lock_blocks=True, reclaim_running_on_lock=True))
        binding = self.valid_session(life)
        life.begin_run(binding.connection_id, "run-before-lock")
        signal.mode = "raise"
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(binding.connection_id, "run-signal-error")
        snap = life.snapshot(binding.connection_id)
        self.assertEqual((snap.state, snap.lock_reasons), (AuthState.LOCKED, ("windows_session",)))
        record = life.run("run-before-lock")
        self.assertEqual((record.status, record.reclaim_reason, record.outcome),
                         (RunStatus.RECLAIM_REQUIRED, "lock:windows_session", None))
        signal.mode = "garbage"
        self.assertIs(life.snapshot(binding.connection_id).state, AuthState.LOCKED)
        signal.mode = "unlocked"
        self.assertIs(life.snapshot(binding.connection_id).state, AuthState.UNKNOWN)

    def test_blocking_lock_policy_requires_a_signal_source(self):
        for policy in (LockPolicy(vault_lock_blocks=True), LockPolicy(session_lock_blocks=True)):
            with self.subTest(policy=policy):
                with self.assertRaises(AuthLifecycleError) as raised:
                    AuthLifecycle(clock=self.clock, policy=policy)
                self.assertEqual(raised.exception.code, "LOCK_SOURCE_REQUIRED")
        with self.assertRaises(AuthLifecycleError):
            LockPolicy(session_lock_blocks="yes")

    @unittest.skipIf(os.name == "nt", "Non-Windows fail-closed behaviour only")
    def test_windows_session_signal_outside_windows_is_treated_as_locked(self):
        with self.assertRaises(AuthLifecycleError):
            windows_session_locked()
        life = AuthLifecycle(clock=self.clock, session_lock_source=windows_session_locked,
                             policy=LockPolicy(session_lock_blocks=True))
        binding = session_binding()
        life.register(binding)
        self.assertIs(life.snapshot(binding.connection_id).state, AuthState.LOCKED)

    def test_forced_logout_is_not_completion_or_refund(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        cid = binding.connection_id
        life.begin_run(cid, "run-in-flight")
        snap = life.force_logout(cid, 2)
        self.assertIs(snap.state, AuthState.REVOKED)
        logout = [e for e in life.events() if e.kind is AuthEventKind.LOGOUT_FORCED][-1]
        self.assertTrue(logout.reauth_required)
        record = life.run("run-in-flight")
        self.assertEqual((record.status, record.outcome, record.reclaim_reason),
                         (RunStatus.RECLAIM_REQUIRED, None, "forced_logout"))
        still = life.record_observation(receipt("run-in-flight", binding, JobState.RUNNING))
        self.assertIs(still.status, RunStatus.RECLAIM_REQUIRED)
        with self.assertRaises(AuthLifecycleError) as raised:
            life.revalidate(cid, snap.revision, observation=authenticated(binding))
        self.assertEqual(raised.exception.code, "REAUTH_REQUIRED")
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(cid, "run-after-logout")
        closed = life.record_observation(receipt("run-in-flight", binding, JobState.CANCELLED))
        self.assertEqual((closed.status, closed.outcome), (RunStatus.CLOSED, "cancelled"))
        self.assertNotIn(closed.outcome, ("succeeded", "refunded"))
        with self.assertRaises(AuthLifecycleError):
            life.record_observation(receipt("run-in-flight", binding, JobState.SUCCEEDED))

    def test_forced_logout_recovers_only_after_reauthentication_and_revalidation(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        cid = binding.connection_id
        snap = life.force_logout(cid, 2)
        with self.assertRaises(AuthLifecycleError) as raised:
            life.begin_rotation(cid, snap.revision)
        self.assertEqual(raised.exception.code, "REAUTH_REQUIRED")
        renewed = session_binding(revision=2)
        snap = life.reauthenticate(cid, renewed, snap.revision)
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertFalse(snap.auth_valid)
        with self.assertRaises(AuthenticationRequired):
            life.begin_run(cid, "run-before-revalidation")
        snap = life.revalidate(cid, snap.revision, observation=authenticated(binding))
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual(life.events()[-1].reason, "stale_observation")
        snap = life.revalidate(cid, snap.revision, observation=authenticated(renewed))
        self.assertIs(snap.state, AuthState.VALID)
        ticket = life.begin_run(cid, "run-after-relogin")
        self.assertEqual((ticket.binding.revision, ticket.credential_version), (2, 1))

    # ----------------------------------------------------------------- 기타
    def test_vault_reference_checks_are_fail_closed(self):
        vault, checker = self.make_vault()
        ref = vault.put("synthetic-api-key")
        binding = api_binding(ref)
        with self.assertRaises(CredentialRefUnavailable):
            AuthLifecycle(clock=self.clock).register(binding)
        life = AuthLifecycle(clock=self.clock, credential_checker=checker)
        life.register(binding)
        life.begin_rotation("conn-api", 1)
        missing = api_binding("secret://aik-vault/" + "0" * 32, revision=2)
        with self.assertRaises(CredentialRefUnavailable):
            life.complete_rotation("conn-api", missing, 2)
        snap = life.snapshot("conn-api")
        self.assertEqual((snap.state, snap.revision, snap.credential_version), (AuthState.ROTATING, 2, 1))
        snap = life.cancel_rotation("conn-api", 2)
        vault.delete(ref)
        snap = life.revalidate("conn-api", snap.revision, observation=authenticated(binding))
        self.assertIs(snap.state, AuthState.UNKNOWN)
        self.assertEqual(life.events()[-1].reason, "credential_unavailable")

    def test_listener_failures_are_counted_and_secrets_never_appear(self):
        vault, checker = self.make_vault()
        ref = vault.put(SECRET_MARKER)
        received = []

        def listener(event):
            received.append(event)
            if event.kind is AuthEventKind.REVALIDATED:
                raise RuntimeError("synthetic listener failure")

        life = AuthLifecycle(clock=self.clock, credential_checker=checker, listener=listener)
        binding = api_binding(ref)
        life.register(binding)
        snap = life.revalidate("conn-api", 1, observation=authenticated(binding))
        self.assertIs(snap.state, AuthState.VALID)
        self.assertEqual(life.listener_failures, 1)
        self.assertEqual([e.kind for e in received], [AuthEventKind.REGISTERED, AuthEventKind.REVALIDATED])
        text = repr((life.events(), life.poll(), life.runs()))
        self.assertNotIn(SECRET_MARKER, text)

    def test_input_validation(self):
        life = AuthLifecycle(clock=self.clock)
        binding = self.valid_session(life)
        with self.assertRaises(AuthLifecycleError):
            life.begin_run(binding.connection_id, "../bad")
        life.begin_run(binding.connection_id, "run-1")
        with self.assertRaises(AuthLifecycleError) as raised:
            life.begin_run(binding.connection_id, "run-1")
        self.assertEqual(raised.exception.code, "DUPLICATE_RUN")
        with self.assertRaises(AuthLifecycleError):
            life.revalidate(binding.connection_id, True)
        with self.assertRaises(AuthLifecycleError):
            life.register({"connection_id": "x"})
        with self.assertRaises(AuthLifecycleError):
            life.register(session_binding(cid="conn-naive"), expires_at=datetime(2030, 1, 1))
        with self.assertRaises(AuthLifecycleError) as raised:
            life.reauthenticate(binding.connection_id, session_binding(revision=1), 2)
        self.assertEqual(raised.exception.code, "STALE_BINDING_REVISION")
        with self.assertRaises(UnknownConnection):
            life.snapshot("conn-missing")


if __name__ == "__main__":
    unittest.main()
