"""AIK-011 synthetic adapter conformance and account conflict regressions."""
from __future__ import annotations

from dataclasses import fields, replace
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aik_provider import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind,
    AuthObservation, AuthState, ConnectionBinding, FaultKind, GuardedAdapter,
    JobReceipt, JobRequest, JobState, Outcome, ProviderAdapter,
    ProviderFault, Support, fault_from_provider,
)


def binding(
    *, name="conn-1", fingerprint="a" * 64,
    kind=AuthKind.NATIVE_SUBSCRIPTION, revision=1,
) -> ConnectionBinding:
    ref = "host-session://codex/session-1" if kind is AuthKind.NATIVE_SUBSCRIPTION else "secret://local/key-1"
    return ConnectionBinding("openai", "codex-app-server", name, revision, kind, ref, fingerprint)


def request(bound: ConnectionBinding, name="request-1") -> JobRequest:
    return JobRequest(bound, name, "route-001", "vm-attempt-001", "synthetic-model")


def code(exc: AdapterFailure) -> FaultKind:
    return exc.fault.kind


class FakeAdapter:
    """No vendor calls, filesystem effects or user credentials."""

    def __init__(self):
        self.isolation = AccountIsolation.SINGLE_ACTIVE
        self.auth_override = {}
        self.start_support = Support.SUPPORTED
        self.observe_support = Support.SUPPORTED
        self.interrupt_support = Support.SUPPORTED
        self.sign_in_support = Support.SUPPORTED
        self.start_calls = []
        self.status_calls = []
        self.observe_calls = []
        self.interrupt_calls = []
        self.job_id = None
        self.start_fault = None
        self.observe_fault = None
        self.interrupt_fault = None
        self.bad_receipt = None
        self.next_observed_state = JobState.RUNNING
        self.next_interrupted_state = JobState.RUNNING
        self.caps_override = None
        self.auth_exception = None

    def describe(self, bound):
        if self.caps_override is not None:
            return self.caps_override
        return AdapterCapabilities(
            provider=bound.provider, runtime=bound.runtime, auth_kind=bound.auth_kind,
            isolation=self.isolation, auth_status=Support.SUPPORTED,
            sign_in=Support.UNSUPPORTED if bound.auth_kind is AuthKind.API_KEY else self.sign_in_support,
            sign_out=Support.SUPPORTED, job_start=self.start_support,
            job_observe=self.observe_support, job_interrupt=self.interrupt_support,
        )

    def auth_status(self, bound):
        self.status_calls.append(bound)
        if self.auth_exception is not None:
            raise self.auth_exception
        return self.auth_override.get(bound.connection_id, AuthObservation(
            bound.connection_id, bound.revision, bound.account_fingerprint, AuthState.AUTHENTICATED
        ))

    def begin_sign_in(self, bound):
        return AuthObservation(bound.connection_id, bound.revision, bound.account_fingerprint, AuthState.PENDING)

    def sign_out(self, bound):
        return AuthObservation(bound.connection_id, bound.revision, None, AuthState.UNAUTHENTICATED)

    def start_job(self, req):
        self.start_calls.append(req)
        if self.start_fault is not None:
            raise self.start_fault
        if self.bad_receipt is not None:
            return self.bad_receipt
        return JobReceipt(req.request_id, self.job_id or ("provider-job-" + str(len(self.start_calls))),
                          req.binding.connection_id, req.binding.revision,
                          req.binding.account_fingerprint, JobState.ACCEPTED)

    def observe_job(self, req, receipt):
        self.observe_calls.append((req, receipt))
        if self.observe_fault is not None:
            raise self.observe_fault
        if self.bad_receipt is not None:
            return self.bad_receipt
        return replace(receipt, state=self.next_observed_state)

    def interrupt_job(self, req, receipt):
        self.interrupt_calls.append((req, receipt))
        if self.interrupt_fault is not None:
            raise self.interrupt_fault
        return replace(receipt, state=self.next_interrupted_state)


