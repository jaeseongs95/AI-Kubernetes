"""AIK-013 adapter fixtures with a synthetic transport and a test-only vault.

No network, no real key and no vendor call are used. Every response below is a
synthetic protocol fixture, not a measurement. Model names are synthetic.
Linux success is development verification, not Windows support evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import traceback
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_adapter_anthropic import (  # noqa: E402
    MESSAGES_PATH, MODELS_PATH, AnthropicAdapter, Completion, HttpRequest, HttpResponse,
    Message, MessagesCall, TextPart, ToolCall, ToolResult, ToolSpec, TransportNotSent,
    TransportTimeout, Usage, assistant_message, tool_results_message,
)
from aik_provider import (  # noqa: E402
    AdapterFailure, AuthKind, AuthState, ConnectionBinding, FaultKind, GuardedAdapter,
    JobRequest, JobState, Outcome, ProviderAdapter, Support,
)
from aik_vault import CredentialNotFound, FakeVaultBackend  # noqa: E402

SECRET = "sk-ant-SYNTHETIC-FAKE-KEY-0001-abcdefghijklmnop"
ROTATED = "sk-ant-SYNTHETIC-FAKE-KEY-0002-qrstuvwxyzabcd"
MODEL = "synthetic-model-a"
HOST = ("anthropic", "messages-api")


def json_response(status: int, payload, headers=()) -> HttpResponse:
    return HttpResponse(status, (("content-type", "application/json"), *headers),
                        json.dumps(payload).encode("utf-8"))


MODELS_OK = json_response(200, {"data": [{"id": MODEL, "type": "model"}], "has_more": False})
_DEFAULT = object()


def message_payload(*, message_id="msg_synthetic_0001", content=_DEFAULT, stop_reason="end_turn",
                    usage=_DEFAULT, model=_DEFAULT) -> dict:
    payload = {"id": message_id, "type": "message", "role": "assistant",
               "content": [{"type": "text", "text": "synthetic answer"}] if content is _DEFAULT else content,
               "stop_reason": stop_reason}
    if model is _DEFAULT:
        payload["model"] = "synthetic-model-a-20990101"
    elif model is not None:
        payload["model"] = model
    if usage is _DEFAULT:
        payload["usage"] = {"input_tokens": 11, "output_tokens": 7,
                            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5}
    elif usage is not None:
        payload["usage"] = usage
    return payload


def error_response(status: int, error_type: str, headers=()) -> HttpResponse:
    return json_response(status, {"type": "error", "error": {
        "type": error_type, "message": f"synthetic failure echoing {SECRET}"}}, headers)


class FakeTransport:
    source = "synthetic_fixture"

    def __init__(self) -> None:
        self.routes: dict = {}
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        route = self.routes.get((request.method, urlsplit(request.url).path))
        if isinstance(route, list):
            route = route.pop(0)
        if route is None:
            raise AssertionError("unexpected synthetic route")
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(request)
        return route

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r.method == method and urlsplit(r.url).path == path)


def simple_call(text: str = "synthetic prompt") -> MessagesCall:
    return MessagesCall((Message("user", (TextPart(text),)),), max_tokens=64)


class AnthropicAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-anthropic-test-")
        self.addCleanup(self.temporary.cleanup)
        self.vault_dir = Path(self.temporary.name) / "vault"
        self.vault = FakeVaultBackend(self.vault_dir, key=bytes(range(32)))
        self.transport = FakeTransport()
        self.transport.routes[("GET", MODELS_PATH)] = MODELS_OK
        self.adapter = AnthropicAdapter(self.vault, self.transport)
        self.guard = GuardedAdapter(self.adapter, authorize_dispatch=lambda request: True)
        self.binding = self.adapter.register_key(SECRET, connection_id="connection-synthetic")

    def job(self, request_id: str, binding: ConnectionBinding | None = None) -> JobRequest:
        return JobRequest(binding or self.binding, request_id, "route-synthetic", "vm-attempt-synthetic", MODEL)

    def guarded_turn(self, request_id: str, response, call: MessagesCall | None = None):
        self.transport.routes[("POST", MESSAGES_PATH)] = response
        self.adapter.prepare(request_id, call or simple_call())
        return self.guard.start_job(self.job(request_id))

    def direct_failure(self, request_id: str, response) -> AdapterFailure:
        self.transport.routes[("POST", MESSAGES_PATH)] = response
        self.adapter.prepare(request_id, simple_call())
        with self.assertRaises(AdapterFailure) as raised:
            self.adapter.start_job(self.job(request_id))
        return raised.exception

    def assert_secret_free(self, error: BaseException) -> None:
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = "".join(traceback.format_exception(error)) + str(error) + repr(error)
        rendered += repr(getattr(error, "fault", ""))
        self.assertNotIn(SECRET, rendered)

    # -- contract and capability -----------------------------------------------
    def test_capabilities_separate_implementation_from_qualification(self) -> None:
        self.assertIsInstance(self.adapter, ProviderAdapter)
        caps = self.guard.capabilities(self.binding)
        self.assertEqual(caps.implementation_state, "implemented")
        self.assertEqual(caps.qualification_status, "NOT_RUN")
        self.assertIs(caps.auth_kind, AuthKind.API_KEY)
        self.assertIs(caps.sign_in, Support.UNSUPPORTED)
        self.assertIs(caps.job_interrupt, Support.UNSUPPORTED)
        self.assertIs(caps.job_start, Support.SUPPORTED)

    def test_foreign_provider_or_runtime_binding_is_stale(self) -> None:
        for provider, runtime in (("openai", "messages-api"), ("anthropic", "other-runtime")):
            binding = ConnectionBinding(provider, runtime, "connection-x", 1, AuthKind.API_KEY,
                                        self.binding.credential_ref, self.binding.account_fingerprint)
            with self.subTest(provider=provider, runtime=runtime):
                with self.assertRaises(AdapterFailure) as raised:
                    self.adapter.describe(binding)
                self.assertIs(raised.exception.fault.kind, FaultKind.STALE_CONNECTION)

    def test_constructor_rejects_plain_http_and_undeclared_transport(self) -> None:
        with self.assertRaises(ValueError):
            AnthropicAdapter(self.vault, self.transport, base_url="http://api.example.invalid")
        with self.assertRaises(ValueError):
            AnthropicAdapter(self.vault, self.transport, base_url="https://user:pw@api.example.invalid")

        class NoSource:
            def send(self, request):
                raise AssertionError

        with self.assertRaises(TypeError):
            AnthropicAdapter(self.vault, NoSource())

    def test_source_does_not_hardcode_vendor_model_names(self) -> None:
        for path in (ROOT / "src" / "aik_adapter_anthropic").glob("*.py"):
            with self.subTest(file=path.name):
                self.assertNotIn("claude", path.read_text(encoding="utf-8").lower())

    # -- key registration and authentication --------------------------------------
    def test_register_stores_only_reference_and_auth_check_uses_header(self) -> None:
        self.assertTrue(self.binding.credential_ref.startswith("secret://aik-vault/"))
        self.assertNotIn(SECRET, repr(self.binding))
        self.assertEqual(len(self.binding.account_fingerprint), 64)
        for file in self.vault_dir.iterdir():
            if file.is_file():
                self.assertNotIn(SECRET.encode("ascii"), file.read_bytes())
        observation = self.guard.auth_status(self.binding)
        self.assertIs(observation.state, AuthState.AUTHENTICATED)
        request = self.transport.requests[-1]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.header("x-api-key"), SECRET)
        self.assertIsNotNone(request.header("anthropic-version"))
        self.assertNotIn(SECRET, request.url)
        self.assertNotIn(SECRET, repr(request))

    def test_invalid_key_format_is_rejected_without_echo(self) -> None:
        with self.assertRaises(ValueError) as raised:
            self.adapter.register_key("bad key with spaces 0123456789", connection_id="connection-bad")
        self.assertNotIn("bad key", str(raised.exception))

    def test_401_on_auth_check_is_unauthenticated_and_blocks_dispatch(self) -> None:
        self.transport.routes[("GET", MODELS_PATH)] = error_response(401, "authentication_error")
        observation = self.adapter.auth_status(self.binding)
        self.assertIs(observation.state, AuthState.UNAUTHENTICATED)
        self.assertIsNone(observation.account_fingerprint)
        self.adapter.prepare("req-unauth", simple_call())
        with self.assertRaises(AdapterFailure) as raised:
            self.guard.start_job(self.job("req-unauth"))
        self.assertIs(raised.exception.fault.kind, FaultKind.AUTH_REQUIRED)
        self.assertEqual(self.transport.count("POST", MESSAGES_PATH), 0)

    def test_missing_or_foreign_credential_reference_is_unauthenticated_without_network(self) -> None:
        for reference in ("secret://aik-vault/" + "a" * 32, "secret://local/not-in-vault"):
            binding = ConnectionBinding("anthropic", "messages-api", "connection-missing", 1,
                                        AuthKind.API_KEY, reference, "b" * 64)
            with self.subTest(reference=reference):
                before = len(self.transport.requests)
                self.assertIs(self.adapter.auth_status(binding).state, AuthState.UNAUTHENTICATED)
                self.assertEqual(len(self.transport.requests), before)

    def test_auth_timeout_and_unsent_request_are_known_provider_failures(self) -> None:
        for raised_error, native in ((TransportTimeout(), "timeout"), (TransportNotSent(), "connection_error")):
            self.transport.routes[("GET", MODELS_PATH)] = raised_error
            with self.subTest(native=native):
                with self.assertRaises(AdapterFailure) as raised:
                    self.adapter.auth_status(self.binding)
                fault = raised.exception.fault
                self.assertIs(fault.kind, FaultKind.PROVIDER_FAILURE)
                self.assertIs(fault.outcome, Outcome.KNOWN_FAILURE)
                self.assertEqual(fault.native_code, native)

    def test_begin_sign_in_is_unsupported_for_api_keys(self) -> None:
        for target in (self.adapter, self.guard):
            with self.subTest(target=type(target).__name__):
                with self.assertRaises(AdapterFailure) as raised:
                    target.begin_sign_in(self.binding)
                self.assertIs(raised.exception.fault.kind, FaultKind.UNSUPPORTED)

    # -- model call and conversion ----------------------------------------------
    def test_guarded_call_converts_model_usage_and_cache_from_provider_values(self) -> None:
        job = self.guarded_turn("req-1", json_response(200, message_payload()))
        self.assertIs(job.receipt.state, JobState.SUCCEEDED)
        self.assertEqual(job.receipt.provider_job_id, "msg_synthetic_0001")
        result = self.adapter.result("req-1")
        self.assertIs(result.completion, Completion.COMPLETE)
        self.assertTrue(result.is_final)
        self.assertEqual(result.text, "synthetic answer")
        self.assertEqual(result.model, "synthetic-model-a-20990101")  # provider-reported, not requested
        self.assertEqual(result.usage, Usage(11, 7, 3, 5))
        self.assertEqual(result.source, "synthetic_fixture")
        self.assertFalse(result.measured)
        post = [r for r in self.transport.requests if r.method == "POST"][-1]
        body = json.loads(post.body)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["messages"], [{"role": "user", "content": [{"type": "text", "text": "synthetic prompt"}]}])
        self.assertNotIn(SECRET.encode("ascii"), post.body)
        self.assertEqual(post.header("x-api-key"), SECRET)
        self.assertEqual(self.adapter.observe_job(job.request, job.receipt), job.receipt)

    def test_missing_or_malformed_usage_and_model_stay_unknown(self) -> None:
        self.guarded_turn("req-none", json_response(200, message_payload(usage=None, model=None)))
        result = self.adapter.result("req-none")
        self.assertEqual(result.usage, Usage())
        self.assertIsNone(result.model)
        self.guarded_turn("req-partial", json_response(200, message_payload(
            message_id="msg_synthetic_0002",
            usage={"input_tokens": 4, "output_tokens": "7", "cache_read_input_tokens": -1, "cache_creation_input_tokens": True})))
        self.assertEqual(self.adapter.result("req-partial").usage, Usage(4, None, None, None))

    def test_truncated_refused_paused_and_unknown_turns_are_not_promoted(self) -> None:
        cases = (
            ("max_tokens", None, Completion.TRUNCATED),
            ("refusal", None, Completion.REFUSED),
            ("pause_turn", None, Completion.INCOMPLETE),
            ("synthetic_new_reason", None, Completion.UNKNOWN),
            (None, None, Completion.UNKNOWN),
            ("end_turn", [{"type": "synthetic_block", "data": "x"}], Completion.UNKNOWN),
            ("tool_use", [{"type": "text", "text": "no call"}], Completion.UNKNOWN),
        )
        for number, (stop, content, expected) in enumerate(cases):
            with self.subTest(stop=stop, expected=expected):
                kwargs = {"message_id": f"msg_synthetic_t{number}", "stop_reason": stop}
                if content is not None:
                    kwargs["content"] = content
                job = self.guarded_turn(f"req-t{number}", json_response(200, message_payload(**kwargs)))
                result = self.adapter.result(f"req-t{number}")
                self.assertIs(job.receipt.state, JobState.FAILED)
                self.assertIs(result.completion, expected)
                self.assertFalse(result.is_final)
        with self.assertRaises(ValueError):
            assistant_message(self.adapter.result("req-t0"))

    def test_tool_use_round_trip_is_connectable_to_an_agent_loop(self) -> None:
        tool = ToolSpec.create("read_file", "Read a synthetic file",
                               {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
        first = json_response(200, message_payload(message_id="msg_synthetic_tool1", stop_reason="tool_use", content=[
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_synthetic_1", "name": "read_file", "input": {"path": "a.txt"}},
        ]))
        second = json_response(200, message_payload(message_id="msg_synthetic_tool2"))
        self.transport.routes[("POST", MESSAGES_PATH)] = [first, second]
        user = Message("user", (TextPart("synthetic task"),))
        self.adapter.prepare("req-tool-1", MessagesCall((user,), max_tokens=64, tools=(tool,)))
        job = self.guard.start_job(self.job("req-tool-1"))
        result = self.adapter.result("req-tool-1")
        self.assertIs(job.receipt.state, JobState.SUCCEEDED)
        self.assertIs(result.completion, Completion.TOOL_USE)
        self.assertFalse(result.is_final)
        self.assertEqual(result.tool_calls, (ToolCall.create("toolu_synthetic_1", "read_file", {"path": "a.txt"}),))
        self.assertEqual(result.tool_calls[0].arguments, {"path": "a.txt"})

        follow_up = MessagesCall((user, assistant_message(result),
                                  tool_results_message([ToolResult("toolu_synthetic_1", "synthetic contents")])),
                                 max_tokens=64, tools=(tool,))
        self.adapter.prepare("req-tool-2", follow_up)
        self.guard.start_job(self.job("req-tool-2"))
        self.assertIs(self.adapter.result("req-tool-2").completion, Completion.COMPLETE)
        body = json.loads([r for r in self.transport.requests if r.method == "POST"][-1].body)
        self.assertEqual(body["tools"][0]["name"], "read_file")
        self.assertEqual(body["messages"][1]["content"][1], {
            "type": "tool_use", "id": "toolu_synthetic_1", "name": "read_file", "input": {"path": "a.txt"}})
        self.assertEqual(body["messages"][2]["content"][0], {
            "type": "tool_result", "tool_use_id": "toolu_synthetic_1", "content": "synthetic contents", "is_error": False})

    def test_message_roles_reject_misplaced_tool_parts(self) -> None:
        with self.assertRaises(ValueError):
            Message("user", (ToolCall.create("toolu_x", "read_file", {}),))
        with self.assertRaises(ValueError):
            Message("assistant", (ToolResult("toolu_x", "result"),))

    # -- common errors ----------------------------------------------------------
    def test_http_errors_map_to_common_faults_without_raw_messages(self) -> None:
        cases = (
            (error_response(401, "authentication_error"), FaultKind.AUTH_REQUIRED, "authentication_error", None),
            (error_response(429, "rate_limit_error", (("retry-after", "7"),)), FaultKind.RATE_LIMITED, "rate_limit_error", 7000),
            (error_response(404, "not_found_error"), FaultKind.UNSUPPORTED, "not_found_error", None),
            (error_response(529, "overloaded_error"), FaultKind.PROVIDER_FAILURE, "overloaded_error", None),
        )
        for number, (response, kind, native, retry) in enumerate(cases):
            with self.subTest(status=response.status):
                error = self.direct_failure(f"req-e{number}", response)
                self.assertIs(error.fault.kind, kind)
                self.assertIs(error.fault.outcome, Outcome.KNOWN_FAILURE)
                self.assertEqual(error.fault.native_code, native)
                self.assertEqual(error.fault.http_status, response.status)
                self.assertEqual(error.fault.retry_after_ms, retry)
                self.assert_secret_free(error)

    def test_model_availability_is_reported_separately(self) -> None:
        self.transport.routes[("GET", f"{MODELS_PATH}/{MODEL}")] = json_response(200, {"id": MODEL, "type": "model"})
        available = self.adapter.check_model(self.binding, MODEL)
        self.assertEqual((available.state, available.provider_model_id), ("available", MODEL))
        self.transport.routes[("GET", f"{MODELS_PATH}/synthetic-model-z")] = error_response(404, "not_found_error")
        self.assertEqual(self.adapter.check_model(self.binding, "synthetic-model-z").state, "unavailable")
        self.transport.routes[("GET", f"{MODELS_PATH}/synthetic-model-r")] = error_response(429, "rate_limit_error")
        with self.assertRaises(AdapterFailure) as raised:
            self.adapter.check_model(self.binding, "synthetic-model-r")
        self.assertIs(raised.exception.fault.kind, FaultKind.RATE_LIMITED)

    def test_call_timeout_is_unknown_outcome_and_fences_the_host(self) -> None:
        self.transport.routes[("POST", MESSAGES_PATH)] = TransportTimeout()
        self.adapter.prepare("req-timeout", simple_call())
        with self.assertRaises(AdapterFailure) as raised:
            self.guard.start_job(self.job("req-timeout"))
        fault = raised.exception.fault
        self.assertIs(fault.kind, FaultKind.UNKNOWN_OUTCOME)
        self.assertIs(fault.outcome, Outcome.UNKNOWN)
        self.assertEqual(fault.native_code, "timeout")
        self.assertIn(HOST, self.guard.uncertain_hosts)
        self.adapter.prepare("req-after-timeout", simple_call())
        with self.assertRaises(AdapterFailure) as again:
            self.guard.start_job(self.job("req-after-timeout"))
        self.assertIs(again.exception.fault.kind, FaultKind.UNKNOWN_OUTCOME)
        self.assertEqual(self.transport.count("POST", MESSAGES_PATH), 1)

    def test_unsent_request_is_known_failure(self) -> None:
        error = self.direct_failure("req-unsent", TransportNotSent())
        self.assertIs(error.fault.kind, FaultKind.PROVIDER_FAILURE)
        self.assertIs(error.fault.outcome, Outcome.KNOWN_FAILURE)

    def test_transport_exception_and_echoed_secret_never_leak(self) -> None:
        error = self.direct_failure("req-boom", RuntimeError(f"synthetic transport crash {SECRET}"))
        self.assertIs(error.fault.kind, FaultKind.UNKNOWN_OUTCOME)
        self.assert_secret_free(error)
        echoed = self.direct_failure("req-echo", json_response(200, message_payload(message_id=SECRET)))
        self.assertEqual(echoed.fault.native_code, "malformed_response")
        self.assert_secret_free(echoed)
        malformed = self.direct_failure("req-garbage", HttpResponse(200, (), b"not json " + SECRET.encode()))
        self.assertIs(malformed.fault.kind, FaultKind.UNKNOWN_OUTCOME)
        self.assert_secret_free(malformed)
        log = json.dumps(self.adapter.audit_log())
        self.assertNotIn(SECRET, log)
        self.assertIn("synthetic_fixture", log)
        self.assertIn("NOT_RUN", log)

    def test_unprepared_and_duplicate_requests_never_reach_the_network(self) -> None:
        with self.assertRaises(AdapterFailure) as raised:
            self.adapter.start_job(self.job("req-unprepared"))
        self.assertIs(raised.exception.fault.kind, FaultKind.INVALID_CONTRACT)
        self.assertEqual(self.transport.count("POST", MESSAGES_PATH), 0)
        self.adapter.prepare("req-dup", simple_call())
        with self.assertRaises(AdapterFailure) as duplicate:
            self.adapter.prepare("req-dup", simple_call())
        self.assertIs(duplicate.exception.fault.kind, FaultKind.DUPLICATE_REQUEST)
        self.transport.routes[("POST", MESSAGES_PATH)] = json_response(200, message_payload())
        self.adapter.start_job(self.job("req-dup"))
        with self.assertRaises(AdapterFailure) as replay:
            self.adapter.prepare("req-dup", simple_call())
        self.assertIs(replay.exception.fault.kind, FaultKind.DUPLICATE_REQUEST)

    def test_interrupt_is_unsupported_for_synchronous_turns(self) -> None:
        job = self.guarded_turn("req-int", json_response(200, message_payload()))
        with self.assertRaises(AdapterFailure) as raised:
            self.adapter.interrupt_job(job.request, job.receipt)
        self.assertIs(raised.exception.fault.kind, FaultKind.UNSUPPORTED)

    # -- rotation and release ------------------------------------------------------
    def test_rotation_issues_new_revision_and_stale_binding_cannot_dispatch(self) -> None:
        rotated = self.adapter.rotate_key(self.binding, ROTATED)
        self.assertEqual(rotated.revision, self.binding.revision + 1)
        self.assertEqual(rotated.credential_ref, self.binding.credential_ref)
        self.assertNotEqual(rotated.account_fingerprint, self.binding.account_fingerprint)
        with self.assertRaises(AdapterFailure) as stale:
            self.adapter.auth_status(self.binding)
        self.assertIs(stale.exception.fault.kind, FaultKind.ACCOUNT_CHANGED)
        self.adapter.prepare("req-stale", simple_call())
        with self.assertRaises(AdapterFailure) as blocked:
            self.guard.start_job(self.job("req-stale"))
        self.assertIs(blocked.exception.fault.kind, FaultKind.ACCOUNT_CHANGED)
        self.assertEqual(self.transport.count("POST", MESSAGES_PATH), 0)
        self.transport.routes[("POST", MESSAGES_PATH)] = json_response(200, message_payload())
        self.adapter.prepare("req-rotated", simple_call())
        self.guard.start_job(self.job("req-rotated", rotated))
        self.assertEqual(self.transport.requests[-1].header("x-api-key"), ROTATED)

    def test_release_deletes_only_the_current_key(self) -> None:
        rotated = self.adapter.rotate_key(self.binding, ROTATED)
        with self.assertRaises(AdapterFailure) as stale:
            self.adapter.sign_out(self.binding)
        self.assertIs(stale.exception.fault.kind, FaultKind.ACCOUNT_CHANGED)
        self.assertEqual(self.vault.resolve(rotated.credential_ref), ROTATED)
        observation = self.guard.sign_out(rotated)
        self.assertIs(observation.state, AuthState.UNAUTHENTICATED)
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(rotated.credential_ref)
        self.assertIs(self.adapter.auth_status(rotated).state, AuthState.UNAUTHENTICATED)

    def test_request_repr_hides_credentials(self) -> None:
        request = HttpRequest("GET", "https://api.example.invalid/v1/models", (("x-api-key", SECRET),), None, 5.0)
        self.assertNotIn(SECRET, repr(request))
        self.assertNotIn(SECRET, repr(self.adapter))


if __name__ == "__main__":
    unittest.main()
