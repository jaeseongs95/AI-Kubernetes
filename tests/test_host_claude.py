"""AIK-015 host protocol fixtures for the Claude Code subscription bridge.

Every host response is synthetic. No real CLI, provider, credential or
network is used.

Some tests launch local Python child processes. These are not the host. They
check the live runner's deadline, output limit and process-tree termination.

A fixture pass is not a live host round trip (NOT_RUN). Linux execution of
this suite is development verification, not Windows support.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_provider import (  # noqa: E402
    AccountIsolation, AdapterFailure, AuthKind, AuthState, ConnectionBinding, FaultKind,
    GuardedAdapter, JobRequest, JobState, Outcome, Support,
)
from aik_host_claude import (  # noqa: E402
    ENV_ALLOWLIST, PROVIDER, RUNTIME, AuthMethod, ClaudeHostBridge, CommandResult, CommandSpec,
    HostCall, HostCommands, HostState, RunnerNotStarted, RunnerTimeout, SubprocessRunner,
    account_fingerprint, build_environment,
)
import aik_host_claude.bridge as bridge_module  # noqa: E402

SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_LEAK_0001"
MODEL = "synthetic-model"
OBSERVED_MODEL = "synthetic-model-observed"
ACCOUNT_A = "acct-synthetic-a"
ACCOUNT_B = "acct-synthetic-b"
PROMPT = "synthetic prompt body that must travel only on stdin"
VERSION_OK = CommandResult(0, b"2.1.0 (Claude Code)\n")
POSIX_ONLY = "The Windows process-tree path (taskkill /T /F) is NOT_RUN in this suite."

# A local synthetic child (not the host). It starts a sleeping grandchild,
# records the grandchild PID, then floods output, sleeps, or exits at once.
TREE_CHILD = r'''
import os, subprocess, sys, time
pidfile, mode, action = sys.argv[1], sys.argv[2], sys.argv[3]
streams = {} if mode == "hold" else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              stdin=subprocess.DEVNULL, **streams)
with open(pidfile + ".tmp", "w", encoding="ascii") as handle:
    handle.write(str(grandchild.pid))
os.replace(pidfile + ".tmp", pidfile)
if action == "flood":
    block = b"x" * 65536
    for _ in range(128):
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
    time.sleep(30)
elif action == "sleep":
    time.sleep(30)
'''


def read_pid(path: Path, wait: float = 10.0) -> int:
    deadline = time.monotonic() + wait
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("The synthetic grandchild PID was not recorded.")
        time.sleep(0.02)
    return int(path.read_text(encoding="ascii"))


def process_alive(pid: int) -> bool:
    if os.path.isfile("/proc/self/stat"):
        try:
            data = Path(f"/proc/{pid}/stat").read_bytes()
        except OSError:
            return False
        return data[data.rindex(b")") + 2:].split()[0] not in (b"Z", b"X", b"x")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid: int, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while process_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def kill_quietly(pid: int) -> None:
    if process_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def status(logged_in=True, method="claude.ai", account=ACCOUNT_A, expired=None, extra=None,
           exit_code=0, stderr=b""):
    payload: dict = {"loggedIn": logged_in}
    if logged_in:
        payload["authMethod"] = method
        if account is not None:
            payload["accountId"] = account
    if expired is not None:
        payload["expired"] = expired
    if extra:
        payload.update(extra)
    return CommandResult(exit_code, json.dumps(payload).encode("utf-8"), stderr)


def job(session="session-synthetic-1", model=OBSERVED_MODEL, text="synthetic answer",
        is_error=False, subtype="success", exit_code=0, stderr=b""):
    payload = {"type": "result", "subtype": subtype, "is_error": is_error, "result": text,
               "session_id": session, "model": model}
    return CommandResult(exit_code, json.dumps(payload).encode("utf-8"), stderr)


class FakeRunner:
    """Synthetic host fixture. The last queued outcome per command is sticky."""

    source = "synthetic_fixture"

    def __init__(self):
        self.responses = {"version": [VERSION_OK], "status": [status()],
                          "logout": [CommandResult(0, b"")], "execute": [job()]}
        self.calls: list[CommandSpec] = []
        self.launches: list[CommandSpec] = []
        self.launch_error = None

    def set(self, key, *outcomes):
        self.responses[key] = list(outcomes)

    @staticmethod
    def key(argv):
        tail = argv[1:]
        if tail == ("--version",):
            return "version"
        if tail[:2] == ("auth", "status"):
            return "status"
        if tail[:2] == ("auth", "logout"):
            return "logout"
        if tail[:1] == ("-p",):
            return "execute"
        raise AssertionError("unexpected synthetic command")

    def run(self, spec):
        assert type(spec) is CommandSpec
        self.calls.append(spec)
        queue = self.responses[self.key(spec.argv)]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def launch(self, spec):
        assert type(spec) is CommandSpec
        self.launches.append(spec)
        if self.launch_error is not None:
            raise self.launch_error

    def count(self, key):
        return sum(1 for spec in self.calls if self.key(spec.argv) == key)

    def specs(self, key):
        return [spec for spec in self.calls if self.key(spec.argv) == key]


class BlockingRunner(FakeRunner):
    """Synthetic fixture whose host run blocks until the test releases it."""

    def __init__(self):
        super().__init__()
        self.execute_started = threading.Event()
        self.execute_release = threading.Event()

    def run(self, spec):
        if self.key(spec.argv) == "execute":
            self.execute_started.set()
            if not self.execute_release.wait(10):
                raise RunnerTimeout()
        return super().run(spec)


def allow(_request):
    return True  # synthetic fixture authorization only; never product authorization


class ClaudeHostBridgeTests(unittest.TestCase):
    def make(self, **kwargs):
        runner = FakeRunner()
        kwargs.setdefault("catalog", (MODEL,))
        kwargs.setdefault("environ", {"PATH": "/synthetic/bin", "HOME": "/synthetic/home"})
        return runner, ClaudeHostBridge(runner, **kwargs)

    def connected(self, **kwargs):
        runner, bridge = self.make(**kwargs)
        return runner, bridge, bridge.connect("connection-1")

    def assert_fault(self, kind, call, *args, native=None, outcome=None, **kwargs):
        with self.assertRaises(AdapterFailure) as caught:
            call(*args, **kwargs)
        self.assertIs(caught.exception.fault.kind, kind)
        if native is not None:
            self.assertEqual(caught.exception.fault.native_code, native)
        if outcome is not None:
            self.assertIs(caught.exception.fault.outcome, outcome)
        return caught.exception

    def test_probe_distinguishes_absent_unsupported_expired_and_unknown(self) -> None:
        cases = (
            ("absent", {"version": (RunnerNotStarted,)}, HostState.ABSENT, AuthState.UNKNOWN, "HOST_ABSENT"),
            ("old", {"version": (CommandResult(0, b"0.9.0\n"),)}, HostState.UNSUPPORTED, AuthState.UNKNOWN,
             "HOST_VERSION_UNSUPPORTED"),
            ("unrecognized", {"version": (CommandResult(0, b"not a version"),)}, HostState.UNSUPPORTED,
             AuthState.UNKNOWN, "HOST_VERSION_UNRECOGNIZED"),
            ("status-unsupported", {"status": (CommandResult(2, b"unknown command"),)}, HostState.UNSUPPORTED,
             AuthState.UNKNOWN, "AUTH_STATUS_UNSUPPORTED"),
            ("status-timeout", {"status": (RunnerTimeout,)}, HostState.PRESENT, AuthState.UNKNOWN,
             "AUTH_STATUS_UNOBSERVED"),
            ("status-truncated", {"status": (CommandResult(0, b"{}", truncated=True),)}, HostState.PRESENT,
             AuthState.UNKNOWN, "AUTH_STATUS_UNOBSERVED"),
            ("expired", {"status": (status(logged_in=False, expired=True),)}, HostState.PRESENT,
             AuthState.EXPIRED, "AUTH_EXPIRED"),
            ("signed-out", {"status": (status(logged_in=False),)}, HostState.PRESENT,
             AuthState.UNAUTHENTICATED, "AUTH_REQUIRED"),
            ("api-key", {"status": (status(method="api_key"),)}, HostState.PRESENT,
             AuthState.UNAUTHENTICATED, "API_KEY_AUTH_NOT_SUBSCRIPTION"),
        )
        for name, responses, host_state, auth_state, reason in cases:
            with self.subTest(case=name):
                runner, bridge = self.make()
                for key, outcomes in responses.items():
                    runner.set(key, *outcomes)
                observed = bridge.probe()
                self.assertIs(observed.host_state, host_state)
                self.assertIs(observed.auth_state, auth_state)
                self.assertIn(reason, observed.reasons)
                self.assertIsNone(observed.account_fingerprint)
                self.assertEqual(observed.qualification_status, "NOT_RUN")
        runner, bridge = self.make()
        observed = bridge.probe()
        self.assertIs(observed.auth_state, AuthState.AUTHENTICATED)
        self.assertIs(observed.auth_method, AuthMethod.SUBSCRIPTION)
        self.assertEqual(observed.account_fingerprint, account_fingerprint(ACCOUNT_A))
        self.assertEqual(observed.version, "2.1.0")

    def test_connect_stores_only_a_host_handle_and_account_fingerprint(self) -> None:
        runner, bridge, binding = self.connected()
        self.assertIs(binding.auth_kind, AuthKind.NATIVE_SUBSCRIPTION)
        self.assertTrue(binding.credential_ref.startswith("host-session://claude-code/"))
        self.assertEqual((binding.provider, binding.runtime, binding.revision), (PROVIDER, RUNTIME, 1))
        self.assertEqual(binding.account_fingerprint, account_fingerprint(ACCOUNT_A))
        self.assertNotIn(ACCOUNT_A, repr(binding))
        self.assertNotIn(ACCOUNT_A, json.dumps(bridge.audit_log()))
        caps = bridge.describe(binding)
        self.assertEqual(caps.qualification_status, "NOT_RUN")
        self.assertEqual(caps.implementation_state, "implemented")
        self.assertIs(caps.job_interrupt, Support.UNSUPPORTED)
        self.assertIs(caps.isolation, AccountIsolation.SINGLE_ACTIVE)
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.connect, "connection-1", native="connection_exists")

    def test_connect_rejects_absent_unsigned_expired_api_key_and_wrong_selected_account(self) -> None:
        cases = (
            ("version", RunnerNotStarted, FaultKind.UNSUPPORTED, "host_absent"),
            ("version", CommandResult(0, b"0.1.0"), FaultKind.UNSUPPORTED, "host_unsupported"),
            ("status", status(logged_in=False), FaultKind.AUTH_REQUIRED, "auth_required"),
            ("status", status(logged_in=False, expired=True), FaultKind.AUTH_REQUIRED, "auth_expired"),
            ("status", status(method="api_key"), FaultKind.AUTH_REQUIRED, "api_key_auth_not_subscription"),
            ("status", status(method="unexpected-method"), FaultKind.AUTH_REQUIRED, "account_not_observed"),
        )
        for key, outcome, kind, native in cases:
            with self.subTest(native=native):
                runner, bridge = self.make()
                runner.set(key, outcome)
                self.assert_fault(kind, bridge.connect, "connection-1", native=native)
        runner, bridge = self.make()
        self.assert_fault(FaultKind.ACCOUNT_CHANGED, bridge.connect, "connection-1",
                          expected_account_fingerprint=account_fingerprint(ACCOUNT_B),
                          native="selected_account_mismatch")
        binding = bridge.connect("connection-1", expected_account_fingerprint=account_fingerprint(ACCOUNT_A))
        self.assertEqual(binding.account_fingerprint, account_fingerprint(ACCOUNT_A))

    def test_unsafe_or_missing_account_identifiers_leave_binding_unknown(self) -> None:
        for account in ("user@example.invalid", "sk-" + "a" * 40, "", None, 123):
            with self.subTest(account=type(account).__name__):
                runner, bridge = self.make()
                runner.set("status", status(account=account))
                observed = bridge.probe()
                self.assertIs(observed.auth_state, AuthState.UNKNOWN)
                self.assertIn("ACCOUNT_NOT_OBSERVED", observed.reasons)
                self.assert_fault(FaultKind.AUTH_REQUIRED, bridge.connect, "connection-1",
                                  native="account_not_observed")

    def test_guarded_execution_uses_stdin_and_keeps_catalog_separate_from_observation(self) -> None:
        runner, bridge, binding = self.connected()
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        observed = guard.auth_status(binding)
        self.assertIs(observed.state, AuthState.AUTHENTICATED)
        bridge.prepare("request-1", HostCall(PROMPT))
        bound = guard.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
        self.assertIs(bound.receipt.state, JobState.SUCCEEDED)
        self.assertEqual(bound.receipt.provider_job_id, "session-synthetic-1")
        result = bridge.result("request-1")
        self.assertEqual(result.text, "synthetic answer")
        self.assertEqual(result.model, OBSERVED_MODEL)  # host-reported, not the requested model
        self.assertEqual(result.source, "synthetic_fixture")
        self.assertFalse(result.measured)
        self.assertEqual(bridge.observe_job(bound.request, bound.receipt), bound.receipt)
        executions = runner.specs("execute")
        self.assertEqual(len(executions), 1)
        self.assertNotIn(PROMPT, " ".join(executions[0].argv))
        self.assertEqual(executions[0].stdin, PROMPT.encode("utf-8"))
        self.assertIn(MODEL, executions[0].argv)
        cap = bridge.capability(binding)
        self.assertEqual(cap.configured_models, (MODEL,))
        self.assertEqual(cap.observed_models, (OBSERVED_MODEL,))
        self.assertEqual(cap.observation_source, "synthetic_fixture")
        self.assertTrue(cap.executable)
        self.assertEqual(cap.account_binding, "matched")
        self.assertEqual(cap.qualification_status, "NOT_RUN")
        self.assertEqual(cap.live_evidence_tasks, ("AIK-022", "AIK-082", "AIK-086"))
        self.assertIn("ISOLATION_SINGLE_ACTIVE", cap.reasons)
        self.assert_fault(FaultKind.UNSUPPORTED, bridge.interrupt_job, bound.request, bound.receipt)
        self.assert_fault(FaultKind.DUPLICATE_REQUEST, bridge.prepare, "request-1", HostCall(PROMPT))

    def test_every_command_has_allowlisted_environment_timeout_and_output_limit(self) -> None:
        environ = {"PATH": "/p", "HOME": "/h", "ANTHROPIC_API_KEY": SECRET_MARKER,
                   "CLAUDE_CODE_OAUTH_TOKEN": SECRET_MARKER, "SYNTHETIC_SECRET": SECRET_MARKER,
                   "UNRELATED_TOKEN": SECRET_MARKER, "UNRELATED": "value"}
        runner, bridge = self.make(environ=environ, exec_timeout=42.0,
                                   env_allowlist=ENV_ALLOWLIST | {"ANTHROPIC_API_KEY", "UNRELATED_TOKEN"})
        binding = bridge.connect("connection-1")
        bridge.prepare("request-1", HostCall(PROMPT))
        bridge.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
        self.assertTrue(runner.calls)
        for spec in runner.calls:
            self.assertEqual(dict(spec.env), {"PATH": "/p", "HOME": "/h"})
            self.assertGreater(spec.timeout, 0)
            self.assertGreater(spec.max_output_bytes, 0)
            self.assertNotIn(SECRET_MARKER, repr(spec))
        self.assertEqual(runner.specs("execute")[0].timeout, 42.0)
        self.assertEqual(build_environment(environ), (("HOME", "/h"), ("PATH", "/p")))

    def test_external_account_change_requires_reconnect_and_new_revision(self) -> None:
        runner, bridge, binding = self.connected()
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        bridge.prepare("request-1", HostCall(PROMPT))
        request = JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL)
        runner.set("status", status(account=ACCOUNT_B))
        self.assert_fault(FaultKind.ACCOUNT_CHANGED, guard.start_job, request)
        self.assertEqual(runner.count("execute"), 0)
        cap = bridge.capability(binding, refresh=False)
        self.assertEqual(cap.account_binding, "mismatched")
        self.assertTrue(cap.reconnect_required)
        self.assertIn("ACCOUNT_CHANGED", cap.reasons)
        self.assertIn("RECONNECT_REQUIRED", cap.reasons)
        self.assertFalse(cap.executable)
        self.assert_fault(FaultKind.ACCOUNT_CHANGED, bridge.reconnect, binding, native="account_switched")

        # Returning to the original account still requires explicit revalidation.
        runner.set("status", status(account=ACCOUNT_A))
        self.assert_fault(FaultKind.STALE_CONNECTION, guard.auth_status, binding, native="reconnect_required")
        self.assert_fault(FaultKind.STALE_CONNECTION, guard.start_job, request)
        rebound = bridge.reconnect(binding)
        self.assertEqual(rebound.revision, 2)
        self.assertEqual(rebound.credential_ref, binding.credential_ref)
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.describe, binding)
        bound = guard.start_job(JobRequest(rebound, "request-1", "route-1", "vm-attempt-1", MODEL))
        self.assertIs(bound.receipt.state, JobState.SUCCEEDED)
        self.assertEqual(bound.receipt.revision, 2)

    def test_expiry_is_distinct_and_official_login_revalidates_the_same_account(self) -> None:
        runner, bridge, binding = self.connected()
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        runner.set("status", status(logged_in=False, expired=True))
        observed = guard.auth_status(binding)
        self.assertIs(observed.state, AuthState.EXPIRED)
        self.assertIsNone(observed.account_fingerprint)
        bridge.prepare("request-1", HostCall(PROMPT))
        request = JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL)
        self.assert_fault(FaultKind.AUTH_REQUIRED, guard.start_job, request)
        cap = bridge.capability(binding)
        self.assertIs(cap.host_state, HostState.PRESENT)
        self.assertIs(cap.auth_state, AuthState.EXPIRED)
        self.assertIn("AUTH_EXPIRED", cap.reasons)
        self.assertIn("RECONNECT_REQUIRED", cap.reasons)
        self.assertNotIn("HOST_ABSENT", cap.reasons)
        self.assert_fault(FaultKind.AUTH_REQUIRED, bridge.reconnect, binding, native="auth_expired")

        pending = guard.begin_sign_in(binding)
        self.assertIs(pending.state, AuthState.PENDING)
        self.assertEqual(len(runner.launches), 1)
        launch = runner.launches[0]
        self.assertEqual(launch.argv, ("claude", "auth", "login"))
        self.assertIsNone(launch.stdin)
        self.assertEqual(dict(launch.env), {"PATH": "/synthetic/bin", "HOME": "/synthetic/home"})
        self.assert_fault(FaultKind.ACCOUNT_CONFLICT, guard.start_job, request)
        runner.set("status", status(account=ACCOUNT_A))
        self.assertIs(guard.auth_status(binding).state, AuthState.AUTHENTICATED)
        self.assertIs(guard.start_job(request).receipt.state, JobState.SUCCEEDED)

    def test_sign_in_launch_failures_are_classified(self) -> None:
        runner, bridge, binding = self.connected()
        runner.set("status", status(logged_in=False))
        runner.launch_error = RunnerNotStarted()
        self.assert_fault(FaultKind.UNSUPPORTED, bridge.begin_sign_in, binding, native="host_absent")
        runner.launch_error = RuntimeError(SECRET_MARKER)
        error = self.assert_fault(FaultKind.UNKNOWN_OUTCOME, bridge.begin_sign_in, binding,
                                  native="login_launch_unknown", outcome=Outcome.UNKNOWN)
        self.assertIsNone(error.__context__)
        self.assertNotIn(SECRET_MARKER, json.dumps(bridge.audit_log()))

    def test_release_forgets_handle_and_host_logout_requires_confirmation(self) -> None:
        runner, bridge, binding = self.connected()
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        released = guard.sign_out(binding)
        self.assertIs(released.state, AuthState.UNAUTHENTICATED)
        self.assertEqual(runner.count("logout"), 0)
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.auth_status, binding)

        runner, bridge, binding = self.connected()
        self.assert_fault(FaultKind.UNKNOWN_OUTCOME, bridge.release, binding, host_logout=True,
                          native="logout_unconfirmed")
        self.assertIs(bridge.auth_status(binding).state, AuthState.AUTHENTICATED)
        runner.set("status", status(logged_in=False))
        self.assertIs(bridge.release(binding, host_logout=True).state, AuthState.UNAUTHENTICATED)
        self.assertEqual(runner.count("logout"), 2)
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.describe, binding)

    def test_host_absent_marks_execution_unavailable_without_requiring_reconnect(self) -> None:
        runner, bridge, binding = self.connected()
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        runner.set("version", RunnerNotStarted)
        self.assertIs(guard.auth_status(binding).state, AuthState.UNKNOWN)
        caps = bridge.describe(binding)
        self.assertIs(caps.job_start, Support.UNSUPPORTED)
        self.assertIs(caps.sign_in, Support.UNSUPPORTED)
        bridge.prepare("request-1", HostCall(PROMPT))
        self.assert_fault(FaultKind.UNSUPPORTED, guard.start_job,
                          JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
        cap = bridge.capability(binding)
        self.assertIs(cap.host_state, HostState.ABSENT)
        self.assertIn("HOST_ABSENT", cap.reasons)
        self.assertNotIn("AUTH_EXPIRED", cap.reasons)
        self.assertFalse(cap.reconnect_required)
        self.assertFalse(cap.executable)
        self.assertEqual(runner.count("execute"), 0)

    def test_execution_timeout_is_unknown_outcome_and_fences_the_host(self) -> None:
        runner, bridge, binding = self.connected()
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        runner.set("execute", RunnerTimeout)
        bridge.prepare("request-1", HostCall(PROMPT))
        self.assert_fault(FaultKind.UNKNOWN_OUTCOME, guard.start_job,
                          JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL),
                          native="timeout", outcome=Outcome.UNKNOWN)
        self.assertIn((PROVIDER, RUNTIME), guard.uncertain_hosts)
        self.assert_fault(FaultKind.DUPLICATE_REQUEST, bridge.prepare, "request-1", HostCall(PROMPT))

    def test_malformed_or_truncated_output_is_unknown_outcome(self) -> None:
        outputs = (
            CommandResult(0, b"not json", SECRET_MARKER.encode()),
            CommandResult(0, job().stdout, truncated=True),
            job(exit_code=1, is_error=False),
            job(session="sk-" + "b" * 40),
            CommandResult(0, json.dumps({"type": "other"}).encode()),
        )
        for number, output in enumerate(outputs):
            with self.subTest(case=number):
                runner, bridge, binding = self.connected()
                runner.set("execute", output)
                bridge.prepare("request-1", HostCall(PROMPT))
                self.assert_fault(FaultKind.UNKNOWN_OUTCOME, bridge.start_job,
                                  JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL),
                                  native="malformed_output", outcome=Outcome.UNKNOWN)
                self.assertNotIn(SECRET_MARKER, json.dumps(bridge.audit_log()))

    def test_reported_failure_is_known_and_post_failure_expiry_requires_reconnect(self) -> None:
        runner, bridge, binding = self.connected()
        runner.set("execute", job(is_error=True, subtype="error_during_execution", exit_code=1))
        runner.set("status", status(), status(logged_in=False, expired=True))
        bridge.prepare("request-1", HostCall(PROMPT))
        receipt = bridge.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
        self.assertIs(receipt.state, JobState.FAILED)
        cap = bridge.capability(binding, refresh=False)
        self.assertTrue(cap.reconnect_required)
        self.assertIn("AUTH_EXPIRED", cap.reasons)

    def test_unconfigured_model_and_missing_prepare_never_run_the_host(self) -> None:
        runner, bridge, binding = self.connected()
        bridge.prepare("request-1", HostCall(PROMPT))
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.start_job,
                          JobRequest(binding, "request-1", "route-1", "vm-attempt-1", "unconfigured-model"),
                          native="model_not_configured")
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.start_job,
                          JobRequest(binding, "request-2", "route-1", "vm-attempt-1", MODEL),
                          native="call_not_prepared")
        self.assertEqual(runner.count("execute"), 0)
        runner, bridge = self.make(catalog=())
        binding = bridge.connect("connection-1")
        self.assertIs(bridge.describe(binding).job_start, Support.UNSUPPORTED)
        self.assertIn("CATALOG_EMPTY", bridge.capability(binding).reasons)

    def test_unknown_isolation_is_reported_and_blocks_execution(self) -> None:
        runner, bridge, binding = self.connected(isolation=AccountIsolation.UNKNOWN)
        guard = GuardedAdapter(bridge, authorize_dispatch=allow)
        bridge.prepare("request-1", HostCall(PROMPT))
        request = JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL)
        self.assert_fault(FaultKind.ACCOUNT_CONFLICT, guard.start_job, request)
        self.assert_fault(FaultKind.ACCOUNT_CONFLICT, bridge.start_job, request, native="isolation_unknown")
        cap = bridge.capability(binding)
        self.assertIn("ISOLATION_UNKNOWN", cap.reasons)
        self.assertFalse(cap.executable)
        self.assertEqual(runner.count("execute"), 0)

    def test_foreign_or_forged_bindings_are_stale(self) -> None:
        runner, bridge, binding = self.connected()
        _, other_bridge = self.make()
        self.assert_fault(FaultKind.STALE_CONNECTION, other_bridge.auth_status, binding)
        forged = ConnectionBinding(PROVIDER, RUNTIME, "connection-1", 1, AuthKind.NATIVE_SUBSCRIPTION,
                                   "host-session://claude-code/" + "0" * 32, binding.account_fingerprint)
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.describe, forged)
        api_binding = ConnectionBinding(PROVIDER, RUNTIME, "connection-1", 1, AuthKind.API_KEY,
                                        "secret://aik-vault/" + "0" * 32, binding.account_fingerprint)
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.describe, api_binding)
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.describe, object())

    def test_host_output_secrets_never_reach_records_or_errors(self) -> None:
        runner, bridge = self.make()
        runner.set("status", status(extra={"oauthToken": SECRET_MARKER, "email": SECRET_MARKER},
                                    stderr=SECRET_MARKER.encode()))
        runner.set("execute", job(stderr=SECRET_MARKER.encode()), CommandResult(0, b"x", SECRET_MARKER.encode()))
        binding = bridge.connect("connection-1")
        bridge.auth_status(binding)
        bridge.prepare("request-1", HostCall(PROMPT))
        bridge.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
        bridge.prepare("request-2", HostCall(PROMPT))
        with self.assertRaises(AdapterFailure) as caught:
            bridge.start_job(JobRequest(binding, "request-2", "route-1", "vm-attempt-1", MODEL))
        self.assertNotIn(SECRET_MARKER, str(caught.exception))
        rendered = " ".join((json.dumps(bridge.audit_log()), repr(binding), repr(bridge.capability(binding)),
                             repr(bridge.last_status()), repr(bridge), repr(bridge.result("request-1"))))
        self.assertNotIn(SECRET_MARKER, rendered)
        self.assertNotIn(PROMPT, rendered)

    def test_command_contract_validation(self) -> None:
        for kwargs in ({"timeout": 0}, {"timeout": 4000}, {"timeout": float("nan")},
                       {"max_output_bytes": 0}, {"max_output_bytes": 10**9}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                values = {"argv": ("claude",), "timeout": 1.0, "max_output_bytes": 16, **kwargs}
                with self.assertRaises(ValueError):
                    CommandSpec(**values)
        with self.assertRaises(ValueError):
            CommandSpec((), 1.0, 16)
        with self.assertRaises(ValueError):
            HostCommands(execute=("-p",))
        with self.assertRaises(ValueError):
            HostCommands(status=("auth", "status", "$(id)"))
        with self.assertRaises(ValueError):
            HostCall("")
        with self.assertRaises(TypeError):
            ClaudeHostBridge(object())

    def test_live_runner_reports_absent_executable_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aik-host-claude-empty-") as empty:
            spec = CommandSpec(("aik-nonexistent-host-cli-015",), 1.0, 16, (("PATH", empty),))
            runner = SubprocessRunner()
            self.assertEqual(runner.source, "live")
            with self.assertRaises(RunnerNotStarted):
                runner.run(spec)
            with self.assertRaises(RunnerNotStarted):
                runner.launch(spec)
        with self.assertRaises(TypeError):
            SubprocessRunner().run(("claude",))

    # -- review regressions (F1, F2, F3, F4, F5, F6, F7, F9, F10) -----------------

    def test_f1_begin_sign_in_does_not_clear_reconnect_after_external_account_round_trip(self) -> None:
        runner, bridge, binding = self.connected()
        runner.set("status", status(account=ACCOUNT_B))
        bridge.auth_status(binding)  # observes the external switch to another account
        runner.set("status", status(account=ACCOUNT_A))
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.begin_sign_in, binding,
                          native="reconnect_required")
        self.assertEqual(runner.launches, [])
        self.assertTrue(bridge.capability(binding).reconnect_required)
        bridge.prepare("request-1", HostCall(PROMPT))
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.start_job,
                          JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL),
                          native="reconnect_required")
        self.assertEqual(runner.count("execute"), 0)
        self.assertEqual(bridge.reconnect(binding).revision, 2)

    def test_f1_pending_login_is_cancelled_when_another_account_is_observed(self) -> None:
        runner, bridge, binding = self.connected()
        runner.set("status", status(logged_in=False))
        self.assertIs(bridge.begin_sign_in(binding).state, AuthState.PENDING)
        self.assertEqual(len(runner.launches), 1)
        runner.set("status", status(account=ACCOUNT_B))
        bridge.auth_status(binding)
        runner.set("status", status(account=ACCOUNT_A))
        self.assert_fault(FaultKind.STALE_CONNECTION, bridge.auth_status, binding, native="reconnect_required")
        cap = bridge.capability(binding)
        self.assertTrue(cap.reconnect_required)
        self.assertNotIn("LOGIN_PENDING", cap.reasons)

    def test_f2_bridge_login_launch_carries_the_login_deadline(self) -> None:
        runner, bridge = self.make()
        runner.set("status", status(logged_in=False))
        bridge.start_login()
        self.assertEqual(runner.launches[0].timeout, bridge_module.LOGIN_TIMEOUT)

    def test_f2_live_runner_kills_a_launched_process_after_its_deadline(self) -> None:
        # A local Python child stands in for the login window; it is not the host.
        runner = SubprocessRunner()
        spec = CommandSpec((sys.executable, "-c", "import time; time.sleep(30)"), 0.3, 16,
                           build_environment(os.environ))
        runner.launch(spec)
        process = runner._launched[-1]

        def cleanup():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        self.addCleanup(cleanup)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("The launched process outlived its absolute deadline.")

    def test_f3_failing_status_command_or_contradictory_status_is_never_authenticated(self) -> None:
        cases = (
            ("nonzero-exit-claims-login", status(exit_code=1), "AUTH_STATUS_UNRECOGNIZED"),
            ("logged-in-and-expired", status(expired=True), "AUTH_STATUS_CONTRADICTORY"),
        )
        for name, outcome, reason in cases:
            with self.subTest(case=name):
                runner, bridge = self.make()
                runner.set("status", outcome)
                observed = bridge.probe()
                self.assertIs(observed.host_state, HostState.PRESENT)
                self.assertIs(observed.auth_state, AuthState.UNKNOWN)
                self.assertIsNone(observed.account_fingerprint)
                self.assertIn(reason, observed.reasons)
                self.assert_fault(FaultKind.AUTH_REQUIRED, bridge.connect, "connection-1",
                                  native="account_not_observed")
        runner, bridge = self.make()
        runner.set("status", status(logged_in=False, exit_code=1))
        observed = bridge.probe()
        self.assertIs(observed.auth_state, AuthState.UNAUTHENTICATED)
        self.assertIn("AUTH_REQUIRED", observed.reasons)

    def test_f4_no_error_without_success_subtype_is_unknown_outcome(self) -> None:
        for subtype in (None, "synthetic-unrecognized-subtype"):
            with self.subTest(subtype=subtype):
                runner, bridge, binding = self.connected()
                runner.set("execute", job(subtype=subtype))
                bridge.prepare("request-1", HostCall(PROMPT))
                self.assert_fault(FaultKind.UNKNOWN_OUTCOME, bridge.start_job,
                                  JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL),
                                  native="malformed_output", outcome=Outcome.UNKNOWN)
                self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.result, "request-1")

    def test_f5_release_and_reconnect_are_refused_while_a_job_is_in_flight(self) -> None:
        runner = BlockingRunner()
        bridge = ClaudeHostBridge(runner, catalog=(MODEL,),
                                  environ={"PATH": "/synthetic/bin", "HOME": "/synthetic/home"})
        binding = bridge.connect("connection-1")
        bridge.prepare("request-1", HostCall(PROMPT))
        request = JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL)
        outcome: dict = {}

        def worker():
            try:
                outcome["receipt"] = bridge.start_job(request)
            except BaseException as error:  # recorded for the assertion below
                outcome["error"] = error

        thread = threading.Thread(target=worker, daemon=True)
        self.addCleanup(runner.execute_release.set)
        thread.start()
        self.assertTrue(runner.execute_started.wait(10))
        try:
            self.assert_fault(FaultKind.STALE_CONNECTION, bridge.release, binding, native="job_in_flight")
            self.assert_fault(FaultKind.STALE_CONNECTION, bridge.reconnect, binding, native="job_in_flight")
        finally:
            runner.execute_release.set()
            thread.join(10)
        self.assertNotIn("error", outcome)
        receipt = outcome["receipt"]
        self.assertIs(receipt.state, JobState.SUCCEEDED)
        self.assertEqual(receipt.revision, 1)
        self.assertEqual(bridge.observe_job(request, receipt), receipt)
        self.assertIs(bridge.release(binding).state, AuthState.UNAUTHENTICATED)

    def test_f6_start_login_launches_only_when_not_signed_in_and_classifies_failures(self) -> None:
        runner, bridge = self.make()
        self.assertIs(bridge.start_login().auth_state, AuthState.AUTHENTICATED)
        self.assertEqual(runner.launches, [])
        runner.set("status", status(logged_in=False, expired=True))
        self.assertIs(bridge.start_login().auth_state, AuthState.EXPIRED)
        self.assertEqual(len(runner.launches), 1)
        self.assertEqual(runner.launches[0].argv, ("claude", "auth", "login"))
        self.assertIsNone(runner.launches[0].stdin)
        runner.launch_error = RuntimeError(SECRET_MARKER)
        error = self.assert_fault(FaultKind.UNKNOWN_OUTCOME, bridge.start_login,
                                  native="login_launch_unknown", outcome=Outcome.UNKNOWN)
        self.assertIsNone(error.__context__)
        runner.launch_error = RunnerNotStarted()
        self.assert_fault(FaultKind.UNSUPPORTED, bridge.start_login, native="host_absent")
        self.assertNotIn(SECRET_MARKER, json.dumps(bridge.audit_log()))
        for outcome, native in ((RunnerNotStarted, "host_absent"),
                                (CommandResult(0, b"0.1.0"), "host_unsupported")):
            with self.subTest(native=native):
                runner, bridge = self.make()
                runner.set("version", outcome)
                self.assert_fault(FaultKind.UNSUPPORTED, bridge.start_login, native=native)
                self.assertEqual(runner.launches, [])

    def test_f6_host_logout_failure_and_absence_keep_the_handle_usable(self) -> None:
        cases = ((CommandResult(1, b""), FaultKind.PROVIDER_FAILURE, "logout_failed"),
                 (RunnerNotStarted, FaultKind.UNSUPPORTED, "host_absent"))
        for outcome, kind, native in cases:
            with self.subTest(native=native):
                runner, bridge, binding = self.connected()
                runner.set("logout", outcome)
                self.assert_fault(kind, bridge.release, binding, host_logout=True, native=native)
                self.assertIs(bridge.auth_status(binding).state, AuthState.AUTHENTICATED)
                bridge.prepare("request-1", HostCall(PROMPT))
                receipt = bridge.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
                self.assertIs(receipt.state, JobState.SUCCEEDED)
                self.assertIs(bridge.release(binding).state, AuthState.UNAUTHENTICATED)

    def test_f6_prepared_capacity_cancel_and_result_discard(self) -> None:
        runner, bridge, binding = self.connected()
        for number in range(bridge_module.MAX_PREPARED):
            bridge.prepare(f"request-{number}", HostCall(PROMPT))
        self.assert_fault(FaultKind.PROVIDER_FAILURE, bridge.prepare, "request-overflow", HostCall(PROMPT),
                          native="prepared_capacity")
        self.assertTrue(bridge.cancel_prepared("request-0"))
        self.assertFalse(bridge.cancel_prepared("request-0"))
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.cancel_prepared, "invalid id!")
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.start_job,
                          JobRequest(binding, "request-0", "route-1", "vm-attempt-1", MODEL),
                          native="call_not_prepared")
        bridge.prepare("request-0", HostCall(PROMPT))
        receipt = bridge.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
        self.assertIs(receipt.state, JobState.SUCCEEDED)
        self.assertEqual(bridge.result("request-1").provider_job_id, "session-synthetic-1")
        self.assertTrue(bridge.discard_result("request-1"))
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.result, "request-1")
        self.assertFalse(bridge.discard_result("request-1"))
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.discard_result, "invalid id!")
        self.assertEqual(runner.count("execute"), 1)

    def test_f6_reconnect_refuses_the_maximum_revision_without_probing(self) -> None:
        runner, bridge, binding = self.connected()
        maximum = 2**63 - 1
        bridge._handles[binding.credential_ref].revision = maximum
        maxed = replace(binding, revision=maximum)
        before = runner.count("status")
        self.assert_fault(FaultKind.INVALID_CONTRACT, bridge.reconnect, maxed)
        self.assertEqual(runner.count("status"), before)

    def test_f6_bridge_alone_consumes_the_request_on_pre_execution_rejection(self) -> None:
        # Without GuardedAdapter, a rejected start still consumes the prepared call.
        runner, bridge, binding = self.connected()
        runner.set("status", status(logged_in=False, expired=True))
        bridge.prepare("request-1", HostCall(PROMPT))
        request = JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL)
        self.assert_fault(FaultKind.AUTH_REQUIRED, bridge.start_job, request, native="auth_expired")
        self.assert_fault(FaultKind.DUPLICATE_REQUEST, bridge.start_job, request)
        self.assert_fault(FaultKind.DUPLICATE_REQUEST, bridge.prepare, "request-1", HostCall(PROMPT))
        self.assertEqual(runner.count("execute"), 0)

    def test_f7_unverified_account_selection_is_reported_in_capability(self) -> None:
        runner, bridge, binding = self.connected()
        self.assertIn("SELECTED_ACCOUNT_UNVERIFIED", bridge.capability(binding).reasons)
        runner, bridge = self.make()
        verified = bridge.connect("connection-1", expected_account_fingerprint=account_fingerprint(ACCOUNT_A))
        self.assertNotIn("SELECTED_ACCOUNT_UNVERIFIED", bridge.capability(verified).reasons)

    def test_f9_request_records_are_bounded_without_evicting_duplicate_protection(self) -> None:
        runner, bridge, binding = self.connected()
        with mock.patch.object(bridge_module, "MAX_REQUEST_RECORDS", 2):
            bridge.prepare("request-1", HostCall(PROMPT))
            bridge.start_job(JobRequest(binding, "request-1", "route-1", "vm-attempt-1", MODEL))
            bridge.prepare("request-2", HostCall(PROMPT))
            self.assert_fault(FaultKind.PROVIDER_FAILURE, bridge.prepare, "request-3", HostCall(PROMPT),
                              native="request_record_capacity")
            self.assertTrue(bridge.discard_result("request-1"))
            self.assert_fault(FaultKind.DUPLICATE_REQUEST, bridge.prepare, "request-1", HostCall(PROMPT))
            self.assertTrue(bridge.cancel_prepared("request-2"))
            bridge.prepare("request-3", HostCall(PROMPT))

    def test_f10_observed_models_are_separated_by_bound_account(self) -> None:
        runner, bridge = self.make()
        first = bridge.connect("connection-1")
        bridge.prepare("request-1", HostCall(PROMPT))
        bridge.start_job(JobRequest(first, "request-1", "route-1", "vm-attempt-1", MODEL))
        self.assertEqual(bridge.capability(first).observed_models, (OBSERVED_MODEL,))
        runner.set("status", status(account=ACCOUNT_B))
        second = bridge.connect("connection-2")
        self.assertEqual(bridge.capability(second).observed_models, ())
        self.assertEqual(bridge.capability(first, refresh=False).observed_models, (OBSERVED_MODEL,))

    # -- review regressions (R-1: live output bound and process-tree termination) --
    # Local synthetic Python processes only; none of them is the Claude Code host.

    def capture_tree(self, call, pidfile: Path):
        began = time.monotonic()
        result = error = None
        try:
            result = call()
        except Exception as caught:  # inspected by the caller
            error = caught
        elapsed = time.monotonic() - began
        grandchild = read_pid(pidfile)
        self.addCleanup(kill_quietly, grandchild)
        return result, error, elapsed, grandchild

    def tree_spec(self, pidfile: Path, mode: str, action: str, timeout: float, limit: int) -> CommandSpec:
        return CommandSpec((sys.executable, "-c", TREE_CHILD, str(pidfile), mode, action), timeout, limit,
                           build_environment(os.environ))

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_live_runner_stops_an_output_flood_at_the_limit_and_kills_the_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aik-host-claude-tree-") as temporary:
            pidfile = Path(temporary) / "grandchild.pid"
            spec = self.tree_spec(pidfile, "hold", "flood", 20.0, 1024)
            result, error, elapsed, grandchild = self.capture_tree(lambda: SubprocessRunner().run(spec), pidfile)
        self.assertIsNone(error)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), 1024)
        self.assertLess(elapsed, 10, "The flood was not stopped at the limit before the deadline.")
        self.assertTrue(wait_gone(grandchild), "A descendant survived the output-limit termination.")

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_live_runner_timeout_terminates_descendants_holding_the_pipe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aik-host-claude-tree-") as temporary:
            pidfile = Path(temporary) / "grandchild.pid"
            spec = self.tree_spec(pidfile, "hold", "sleep", 3.0, 1024)
            result, error, elapsed, grandchild = self.capture_tree(lambda: SubprocessRunner().run(spec), pidfile)
        self.assertIsNone(result)
        self.assertIs(type(error), RunnerTimeout)
        self.assertLess(elapsed, 15)
        self.assertTrue(wait_gone(grandchild), "A descendant survived the deadline.")

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_live_runner_terminates_descendants_left_after_a_normal_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aik-host-claude-tree-") as temporary:
            pidfile = Path(temporary) / "grandchild.pid"
            spec = self.tree_spec(pidfile, "hold", "exit", 20.0, 1024)
            result, error, elapsed, grandchild = self.capture_tree(lambda: SubprocessRunner().run(spec), pidfile)
        self.assertIsNone(error)
        self.assertEqual((result.exit_code, result.truncated), (0, False))
        self.assertLess(elapsed, 15)
        self.assertTrue(wait_gone(grandchild), "A descendant left behind by the command survived.")

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_unconfirmed_tree_termination_is_unknown_not_success(self) -> None:
        from aik_host_claude import _process
        import aik_host_claude.runner as runner_module

        spec = CommandSpec((sys.executable, "-c", "print('ok')"), 20.0, 1024, build_environment(os.environ))
        with mock.patch.object(_process, "GRACE_SECONDS", 0.2), \
                mock.patch.object(_process, "_group_alive", return_value=None):
            with self.assertRaises(runner_module.RunnerTerminationUnknown):
                SubprocessRunner().run(spec)
        self.assertTrue(issubclass(runner_module.RunnerTerminationUnknown, RunnerTimeout))
        result = SubprocessRunner().run(spec)
        self.assertEqual((result.exit_code, result.stdout.strip(), result.truncated), (0, b"ok", False))

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_live_launch_deadline_terminates_the_descendant_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aik-host-claude-tree-") as temporary:
            pidfile = Path(temporary) / "grandchild.pid"
            runner = SubprocessRunner()
            runner.launch(self.tree_spec(pidfile, "devnull", "sleep", 2.0, 16))
            process = runner._launched[-1]

            def cleanup():
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

            self.addCleanup(cleanup)
            grandchild = read_pid(pidfile)
            self.addCleanup(kill_quietly, grandchild)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("The launched process outlived its absolute deadline.")
        self.assertTrue(wait_gone(grandchild), "A descendant survived the launch deadline.")

    def test_r1_live_runner_keeps_stdin_delivery_and_bounded_stderr(self) -> None:
        code = ("import sys; data = sys.stdin.buffer.read(); sys.stdout.write(str(len(data)));"
                " sys.stderr.write('e' * 100)")
        spec = CommandSpec((sys.executable, "-c", code), 30.0, 1024, build_environment(os.environ),
                           b"y" * 200000)
        self.assertNotIn("y" * 16, " ".join(spec.argv))
        result = SubprocessRunner().run(spec)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, b"200000")
        self.assertEqual(result.stderr, b"e" * 100)
        self.assertFalse(result.truncated)


if __name__ == "__main__":
    unittest.main()
