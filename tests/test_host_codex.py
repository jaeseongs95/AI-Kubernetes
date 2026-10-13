"""AIK-014 host protocol fixture tests for the Codex host-auth bridge.

Only synthetic runners, synthetic host outputs and a synthetic VM runtime are
used. No Codex CLI, provider, credential, network or real process is touched.
Passing on Linux is development verification, not Windows support evidence,
and fixture success is not a real login success.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_host_codex import (  # noqa: E402
    ENV_ALLOWLIST, GUI_COMMANDS, BindingState, CodexHostBridge, HostNotFound, HostPresence,
    LoginMethod, LoginState, OutputLimitExceeded, ProtocolSupport, RunnerTimeout, RunResult,
    SubprocessRunner, build_environment, validate_invocation,
)
from aik_host_codex import runner as runner_module  # noqa: E402
from aik_provider import (  # noqa: E402
    AccountIsolation, AdapterFailure, AuthKind, AuthState, ConnectionBinding, FaultKind,
    GuardedAdapter, JobRequest, JobState, Support,
)

SECRET = "SYNTHETIC-CODEX-SECRET-MUST-NOT-LEAK-0001"
MASKED_KEY = "sk-SYNTH************MASK"
ACCOUNT_A = "acct-synthetic-a"
ACCOUNT_B = "acct-synthetic-b"
HOST = ("openai", "codex-host")


class FakeLoginHandle:
    def __init__(self, host: "FakeHost") -> None:
        self.host = host
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def complete(self, account: str) -> None:
        self.host.status = ("chatgpt", account)
        self.code = 0


class FakeHost:
    """Host protocol fixture: assumed (unverified) Codex CLI output formats."""

    def __init__(self) -> None:
        self.present = True
        self.version_output = b"codex-cli 0.40.0\n"
        self.status = ("chatgpt", ACCOUNT_A)
        self.run_errors: dict = {}
        self.logout_effect = True
        self.logout_exit = 0
        self.handles: list[FakeLoginHandle] = []

    def status_result(self) -> RunResult:
        kind = self.status[0]
        if kind == "chatgpt":
            account = self.status[1]
            text = "Logged in using ChatGPT\n" + (f"Account ID: {account}\n" if account else "")
            return RunResult(0, text.encode())
        if kind == "logged_out":
            return RunResult(1, b"Not logged in\n")
        if kind == "expired":
            return RunResult(1, b"Session expired. Please log in again.\n")
        if kind == "api_key":
            return RunResult(0, f"Logged in using an API key - {MASKED_KEY}\n".encode())
        return RunResult(self.status[1], self.status[2])  # ("raw", exit, bytes)


class FakeRunner:
    source = "synthetic_fixture"

    def __init__(self, host: FakeHost) -> None:
        self.host = host
        self.calls: list = []

    def run(self, argv, *, env, timeout, max_output):
        validate_invocation(argv, env, timeout, max_output)
        self.calls.append((argv, dict(env), timeout, max_output))
        if not self.host.present:
            raise HostNotFound()
        args = tuple(argv[1:])
        if args in self.host.run_errors:
            raise self.host.run_errors[args]
        if args == ("--version",):
            return RunResult(0, self.host.version_output)
        if args == ("login", "status"):
            return self.host.status_result()
        if args == ("logout",):
            if self.host.logout_effect and self.host.logout_exit == 0:
                self.host.status = ("logged_out", None)
            return RunResult(self.host.logout_exit, b"synthetic logout\n")
        raise AssertionError("unexpected host command")

    def spawn(self, argv, *, env):
        validate_invocation(argv, env)
        self.calls.append((argv, dict(env), None, None))
        if not self.host.present:
            raise HostNotFound()
        handle = FakeLoginHandle(self.host)
        self.host.handles.append(handle)
        return handle


class FakeVm:
    def __init__(self) -> None:
        self.started: list = []
        self.interrupted: list = []
        self.fail: Exception | None = None
        self.state = JobState.SUCCEEDED
        self.observe_state = JobState.RUNNING

    def start(self, handle, request):
        self.started.append((handle, request))
        if self.fail is not None:
            raise self.fail
        return (f"vm-job-{len(self.started)}", self.state)

    def observe(self, handle, request, job_id):
        return self.observe_state

    def interrupt(self, handle, request, job_id):
        self.interrupted.append(request.request_id)
        return JobState.CANCELLED


def job(binding: ConnectionBinding, request_id: str) -> JobRequest:
    return JobRequest(binding, request_id, "route-1", "vm-attempt-1", "synthetic-model")


class HostCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-host-codex-")
        self.addCleanup(self.temporary.cleanup)
        self.host = FakeHost()
        self.runner = FakeRunner(self.host)
        self.vm = FakeVm()
        self.now = [1000.0]
        self.bridge = self.make_bridge(self.vm)

    def make_bridge(self, vm) -> CodexHostBridge:
        environment = {
            "PATH": "/synthetic/bin", "HOME": self.temporary.name, "LANG": "C.UTF-8",
            "OPENAI_API_KEY": SECRET, "CODEX_ACCESS_TOKEN": SECRET,
        }
        return CodexHostBridge(self.runner, vm_runtime=vm, environment=environment,
                               clock=lambda: self.now[0])

    def guard(self, bridge=None) -> GuardedAdapter:
        return GuardedAdapter(bridge or self.bridge, authorize_dispatch=lambda request: True)

    def assertFault(self, kind, call, *args, native=None) -> AdapterFailure:
        with self.assertRaises(AdapterFailure) as caught:
            call(*args)
        self.assertEqual(caught.exception.fault.kind, kind)
        if native is not None:
            self.assertEqual(caught.exception.fault.native_code, native)
        for text in (SECRET, MASKED_KEY, ACCOUNT_A, ACCOUNT_B):
            self.assertNotIn(text, str(caught.exception))
            self.assertNotIn(text, repr(caught.exception.fault))
        return caught.exception

    def test_absent_host_is_registered_but_not_executable(self) -> None:
        self.host.present = False
        capability = self.bridge.probe()
        self.assertIs(capability.presence, HostPresence.ABSENT)
        self.assertIn("host_absent", capability.reasons)
        self.assertFalse(capability.execution_ready)
        self.assertEqual(capability.implementation_state, "implemented")
        for name in ("auth_qualification", "probe_qualification", "governed_qualification"):
            self.assertEqual(getattr(capability, name), "NOT_RUN")
        registered = ConnectionBinding("openai", "codex-host", "connection-1", 1, AuthKind.NATIVE_SUBSCRIPTION,
                                       "host-session://codex-cli/default", "0" * 64)
        caps = self.bridge.describe(registered)
        self.assertIs(caps.sign_in, Support.SUPPORTED)
        self.assertIs(caps.isolation, AccountIsolation.SINGLE_ACTIVE)
        self.assertEqual(caps.qualification_status, "NOT_RUN")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.auth_status, registered, native="host_absent")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.connect, "connection-2", native="host_absent")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.start_login, "connection-2", native="host_absent")

    def test_unsupported_protocol_and_expired_auth_are_distinct_capabilities(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.status = ("expired", None)
        observation = self.bridge.auth_status(binding)
        self.assertIs(observation.state, AuthState.EXPIRED)
        self.assertIsNone(observation.account_fingerprint)
        expired = self.bridge.probe(binding)
        self.assertIs(expired.presence, HostPresence.PRESENT)
        self.assertIs(expired.protocol, ProtocolSupport.SUPPORTED)
        self.assertIs(expired.login, LoginState.EXPIRED)
        self.assertIn("auth_expired", expired.reasons)
        self.assertNotIn("host_absent", expired.reasons)

        self.runner.calls.clear()
        self.host.version_output = b"unrelated tool 1\n"
        unsupported = self.bridge.probe()
        self.assertIs(unsupported.presence, HostPresence.PRESENT)
        self.assertIs(unsupported.protocol, ProtocolSupport.UNSUPPORTED)
        self.assertIs(unsupported.login, LoginState.UNKNOWN)
        self.assertIn("host_unsupported", unsupported.reasons)
        self.assertNotIn(("login", "status"), [call[0][1:] for call in self.runner.calls])

    def test_connect_binds_a_host_handle_without_raw_account_or_token(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.assertIs(binding.auth_kind, AuthKind.NATIVE_SUBSCRIPTION)
        self.assertEqual(binding.credential_ref, "host-session://codex-cli/default")
        self.assertEqual(binding.revision, 1)
        self.assertRegex(binding.account_fingerprint, r"\A[0-9a-f]{64}\Z")
        self.assertNotIn(ACCOUNT_A, repr(binding))
        observation = self.bridge.auth_status(binding)
        self.assertIs(observation.state, AuthState.AUTHENTICATED)
        self.assertEqual(observation.account_fingerprint, binding.account_fingerprint)
        self.assertEqual(self.bridge.connect("connection-1"), binding)
        capability = self.bridge.probe(binding)
        self.assertIs(capability.binding, BindingState.BOUND)
        self.assertTrue(capability.execution_ready)
        self.assertEqual(capability.reasons, ())
        self.assertEqual(capability.source, "synthetic_fixture")
        self.assertEqual(capability.governed_qualification, "NOT_RUN")

    def test_environment_allowlist_timeout_and_output_bounds(self) -> None:
        self.bridge.probe()
        self.assertTrue(self.runner.calls)
        for argv, env, timeout, max_output in self.runner.calls:
            self.assertEqual(argv[0], "codex")
            self.assertLessEqual(set(env), ENV_ALLOWLIST)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_ACCESS_TOKEN", env)
            self.assertNotIn(SECRET, "".join(env.values()))
            self.assertEqual(env["NO_COLOR"], "1")
            self.assertEqual(timeout, 15.0)
            self.assertEqual(max_output, 65536)
        with self.assertRaises(ValueError) as rejected:
            validate_invocation(("codex",), {"OPENAI_API_KEY": SECRET}, 1.0, 10)
        self.assertNotIn(SECRET, str(rejected.exception))
        for bad in (0, float("inf"), True, 1000):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                validate_invocation(("codex",), {}, bad, 10)
        with self.assertRaises(ValueError):
            validate_invocation(("codex",), {}, 1.0, 0)
        with self.assertRaises(ValueError):
            CodexHostBridge(self.runner, command_timeout=0)
        self.assertEqual(build_environment({"PATH": "x", "OPENAI_API_KEY": SECRET}),
                         {"PATH": "x", "NO_COLOR": "1"})

    def test_subprocess_runner_reports_absent_host_without_spawning(self) -> None:
        live = SubprocessRunner()
        empty = Path(self.temporary.name) / "empty-bin"
        empty.mkdir()
        with mock.patch.object(runner_module.subprocess, "Popen") as popen:
            with self.assertRaises(HostNotFound):
                live.run(("aik-synthetic-missing-codex", "--version"), env={"PATH": str(empty)},
                         timeout=1.0, max_output=16)
            with self.assertRaises(HostNotFound):
                live.spawn(("aik-synthetic-missing-codex", "login"), env={"PATH": str(empty)})
            with self.assertRaises(ValueError):
                live.run(("codex",), env={"OPENAI_API_KEY": SECRET}, timeout=1.0, max_output=16)
            popen.assert_not_called()
        self.assertNotIn(SECRET, repr(RunResult(0, SECRET.encode())))

    def test_api_key_login_is_not_reused_as_subscription(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.status = ("api_key", None)
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.connect, "connection-2", native="non_subscription_login")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.auth_status, binding, native="non_subscription_login")
        capability = self.bridge.probe(binding)
        self.assertIs(capability.method, LoginMethod.API_KEY)
        self.assertIn("non_subscription_login", capability.reasons)
        self.assertFalse(capability.execution_ready)
        self.assertNotIn(MASKED_KEY, repr(capability))
        self.host.status = ("chatgpt", ACCOUNT_A)
        self.assertFault(FaultKind.STALE_CONNECTION, self.bridge.auth_status, binding, native="reconnect_required")

    def test_unknown_account_and_unrecognized_output_fail_closed(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.status = ("chatgpt", None)
        self.assertFault(FaultKind.AUTH_REQUIRED, self.bridge.connect, "connection-2", native="account_unknown")
        observation = self.bridge.auth_status(binding)
        self.assertIs(observation.state, AuthState.UNKNOWN)
        self.assertIsNone(observation.account_fingerprint)
        capability = self.bridge.probe(binding)
        self.assertIs(capability.binding, BindingState.UNKNOWN)
        self.assertIn("account_unknown", capability.reasons)
        for raw in ((0, ("unexpected format " + SECRET).encode()),
                    (1, b"Logged in using ChatGPT\nAccount ID: acct-synthetic-a\n"),
                    (0, b"\xff\xfe")):
            with self.subTest(exit_code=raw[0]):
                self.host.status = ("raw", *raw)
                observation = self.bridge.auth_status(binding)
                self.assertIs(observation.state, AuthState.UNKNOWN)
                self.assertNotIn(SECRET, repr(observation))
        self.host.status = ("chatgpt", "A" * 40)
        self.assertFault(FaultKind.AUTH_REQUIRED, self.bridge.connect, "connection-3", native="account_unknown")

    def test_guarded_sign_out_sign_in_and_dispatch_revalidate_the_same_binding(self) -> None:
        guard = self.guard()
        binding = self.bridge.connect("connection-1")
        self.assertIs(guard.sign_out(binding).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(self.host.status, ("logged_out", None))
        self.assertIs(guard.begin_sign_in(binding).state, AuthState.PENDING)
        self.assertEqual(len(self.host.handles), 1)
        self.assertIs(guard.auth_status(binding).state, AuthState.PENDING)
        self.assertFault(FaultKind.ACCOUNT_CONFLICT, guard.start_job, job(binding, "request-1"))
        self.assertEqual(self.vm.started, [])
        self.host.handles[0].complete(ACCOUNT_A)
        self.assertIs(guard.auth_status(binding).state, AuthState.AUTHENTICATED)
        started = guard.start_job(job(binding, "request-2"))
        self.assertIs(started.receipt.state, JobState.SUCCEEDED)
        self.assertEqual(len(self.vm.started), 1)
        handle = self.vm.started[0][0]
        self.assertEqual((handle.revision, handle.account_fingerprint), (1, binding.account_fingerprint))
        self.assertEqual(handle.credential_ref, "host-session://codex-cli/default")
        self.assertNotIn("OPENAI_API_KEY", dict(handle.environment))
        self.assertNotIn(SECRET, repr(handle))

    def test_external_account_switch_marks_binding_stale_until_reconnect(self) -> None:
        guard = self.guard()
        binding = self.bridge.connect("connection-1")
        self.host.status = ("chatgpt", ACCOUNT_B)
        self.assertFault(FaultKind.ACCOUNT_CHANGED, guard.start_job, job(binding, "request-1"))
        self.assertEqual(self.vm.started, [])
        self.host.status = ("chatgpt", ACCOUNT_A)
        self.assertFault(FaultKind.STALE_CONNECTION, guard.start_job, job(binding, "request-2"),
                         native="reconnect_required")
        capability = self.bridge.probe(binding)
        self.assertIs(capability.binding, BindingState.CHANGED)
        self.assertIn("connection_stale", capability.reasons)
        renewed = self.bridge.reconnect(binding)
        self.assertEqual(renewed.revision, 2)
        self.assertEqual(renewed.account_fingerprint, binding.account_fingerprint)
        self.assertFault(FaultKind.STALE_CONNECTION, self.bridge.auth_status, binding)
        guard.start_job(job(renewed, "request-3"))
        self.assertEqual(self.vm.started[0][0].revision, 2)

    def test_external_logout_and_login_with_same_account_requires_reconnect(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.status = ("logged_out", None)
        self.assertIs(self.bridge.auth_status(binding).state, AuthState.UNAUTHENTICATED)
        self.host.status = ("chatgpt", ACCOUNT_A)
        self.assertFault(FaultKind.STALE_CONNECTION, self.bridge.auth_status, binding)
        renewed = self.bridge.reconnect(binding)
        self.assertEqual(renewed.revision, 2)
        self.assertEqual(self.bridge.connect("connection-1"), renewed)
        self.assertIs(self.bridge.auth_status(renewed).state, AuthState.AUTHENTICATED)

    def test_login_deadline_terminates_process_and_cancel_is_supported(self) -> None:
        self.host.status = ("logged_out", None)
        pending = self.bridge.start_login("connection-1")
        self.assertIs(pending.login, LoginState.PENDING)
        self.assertIn("auth_pending", pending.reasons)
        self.assertFault(FaultKind.ACCOUNT_CONFLICT, self.bridge.start_login, "connection-2")
        self.now[0] += 601
        after = self.bridge.probe()
        self.assertTrue(self.host.handles[0].terminated)
        self.assertIs(after.login, LoginState.LOGGED_OUT)
        self.assertIn("auth_required", after.reasons)
        self.bridge.start_login("connection-1")
        self.assertTrue(self.bridge.cancel_login())
        self.assertTrue(self.host.handles[1].terminated)
        self.assertFalse(self.bridge.cancel_login())

    def test_sign_out_failures_are_known_or_unknown_and_unknown_fences_the_host(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.logout_exit = 1
        self.assertFault(FaultKind.PROVIDER_FAILURE, self.bridge.sign_out, binding, native="logout_failed")
        self.host.logout_exit = 0
        self.host.logout_effect = False
        self.assertIs(self.bridge.sign_out(binding).state, AuthState.UNKNOWN)
        guard = self.guard()
        self.host.run_errors[("logout",)] = RunnerTimeout()
        self.assertFault(FaultKind.UNKNOWN_OUTCOME, guard.sign_out, binding)
        self.assertIn(HOST, guard.uncertain_hosts)
        self.assertFault(FaultKind.UNKNOWN_OUTCOME, guard.start_job, job(binding, "request-1"))
        self.assertEqual(self.vm.started, [])

    def test_runner_failures_are_classified_without_raw_output(self) -> None:
        binding = self.bridge.connect("connection-1")
        for error, native in ((RunnerTimeout(), "host_timeout"),
                              (OutputLimitExceeded(), "host_output_limit"),
                              (RuntimeError(SECRET), "host_runner_error")):
            with self.subTest(native=native):
                self.host.run_errors[("login", "status")] = error
                failure = self.assertFault(FaultKind.PROVIDER_FAILURE, self.bridge.auth_status, binding,
                                           native=native)
                self.assertIsNone(failure.__context__)
                self.assertIsNone(failure.__cause__)
        self.host.run_errors.clear()
        self.host.status = ("raw", 0, b"x" * 70000)
        self.assertFault(FaultKind.PROVIDER_FAILURE, self.bridge.auth_status, binding, native="host_output_limit")
        self.host.run_errors[("--version",)] = RunnerTimeout()
        capability = self.bridge.probe()
        self.assertIs(capability.presence, HostPresence.UNKNOWN)
        self.assertIn("host_state_unknown", capability.reasons)

    def test_vm_runtime_is_required_and_its_failures_are_unknown_outcomes(self) -> None:
        unwired = self.make_bridge(None)
        binding = unwired.connect("connection-1")
        self.assertIs(unwired.describe(binding).job_start, Support.UNSUPPORTED)
        capability = unwired.probe(binding)
        self.assertFalse(capability.execution_ready)
        self.assertIn("vm_runtime_unavailable", capability.reasons)
        self.assertFault(FaultKind.UNSUPPORTED, self.guard(unwired).start_job, job(binding, "request-0"))

        wired = self.bridge.connect("connection-1")
        guard = self.guard()
        self.vm.fail = RuntimeError(SECRET)
        self.assertFault(FaultKind.UNKNOWN_OUTCOME, guard.start_job, job(wired, "request-1"))
        self.assertIn(HOST, guard.uncertain_hosts)
        self.vm.fail = None
        self.assertFault(FaultKind.DUPLICATE_REQUEST, self.bridge.start_job, job(wired, "request-1"))
        self.assertFault(FaultKind.STALE_CONNECTION, self.bridge.start_job,
                         job(replace(wired, revision=5), "request-9"))
        self.assertEqual(len(self.vm.started), 1)

    def test_observe_and_interrupt_use_the_original_binding(self) -> None:
        self.vm.state = JobState.RUNNING
        guard = self.guard()
        binding = self.bridge.connect("connection-1")
        started = guard.start_job(job(binding, "request-1"))
        self.assertIs(started.receipt.state, JobState.RUNNING)
        self.assertIs(guard.observe_job("request-1").receipt.state, JobState.RUNNING)
        interrupted = guard.interrupt_job("request-1")
        self.assertIs(interrupted.receipt.state, JobState.CANCELLED)
        self.assertEqual(interrupted.receipt.revision, binding.revision)
        self.assertEqual(self.vm.interrupted, ["request-1"])

    def test_gui_commands_and_capability_disclosures(self) -> None:
        for command, method in GUI_COMMANDS.items():
            with self.subTest(command=command):
                self.assertTrue(callable(getattr(self.bridge, method)))
        capability = self.bridge.probe()
        for name in ("auth_qualification", "probe_qualification", "governed_qualification"):
            with self.subTest(field=name), self.assertRaises(ValueError):
                replace(capability, **{name: "PASS"})
        self.assertIsNone(re.search(ACCOUNT_A, repr(capability)))


if __name__ == "__main__":
    unittest.main()
