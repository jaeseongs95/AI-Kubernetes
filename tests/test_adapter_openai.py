"""AIK-012 OpenAI API adapter contract tests with synthetic fixtures only.

No network, no vendor, no real key. The fake transport and response bodies are
synthetic protocol fixtures based on unverified assumptions; passing them is
implementation conformance, not a vendor observation or route qualification.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_adapter_openai import (  # noqa: E402
    BILLING_BASIS, MODEL_AVAILABLE, MODEL_DENIED, MODEL_NOT_FOUND, AdapterStatus,
    HttpRequest, HttpResponse, Message, OpenAIApiAdapter, ToolCall, ToolResult,
    ToolSpec, TurnInput, UrllibTransport, slot_fingerprint,
)
import aik_adapter_openai.adapter as adapter_module  # noqa: E402
from aik_provider import (  # noqa: E402
    AdapterFailure, AuthState, CacheMode, CachePolicy, FaultKind, GuardedAdapter, JobRequest, JobState,
    Measured, Outcome, PromptCacheCapability, Provenance, ProviderAdapter, Support,
)
from aik_adapter_openai import Usage  # noqa: E402
from aik_runtime import CredentialRef  # noqa: E402
from aik_vault import CredentialNotFound, FakeVaultBackend, VaultAccessDenied  # noqa: E402


SECRET = "FAKE-openai-key-0000-SYNTHETIC-not-real-A"
ROTATED = "FAKE-openai-key-1111-SYNTHETIC-rotated-B"
MODEL = "synthetic-model-a"
TOOL = ToolSpec("read_file", "Read a synthetic file",
                {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


def resp(status: int, data=None, headers=(), raw: bytes | None = None) -> HttpResponse:
    body = raw if raw is not None else (b"" if data is None else json.dumps(data).encode("utf-8"))
    return HttpResponse(status, tuple(headers), body)


def completed(rid="resp_synthetic01", **extra) -> dict:
    data = {
        "id": rid, "object": "response", "status": "completed", "model": "synthetic-model-a-2030-01-01",
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "synthetic answer"}]}],
    }
    data.update(extra)
    return data


class FakeTransport:
    kind = "fixture"

    def __init__(self) -> None:
        self.script: dict[tuple[str, str], list] = {}
        self.seen: list[SimpleNamespace] = []
        self.unexpected: list[tuple[str, str]] = []

    def add(self, method: str, path: str, *outcomes) -> None:
        self.script.setdefault((method, path), []).extend(outcomes)

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        self.seen.append(SimpleNamespace(method=request.method, path=request.path,
                                         authorization=request.header("Authorization"), body=request.body))
        queue = self.script.get((request.method, request.path))
        if not queue:
            self.unexpected.append((request.method, request.path))
            return HttpResponse(599, (), b"")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FlakyVault(FakeVaultBackend):
    """Synthetic fault injection: each queued exception fails one call before storage is touched."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.injected = {"put": [], "delete": [], "resolve": []}
        self.stored: list = []

    def _maybe_fail(self, operation: str) -> None:
        queue = self.injected[operation]
        if queue:
            raise queue.pop(0)

    def put(self, *args, **kwargs):
        self._maybe_fail("put")
        reference = super().put(*args, **kwargs)
        self.stored.append(reference)
        return reference

    def delete(self, *args, **kwargs):
        self._maybe_fail("delete")
        return super().delete(*args, **kwargs)

    def resolve(self, *args, **kwargs):
        self._maybe_fail("resolve")
        return super().resolve(*args, **kwargs)


class OpenAIAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-openai-adapter-test-")
        self.addCleanup(temporary.cleanup)
        self.temp_root = Path(temporary.name)
        self.vault = FakeVaultBackend(self.temp_root / "vault", key=bytes(range(32)))
        self.transport = FakeTransport()
        self.now = 1000.0
        self.adapter = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.assertEqual(self.transport.unexpected, [])
        self.assert_no_secret(self.adapter.events(), json.dumps(self.adapter.events()))
        directory = self.vault.directory
        for path in (directory.iterdir() if directory.exists() else ()):
            if not path.is_file():
                continue
            data = path.read_bytes()
            for secret in (SECRET, ROTATED):
                self.assertNotIn(secret.encode("ascii"), data)

    # -- helpers ------------------------------------------------------------
    def vault_files(self) -> list:
        directory = self.vault.directory
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else []

    def use_flaky_vault(self) -> None:
        self.vault = FlakyVault(self.temp_root / "flaky-vault", key=bytes(range(32)))
        self.adapter = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: self.now)

    def assert_no_secret(self, *values) -> None:
        for value in values:
            texts = [repr(value), str(value)]
            if isinstance(value, BaseException):
                texts.extend(traceback.format_exception(value))
                current = value
                while current is not None:
                    texts.append(repr(current))
                    current = current.__cause__ or current.__context__
            for text in texts:
                for secret in (SECRET, ROTATED):
                    self.assertNotIn(secret, text)

    def assert_fault(self, context, kind: FaultKind, outcome: Outcome = Outcome.KNOWN_FAILURE) -> None:
        self.assertEqual(context.exception.fault.kind, kind)
        self.assertEqual(context.exception.fault.outcome, outcome)
        self.assert_no_secret(context.exception)

    def ready(self, connection_id: str = "conn-1", adapter=None):
        adapter = self.adapter if adapter is None else adapter
        binding = adapter.register_key(connection_id, SECRET)
        self.transport.add("GET", "/models", resp(200, {"object": "list", "data": []}))
        self.assertEqual(adapter.verify_key(binding).state, AuthState.AUTHENTICATED)
        self.transport.add("GET", f"/models/{MODEL}", resp(200, {"id": MODEL, "object": "model"}))
        self.assertEqual(adapter.check_model(binding, MODEL).availability, MODEL_AVAILABLE)
        return binding

    def staged(self, binding, request_id: str, turn: TurnInput | None = None, model: str = MODEL,
               adapter=None) -> JobRequest:
        adapter = self.adapter if adapter is None else adapter
        request = JobRequest(binding, request_id, "route-1", "vm-attempt-1", model)
        adapter.stage_turn(request, turn or TurnInput(messages=(Message("user", "synthetic prompt"),)))
        return request

    # -- registration and authentication -----------------------------------
    def test_registration_stores_only_an_opaque_reference_and_slot_fingerprint(self) -> None:
        binding = self.adapter.register_key("conn-1", SECRET)
        self.assertTrue(binding.credential_ref.startswith("secret://aik-vault/"))
        CredentialRef("credential-1", binding.credential_ref)  # runtime DB locator compatible
        self.assertEqual(binding.account_fingerprint, slot_fingerprint("conn-1", binding.credential_ref, 1))
        self.assertEqual(self.vault.resolve(binding.credential_ref), SECRET)
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNKNOWN)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assert_no_secret(binding, self.adapter, self.adapter.resource_view(binding))
        with self.assertRaises(AdapterFailure) as duplicate:
            self.adapter.register_key("conn-1", ROTATED)
        self.assert_fault(duplicate, FaultKind.ACCOUNT_CONFLICT)

    def test_malformed_keys_are_rejected_before_the_vault_or_network(self) -> None:
        # Compare the whole directory instead of a guessed file extension so the
        # assertion cannot pass vacuously.
        before = self.vault_files()
        for candidate in ("short", SECRET + "\r\nX-Injected: 1", "has space " + SECRET, 12345, ""):
            with self.subTest(kind=type(candidate).__name__):
                with self.assertRaises(AdapterFailure) as rejected:
                    self.adapter.register_key("conn-bad", candidate)
                self.assert_fault(rejected, FaultKind.INVALID_CONTRACT)
        self.assertEqual(self.vault_files(), before)
        self.assertEqual(self.transport.seen, [])

    def test_successful_verification_uses_the_key_only_in_the_request_header(self) -> None:
        binding = self.ready()
        self.assertEqual([entry.authorization for entry in self.transport.seen], ["Bearer " + SECRET] * 2)
        self.assertEqual(self.adapter.executable_models(binding), (MODEL,))
        observed = self.adapter.auth_status(binding)
        self.assertEqual(observed.account_fingerprint, binding.account_fingerprint)
        view = self.adapter.resource_view(binding)
        self.assertEqual(view["billing_basis"], BILLING_BASIS)
        self.assertEqual(view["subscription_quota"], "not_applicable")
        self.assertEqual(view["route_qualification"], "NOT_RUN")
        self.assert_no_secret(view, observed)

    def test_invalid_key_and_permission_are_not_advertised_as_executable(self) -> None:
        binding = self.adapter.register_key("conn-1", SECRET)
        self.transport.add("GET", "/models", resp(401, {"error": {
            "code": "invalid_api_key", "message": "Incorrect API key provided: " + SECRET[:20]}}))
        self.assertEqual(self.adapter.verify_key(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assertEqual(self.adapter.resource_view(binding)["auth_state"], "unauthenticated")
        with self.assertRaises(AdapterFailure) as no_auth:
            self.adapter.check_model(binding, MODEL)
        self.assert_fault(no_auth, FaultKind.AUTH_REQUIRED)
        request = self.staged(binding, "req-unauth")
        with self.assertRaises(AdapterFailure) as refused:
            self.adapter.start_job(request)
        self.assert_fault(refused, FaultKind.AUTH_REQUIRED)
        self.transport.add("GET", "/models", resp(403, {"error": {"code": "insufficient_permissions"}}))
        self.assertEqual(self.adapter.verify_key(binding).state, AuthState.LOCKED)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assertNotIn("POST", [entry.method for entry in self.transport.seen])

    def test_transport_exception_echoing_the_key_becomes_unknown_without_leak(self) -> None:
        binding = self.adapter.register_key("conn-1", SECRET)
        self.transport.add("GET", "/models", RuntimeError("connection reset; Authorization: Bearer " + SECRET))
        observed = self.adapter.verify_key(binding)
        self.assertEqual(observed.state, AuthState.UNKNOWN)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assert_no_secret(observed)

    def test_missing_vault_credential_fails_closed_without_a_request(self) -> None:
        binding = self.adapter.register_key("conn-1", SECRET)
        self.vault.delete(binding.credential_ref)
        self.assertEqual(self.adapter.verify_key(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(self.transport.seen, [])

    def test_vault_access_denial_locks_the_connection_without_a_request(self) -> None:
        self.use_flaky_vault()
        binding = self.adapter.register_key("conn-1", SECRET)
        self.vault.injected["resolve"].append(VaultAccessDenied("synthetic vault access denial"))
        self.assertEqual(self.adapter.verify_key(binding).state, AuthState.LOCKED)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assertEqual(self.transport.seen, [])

    def test_attach_rejects_forged_fingerprints_revisions_and_foreign_bindings(self) -> None:
        binding = self.adapter.register_key("conn-1", SECRET)
        other = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: self.now)
        candidates = [("fingerprint", replace(binding, account_fingerprint="0" * 64)),
                      ("revision", replace(binding, revision=2))]
        try:
            candidates.append(("provider", replace(binding, provider="anthropic")))
        except ValueError:
            pass  # the AIK-011 contract itself refuses the forged provider
        for label, candidate in candidates:
            with self.subTest(forged=label):
                with self.assertRaises(AdapterFailure) as rejected:
                    other.attach(candidate)
                self.assert_fault(rejected, FaultKind.INVALID_CONTRACT)
        with self.assertRaises(AdapterFailure) as not_binding:
            other.attach(object())
        self.assert_fault(not_binding, FaultKind.INVALID_CONTRACT)
        other.attach(binding)
        other.attach(binding)  # idempotent for the identical binding
        self.assertEqual(other.auth_status(binding).state, AuthState.UNKNOWN)
        self.assertEqual(self.transport.seen, [])

    def test_expired_authentication_observation_is_not_advertised(self) -> None:
        binding = self.ready()
        self.now += 301
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNKNOWN)
        self.assertEqual(self.adapter.executable_models(binding), ())
        request = self.staged(binding, "req-stale-auth")
        with self.assertRaises(AdapterFailure) as refused:
            self.adapter.start_job(request)
        self.assert_fault(refused, FaultKind.AUTH_REQUIRED)

    def test_unknown_or_denied_models_are_not_executable(self) -> None:
        binding = self.ready()
        self.transport.add("GET", "/models/synthetic-missing", resp(404, {"error": {"code": "model_not_found"}}))
        self.transport.add("GET", "/models/synthetic-denied", resp(403, {"error": {"code": "model_denied"}}))
        self.transport.add("GET", "/models/synthetic-renamed", resp(200, {"id": "other-model"}))
        self.assertEqual(self.adapter.check_model(binding, "synthetic-missing").availability, MODEL_NOT_FOUND)
        self.assertEqual(self.adapter.check_model(binding, "synthetic-denied").availability, MODEL_DENIED)
        self.assertEqual(self.adapter.check_model(binding, "synthetic-renamed").availability, "unknown")
        self.assertEqual(self.adapter.executable_models(binding), (MODEL,))
        request = self.staged(binding, "req-missing-model", model="synthetic-missing")
        with self.assertRaises(AdapterFailure) as refused:
            self.adapter.start_job(request)
        self.assert_fault(refused, FaultKind.UNSUPPORTED)

    # -- execution ---------------------------------------------------------
    def test_guarded_job_lifecycle_records_only_provider_stated_model_and_usage(self) -> None:
        binding = self.ready()
        self.assertIsInstance(self.adapter, ProviderAdapter)
        request = self.staged(binding, "req-1")
        guard = GuardedAdapter(self.adapter, authorize_dispatch=lambda candidate: True)
        self.transport.add("POST", "/responses", resp(200, {"id": "resp_synthetic01", "status": "queued", "output": []}))
        job = guard.start_job(request)
        self.assertEqual(job.receipt.state, JobState.ACCEPTED)
        first = self.adapter.result("req-1")
        self.assertIsNone(first.model)
        self.assertFalse(first.usage.reported)
        self.assertIsNone(first.usage.total_tokens)
        body = json.loads(self.transport.seen[-1].body)
        self.assertEqual(body, {"model": MODEL, "input": [{"role": "user", "content": "synthetic prompt"}],
                                "background": True})
        self.transport.add("GET", "/responses/resp_synthetic01", resp(200, completed()))
        job = guard.observe_job("req-1")
        self.assertEqual(job.receipt.state, JobState.SUCCEEDED)
        result = self.adapter.result("req-1")
        self.assertEqual(result.model, "synthetic-model-a-2030-01-01")  # provider value, not request value
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens), (11, 7, 18))
        self.assertEqual(result.usage.billing_basis, BILLING_BASIS)
        self.assertEqual(result.output_text, "synthetic answer")
        self.assertEqual(result.evidence_kind, "fixture")
        self.assertEqual(guard.unresolved(), ())
        self.assert_no_secret(result, job)

    def test_tool_calls_round_trip_into_the_next_bounded_turn(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-tool", TurnInput(messages=(Message("user", "use a tool"),), tools=(TOOL,)))
        self.transport.add("POST", "/responses", resp(200, completed("resp_tool01", output=[
            {"type": "function_call", "call_id": "call_synthetic1", "name": "read_file",
             "arguments": "{\"path\": \"a.txt\"}"},
            {"type": "function_call", "call_id": "call_synthetic2", "name": "read_file", "arguments": "not json"},
            {"type": "reasoning", "id": "rs_synthetic"},
        ])))
        self.adapter.start_job(request)
        sent = json.loads(self.transport.seen[-1].body)
        self.assertEqual(sent["tools"], [{"type": "function", "name": "read_file", "description": "Read a synthetic file",
                                          "parameters": TOOL.parameters}])
        result = self.adapter.result("req-tool")
        self.assertEqual(result.tool_calls, (
            ToolCall("call_synthetic1", "read_file", {"path": "a.txt"}, True),
            ToolCall("call_synthetic2", "read_file", None, False),
        ))
        self.assertEqual(result.unhandled_item_types, ("reasoning",))
        follow_up = self.staged(binding, "req-tool-2", TurnInput(
            tool_results=(ToolResult("call_synthetic1", "synthetic file body"),), tools=(TOOL,),
            previous_response_id=result.response_id))
        self.transport.add("POST", "/responses", resp(200, completed("resp_tool02")))
        self.adapter.start_job(follow_up)
        sent = json.loads(self.transport.seen[-1].body)
        self.assertEqual(sent["input"], [{"type": "function_call_output", "call_id": "call_synthetic1",
                                          "output": "synthetic file body"}])
        self.assertEqual(sent["previous_response_id"], "resp_tool01")
        self.assertEqual(self.adapter.result("req-tool-2").output_text, "synthetic answer")

    def test_interrupt_acknowledgement_is_not_termination(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-cancel")
        self.transport.add("POST", "/responses", resp(200, {"id": "resp_cancel01", "status": "in_progress"}))
        receipt = self.adapter.start_job(request)
        self.assertEqual(receipt.state, JobState.RUNNING)
        self.transport.add("POST", "/responses/resp_cancel01/cancel", resp(200, {"id": "resp_cancel01", "status": "in_progress"}))
        ack = self.adapter.interrupt_job(request, receipt)
        self.assertEqual(ack.state, JobState.RUNNING)
        self.transport.add("GET", "/responses/resp_cancel01", resp(200, {"id": "resp_cancel01", "status": "cancelled"}))
        self.assertEqual(self.adapter.observe_job(request, ack).state, JobState.CANCELLED)

    def test_uncertain_start_and_cancel_fence_the_host(self) -> None:
        binding = self.ready()
        guard = GuardedAdapter(self.adapter, authorize_dispatch=lambda candidate: True)
        first = self.staged(binding, "req-5xx")
        self.transport.add("POST", "/responses", resp(503, {"error": {"code": "server_error"}}))
        with self.assertRaises(AdapterFailure) as uncertain:
            guard.start_job(first)
        self.assert_fault(uncertain, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        self.assertIn(("openai", "openai-api"), guard.uncertain_hosts)
        second = self.staged(binding, "req-after-5xx")
        with self.assertRaises(AdapterFailure) as fenced:
            guard.start_job(second)
        self.assert_fault(fenced, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)

        direct = self.staged(binding, "req-reset")
        self.transport.add("POST", "/responses", RuntimeError("reset while sending Bearer " + SECRET))
        with self.assertRaises(AdapterFailure) as lost:
            self.adapter.start_job(direct)
        self.assert_fault(lost, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        self.assertIsNone(lost.exception.__context__)
        with self.assertRaises(AdapterFailure) as duplicate:
            self.adapter.start_job(direct)
        self.assert_fault(duplicate, FaultKind.DUPLICATE_REQUEST)

        other = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: self.now)
        other_binding = binding
        other.attach(other_binding)
        self.transport.add("GET", "/models", resp(200, {"data": []}))
        other.verify_key(other_binding)
        self.transport.add("GET", f"/models/{MODEL}", resp(200, {"id": MODEL}))
        other.check_model(other_binding, MODEL)
        other_guard = GuardedAdapter(other, authorize_dispatch=lambda candidate: True)
        cancel_request = JobRequest(other_binding, "req-cancel-lost", "route-1", "vm-attempt-1", MODEL)
        other.stage_turn(cancel_request, TurnInput(messages=(Message("user", "synthetic"),)))
        self.transport.add("POST", "/responses", resp(200, {"id": "resp_cancel02", "status": "in_progress"}))
        other_guard.start_job(cancel_request)
        self.transport.add("POST", "/responses/resp_cancel02/cancel", RuntimeError("timeout"))
        with self.assertRaises(AdapterFailure) as cancel_unknown:
            other_guard.interrupt_job("req-cancel-lost")
        self.assert_fault(cancel_unknown, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        self.assertIn(("openai", "openai-api"), other_guard.uncertain_hosts)

    def test_rate_limit_and_unsafe_vendor_codes_are_bounded(self) -> None:
        binding = self.ready()
        limited = self.staged(binding, "req-429")
        self.transport.add("POST", "/responses", resp(429, {"error": {"code": "rate_limit_exceeded"}},
                                                      headers=(("Retry-After", "2"),)))
        with self.assertRaises(AdapterFailure) as rate:
            self.adapter.start_job(limited)
        self.assert_fault(rate, FaultKind.RATE_LIMITED)
        self.assertEqual(rate.exception.fault.retry_after_ms, 2000)
        self.assertEqual(rate.exception.fault.native_code, "rate_limit_exceeded")
        for request_id, code in (("req-token-code", "x" * 40), ("req-secret-code", SECRET)):
            with self.subTest(request_id=request_id):
                request = self.staged(binding, request_id)
                self.transport.add("POST", "/responses", resp(400, {"error": {"code": code}}))
                with self.assertRaises(AdapterFailure) as rejected:
                    self.adapter.start_job(request)
                self.assert_fault(rejected, FaultKind.PROVIDER_FAILURE)
                self.assertIsNone(rejected.exception.fault.native_code)

    def test_unauthorized_start_withdraws_the_advertised_resource(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-401")
        self.transport.add("POST", "/responses", resp(401, {"error": {"code": "invalid_api_key"}}))
        with self.assertRaises(AdapterFailure) as denied:
            self.adapter.start_job(request)
        self.assert_fault(denied, FaultKind.AUTH_REQUIRED)
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(self.adapter.executable_models(binding), ())

    def test_unauthorized_observation_withdraws_the_advertised_resource(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-observe-401")
        self.transport.add("POST", "/responses", resp(200, {"id": "resp_observe01", "status": "queued"}))
        receipt = self.adapter.start_job(request)
        self.transport.add("GET", "/responses/resp_observe01", resp(401, {"error": {"code": "invalid_api_key"}}))
        with self.assertRaises(AdapterFailure) as denied:
            self.adapter.observe_job(request, receipt)
        self.assert_fault(denied, FaultKind.AUTH_REQUIRED)
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(self.adapter.executable_models(binding), ())

    def test_start_status_marks_only_404_and_403_on_the_model(self) -> None:
        binding = self.ready()
        second_model = "synthetic-model-b"
        self.transport.add("GET", f"/models/{second_model}", resp(200, {"id": second_model}))
        self.assertEqual(self.adapter.check_model(binding, second_model).availability, MODEL_AVAILABLE)
        self.assertEqual(self.adapter.executable_models(binding), (MODEL, second_model))
        denied = self.staged(binding, "req-start-403", model=second_model)
        self.transport.add("POST", "/responses", resp(403, {"error": {"code": "model_denied"}}))
        with self.assertRaises(AdapterFailure) as forbidden:
            self.adapter.start_job(denied)
        self.assert_no_secret(forbidden.exception)
        self.assertEqual(self.adapter.executable_models(binding), (MODEL,))
        not_allowed = self.staged(binding, "req-start-405")
        self.transport.add("POST", "/responses", resp(405, {"error": {"code": "method_not_allowed"}}))
        with self.assertRaises(AdapterFailure) as method:
            self.adapter.start_job(not_allowed)
        self.assert_no_secret(method.exception)
        self.assertEqual(self.adapter.executable_models(binding), (MODEL,))  # 405 does not mark the model
        missing = self.staged(binding, "req-start-404")
        self.transport.add("POST", "/responses", resp(404, {"error": {"code": "model_not_found"}}))
        with self.assertRaises(AdapterFailure) as not_found:
            self.adapter.start_job(missing)
        self.assert_no_secret(not_found.exception)
        self.assertEqual(self.adapter.executable_models(binding), ())

    def test_malformed_success_and_mismatched_ids_are_invalid_receipts(self) -> None:
        binding = self.ready()
        for request_id, response in (("req-not-json", resp(200, raw=b"not json")),
                                     ("req-bad-id", resp(200, {"id": "resp/../x", "status": "queued"}))):
            with self.subTest(request_id=request_id):
                request = self.staged(binding, request_id)
                self.transport.add("POST", "/responses", response)
                with self.assertRaises(AdapterFailure) as invalid:
                    self.adapter.start_job(request)
                self.assert_fault(invalid, FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        request = self.staged(binding, "req-mismatch")
        self.transport.add("POST", "/responses", resp(200, {"id": "resp_mine", "status": "queued"}))
        receipt = self.adapter.start_job(request)
        self.transport.add("GET", "/responses/resp_mine", resp(200, {"id": "resp_other", "status": "completed"}))
        with self.assertRaises(AdapterFailure) as mismatch:
            self.adapter.observe_job(request, receipt)
        self.assert_fault(mismatch, FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)

    def test_echoed_key_in_model_output_is_redacted_and_flagged(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-echo")
        self.transport.add("POST", "/responses", resp(200, completed("resp_echo01", output=[
            {"type": "message", "content": [{"type": "output_text", "text": "the key is " + SECRET}]},
            {"type": "function_call", "call_id": "call_echo", "name": "read_file",
             "arguments": json.dumps({"path": SECRET})},
        ])))
        self.adapter.start_job(request)
        result = self.adapter.result("req-echo")
        self.assertTrue(result.secret_echo_redacted)
        self.assertIn("[REDACTED]", result.output_text)
        self.assertEqual(result.tool_calls, (ToolCall("call_echo", "read_file", None, False),))
        self.assert_no_secret(result)

    def test_identifier_fields_echoing_the_key_are_not_kept_or_used_as_paths(self) -> None:
        binding = self.ready()
        echoed_id = self.staged(binding, "req-id-echo")
        self.transport.add("POST", "/responses", resp(200, {"id": SECRET, "status": "queued", "model": SECRET}))
        with self.assertRaises(AdapterFailure) as invalid:
            self.adapter.start_job(echoed_id)
        self.assert_fault(invalid, FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        self.assertIsNone(self.adapter.result("req-id-echo"))

        echoed_fields = self.staged(binding, "req-field-echo")
        self.transport.add("POST", "/responses", resp(200, completed("resp_idecho01", model=SECRET, output=[
            {"type": "function_call", "call_id": SECRET, "name": "read_file", "arguments": "{}"},
            {"type": "function_call", "call_id": "call_ok", "name": SECRET, "arguments": "{}"},
        ])))
        receipt = self.adapter.start_job(echoed_fields)
        result = self.adapter.result("req-field-echo")
        self.assertIsNone(result.model)
        self.assertEqual(result.tool_calls, ())
        self.assertEqual(result.unhandled_item_types, ("function_call.invalid", "function_call.invalid"))
        self.assertTrue(result.secret_echo_redacted)
        self.assert_no_secret(result, receipt)
        self.transport.add("GET", "/responses/resp_idecho01", resp(200, completed("resp_idecho01")))
        self.assertEqual(self.adapter.observe_job(echoed_fields, receipt).state, JobState.SUCCEEDED)
        for entry in self.transport.seen:
            self.assertNotIn(SECRET, entry.path)

    def test_partial_usage_and_oversized_echo_are_reported_honestly(self) -> None:
        binding = self.ready()
        partial = self.staged(binding, "req-partial-usage")
        self.transport.add("POST", "/responses", resp(200, completed(
            "resp_partial01", usage={"input_tokens": 3, "output_tokens": True})))
        self.adapter.start_job(partial)
        usage = self.adapter.result("req-partial-usage").usage
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens), (3, None, None))
        self.assertTrue(usage.reported)
        large = self.staged(binding, "req-large-echo")
        self.transport.add("POST", "/responses", resp(200, completed("resp_large01", output=[
            {"type": "message", "content": [{"type": "output_text", "text": "x" * 70_000 + SECRET}]},
        ])))
        self.adapter.start_job(large)
        result = self.adapter.result("req-large-echo")
        self.assertTrue(result.secret_echo_redacted)
        self.assert_no_secret(result)

    # -- AIK-093 usage components -------------------------------------------
    def test_usage_details_become_vendor_neutral_components_with_provenance(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-usage-details")
        body_usage = {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
                      "input_tokens_details": {"cached_tokens": 1000},
                      "output_tokens_details": {"reasoning_tokens": 120}}
        self.transport.add("POST", "/responses", resp(200, completed("resp_details01", usage=body_usage)))
        self.adapter.start_job(request)
        usage = self.adapter.result("req-usage-details").usage
        self.assertEqual((usage.cached_input_tokens, usage.reasoning_tokens), (1000, 120))
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens), (1200, 300, 1500))
        breakdown = usage.breakdown()
        self.assertEqual(breakdown.input_uncached, Measured(200, Provenance.LOCAL_DERIVED))
        self.assertEqual(breakdown.cache_read, Measured(1000, Provenance.PROVIDER_OBSERVED))
        self.assertEqual(breakdown.output, Measured(300, Provenance.PROVIDER_OBSERVED))
        self.assertEqual(breakdown.reasoning, Measured(120, Provenance.PROVIDER_OBSERVED))
        for name in ("cache_write_short", "cache_write_long", "cache_write_unspecified"):
            self.assertFalse(getattr(breakdown, name).known)  # writes are not reported, never 0
        self.assertIsNone(breakdown.cache_write())
        self.assertIsNone(breakdown.total_input())
        zero = Usage(50, 5, 55, cached_input_tokens=0, reasoning_tokens=0).breakdown()
        self.assertEqual((zero.input_uncached.value, zero.cache_read.value, zero.reasoning.value), (50, 0, 0))

    def test_missing_or_malformed_usage_details_stay_unknown(self) -> None:
        binding = self.ready()
        request = self.staged(binding, "req-no-details")
        self.transport.add("POST", "/responses", resp(200, completed("resp_nodetail01")))
        self.adapter.start_job(request)
        usage = self.adapter.result("req-no-details").usage
        self.assertEqual((usage.cached_input_tokens, usage.reasoning_tokens), (None, None))
        breakdown = usage.breakdown()
        # Without the cached split neither part of the input is known; nothing is assumed to be 0.
        self.assertFalse(breakdown.input_uncached.known)
        self.assertFalse(breakdown.cache_read.known)
        self.assertFalse(breakdown.reasoning.known)
        self.assertEqual(breakdown.output, Measured(7, Provenance.PROVIDER_OBSERVED))
        for cached, reasoning in (({"cached_tokens": -1}, {"reasoning_tokens": True}), ([5], "3"),
                                  ({"cached_tokens": "5"}, {"reasoning_tokens": 2.0}), (None, {})):
            details = {"input_tokens_details": cached, "output_tokens_details": reasoning}
            with self.subTest(details=details):
                parsed = adapter_module._usage({"input_tokens": 9, "output_tokens": 4, **details})
                self.assertEqual((parsed.cached_input_tokens, parsed.reasoning_tokens), (None, None))
                self.assertEqual((parsed.input_tokens, parsed.output_tokens), (9, 4))
        self.assertTrue(Usage(cached_input_tokens=3).reported)
        self.assertEqual(Usage(), Usage(None, None, None))  # positional construction is unchanged
        for bad in (dict(cached_input_tokens=-1), dict(reasoning_tokens=True), dict(cached_input_tokens=2**63)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Usage(**bad)

    def test_inconsistent_usage_details_are_not_trusted_and_do_not_raise(self) -> None:
        over = adapter_module._usage({"input_tokens": 10, "output_tokens": 4,
                                      "input_tokens_details": {"cached_tokens": 11},
                                      "output_tokens_details": {"reasoning_tokens": 5}})
        self.assertEqual((over.cached_input_tokens, over.reasoning_tokens), (11, 5))  # raw values are kept
        breakdown = over.breakdown()
        self.assertFalse(breakdown.input_uncached.known)
        self.assertFalse(breakdown.cache_read.known)
        self.assertFalse(breakdown.reasoning.known)
        self.assertEqual(breakdown.output.value, 4)
        no_input = Usage(None, None, None, cached_input_tokens=6, reasoning_tokens=2).breakdown()
        self.assertFalse(no_input.input_uncached.known)
        self.assertEqual(no_input.cache_read, Measured(6, Provenance.PROVIDER_OBSERVED))
        self.assertEqual(no_input.reasoning, Measured(2, Provenance.PROVIDER_OBSERVED))

    def test_prompt_cache_is_disclosed_as_automatic_without_changing_the_request(self) -> None:
        binding = self.ready()
        caps = self.adapter.describe(binding)
        self.assertEqual(caps.prompt_cache, PromptCacheCapability(
            CacheMode.AUTOMATIC_PREFIX, frozenset({CachePolicy.PROVIDER_DEFAULT}), None))
        self.assertTrue(caps.prompt_cache.supports(CachePolicy.PROVIDER_DEFAULT))
        self.assertFalse(caps.prompt_cache.supports(CachePolicy.LONG))
        request = self.staged(binding, "req-cache-body")
        self.transport.add("POST", "/responses", resp(200, completed("resp_cachebody01")))
        self.adapter.start_job(request)
        body = json.loads(self.transport.seen[-1].body)
        self.assertEqual(body, {"model": MODEL, "input": [{"role": "user", "content": "synthetic prompt"}],
                                "background": True})

    def test_staging_model_and_output_bounds_are_enforced(self) -> None:
        binding = self.ready()
        with mock.patch.object(adapter_module, "MAX_STAGED", 2):
            self.staged(binding, "req-bound-1")
            self.staged(binding, "req-bound-2")
            with self.assertRaises(AdapterFailure) as staged_limit:
                self.staged(binding, "req-bound-3")
            self.assert_fault(staged_limit, FaultKind.INVALID_CONTRACT)
        with mock.patch.object(adapter_module, "MAX_MODELS", 1):
            with self.assertRaises(AdapterFailure) as model_limit:
                self.adapter.check_model(binding, "synthetic-model-extra")
            self.assert_fault(model_limit, FaultKind.INVALID_CONTRACT)
            self.transport.add("GET", f"/models/{MODEL}", resp(200, {"id": MODEL}))
            self.assertEqual(self.adapter.check_model(binding, MODEL).availability, MODEL_AVAILABLE)
        message = {"type": "message", "content": [{"type": "output_text", "text": "synthetic answer"}]}
        self.transport.add("POST", "/responses", resp(200, completed("resp_trunc01", output=[message] * 3)))
        with mock.patch.object(adapter_module, "MAX_ITEMS", 2):
            self.adapter.start_job(JobRequest(binding, "req-bound-1", "route-1", "vm-attempt-1", MODEL))
        truncated = self.adapter.result("req-bound-1")
        self.assertIn("output_truncated", truncated.unhandled_item_types)
        self.assertEqual(truncated.output_text, "synthetic answer" * 2)
        self.transport.add("POST", "/responses", resp(200, completed("resp_large02")))
        with mock.patch.object(adapter_module, "MAX_TEXT_CHARS", 5):
            self.adapter.start_job(JobRequest(binding, "req-bound-2", "route-1", "vm-attempt-1", MODEL))
        too_large = self.adapter.result("req-bound-2")
        self.assertIsNone(too_large.output_text)
        self.assertIn("output_text_too_large", too_large.unhandled_item_types)

    def test_evidence_kind_is_not_copied_from_a_transport_self_declaration(self) -> None:
        self.transport.kind = "network"  # synthetic fixture falsely claiming a live network
        self.adapter = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: self.now)
        binding = self.ready()
        request = self.staged(binding, "req-claimed-network")
        self.transport.add("POST", "/responses", resp(200, completed("resp_claimed01")))
        self.adapter.start_job(request)
        self.assertEqual(self.adapter.result("req-claimed-network").evidence_kind, "unspecified")

        class SpoofedNetworkTransport(UrllibTransport):
            def __init__(self, delegate):
                super().__init__()
                self.delegate = delegate

            def send(self, request, *, timeout):
                return self.delegate.send(request, timeout=timeout)

        spoof = OpenAIApiAdapter(transport=SpoofedNetworkTransport(self.transport), vault=self.vault,
                                 clock=lambda: self.now)
        spoof_binding = self.ready("conn-spoof", adapter=spoof)
        spoof_request = self.staged(spoof_binding, "req-spoofed-network", adapter=spoof)
        self.transport.add("POST", "/responses", resp(200, completed("resp_spoof01")))
        spoof.start_job(spoof_request)
        self.assertEqual(spoof.result("req-spoofed-network").evidence_kind, "unspecified")
        self.assert_no_secret(spoof.events())
        # Construction only; no request is sent through these transports.
        local_mock = OpenAIApiAdapter(transport=UrllibTransport("https://localhost-mock.invalid/v1"), vault=self.vault)
        self.assertEqual(local_mock._evidence_kind, "unspecified")
        vendor = OpenAIApiAdapter(transport=UrllibTransport(), vault=self.vault)
        self.assertEqual(vendor._evidence_kind, "network")

    # -- rotation and release ---------------------------------------------
    def test_rotation_advances_revision_requires_reverification_and_blocks_active_jobs(self) -> None:
        binding = self.ready()
        active = self.staged(binding, "req-active")
        self.transport.add("POST", "/responses", resp(200, {"id": "resp_active", "status": "in_progress"}))
        receipt = self.adapter.start_job(active)
        with self.assertRaises(AdapterFailure) as busy:
            self.adapter.rotate_key(binding, ROTATED)
        self.assert_fault(busy, FaultKind.ACCOUNT_CONFLICT)
        self.transport.add("GET", "/responses/resp_active", resp(200, completed("resp_active")))
        self.adapter.observe_job(active, receipt)

        rotated = self.adapter.rotate_key(binding, ROTATED)
        self.assertEqual(rotated.revision, 2)
        # F-02: rotation now moves to a new vault reference and deletes the old one.
        self.assertNotEqual(rotated.credential_ref, binding.credential_ref)
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(binding.credential_ref)
        self.assertNotEqual(rotated.account_fingerprint, binding.account_fingerprint)
        self.assertEqual(self.adapter.auth_status(rotated).state, AuthState.UNKNOWN)
        self.assertEqual(self.adapter.executable_models(rotated), ())
        with self.assertRaises(AdapterFailure) as stale:
            self.adapter.auth_status(binding)
        self.assert_fault(stale, FaultKind.STALE_CONNECTION)
        self.transport.add("GET", "/models", resp(200, {"data": []}))
        self.assertEqual(self.adapter.verify_key(rotated).state, AuthState.AUTHENTICATED)
        self.assertEqual(self.transport.seen[-1].authorization, "Bearer " + ROTATED)
        with self.assertRaises(AdapterFailure) as invalid:
            self.adapter.rotate_key(rotated, "bad key with spaces")
        self.assert_fault(invalid, FaultKind.INVALID_CONTRACT)

    def test_rotation_invalidates_the_previous_binding_held_by_another_instance(self) -> None:
        binding = self.ready()
        other = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: self.now)
        other.attach(binding)
        self.transport.add("GET", "/models", resp(200, {"data": []}))
        self.assertEqual(other.verify_key(binding).state, AuthState.AUTHENTICATED)
        self.transport.add("GET", f"/models/{MODEL}", resp(200, {"id": MODEL}))
        self.assertEqual(other.check_model(binding, MODEL).availability, MODEL_AVAILABLE)

        rotated = self.adapter.rotate_key(binding, ROTATED)
        self.assertNotEqual(rotated.credential_ref, binding.credential_ref)
        self.assertEqual(rotated.account_fingerprint, slot_fingerprint("conn-1", rotated.credential_ref, 2))
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(binding.credential_ref)
        self.assertEqual(self.vault.resolve(rotated.credential_ref), ROTATED)

        sent_before = len(self.transport.seen)
        stale = JobRequest(binding, "req-other-stale", "route-1", "vm-attempt-1", MODEL)
        other.stage_turn(stale, TurnInput(messages=(Message("user", "synthetic"),)))
        with self.assertRaises(AdapterFailure) as refused:
            other.start_job(stale)
        self.assert_fault(refused, FaultKind.AUTH_REQUIRED)
        self.assertEqual(len(self.transport.seen), sent_before)  # the new key was never sent
        self.assertEqual(other.auth_status(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(other.executable_models(binding), ())
        self.assertEqual(other.verify_key(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(len(self.transport.seen), sent_before)
        self.assert_no_secret(other.events())

    def test_rotation_with_uncertain_old_key_deletion_keeps_the_old_binding_and_can_retry(self) -> None:
        self.use_flaky_vault()
        binding = self.ready()
        self.vault.injected["delete"].append(OSError("synthetic vault I/O failure"))
        with self.assertRaises(AdapterFailure) as uncertain:
            self.adapter.rotate_key(binding, ROTATED)
        self.assert_fault(uncertain, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        discarded = self.vault.stored[-1]
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(discarded)
        self.assertEqual(self.vault.resolve(binding.credential_ref), SECRET)
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNKNOWN)
        self.assertEqual(self.adapter.executable_models(binding), ())
        rotated = self.adapter.rotate_key(binding, ROTATED)
        self.assertEqual(rotated.revision, 2)
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(binding.credential_ref)
        self.assertEqual(self.vault.resolve(rotated.credential_ref), ROTATED)

    def test_rotation_with_uncertain_new_key_storage_requires_reverification(self) -> None:
        self.use_flaky_vault()
        binding = self.ready()
        self.vault.injected["put"].append(OSError("synthetic vault I/O failure"))
        with self.assertRaises(AdapterFailure) as uncertain:
            self.adapter.rotate_key(binding, ROTATED)
        self.assert_fault(uncertain, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNKNOWN)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assertEqual(self.vault.resolve(binding.credential_ref), SECRET)
        self.transport.add("GET", "/models", resp(200, {"data": []}))
        self.assertEqual(self.adapter.verify_key(binding).state, AuthState.AUTHENTICATED)
        self.assertEqual(self.transport.seen[-1].authorization, "Bearer " + SECRET)

    def test_release_deletes_the_vault_secret_and_withdraws_resources(self) -> None:
        binding = self.ready()
        guard = GuardedAdapter(self.adapter, authorize_dispatch=lambda candidate: True)
        observed = guard.sign_out(binding)
        self.assertEqual(observed.state, AuthState.UNAUTHENTICATED)
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(binding.credential_ref)
        self.assertEqual(self.adapter.executable_models(binding), ())
        self.assertEqual(self.adapter.auth_status(binding).state, AuthState.UNAUTHENTICATED)
        with self.assertRaises(AdapterFailure) as released:
            self.adapter.verify_key(binding)
        self.assert_fault(released, FaultKind.AUTH_REQUIRED)

    def test_uncertain_release_can_be_retried_until_the_vault_secret_is_deleted(self) -> None:
        self.use_flaky_vault()
        binding = self.ready()
        self.vault.injected["delete"].append(OSError("synthetic vault I/O failure"))
        with self.assertRaises(AdapterFailure) as uncertain:
            self.adapter.sign_out(binding)
        self.assert_fault(uncertain, FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        self.assertEqual(self.vault.resolve(binding.credential_ref), SECRET)  # still present
        self.assertEqual(self.adapter.executable_models(binding), ())
        with self.assertRaises(AdapterFailure) as blocked:
            self.adapter.verify_key(binding)
        self.assert_fault(blocked, FaultKind.AUTH_REQUIRED)
        self.assertEqual(self.adapter.sign_out(binding).state, AuthState.UNAUTHENTICATED)
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(binding.credential_ref)
        self.assertEqual(self.adapter.sign_out(binding).state, AuthState.UNAUTHENTICATED)  # idempotent

    # -- disclosures and boundaries ---------------------------------------
    def test_capabilities_separate_implementation_from_qualification(self) -> None:
        binding = self.adapter.register_key("conn-1", SECRET)
        caps = self.adapter.describe(binding)
        self.assertEqual(caps.sign_in, Support.UNSUPPORTED)
        self.assertEqual(caps.job_start, Support.SUPPORTED)
        self.assertEqual(caps.implementation_state, "implemented")
        self.assertEqual(caps.qualification_status, "NOT_RUN")
        status = self.adapter.status()
        self.assertEqual((status.route_qualification, status.live_vendor_round_trip), ("NOT_RUN", "NOT_RUN"))
        with self.assertRaises(ValueError):
            AdapterStatus(route_qualification="qualified")
        with self.assertRaises(AdapterFailure) as sign_in:
            self.adapter.begin_sign_in(binding)
        self.assert_fault(sign_in, FaultKind.UNSUPPORTED)

    def test_explicit_transport_and_vault_are_required_and_requests_hide_headers(self) -> None:
        with self.assertRaises(TypeError):
            OpenAIApiAdapter(transport=object(), vault=self.vault)
        with self.assertRaises(TypeError):
            OpenAIApiAdapter(transport=self.transport, vault=object())
        with self.assertRaises(ValueError):
            UrllibTransport("http://example.invalid/v1")
        UrllibTransport()  # construction only; no network call
        request = HttpRequest("GET", "/models", (("Authorization", "Bearer " + SECRET),))
        self.assert_no_secret(request)
        with self.assertRaises(ValueError) as injected:
            HttpRequest("GET", "/models", (("Authorization", "Bearer " + SECRET + "\r\nX: y"),))
        self.assert_no_secret(injected.exception)
        with self.assertRaises(ValueError):
            HttpRequest("GET", "/responses/../admin")


if __name__ == "__main__":
    unittest.main()
