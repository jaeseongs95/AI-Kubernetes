"""AIK-014 host protocol fixture tests for the Codex host-auth bridge.

The bridge is exercised only with synthetic runners, synthetic host outputs
and a synthetic VM runtime. No Codex CLI, provider, credential or network is
touched.

The SubprocessRunner bound and process-tree tests start only local
``sys.executable -c`` processes with synthetic code.

Passing on Linux is development verification, not Windows support evidence.
Fixture success is not a real login success.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import re
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

from aik_host_codex import (  # noqa: E402
    ENV_ALLOWLIST, GUI_COMMANDS, BindingState, CodexHostBridge, HostNotFound, HostPresence,
    LoginMethod, LoginState, OutputLimitExceeded, ProtocolSupport, RunnerError, RunnerTimeout,
    RunResult, SubprocessRunner, build_environment, validate_invocation,
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
POSIX_ONLY = "The Windows process-tree path (Job Object / taskkill) is NOT_RUN in this suite."
POSIX_WAITID_ONLY = "Requires POSIX waitid(WNOWAIT); the Windows path is NOT_RUN in this suite."

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


class BlockingObserveVm(FakeVm):
    """observe() blocks until released, to interleave a concurrent interrupt."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def observe(self, handle, request, job_id):
        self.entered.set()
        self.release.wait(10)
        return JobState.RUNNING


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

    @staticmethod
    def local_process_env() -> dict[str, str]:
        env = {"PATH": os.path.dirname(sys.executable)}
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return env

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

    # -- regression tests for the independent review (AIK-014) -----------------
    def test_f1_failed_or_expired_sign_in_does_not_clear_a_stale_binding(self) -> None:
        for ending in ("deadline", "failed_exit", "poll_error"):
            with self.subTest(ending=ending):
                self.host.status = ("chatgpt", ACCOUNT_A)
                bridge = self.make_bridge(self.vm)
                binding = bridge.connect("connection-1")
                self.host.status = ("chatgpt", ACCOUNT_B)
                bridge.auth_status(binding)  # marks the binding stale
                self.host.status = ("chatgpt", ACCOUNT_A)
                self.assertIs(bridge.begin_sign_in(binding).state, AuthState.PENDING)
                handle = self.host.handles[-1]
                if ending == "deadline":
                    self.now[0] += 601
                elif ending == "failed_exit":
                    handle.code = 1
                else:
                    handle.poll = mock.Mock(side_effect=RuntimeError(SECRET))
                self.assertFault(FaultKind.STALE_CONNECTION, bridge.auth_status, binding,
                                 native="reconnect_required")
                self.assertFault(FaultKind.STALE_CONNECTION, bridge.start_job,
                                 job(binding, f"request-{ending}"), native="reconnect_required")
                if ending == "deadline":
                    self.assertTrue(handle.terminated)
        self.assertEqual(self.vm.started, [])

    def test_f2_unsupported_host_version_blocks_every_adapter_path(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.version_output = b"unrelated tool 1\n"
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.start_job, job(binding, "request-1"),
                         native="host_unsupported")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.auth_status, binding, native="host_unsupported")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.connect, "connection-2", native="host_unsupported")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.begin_sign_in, binding, native="host_unsupported")
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.sign_out, binding, native="host_unsupported")
        self.assertEqual(self.vm.started, [])
        self.assertEqual(self.host.handles, [])
        self.assertEqual(self.host.status, ("chatgpt", ACCOUNT_A))
        self.assertNotIn(("logout",), [call[0][1:] for call in self.runner.calls])

        # The rejected request ID was not consumed; a running job then cannot
        # be interrupted through an unsupported host either.
        self.host.version_output = b"codex-cli 0.40.0\n"
        self.vm.state = JobState.RUNNING
        started = self.bridge.start_job(job(binding, "request-1"))
        self.assertIs(started.state, JobState.RUNNING)
        self.host.version_output = b"unrelated tool 1\n"
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.interrupt_job, job(binding, "request-1"), started,
                         native="host_unsupported")
        self.assertEqual(self.vm.interrupted, [])

    def test_f3_concurrent_observe_cannot_revert_an_interrupted_terminal_receipt(self) -> None:
        vm = BlockingObserveVm()
        vm.state = JobState.RUNNING
        bridge = self.make_bridge(vm)
        binding = bridge.connect("connection-1")
        request = job(binding, "request-1")
        started = bridge.start_job(request)
        results: list = []

        def observe() -> None:
            try:
                results.append(bridge.observe_job(request, started))
            except Exception as error:  # surfaced through the assertions below
                results.append(error)

        thread = threading.Thread(target=observe, daemon=True)
        thread.start()
        self.assertTrue(vm.entered.wait(5))
        interrupted = bridge.interrupt_job(request, started)
        self.assertIs(interrupted.state, JobState.CANCELLED)
        vm.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], interrupted)
        self.assertEqual(bridge.observe_job(request, interrupted), interrupted)

    def test_f4_interrupt_requires_current_auth_and_does_not_reach_vm_after_logout(self) -> None:
        # Documented limitation (kept by design): interrupt is not a
        # credential-free path; after a host logout it is refused.
        self.vm.state = JobState.RUNNING
        binding = self.bridge.connect("connection-1")
        request = job(binding, "request-1")
        started = self.bridge.start_job(request)
        self.host.status = ("logged_out", None)
        with self.assertRaises(AdapterFailure):
            self.bridge.interrupt_job(request, started)
        self.assertEqual(self.vm.interrupted, [])

    def test_f5_sign_out_refuses_to_log_out_a_different_host_account(self) -> None:
        first = self.bridge.connect("connection-1")
        self.host.status = ("chatgpt", ACCOUNT_B)
        second = self.bridge.connect("connection-2")
        self.runner.calls.clear()
        self.assertFault(FaultKind.ACCOUNT_CHANGED, self.bridge.sign_out, first, native="account_changed")
        self.assertEqual(self.host.status, ("chatgpt", ACCOUNT_B))
        self.assertNotIn(("logout",), [call[0][1:] for call in self.runner.calls])
        self.assertIs(self.bridge.auth_status(second).state, AuthState.AUTHENTICATED)

    def test_f6_restart_cannot_revive_a_fenced_revision_over_a_newer_one(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.status = ("chatgpt", ACCOUNT_B)
        self.bridge.auth_status(binding)
        self.host.status = ("chatgpt", ACCOUNT_A)
        renewed = self.bridge.reconnect(binding)
        self.assertEqual(renewed.revision, 2)

        restarted = self.make_bridge(self.vm)
        capability = restarted.probe(renewed)
        self.assertIs(capability.binding, BindingState.CHANGED)
        self.assertIn("connection_stale", capability.reasons)
        self.assertFalse(capability.execution_ready)
        self.assertFault(FaultKind.STALE_CONNECTION, restarted.start_job, job(binding, "request-1"),
                         native="reconnect_required")
        self.assertFault(FaultKind.STALE_CONNECTION, restarted.start_job, job(renewed, "request-2"),
                         native="reconnect_required")
        self.assertEqual(self.vm.started, [])
        latest = restarted.reconnect(renewed)
        self.assertEqual(latest.revision, 3)
        restarted.start_job(job(latest, "request-3"))
        self.assertEqual(self.vm.started[0][0].revision, 3)
        self.assertFault(FaultKind.STALE_CONNECTION, restarted.start_job, job(binding, "request-4"))

        # reconnect(previous) right after restart issues a newer revision.
        self.assertEqual(self.make_bridge(self.vm).reconnect(renewed).revision, 3)
        # A bridge-started sign-in cannot re-validate an adopted binding.
        third = self.make_bridge(self.vm)
        self.assertIs(third.begin_sign_in(renewed).state, AuthState.PENDING)
        self.host.handles[-1].complete(ACCOUNT_A)
        self.assertFault(FaultKind.STALE_CONNECTION, third.auth_status, renewed, native="reconnect_required")

    def test_f7_live_runner_resolves_only_through_the_allowlisted_path(self) -> None:
        live = SubprocessRunner()
        root = Path(self.temporary.name)
        outside = str(root / "elsewhere" / "codex")
        with mock.patch.object(runner_module.shutil, "which", return_value=outside), \
                mock.patch.object(runner_module.subprocess, "Popen",
                                  side_effect=AssertionError("must not spawn")) as popen:
            with self.assertRaises(HostNotFound):
                live.run(("codex", "--version"), env={}, timeout=1.0, max_output=16)
            with self.assertRaises(HostNotFound):
                live.spawn(("codex", "login"), env={})
            with self.assertRaises(HostNotFound):
                live.run(("codex", "--version"), env={"PATH": str(root / "synthetic-bin")},
                         timeout=1.0, max_output=16)
            with self.assertRaises(HostNotFound):
                live.spawn(("relative/codex", "login"), env={"PATH": str(root / "synthetic-bin")})
            popen.assert_not_called()
        inside = root / "synthetic-bin"
        with mock.patch.object(runner_module.shutil, "which", return_value=str(inside / "codex")), \
                mock.patch.object(runner_module.subprocess, "Popen", side_effect=OSError()) as popen:
            with self.assertRaises(RunnerError) as caught:
                live.run(("codex", "--version"), env={"PATH": str(inside)}, timeout=1.0, max_output=16)
            self.assertIs(type(caught.exception), RunnerError)
            popen.assert_called_once()

    def test_f8_probe_enforces_login_deadline_even_when_host_is_unsupported(self) -> None:
        self.host.status = ("logged_out", None)
        self.bridge.start_login("connection-1")
        self.host.version_output = b"unrelated tool 1\n"
        self.now[0] += 601
        capability = self.bridge.probe()
        self.assertIs(capability.protocol, ProtocolSupport.UNSUPPORTED)
        self.assertTrue(self.host.handles[0].terminated)

    def test_f9_subprocess_runner_bounds_with_a_local_synthetic_process(self) -> None:
        live = SubprocessRunner()
        env = self.local_process_env()
        result = live.run((sys.executable, "-c", "print('ok')"), env=env, timeout=30.0, max_output=1024)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output.strip(), b"ok")
        failed = live.run((sys.executable, "-c", "import sys; sys.exit(3)"), env=env, timeout=30.0,
                          max_output=1024)
        self.assertEqual(failed.exit_code, 3)
        began = time.monotonic()
        with self.assertRaises(RunnerTimeout):
            live.run((sys.executable, "-c", "import time; time.sleep(30)"), env=env, timeout=0.5,
                     max_output=1024)
        self.assertLess(time.monotonic() - began, 15)
        with self.assertRaises(OutputLimitExceeded):
            live.run((sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush()"),
                     env=env, timeout=30.0, max_output=1024)

    def test_f9_connect_and_reconnect_failure_paths_are_classified(self) -> None:
        binding = self.bridge.connect("connection-1")
        self.host.status = ("expired", None)
        self.assertFault(FaultKind.AUTH_REQUIRED, self.bridge.connect, "connection-2", native="auth_expired")
        self.assertFault(FaultKind.AUTH_REQUIRED, self.bridge.reconnect, binding, native="auth_expired")
        self.host.status = ("logged_out", None)
        self.assertFault(FaultKind.AUTH_REQUIRED, self.bridge.reconnect, binding, native="auth_required")
        self.host.present = False
        self.assertFault(FaultKind.UNSUPPORTED, self.bridge.reconnect, binding, native="host_absent")

    # -- review regressions (R-1: live output bound and process-tree termination) --
    # Local synthetic Python processes only; none of them is the Codex host.

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

    def tree_argv(self, pidfile: Path, mode: str, action: str) -> tuple[str, ...]:
        return (sys.executable, "-c", TREE_CHILD, str(pidfile), mode, action)

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_output_overflow_terminates_the_descendant_tree(self) -> None:
        pidfile = Path(self.temporary.name) / "overflow.pid"
        live = SubprocessRunner()
        result, error, elapsed, grandchild = self.capture_tree(
            lambda: live.run(self.tree_argv(pidfile, "hold", "flood"), env=self.local_process_env(),
                             timeout=30.0, max_output=1024), pidfile)
        self.assertIsNone(result)
        self.assertIs(type(error), OutputLimitExceeded)
        self.assertLess(elapsed, 15)
        self.assertTrue(wait_gone(grandchild), "A descendant survived the output-limit termination.")

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_timeout_terminates_descendants_holding_the_pipe_promptly(self) -> None:
        pidfile = Path(self.temporary.name) / "timeout.pid"
        live = SubprocessRunner()
        result, error, elapsed, grandchild = self.capture_tree(
            lambda: live.run(self.tree_argv(pidfile, "hold", "sleep"), env=self.local_process_env(),
                             timeout=3.0, max_output=1024), pidfile)
        self.assertIsNone(result)
        self.assertIs(type(error), RunnerTimeout)
        self.assertLess(elapsed, 15, "The runner waited for a descendant instead of terminating it.")
        self.assertTrue(wait_gone(grandchild), "A descendant survived the deadline.")

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_descendants_left_after_a_normal_exit_are_terminated(self) -> None:
        pidfile = Path(self.temporary.name) / "exit.pid"
        live = SubprocessRunner()
        result, error, elapsed, grandchild = self.capture_tree(
            lambda: live.run(self.tree_argv(pidfile, "devnull", "exit"), env=self.local_process_env(),
                             timeout=20.0, max_output=1024), pidfile)
        self.assertIsNone(error)
        self.assertEqual(result.exit_code, 0)
        self.assertLess(elapsed, 15)
        self.assertTrue(wait_gone(grandchild), "A descendant left behind by the command survived.")

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_unconfirmed_tree_termination_is_unknown_not_success(self) -> None:
        from aik_platform import process as _process

        live = SubprocessRunner()
        argv = (sys.executable, "-c", "print('ok')")
        with mock.patch.object(_process, "GRACE_SECONDS", 0.2), \
                mock.patch.object(_process, "_group_alive", return_value=None):
            with self.assertRaises(runner_module.ProcessTreeUnconfirmed) as caught:
                live.run(argv, env=self.local_process_env(), timeout=20.0, max_output=1024)
        self.assertIsInstance(caught.exception, RunnerTimeout)
        self.assertIsInstance(caught.exception, RunnerError)
        result = live.run(argv, env=self.local_process_env(), timeout=20.0, max_output=1024)
        self.assertEqual((result.exit_code, result.output.strip()), (0, b"ok"))

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r1_spawned_login_terminate_ends_the_descendant_tree(self) -> None:
        pidfile = Path(self.temporary.name) / "spawn.pid"
        handle = SubprocessRunner().spawn(self.tree_argv(pidfile, "devnull", "sleep"), env=self.local_process_env())
        process = handle._process

        def cleanup():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        self.addCleanup(cleanup)
        grandchild = read_pid(pidfile)
        self.addCleanup(kill_quietly, grandchild)
        handle.terminate()
        self.assertIsNotNone(handle.poll())
        self.assertTrue(wait_gone(grandchild), "A descendant survived the login cancellation.")
        self.assertIs(handle.termination_confirmed, True)

    # -- second review regressions (R2: process supervision and runner contract) --
    # Local synthetic processes or fake process objects only; none is the host.

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "waitid"), POSIX_WAITID_ONLY)
    def test_r2_f1_group_is_reobserved_before_the_leader_is_reaped(self) -> None:
        from aik_platform import process as _process

        process = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   **_process.popen_options())

        def cleanup():
            if process.returncode is None:
                process.wait(timeout=5)

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 10
        while not _process.exited(process):
            self.assertLess(time.monotonic(), deadline, "The synthetic leader did not exit.")
            time.sleep(0.02)
        self.assertIsNone(process.returncode)
        leader_unreaped: list = []
        real_confirm = _process._confirm_gone

        def recording(pgid):
            try:
                leader_unreaped.append(os.waitid(os.P_PID, process.pid,
                                                 os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)
            except ChildProcessError:
                leader_unreaped.append(False)
            return real_confirm(pgid)

        with mock.patch.object(_process, "_confirm_gone", side_effect=recording):
            self.assertTrue(_process.terminate_tree(process))
        self.assertEqual(leader_unreaped, [True], "The leader was reaped before its group was re-observed.")
        self.assertIsNotNone(process.returncode)

    @unittest.skipUnless(os.name == "posix", POSIX_ONLY)
    def test_r2_f1_already_reaped_leader_is_unconfirmed_without_signalling_its_group(self) -> None:
        from aik_platform import process as _process

        class ReapedLeader:
            pid = 999999999  # never signalled: the helpers below are mocked
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def send_signal(self, sig):
                raise AssertionError("A reaped leader must not be signalled.")

        with mock.patch.object(_process, "_signal_group") as signal_group, \
                mock.patch.object(_process, "_confirm_gone") as confirm:
            self.assertFalse(_process.terminate_tree(ReapedLeader()))
        signal_group.assert_not_called()
        confirm.assert_not_called()

    def test_r2_f2_windows_tree_is_confirmed_only_by_job_observation(self) -> None:
        from aik_platform import process as _process

        class ExitedChild:
            pid = 4242
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        with mock.patch.object(_process, "_WINDOWS", True), \
                mock.patch.object(_process, "_taskkill", return_value=True):
            self.assertFalse(_process.terminate_tree(ExitedChild()),
                             "A Windows tree without job observation was reported as confirmed.")
            tracked = ExitedChild()
            tracked._aik_job = 7
            with mock.patch.object(_process, "_job_active", side_effect=[2, 0]), \
                    mock.patch.object(_process, "_job_terminate") as terminate:
                self.assertTrue(_process.terminate_tree(tracked))
            terminate.assert_called_once_with(7)
            with mock.patch.object(_process, "GRACE_SECONDS", 0.05), \
                    mock.patch.object(_process, "_job_active", return_value=None), \
                    mock.patch.object(_process, "_job_terminate"):
                self.assertFalse(_process.terminate_tree(tracked))

    def test_r2_f2_spawned_process_is_tracked_for_tree_observation(self) -> None:
        from aik_platform import process as _process

        child = mock.Mock(returncode=0)
        with mock.patch.object(runner_module.subprocess, "Popen", return_value=child), \
                mock.patch.object(_process, "track_tree") as track:
            SubprocessRunner().spawn((sys.executable, "-c", "pass"), env=self.local_process_env())
        track.assert_called_once_with(child)

    def test_r2_f7_runner_timeout_does_not_claim_termination_for_its_subclass(self) -> None:
        doc = RunnerTimeout.__doc__
        self.assertIn("ProcessTreeUnconfirmed", doc)
        self.assertNotIn("the process tree was terminated.", doc)
        self.assertTrue(issubclass(runner_module.ProcessTreeUnconfirmed, RunnerTimeout))

    def test_r2_f8_runner_documents_the_cleanup_grace_beyond_the_timeout(self) -> None:
        self.assertIn("GRACE_SECONDS", runner_module.__doc__)

    def test_r2_f9_module_documents_spawn_as_the_unbounded_exception(self) -> None:
        doc = runner_module.__doc__
        self.assertIn("``spawn``", doc)
        self.assertIn("no deadline", doc)


if __name__ == "__main__":
    unittest.main()
