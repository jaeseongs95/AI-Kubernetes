"""AIK-011 auth lifecycle regressions; no live credentials or provider effects."""
from __future__ import annotations

from dataclasses import replace
import threading
import unittest

from test_provider_contract import (
    FakeAdapter, binding, request, authorized, AccountIsolation, AdapterFailure,
    AuthObservation, AuthState, FaultKind, GuardedAdapter, JobState,
    Outcome, ProviderFault,
)


class AuthAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.signin_calls = []
        self.signout_calls = []
        self.signin_state = AuthState.PENDING
        self.signout_state = AuthState.UNAUTHENTICATED
        self.auth_fault = None
        self.start_state = JobState.ACCEPTED

    def _change(self, bound, state, calls):
        calls.append(bound)
        if self.auth_fault is not None:
            raise self.auth_fault
        fingerprint = bound.account_fingerprint if state is AuthState.AUTHENTICATED else None
        return AuthObservation(bound.connection_id, bound.revision, fingerprint, state)

    def begin_sign_in(self, bound):
        return self._change(bound, self.signin_state, self.signin_calls)

    def sign_out(self, bound):
        return self._change(bound, self.signout_state, self.signout_calls)

    def start_job(self, req):
        return replace(super().start_job(req), state=self.start_state)


def other():
    return binding(name="conn-2", fingerprint="b" * 64)


def observed(bound, state, fingerprint=None):
    return AuthObservation(bound.connection_id, bound.revision,
                           bound.account_fingerprint if state is AuthState.AUTHENTICATED and fingerprint is None
                           else fingerprint, state)