def authorized(adapter):
    # Synthetic fixture only; a product must call VM/AGS/AIK authorization.
    return GuardedAdapter(adapter, authorize_dispatch=lambda request: True)


class ProviderContractTests(unittest.TestCase):
    def test_runtime_protocol_is_explicitly_implemented(self):
        self.assertIsInstance(FakeAdapter(), ProviderAdapter)

    def test_binding_ref_is_opaque_and_auth_kinds_not_interchangeable(self):
        self.assertEqual(binding().auth_kind, AuthKind.NATIVE_SUBSCRIPTION)
        self.assertEqual(binding(kind=AuthKind.API_KEY).credential_ref, "secret://local/key-1")
        with self.assertRaisesRegex(ValueError, "AUTH_KINDS_ARE_NOT_INTERCHANGEABLE"):
            replace(binding(), credential_ref="secret://local/key-1")
        with self.assertRaisesRegex(ValueError, "AUTH_KINDS_ARE_NOT_INTERCHANGEABLE"):
            replace(binding(kind=AuthKind.API_KEY), credential_ref="host-session://codex/session-1")
        with self.assertRaisesRegex(ValueError, "INVALID_CREDENTIAL_REFERENCE"):
            replace(binding(), credential_ref="host-session://codex/../other")
        with self.assertRaisesRegex(ValueError, "INVALID_CREDENTIAL_REFERENCE"):
            replace(binding(), credential_ref="actual-sk-secret")
        with self.assertRaisesRegex(ValueError, "INVALID_ACCOUNT_FINGERPRINT"):
            replace(binding(), account_fingerprint="user@example.com")

    def test_type_validation_excludes_boolean_revision_and_unknown_states(self):
        with self.assertRaisesRegex(ValueError, "INVALID_CONNECTION_REVISION"):
            replace(binding(), revision=True)
        with self.assertRaisesRegex(ValueError, "INVALID_AUTH_KIND"):
            replace(binding(), auth_kind="api_key")
        with self.assertRaisesRegex(ValueError, "INVALID_JOB_STATE"):
            JobReceipt("request-1", "job-1", "conn-1", 1, "a" * 64, "succeeded")

    def test_api_key_connection_has_no_subscription_host_sign_in(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding(kind=AuthKind.API_KEY)
        caps = guard.capabilities(target)
        self.assertIs(caps.sign_in, Support.UNSUPPORTED)
        with self.assertRaises(AdapterFailure) as error:
            guard.begin_sign_in(target)
        self.assertEqual(code(error.exception), FaultKind.UNSUPPORTED)
        self.assertEqual(guard.auth_status(target).state, AuthState.AUTHENTICATED)

    def test_implementation_disclosure_is_not_operational_qualification(self):
        caps = authorized(FakeAdapter()).capabilities(binding())
        self.assertEqual(caps.implementation_state, "implemented")
        self.assertEqual(caps.qualification_status, "NOT_RUN")
        with self.assertRaisesRegex(ValueError, "QUALIFICATION_REQUIRES_EXTERNAL_EVIDENCE"):
            replace(caps, qualification_status="QUALIFIED")
        with self.assertRaisesRegex(ValueError, "API_KEY_IS_NOT_HOST_SIGN_IN"):
            replace(caps, auth_kind=AuthKind.API_KEY, sign_in=Support.SUPPORTED)

    def test_auth_success_does_not_imply_supported_job_execution(self):
        adapter = FakeAdapter()
        adapter.start_support = Support.UNKNOWN
        guard = authorized(adapter)
        target = binding()
        self.assertEqual(guard.auth_status(target).state, AuthState.AUTHENTICATED)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(target))
        self.assertEqual(code(error.exception), FaultKind.UNSUPPORTED)
        self.assertFalse(adapter.start_calls)

    def test_status_mismatch_revision_blocks_provider_effect(self):
        adapter = FakeAdapter()
        bound = binding()
        adapter.auth_override[bound.connection_id] = AuthObservation(
            bound.connection_id, 2, bound.account_fingerprint, AuthState.AUTHENTICATED
        )
        with self.assertRaises(AdapterFailure) as error:
            authorized(adapter).start_job(request(bound))
        self.assertEqual(code(error.exception), FaultKind.STALE_CONNECTION)
        self.assertFalse(adapter.start_calls)

    def test_host_login_changed_does_not_silently_fallback(self):
        adapter = FakeAdapter()
        bound = binding()
        adapter.auth_override[bound.connection_id] = AuthObservation(
            bound.connection_id, bound.revision, "b" * 64, AuthState.AUTHENTICATED
        )
        with self.assertRaises(AdapterFailure) as error:
            authorized(adapter).start_job(request(bound))
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CHANGED)
        self.assertFalse(adapter.start_calls)

    def test_expired_and_unknown_auth_reject_job_start(self):
        for state in (AuthState.EXPIRED, AuthState.LOCKED, AuthState.UNKNOWN, AuthState.PENDING):
            with self.subTest(state=state):
                adapter = FakeAdapter()
                bound = binding()
                adapter.auth_override[bound.connection_id] = AuthObservation(
                    bound.connection_id, 1, None, state
                )
                with self.assertRaises(AdapterFailure) as error:
                    authorized(adapter).start_job(request(bound))
                self.assertEqual(code(error.exception), FaultKind.AUTH_REQUIRED)
                self.assertFalse(adapter.start_calls)

    def test_provider_capability_mismatch_rejected(self):
        adapter = FakeAdapter()
        adapter.caps_override = replace(adapter.describe(binding()), provider="other")
        with self.assertRaises(AdapterFailure) as error:
            authorized(adapter).start_job(request(binding()))
        self.assertEqual(code(error.exception), FaultKind.STALE_CONNECTION)
        self.assertFalse(adapter.start_calls)

    def test_isolation_unknown_blocks_native_job_dispatch(self):
        adapter = FakeAdapter()
        adapter.isolation = AccountIsolation.UNKNOWN
        with self.assertRaises(AdapterFailure) as error:
            authorized(adapter).start_job(request(binding()))
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CONFLICT)
        self.assertFalse(adapter.start_calls)

    def test_non_isolated_host_rejects_other_account_during_active_job(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        a = binding()
        b = binding(name="conn-2", fingerprint="b" * 64)
        guard.start_job(request(a, "request-a"))
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(b, "request-b"))
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CONFLICT)
        self.assertEqual(len(adapter.start_calls), 1)
        self.assertEqual(len(guard.unresolved()), 1)

    def test_completed_job_releases_single_active_account_slot(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        a = binding()
        b = binding(name="conn-2", fingerprint="b" * 64)
        guard.start_job(request(a, "request-a"))
        adapter.next_observed_state = JobState.SUCCEEDED
        observed = guard.observe_job("request-a")
        self.assertIs(observed.receipt.state, JobState.SUCCEEDED)
        self.assertEqual(len(guard.unresolved()), 0)
        accepted = guard.start_job(request(b, "request-b"))
        self.assertEqual(accepted.request.binding, b)
        self.assertEqual(adapter.status_calls[-1].connection_id, "conn-2")

    def test_job_cancel_ack_does_not_release_native_account(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        guard.start_job(request(binding(), "request-a"))
        pending = guard.interrupt_job("request-a")
        self.assertEqual(pending.receipt.state, JobState.RUNNING)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding(name="conn-b", fingerprint="b" * 64), "request-b"))
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CONFLICT)
        adapter.next_interrupted_state = JobState.CANCELLED
        cancelled = guard.interrupt_job("request-a")
        self.assertEqual(cancelled.receipt.state, JobState.CANCELLED)

    def test_isolated_accounts_can_run_simultaneously(self):
        adapter = FakeAdapter()
        adapter.isolation = AccountIsolation.ISOLATED
        guard = authorized(adapter)
        guard.start_job(request(binding(), "request-a"))
        guard.start_job(request(binding(name="conn-2", fingerprint="b" * 64), "request-b"))
        self.assertEqual(len(guard.unresolved()), 2)

    def test_non_isolated_host_can_run_same_bound_account_multiple_jobs(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding()
        guard.start_job(request(target, "request-a"))
        guard.start_job(request(target, "request-b"))
        self.assertEqual(len(guard.unresolved()), 2)

    def test_account_binding_revision_change_cannot_reuse_inflight_job(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        guard.start_job(request(binding(), "request-a"))
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding(revision=2), "request-b"))
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CONFLICT)

    def test_replay_never_autostarts_a_second_job(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        original = request(binding())
        first = guard.start_job(original)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(original)
        self.assertEqual(code(error.exception), FaultKind.DUPLICATE_REQUEST)
        self.assertEqual(len(adapter.start_calls), 1)
        self.assertEqual(guard.get("request-1"), first)

    def test_invalid_job_receipt_poisons_same_host_and_blocks_retry(self):
        adapter = FakeAdapter()
        adapter.bad_receipt = JobReceipt(
            "request-other", "job-other", "conn-1", 1, "a" * 64, JobState.ACCEPTED
        )
        guard = authorized(adapter)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding(), "request-a"))
        self.assertEqual(code(error.exception), FaultKind.INVALID_RECEIPT)
        self.assertIn(("openai", "codex-app-server"), guard.uncertain_hosts)
        with self.assertRaises(AdapterFailure) as second:
            guard.start_job(request(binding(), "request-b"))
        self.assertEqual(code(second.exception), FaultKind.UNKNOWN_OUTCOME)
        self.assertEqual(len(adapter.start_calls), 1)

    def test_transport_unknown_poison_prevents_unsafe_replay(self):
        adapter = FakeAdapter()
        adapter.start_fault = TimeoutError("raw secret must not be shown")
        guard = authorized(adapter)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding()))
        self.assertEqual(code(error.exception), FaultKind.UNKNOWN_OUTCOME)
        self.assertNotIn("raw secret", str(error.exception))
        with self.assertRaises(AdapterFailure) as second:
            guard.start_job(request(binding(), "request-b"))
        self.assertEqual(code(second.exception), FaultKind.UNKNOWN_OUTCOME)

    def test_provider_known_rate_limit_preserves_retry_without_poison(self):
        adapter = FakeAdapter()
        fault = fault_from_provider(http_status=429, native_code="rate_limit_exceeded",
                                    retry_after_ms=1250)
        adapter.start_fault = AdapterFailure(fault)
        guard = authorized(adapter)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding()))
        self.assertEqual(code(error.exception), FaultKind.RATE_LIMITED)
        self.assertEqual(error.exception.fault.native_code, "rate_limit_exceeded")
        self.assertEqual(error.exception.fault.retry_after_ms, 1250)
        self.assertFalse(guard.uncertain_hosts)
        adapter.start_fault = None
        guard.start_job(request(binding(), "request-new"))
        self.assertEqual(len(adapter.start_calls), 2)

    def test_unsafe_vendor_message_cannot_become_standard_error(self):
        native = "token=" + "SECRET-VALUE-IN-PROVIDER-BODY"
        fault = fault_from_provider(http_status=401, native_code=native)
        self.assertEqual(fault.kind, FaultKind.AUTH_REQUIRED)
        self.assertIsNone(fault.native_code)
        self.assertNotIn("SECRET", str(AdapterFailure(fault)))
        self.assertEqual(fault_from_provider(http_status=403).kind, FaultKind.ACCESS_DENIED)
        self.assertEqual(fault_from_provider(http_status=501).kind, FaultKind.UNSUPPORTED)
        unknown = fault_from_provider(http_status=504, outcome_unknown=True)
        self.assertEqual(unknown.kind, FaultKind.UNKNOWN_OUTCOME)
        self.assertEqual(unknown.outcome, Outcome.UNKNOWN)
        with self.assertRaisesRegex(ValueError, "INVALID_RETRY_AFTER"):
            fault_from_provider(http_status=429, retry_after_ms=-1)

    def test_observe_job_reuses_exact_original_route_and_account(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding()
        guard.start_job(request(target))
        result = guard.observe_job("request-1")
        self.assertEqual(result.receipt.state, JobState.RUNNING)
        self.assertEqual(adapter.observe_calls[0][0].binding, target)
        self.assertEqual(adapter.observe_calls[0][1].connection_id, target.connection_id)

    def test_changed_login_blocks_original_job_observation(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding()
        guard.start_job(request(target))
        adapter.auth_override[target.connection_id] = AuthObservation(
            target.connection_id, target.revision, "b" * 64, AuthState.AUTHENTICATED
        )
        with self.assertRaises(AdapterFailure) as error:
            guard.observe_job("request-1")
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CHANGED)
        self.assertEqual(len(adapter.observe_calls), 0)
        self.assertEqual(len(guard.unresolved()), 1)

    def test_wrong_provider_job_id_does_not_release_account(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding()
        guard.start_job(request(target))
        adapter.bad_receipt = JobReceipt(
            "request-1", "other-job", "conn-1", 1, "a" * 64, JobState.SUCCEEDED
        )
        with self.assertRaises(AdapterFailure) as error:
            guard.observe_job("request-1")
        self.assertEqual(code(error.exception), FaultKind.INVALID_RECEIPT)
        self.assertEqual(len(guard.unresolved()), 1)

    def test_sign_out_refuses_inflight_job_and_no_implicit_account_swap(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding()
        pending = guard.begin_sign_in(target)
        self.assertEqual(pending.state, AuthState.PENDING)
        guard.start_job(request(target))
        with self.assertRaises(AdapterFailure) as error:
            guard.sign_out(target)
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CONFLICT)
        adapter.next_observed_state = JobState.CANCELLED
        guard.observe_job("request-1")
        signed_out = guard.sign_out(target)
        self.assertEqual(signed_out.state, AuthState.UNAUTHENTICATED)

    def test_interrupt_unknown_retains_account_slot_and_poison(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        guard.start_job(request(binding()))
        adapter.interrupt_fault = TimeoutError("must stay unknown")
        with self.assertRaises(AdapterFailure) as error:
            guard.interrupt_job("request-1")
        self.assertEqual(code(error.exception), FaultKind.UNKNOWN_OUTCOME)
        self.assertEqual(len(guard.unresolved()), 1)
        self.assertTrue(guard.uncertain_hosts)

    def test_concurrent_account_claims_cannot_both_enter_single_active_host(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        start = threading.Barrier(3)
        outcomes = []

        def launch(bnd, name):
            start.wait()
            try:
                guard.start_job(request(bnd, name))
                outcomes.append("accepted")
            except AdapterFailure as err:
                outcomes.append(err.fault.kind.value)

        a = threading.Thread(target=launch, args=(binding(), "request-a"))
        b = threading.Thread(target=launch, args=(binding(name="conn-b", fingerprint="b" * 64), "request-b"))
        a.start()
        b.start()
        start.wait()
        a.join(timeout=5)
        b.join(timeout=5)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertCountEqual(outcomes, ["accepted", "ACCOUNT_CONFLICT"])
        self.assertEqual(len(adapter.start_calls), 1)

    def test_missing_or_forged_run_authorization_never_dispatches(self):
        for authorization in (
            None, lambda req: False, lambda req: 1,
            lambda req: (_ for _ in ()).throw(RuntimeError("secret exception"))
        ):
            with self.subTest(mode=str(type(authorization))):
                adapter = FakeAdapter()
                guard = GuardedAdapter(adapter, authorize_dispatch=authorization)
                with self.assertRaises(AdapterFailure) as error:
                    guard.start_job(request(binding()))
                self.assertEqual(code(error.exception), FaultKind.ACCESS_DENIED)
                self.assertNotIn("secret", str(error.exception))
                self.assertFalse(adapter.start_calls)

    def test_native_signin_cannot_race_active_single_account_job(self):
        adapter = FakeAdapter()
        guard = authorized(adapter)
        target = binding()
        guard.start_job(request(target))
        with self.assertRaises(AdapterFailure) as error:
            guard.begin_sign_in(target)
        self.assertEqual(code(error.exception), FaultKind.ACCOUNT_CONFLICT)

    def test_schema_is_versioned_and_matches_python_enum_contract(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "contracts/provider-adapter/v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "urn:aik:provider-adapter:v1")
        definitions = schema["$defs"]
        caps = definitions["capabilities"]
        self.assertEqual(set(caps["properties"]["job_start"]["enum"]), {s.value for s in Support})
        self.assertEqual(set(caps["properties"]["auth_kind"]["enum"]), {s.value for s in AuthKind})
        self.assertEqual(set(caps["properties"]["isolation"]["enum"]), {s.value for s in AccountIsolation})
        self.assertEqual(caps["properties"]["qualification_status"]["const"], "NOT_RUN")
        self.assertEqual(set(definitions["auth_observation"]["properties"]["state"]["enum"]),
                         {s.value for s in AuthState})
        self.assertEqual(set(definitions["job_receipt"]["properties"]["state"]["enum"]),
                         {s.value for s in JobState})
        self.assertEqual(set(definitions["provider_fault"]["properties"]["code"]["enum"]),
                         {s.value for s in FaultKind})
        schema_text = json.dumps(schema, ensure_ascii=False).lower()
        for forbidden in ("raw_secret", "api_key_value", "raw_response", "access_token_value",
                          "vm_verdict", "password_value"):
            self.assertNotIn('"' + forbidden + '"', schema_text)

    def test_upstream_matrix_auth_kinds_match_without_upgrading_support(self):
        root = Path(__file__).resolve().parents[1]
        matrix = json.loads((root / "contracts/upstream/support-matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(matrix["format"], "aik-support-matrix-v1")
        self.assertEqual(len(matrix["routes"]), 12)
        for route in matrix["routes"]:
            with self.subTest(route=route["route_id"]):
                self.assertIn(route["auth_kind"], [value.value for value in AuthKind])
                self.assertEqual(route["adapter_status"], "not_implemented")
                self.assertEqual(route["qualification_status"], "NOT_RUN")
                self.assertFalse(route["operationally_advertised"])

    def test_unknown_auth_probe_support_blocks_start_even_when_adapter_claims_auth(self):
        adapter = FakeAdapter()
        adapter.caps_override = replace(adapter.describe(binding()), auth_status=Support.UNKNOWN)
        guard = authorized(adapter)
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding()))
        self.assertEqual(code(error.exception), FaultKind.UNSUPPORTED)
        self.assertFalse(adapter.start_calls)

    def test_duplicate_provider_job_receipt_is_not_assumed_new_execution(self):
        adapter = FakeAdapter()
        adapter.job_id = "same-job"
        guard = authorized(adapter)
        guard.start_job(request(binding(), "request-a"))
        with self.assertRaises(AdapterFailure) as error:
            guard.start_job(request(binding(), "request-b"))
        self.assertEqual(code(error.exception), FaultKind.INVALID_RECEIPT)
        self.assertTrue(guard.uncertain_hosts)
        self.assertEqual(len(guard.unresolved()), 1)

    def test_no_secret_material_or_vm_authority_in_receipt_contract(self):
        receipt_names = {field.name for field in fields(JobReceipt)}
        req_names = {field.name for field in fields(JobRequest)}
        for forbidden in ("api_key", "secret", "token", "password", "vm_verdict",
                          "goal_status", "raw_prompt", "output_text"):
            self.assertNotIn(forbidden, receipt_names | req_names)
        self.assertEqual(set(req_names), {"binding", "request_id", "route_id", "vm_attempt_id", "model"})


if __name__ == "__main__":
    unittest.main()