class ProviderLifecycleTests(unittest.TestCase):
    def refused(self, expected, function, *args):
        with self.assertRaises(AdapterFailure) as failure:
            function(*args)
        self.assertEqual(failure.exception.fault.kind, expected)
        return failure.exception

    # Eight targeted regressions run separately against the pinned old source.
    def test_01_pending_login_reserves_single_active_host(self):
        adapter = AuthAdapter(); guard = authorized(adapter)
        self.assertEqual(guard.begin_sign_in(binding()).state, AuthState.PENDING)
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, other())
        self.assertEqual(len(adapter.signin_calls), 1)

    def test_02_pending_login_prevents_dispatch_for_another_account(self):
        adapter = AuthAdapter(); guard = authorized(adapter)
        guard.begin_sign_in(binding())
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.start_job, request(other()))
        self.assertFalse(adapter.start_calls)

    def test_03_pending_logout_does_not_release_still_authenticated_context(self):
        adapter = AuthAdapter(); adapter.signout_state = AuthState.PENDING
        guard = authorized(adapter); target = binding()
        guard.sign_out(target)
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.start_job, request(target))
        self.assertFalse(adapter.start_calls)

    def test_04_typed_unknown_auth_failure_fences_both_mutation_paths(self):
        for method in ("begin_sign_in", "sign_out"):
            with self.subTest(method=method):
                adapter = AuthAdapter(); guard = authorized(adapter)
                adapter.auth_fault = AdapterFailure(ProviderFault(FaultKind.PROVIDER_FAILURE, Outcome.UNKNOWN))
                self.refused(FaultKind.PROVIDER_FAILURE, getattr(guard, method), binding())
                self.assertIn(("openai", "codex-app-server"), guard.uncertain_hosts)
                adapter.auth_fault = None
                self.refused(FaultKind.UNKNOWN_OUTCOME, guard.start_job, request(binding()))
                self.assertFalse(adapter.start_calls)

    def test_05_isolation_does_not_allow_relogin_of_its_own_active_connection(self):
        adapter = AuthAdapter(); adapter.isolation = AccountIsolation.ISOLATED
        guard = authorized(adapter); target = binding()
        guard.start_job(request(target))
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, target)
        self.assertFalse(adapter.signin_calls)

    def test_06_authorization_callback_cannot_reenter_dispatch(self):
        adapter = AuthAdapter(); nested = []; entered = False
        def authorize(req):
            nonlocal entered
            if not entered:
                entered = True
                try:
                    guard.start_job(req)
                except AdapterFailure as failure:
                    nested.append(failure.fault.kind)
            return True
        guard = GuardedAdapter(adapter, authorize_dispatch=authorize)
        guard.start_job(request(binding()))
        self.assertEqual(len(adapter.start_calls), 1)
        self.assertEqual(nested, [FaultKind.ACCOUNT_CONFLICT])

    def test_07_interrupt_timeout_cannot_be_replayed_automatically(self):
        adapter = AuthAdapter(); guard = authorized(adapter)
        guard.start_job(request(binding()))
        adapter.interrupt_fault = TimeoutError("synthetic-private-detail")
        self.refused(FaultKind.UNKNOWN_OUTCOME, guard.interrupt_job, "request-1")
        error = self.refused(FaultKind.UNKNOWN_OUTCOME, guard.interrupt_job, "request-1")
        self.assertEqual(len(adapter.interrupt_calls), 1)
        self.assertNotIn("synthetic-private-detail", str(error))

    def test_08_account_is_rechecked_after_external_authorization(self):
        adapter = AuthAdapter(); target = binding()
        def authorize(req):
            adapter.auth_override[target.connection_id] = observed(target, AuthState.AUTHENTICATED, "b" * 64)
            return True
        guard = GuardedAdapter(adapter, authorize_dispatch=authorize)
        self.refused(FaultKind.ACCOUNT_CHANGED, guard.start_job, request(target))
        self.assertFalse(adapter.start_calls)

    def test_pending_login_is_not_started_twice_for_same_binding(self):
        adapter = AuthAdapter(); guard = authorized(adapter); target = binding()
        guard.begin_sign_in(target)
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, target)
        self.assertEqual(adapter.signin_calls, [target])

    def test_matching_authenticated_observation_releases_only_completed_login(self):
        adapter = AuthAdapter(); guard = authorized(adapter); target = binding()
        guard.begin_sign_in(target)
        self.assertEqual(guard.auth_status(target).state, AuthState.AUTHENTICATED)
        self.assertEqual(guard.begin_sign_in(other()).state, AuthState.PENDING)
        self.assertEqual(len(adapter.signin_calls), 2)

    def test_another_revision_status_cannot_release_original_reservation(self):
        adapter = AuthAdapter(); guard = authorized(adapter); target = binding()
        guard.begin_sign_in(target)
        later = replace(target, revision=2)
        adapter.auth_override[target.connection_id] = observed(later, AuthState.AUTHENTICATED)
        guard.auth_status(later)
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, other())
        self.refused(FaultKind.STALE_CONNECTION, guard.auth_status, target)
        self.assertEqual(len(adapter.signin_calls), 1)

    def test_wrong_account_observation_cannot_release_original_reservation(self):
        adapter = AuthAdapter(); guard = authorized(adapter); target = binding()
        guard.begin_sign_in(target)
        adapter.auth_override[target.connection_id] = observed(target, AuthState.AUTHENTICATED, "b" * 64)
        self.refused(FaultKind.ACCOUNT_CHANGED, guard.auth_status, target)
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, other())

    def test_unauthenticated_status_does_not_prove_pending_login_finished(self):
        adapter = AuthAdapter(); guard = authorized(adapter); target = binding()
        guard.begin_sign_in(target)
        for state in (AuthState.UNAUTHENTICATED, AuthState.PENDING, AuthState.LOCKED, AuthState.UNKNOWN):
            adapter.auth_override[target.connection_id] = observed(target, state)
            self.assertEqual(guard.auth_status(target).state, state)
            self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, other())
        self.assertEqual(len(adapter.signin_calls), 1)

    def test_logout_releases_only_after_same_binding_observes_unauthenticated(self):
        adapter = AuthAdapter(); adapter.signout_state = AuthState.PENDING
        guard = authorized(adapter); target = binding()
        guard.sign_out(target)
        guard.auth_status(target)  # Still authenticated: no completion evidence.
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, other())
        adapter.auth_override[target.connection_id] = observed(target, AuthState.UNAUTHENTICATED)
        guard.auth_status(target)
        guard.begin_sign_in(other())
        self.assertEqual(len(adapter.signout_calls), 1)

    def test_isolated_pending_contexts_allow_other_connections_not_same_revision_aliases(self):
        adapter = AuthAdapter(); adapter.isolation = AccountIsolation.ISOLATED
        guard = authorized(adapter); target = binding()
        guard.begin_sign_in(target)
        guard.begin_sign_in(other())
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, replace(target, revision=2))
        self.assertEqual(len(adapter.signin_calls), 2)

    def test_later_isolation_claim_cannot_widen_a_pending_shared_context(self):
        adapter = AuthAdapter(); guard = authorized(adapter)
        guard.begin_sign_in(binding())
        adapter.isolation = AccountIsolation.ISOLATED
        self.refused(FaultKind.ACCOUNT_CONFLICT, guard.begin_sign_in, other())
        self.assertEqual(len(adapter.signin_calls), 1)

    def test_explicit_known_auth_failure_releases_its_reservation(self):
        for method in ("begin_sign_in", "sign_out"):
            with self.subTest(method=method):
                adapter = AuthAdapter(); guard = authorized(adapter)
                adapter.auth_fault = AdapterFailure(ProviderFault(FaultKind.RATE_LIMITED, Outcome.KNOWN_FAILURE))
                self.refused(FaultKind.RATE_LIMITED, getattr(guard, method), binding())
                adapter.auth_fault = None
                self.assertFalse(guard.uncertain_hosts)
                guard.begin_sign_in(other())

    def test_bad_auth_response_keeps_host_fenced_without_echoing_payload(self):
        for method in ("begin_sign_in", "sign_out"):
            with self.subTest(method=method):
                adapter = AuthAdapter(); guard = authorized(adapter)
                setattr(adapter, method, lambda target: {"secret": "synthetic-private-detail"})
                error = self.refused(FaultKind.INVALID_CONTRACT, getattr(guard, method), binding())
                self.assertNotIn("synthetic-private-detail", str(error))
                self.refused(FaultKind.UNKNOWN_OUTCOME, guard.start_job, request(binding()))

    def test_unknown_auth_state_cannot_be_cleared_by_a_later_healthy_read(self):
        adapter = AuthAdapter(); adapter.signin_state = AuthState.UNKNOWN
        guard = authorized(adapter); target = binding()
        self.assertEqual(guard.begin_sign_in(target).state, AuthState.UNKNOWN)
        self.assertEqual(guard.auth_status(target).state, AuthState.AUTHENTICATED)
        self.refused(FaultKind.UNKNOWN_OUTCOME, guard.start_job, request(target))
        self.assertFalse(adapter.start_calls)

    def test_unknown_start_receipt_fences_new_effects_but_allows_original_observation(self):
        adapter = AuthAdapter(); adapter.start_state = JobState.UNKNOWN
        guard = authorized(adapter); target = binding()
        guard.start_job(request(target))
        self.refused(FaultKind.UNKNOWN_OUTCOME, guard.start_job, request(target, "request-2"))
        adapter.next_observed_state = JobState.SUCCEEDED
        self.assertEqual(guard.observe_job("request-1").receipt.state, JobState.SUCCEEDED)
        self.refused(FaultKind.UNKNOWN_OUTCOME, guard.start_job, request(target, "request-3"))
        self.assertEqual(len(adapter.start_calls), 1)

    def test_unknown_interrupt_receipt_fences_repeat_interrupt_not_readonly_lookup(self):
        adapter = AuthAdapter(); guard = authorized(adapter)
        guard.start_job(request(binding()))
        adapter.next_interrupted_state = JobState.UNKNOWN
        self.assertEqual(guard.interrupt_job("request-1").receipt.state, JobState.UNKNOWN)
        self.refused(FaultKind.UNKNOWN_OUTCOME, guard.interrupt_job, "request-1")
        self.assertEqual(guard.observe_job("request-1").receipt.state, JobState.RUNNING)
        self.assertEqual(len(adapter.interrupt_calls), 1)

    def test_provider_callback_cannot_reenter_auth_mutation_and_local_getters_stay_usable(self):
        adapter = AuthAdapter(); guard = authorized(adapter); target = binding(); rejected = []
        original_status = adapter.auth_status
        def status(bound):
            self.assertEqual(guard.unresolved(), ())
            try:
                guard.begin_sign_in(bound)
            except AdapterFailure as error:
                rejected.append(error.fault.kind)
            return original_status(bound)
        adapter.auth_status = status
        guard.start_job(request(target))
        self.assertTrue(rejected)
        self.assertTrue(all(kind is FaultKind.ACCOUNT_CONFLICT for kind in rejected))
        self.assertFalse(adapter.signin_calls)
        self.assertEqual(len(adapter.start_calls), 1)

    def test_concurrent_login_requests_cannot_both_claim_single_active_host(self):
        adapter = AuthAdapter(); guard = authorized(adapter); barrier = threading.Barrier(3)
        outcomes = []
        def login(target):
            barrier.wait(timeout=5)
            try:
                guard.begin_sign_in(target); outcomes.append("pending")
            except AdapterFailure as error:
                outcomes.append(error.fault.kind.value)
        threads = [threading.Thread(target=login, args=(target,)) for target in (binding(), other())]
        for thread in threads: thread.start()
        barrier.wait(timeout=5)
        for thread in threads: thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["pending", "ACCOUNT_CONFLICT"])
        self.assertEqual(len(adapter.signin_calls), 1)

    def test_invalid_bindings_fail_with_a_fixed_contract_code(self):
        guard = authorized(AuthAdapter())
        for method in (guard.capabilities, guard.auth_status, guard.begin_sign_in, guard.sign_out):
            self.refused(FaultKind.INVALID_CONTRACT, method, {"secret": "synthetic-private-detail"})


if __name__ == "__main__":
    unittest.main()
