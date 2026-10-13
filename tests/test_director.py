"""AIK-024 director bootstrap tests with a fake runner, fake profile gate and fake broker.

Supervisor ownership and the inbox are the real packages over OS temporary
directories. No model, provider, credential or network is used. Linux passing
is development verification, not Windows support.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_broker.errors import BrokerDenied  # noqa: E402
from aik_config.errors import InvalidReference  # noqa: E402
from aik_director import (  # noqa: E402
    CredentialScope, Director, DirectorConfig, DirectorRunner, InvalidDirectorConfig,
    ProbeResult, RunnerSession,
)
from aik_inbox import Consumer, InboxMessage, InboxStore  # noqa: E402
from aik_profiles import (  # noqa: E402
    CapabilityAttempt, DirectorProfile, ExecutionChoice, ExecutionDecision, RoleProfile,
)
from aik_runtime import CredentialRef, ExecutionProfile, Lease, Placement, Pool, RuntimeStore  # noqa: E402
from aik_runtime.errors import StorageError  # noqa: E402


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"
STAMP = "2030-01-01T00:00:00Z"
CHILD_DIRECTOR = r'''
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, sys.argv[1])
from aik_director import Director, DirectorConfig, DirectorRunner, ProbeResult, RunnerSession
from aik_profiles import DirectorProfile, ExecutionChoice, ExecutionDecision


class Profiles:
    def prepare_execution(self, profile_id, inventory, *, approval=None, clock=None):
        selected = ExecutionChoice("conn-a", "synthetic-model", "high", ("shell",))
        profile = DirectorProfile("director-1", "ws-1", selected, 40)
        return ExecutionDecision("bound", profile, 3, selected, "primary", None, (), (), "2030-01-01T00:00:00Z")


class Runner(DirectorRunner):
    def probe(self, binding):
        return ProbeResult("ok")

    def start(self, request):
        return RunnerSession("child-session", "synthetic-model", "high")

    def stop(self, session):
        pass

    def health(self, session):
        return "alive"

    def wake(self, session, signal):
        pass


config = DirectorConfig("ws-1", profile_id="director-1", state_root=Path(sys.argv[2]), attach_timeout=0.2)
result = Director(config, Runner(), Profiles()).start()
temporary = Path(sys.argv[3] + ".tmp")
temporary.write_text(f"{result.status} {result.instance_id}", encoding="utf-8")
os.replace(temporary, sys.argv[3])
time.sleep(30)
'''


class SyntheticInterrupt(BaseException):
    """A non-Exception interruption (like KeyboardInterrupt) for fault paths."""


def fixed_clock():
    return datetime(2030, 1, 1, tzinfo=timezone.utc)


def choice():
    return ExecutionChoice("conn-a", "synthetic-model", "high", ("shell",))


class FakeProfiles:
    """Same signature as aik_profiles.ProfileStore.prepare_execution."""

    def __init__(self, mode="bound"):
        self.mode = mode
        self.calls = []

    def prepare_execution(self, profile_id, inventory, *, approval=None, clock=None):
        self.calls.append(profile_id)
        selected = choice()
        if self.mode == "missing":
            raise InvalidReference("synthetic missing profile")
        if self.mode == "storage_error":
            raise StorageError("synthetic profile store outage")
        if self.mode == "error":
            raise RuntimeError(SECRET_MARKER)
        if self.mode == "worker":
            profile = RoleProfile("director-1", "ws-1", "worker", selected, 10)
            return ExecutionDecision("bound", profile, 1, selected, "primary", None, (), (), STAMP)
        if self.mode == "scope":
            profile = DirectorProfile("director-other", "ws-1", selected, 40)
            return ExecutionDecision("bound", profile, 3, selected, "primary", None, (), (), STAMP)
        profile = DirectorProfile("director-1", "ws-1", selected, 40)
        if self.mode == "rejected":
            attempt = CapabilityAttempt("primary", selected, False, ("EFFORT_NOT_SUPPORTED",), None)
            return ExecutionDecision("rejected", profile, 3, None, None, None, (attempt,),
                                     ("PRIMARY_REJECTED", "FALLBACK_NOT_APPROVED"), STAMP)
        if self.mode == "references":
            return ExecutionDecision("rejected", profile, 3, None, None, None, (),
                                     ("REFERENCE_MISSING", "CROSS_WORKSPACE_REFERENCE"), STAMP)
        return ExecutionDecision("bound", profile, 3, selected, "primary", None, (), (), STAMP)


class FakeRunner(DirectorRunner):
    def __init__(self, probe=None, observed=("synthetic-model", "high"), start_error=None,
                 probe_error=None, invalid_session=False, stop_failures=0):
        self.probe_result = ProbeResult("ok") if probe is None else probe
        self.observed = observed
        self.start_error = start_error
        self.probe_error = probe_error
        self.invalid_session = invalid_session
        self.stop_failures = stop_failures
        self.probes = 0
        self.starts = []
        self.stops = []
        self.wakes = []

    def probe(self, binding):
        self.probes += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result

    def start(self, request):
        if self.start_error is not None:
            raise self.start_error
        self.starts.append(request)
        if self.invalid_session:
            return SimpleNamespace(session_id="invalid-session")
        return RunnerSession(f"session-{len(self.starts)}", *self.observed)

    def stop(self, session):
        # Every attempt is recorded, including the ones that fail.
        self.stops.append(getattr(session, "session_id", None))
        if self.stop_failures > 0:
            self.stop_failures -= 1
            raise RuntimeError(SECRET_MARKER)

    def health(self, session):
        return "alive"

    def wake(self, session, signal):
        # The only path where a real runner would run a model turn.
        self.wakes.append(signal.available)


class FakeBroker:
    def __init__(self, deny=None, revoke_error=False):
        self.deny = deny
        self.revoke_error = revoke_error
        self.issued = []
        self.revoked = []

    def issue_grant(self, **request):
        if self.deny is not None:
            raise BrokerDenied(self.deny)
        self.issued.append(request)
        return SimpleNamespace(grant_id=f"grant-{len(self.issued)}")

    def revoke(self, grant_id):
        self.revoked.append(grant_id)
        if self.revoke_error:
            raise RuntimeError(SECRET_MARKER)
        return True


class DirectorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-director-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_root = self.root / "state"
        self.broker = FakeBroker()
        self.now = 1000.0

    def config(self, **overrides) -> DirectorConfig:
        values = {
            "workspace_id": "ws-1", "profile_id": "director-1", "state_root": self.state_root,
            "attach_timeout": 0.2,
            "credential_scope": CredentialScope(self.broker, "adapter-vendor", "lease-director", 1),
        }
        values.update(overrides)
        return DirectorConfig(**values)

    def director(self, runner=None, profiles=None, inbox=None, **overrides):
        runner = runner or FakeRunner()
        profiles = profiles or FakeProfiles()
        director = Director(self.config(**overrides), runner, profiles, inbox=inbox, clock=fixed_clock)
        self.addCleanup(director.stop)
        return director, runner, profiles

    def inbox(self) -> InboxStore:
        return InboxStore(self.root / "inbox" / "inbox.sqlite3", clock=lambda: self.now)

    def endpoint(self) -> Path:
        return self.state_root / "ws-1" / "director.endpoint.json"

    @staticmethod
    def stop_process(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    @staticmethod
    def event_names(director) -> list:
        return [event.event for event in director.events()]

    # ---------------------------------------------------- criterion 1
    def test_check_verifies_connection_without_owning_starting_or_workers(self) -> None:
        director, runner, profiles = self.director()
        result = director.check()
        self.assertTrue(result.ready)
        self.assertEqual(result.binding.model, "synthetic-model")
        self.assertEqual(result.binding.effort, "high")
        self.assertEqual(profiles.calls, ["director-1"])
        self.assertEqual(runner.probes, 1)
        self.assertEqual(runner.starts, [])
        self.assertEqual([grant["connection_ids"] for grant in self.broker.issued], [("conn-a",)])
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertFalse(self.endpoint().exists())
        self.assertEqual(director.state, "STOPPED")

    def test_start_without_any_worker_placement_passes_only_ids_to_runner(self) -> None:
        director, runner, _ = self.director()
        result = director.start()
        self.assertEqual(result.status, "started")
        self.assertEqual(director.state, "RUNNING")
        self.assertTrue(self.endpoint().is_file())
        request = runner.starts[0]
        self.assertEqual(request.grant_id, "grant-1")
        self.assertEqual(request.binding.connection_id, "conn-a")
        self.assertEqual(request.instance_id, result.instance_id)
        started = [event for event in director.events() if event.event == "started"][0]
        self.assertEqual(dict(started.details)["worker_placements"], 0)
        self.assertEqual(started.at, STAMP)

    # ---------------------------------------------------- criterion 2
    def test_unconfigured_profile_returns_select_model_without_probe_or_start(self) -> None:
        director, runner, profiles = self.director(profile_id=None)
        result = director.start()
        self.assertEqual(result.status, "not_configured")
        self.assertEqual([(i.field, i.code, i.action) for i in result.issues],
                         [("director_profile", "PROFILE_NOT_CONFIGURED", "select_model")])
        self.assertEqual((profiles.calls, runner.probes, runner.starts), ([], 0, []))
        self.assertFalse(self.endpoint().exists())
        # Ownership was released: a configured director can start immediately.
        other, _, _ = self.director()
        self.assertEqual(other.start().status, "started")

    def test_missing_profile_and_non_director_profile_are_specific(self) -> None:
        missing, _, _ = self.director(profiles=FakeProfiles("missing"))
        self.assertEqual(missing.check().issues[0].code, "PROFILE_NOT_FOUND")
        worker, runner, _ = self.director(profiles=FakeProfiles("worker"))
        result = worker.start()
        self.assertEqual(result.status, "not_configured")
        self.assertEqual(result.issues[0].code, "NOT_A_DIRECTOR_PROFILE")
        self.assertEqual(runner.starts, [])

    def test_capability_rejection_lists_model_and_fallback_items(self) -> None:
        director, runner, _ = self.director(profiles=FakeProfiles("rejected"))
        result = director.start()
        self.assertEqual(result.status, "not_configured")
        self.assertEqual([(i.field, i.code, i.action, i.details) for i in result.issues], [
            ("model", "MODEL_NOT_SUPPORTED", "select_model", ("EFFORT_NOT_SUPPORTED",)),
            ("fallback", "FALLBACK_APPROVAL_REQUIRED", "approve_fallback", ()),
        ])
        self.assertEqual((runner.probes, runner.starts), (0, []))
        self.assertEqual(director.health()["issues"][0]["code"], "MODEL_NOT_SUPPORTED")

    def test_login_required_from_probe_does_not_start_or_issue_grant(self) -> None:
        runner = FakeRunner(probe=ProbeResult("auth_required", "LOGIN_REQUIRED"))
        director, _, _ = self.director(runner=runner)
        result = director.start()
        self.assertEqual(result.status, "auth_required")
        self.assertEqual([(i.field, i.action) for i in result.issues], [("login", "login")])
        self.assertEqual((runner.starts, self.broker.issued), ([], []))
        self.assertFalse(self.endpoint().exists())

    def test_broker_auth_required_is_reported_as_credential_login(self) -> None:
        self.broker.deny = "AUTH_REQUIRED"
        director, runner, _ = self.director()
        result = director.start()
        self.assertEqual(result.status, "auth_required")
        self.assertEqual([(i.field, i.code) for i in result.issues], [("credential", "AUTH_REQUIRED")])
        self.assertEqual(runner.starts, [])

    def test_connection_unavailable_probe_is_unavailable(self) -> None:
        director, _, _ = self.director(runner=FakeRunner(probe=ProbeResult("unavailable")))
        result = director.check()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.issues[0].code, "CONNECTION_UNAVAILABLE")

    def test_probe_exception_and_invalid_probe_result_are_retry_items(self) -> None:
        raising = FakeRunner(probe_error=RuntimeError(SECRET_MARKER))
        director, _, _ = self.director(runner=raising)
        result = director.start()
        self.assertEqual((result.status, result.code), ("unavailable", "PROBE_FAILED"))
        self.assertEqual([(i.field, i.action) for i in result.issues], [("runner", "retry")])
        self.assertEqual((raising.starts, self.broker.issued), ([], []))
        self.assertFalse(self.endpoint().exists())
        self.assertNotIn(SECRET_MARKER, json.dumps([event.to_dict() for event in director.events()]))
        invalid, _, _ = self.director(runner=FakeRunner(probe=object()))
        self.assertEqual(invalid.check().issues[0].code, "PROBE_INVALID")

    def test_profile_store_errors_are_retry_items_without_raw_text(self) -> None:
        store_down, _, _ = self.director(profiles=FakeProfiles("storage_error"))
        result = store_down.check()
        self.assertEqual((result.status, result.issues[0].code, result.issues[0].action),
                         ("unavailable", "PROFILE_STORE_UNAVAILABLE", "retry"))
        failing, runner, _ = self.director(profiles=FakeProfiles("error"))
        started = failing.start()
        self.assertEqual((started.status, started.code), ("unavailable", "PROFILE_CHECK_FAILED"))
        self.assertEqual((runner.probes, runner.starts), (0, []))
        text = json.dumps([event.to_dict() for event in failing.events()]) + json.dumps(failing.health())
        self.assertNotIn(SECRET_MARKER, text)

    def test_profile_scope_mismatch_and_reference_rejections_are_configuration_items(self) -> None:
        scope, runner, _ = self.director(profiles=FakeProfiles("scope"))
        result = scope.start()
        self.assertEqual(result.status, "not_configured")
        self.assertEqual([(i.field, i.code, i.action) for i in result.issues],
                         [("director_profile", "PROFILE_SCOPE_MISMATCH", "configure_profile")])
        self.assertEqual((runner.probes, runner.starts), (0, []))
        references, _, _ = self.director(profiles=FakeProfiles("references"))
        rejected = references.check()
        self.assertEqual(rejected.status, "not_configured")
        self.assertEqual([(i.field, i.code, i.action) for i in rejected.issues], [
            ("connection", "CONNECTION_NOT_CONFIGURED", "configure_connection"),
            ("connection", "CONNECTION_OTHER_WORKSPACE", "configure_connection"),
        ])

    def test_broker_connection_unavailable_and_lease_inactive_are_retryable(self) -> None:
        self.broker.deny = "CONNECTION_UNAVAILABLE"
        unavailable, runner, _ = self.director()
        result = unavailable.start()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual([(i.field, i.code, i.action) for i in result.issues],
                         [("connection", "CONNECTION_UNAVAILABLE", "fix_connection")])
        self.assertEqual(runner.starts, [])
        self.assertFalse(self.endpoint().exists())
        self.broker.deny = "LEASE_INACTIVE"
        inactive, _, _ = self.director()
        checked = inactive.check()
        self.assertEqual(checked.status, "unavailable")
        self.assertEqual([(i.field, i.code, i.action) for i in checked.issues],
                         [("credential", "LEASE_INACTIVE", "retry")])

    def test_grant_revoke_failure_is_recorded_without_raising_or_raw_text(self) -> None:
        self.broker.revoke_error = True
        director, _, _ = self.director()
        result = director.check()
        self.assertTrue(result.ready)
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertIn("grant_revoke_failed", self.event_names(director))
        self.assertNotIn(SECRET_MARKER, json.dumps([event.to_dict() for event in director.events()]))

    # ---------------------------------------------------- criterion 3
    def test_duplicate_start_in_process_and_from_second_director_is_rejected(self) -> None:
        first, first_runner, _ = self.director()
        started = first.start()
        self.assertEqual(first.start().status, "already_running")
        second_runner, second_profiles = FakeRunner(), FakeProfiles()
        second, _, _ = self.director(runner=second_runner, profiles=second_profiles)
        duplicate = second.start()
        self.assertEqual(duplicate.status, "already_running")
        self.assertEqual(duplicate.instance_id, started.instance_id)
        self.assertEqual((second_runner.starts, second_profiles.calls, second_runner.probes), ([], [], 0))
        self.assertEqual(len(first_runner.starts), 1)
        self.assertIn("start_rejected_duplicate", [event.event for event in second.events()])
        self.assertEqual(first.stop().status, "stopped")
        self.assertEqual(second.start().status, "started")

    def test_duplicate_start_from_another_process_is_rejected_until_it_dies(self) -> None:
        marker = self.root / "child-started.marker"
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD_DIRECTOR, str(ROOT / "src"), str(self.state_root), str(marker)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.addCleanup(self.stop_process, process)
        deadline = time.monotonic() + 20
        while not marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"Child director exited before starting: {stdout}{stderr}")
            if time.monotonic() >= deadline:
                self.fail("Child director did not start within twenty seconds.")
            time.sleep(0.02)
        status, child_instance = marker.read_text(encoding="utf-8").split()
        self.assertEqual(status, "started")
        director, runner, profiles = self.director()
        duplicate = director.start()
        self.assertEqual((duplicate.status, duplicate.instance_id), ("already_running", child_instance))
        self.assertEqual((runner.starts, runner.probes, profiles.calls), ([], 0, []))
        process.kill()
        process.communicate(timeout=5)
        # Process death releases the OS lock; a new owner can start.
        self.assertEqual(director.start().status, "started")

    def test_actual_model_and_effort_observation_is_recorded(self) -> None:
        director, _, _ = self.director()
        result = director.start()
        self.assertEqual((result.observation.observed_model, result.observation.observed_effort),
                         ("synthetic-model", "high"))
        self.assertIs(result.observation.matches, True)
        observed = [event for event in director.events() if event.event == "observed"][0]
        details = dict(observed.details)
        self.assertEqual((details["requested_model"], details["observed_model"], details["matches"]),
                         ("synthetic-model", "synthetic-model", True))
        self.assertIsNone(observed.code)
        health = director.health()
        self.assertEqual((health["observed_model"], health["observation_match"]), ("synthetic-model", True))

    def test_unobserved_model_is_unknown_not_matched(self) -> None:
        director, _, _ = self.director(runner=FakeRunner(observed=(None, None)))
        result = director.start()
        self.assertEqual(result.status, "started")
        self.assertIsNone(result.observation.matches)

    def test_incomplete_observation_starts_with_a_distinct_event_code(self) -> None:
        director, _, _ = self.director(runner=FakeRunner(observed=("synthetic-model", None)))
        result = director.start()
        self.assertEqual(result.status, "started")
        self.assertIsNone(result.observation.matches)
        observed = [event for event in director.events() if event.event == "observed"][0]
        self.assertEqual(observed.code, "OBSERVATION_INCOMPLETE")
        self.assertIsNone(dict(observed.details)["matches"])

    def test_observed_mismatch_fails_closed_and_releases_everything(self) -> None:
        runner = FakeRunner(observed=("other-model", "high"))
        director, _, _ = self.director(runner=runner)
        result = director.start()
        self.assertEqual((result.status, result.code), ("failed", "OBSERVED_BINDING_MISMATCH"))
        self.assertEqual(runner.stops, ["session-1"])
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertEqual(director.state, "FAILED")
        self.assertFalse(self.endpoint().exists())
        self.assertIn("start_failed", [event.event for event in director.events()])

    def test_effort_only_mismatch_fails_closed(self) -> None:
        runner = FakeRunner(observed=("synthetic-model", "low"))
        director, _, _ = self.director(runner=runner)
        result = director.start()
        self.assertEqual((result.status, result.code), ("failed", "OBSERVED_BINDING_MISMATCH"))
        self.assertIs(result.observation.matches, False)
        self.assertEqual(runner.stops, ["session-1"])
        self.assertFalse(self.endpoint().exists())

    def test_invalid_runner_session_is_stopped_and_rejected(self) -> None:
        runner = FakeRunner(invalid_session=True)
        director, _, _ = self.director(runner=runner)
        result = director.start()
        self.assertEqual((result.status, result.code), ("failed", "RUNNER_SESSION_INVALID"))
        self.assertEqual(runner.stops, ["invalid-session"])
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertEqual(director.state, "FAILED")
        self.assertFalse(self.endpoint().exists())

    def test_runner_start_failure_is_recorded_without_raw_exception_text(self) -> None:
        sink = []
        runner = FakeRunner(start_error=RuntimeError(SECRET_MARKER))
        director = Director(self.config(), runner, FakeProfiles(), clock=fixed_clock, event_sink=sink.append)
        result = director.start()
        self.assertEqual((result.status, result.code), ("failed", "RUNNER_START_FAILED"))
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertEqual(director.state, "FAILED")
        text = json.dumps([event.to_dict() for event in director.events()]) + json.dumps(sink) \
            + json.dumps(director.health()) + repr(result)
        self.assertNotIn(SECRET_MARKER, text)
        retry, _, _ = self.director()
        self.assertEqual(retry.start().status, "started")

    # F1: a runner that could not be stopped keeps single ownership.
    def test_mismatch_with_failed_runner_stop_keeps_ownership_until_stop_succeeds(self) -> None:
        runner = FakeRunner(observed=("other-model", "high"), stop_failures=1)
        director, _, _ = self.director(runner=runner)
        result = director.start()
        self.assertEqual((result.status, result.code), ("failed", "OBSERVED_BINDING_MISMATCH"))
        self.assertIn("RUNNER_STOP_FAILED", [issue.code for issue in result.issues])
        self.assertEqual(director.state, "FAILED")
        self.assertTrue(self.endpoint().is_file())
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertIn("runner_stop_failed", self.event_names(director))
        health = director.health()
        self.assertTrue(health["runner_stop_pending"])
        self.assertFalse(health["accepting_work"])
        again = director.start()
        self.assertEqual((again.status, again.code), ("failed", "RUNNER_STOP_FAILED"))
        self.assertEqual((runner.probes, len(runner.starts)), (1, 1))
        other_runner = FakeRunner()
        other, _, _ = self.director(runner=other_runner)
        self.assertEqual(other.start().status, "already_running")
        self.assertEqual(other_runner.starts, [])
        stopped = director.stop()
        self.assertEqual((stopped.status, stopped.runner_stopped), ("stopped", True))
        self.assertEqual(runner.stops, ["session-1", "session-1"])
        self.assertFalse(self.endpoint().exists())
        self.assertFalse(director.health()["runner_stop_pending"])
        self.assertEqual(other.start().status, "started")

    def test_stop_failure_keeps_ownership_and_reports_unknown_scan(self) -> None:
        runner = FakeRunner()
        director, _, _ = self.director(runner=runner)
        self.assertEqual(director.start().status, "started")
        runner.stop_failures = 1
        result = director.stop()
        self.assertEqual((result.status, result.runner_stopped, result.scan_complete),
                         ("stop_failed", False, False))
        self.assertEqual(director.state, "FAILED")
        self.assertTrue(self.endpoint().is_file())
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertEqual(director.health()["issues"][0]["code"], "RUNNER_STOP_FAILED")
        self.assertIsNone(director.poll())
        other_runner = FakeRunner()
        other, _, _ = self.director(runner=other_runner)
        self.assertEqual(other.start().status, "already_running")
        self.assertEqual(other_runner.starts, [])
        retry = director.stop()
        self.assertEqual((retry.status, retry.runner_stopped), ("stopped", True))
        self.assertFalse(self.endpoint().exists())
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertNotIn(SECRET_MARKER, json.dumps([event.to_dict() for event in director.events()]))

    # F2: an unexpected launch exception must not strand STARTING.
    def test_unexpected_launch_exception_does_not_strand_starting_state(self) -> None:
        director, runner, _ = self.director()
        with mock.patch("aik_director.director.launch", side_effect=ValueError(SECRET_MARKER)):
            result = director.start()
        self.assertEqual((result.status, result.code), ("unavailable", "SUPERVISOR_UNAVAILABLE"))
        self.assertEqual(director.state, "FAILED")
        self.assertEqual(runner.probes, 0)
        self.assertNotIn(SECRET_MARKER, json.dumps([event.to_dict() for event in director.events()]))
        with mock.patch("aik_director.director.launch", side_effect=SyntheticInterrupt()):
            with self.assertRaises(SyntheticInterrupt):
                director.start()
        self.assertEqual(director.state, "FAILED")
        self.assertEqual(director.start().status, "started")

    # F3: attempts and interrupted starts are recorded.
    def test_start_attempt_and_rejected_stop_are_recorded(self) -> None:
        director, _, _ = self.director()
        director.start()
        self.assertEqual(self.event_names(director)[0], "start_requested")
        idle, _, _ = self.director()
        idle.stop()
        self.assertIn("stop_rejected_not_running", self.event_names(idle))

    def test_interrupted_start_records_failure_and_stops_the_session(self) -> None:
        def sink(record):
            if record["event"] == "observed":
                raise SyntheticInterrupt()

        runner = FakeRunner()
        director = Director(self.config(), runner, FakeProfiles(), clock=fixed_clock, event_sink=sink)
        self.addCleanup(director.stop)
        with self.assertRaises(SyntheticInterrupt):
            director.start()
        failed = [event for event in director.events() if event.event == "start_failed"]
        self.assertEqual([event.code for event in failed], ["UNCLASSIFIED"])
        self.assertEqual(runner.stops, ["session-1"])
        self.assertEqual(director.state, "FAILED")
        self.assertFalse(self.endpoint().exists())
        self.assertEqual(self.broker.revoked, ["grant-1"])

    # F4: a stop that scanned nothing must not claim a complete scan.
    def test_not_running_stop_reports_unknown_instead_of_complete(self) -> None:
        director, _, _ = self.director()
        result = director.stop()
        self.assertEqual(result.status, "not_running")
        self.assertEqual((result.in_flight_lease_ids, result.inbox_leased_ids), ((), ()))
        self.assertFalse(result.scan_complete)
        self.assertFalse(result.inbox_scan_complete)
        self.assertFalse(result.runner_stopped)

    def test_stop_preserves_in_flight_inbox_and_runtime_leases(self) -> None:
        store = RuntimeStore(self.root / "runtime" / "runtime.sqlite3")
        lease = Lease("lease-1", "placement-1", "pool-1", "worker-1", 1, STAMP, state="active")
        with store.transaction() as tx:
            for model in (
                CredentialRef("credential-1", "secret://local/test-key"),
                Pool("pool-1", "synthetic", "connection-1", "credential-1"),
                ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model"),
                Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                          "vm-task-1", "vm-attempt-1"),
                lease,
            ):
                tx.save(model, expected_revision=0)
        inbox = self.inbox()
        inbox.receive(InboxMessage("msg-1", "goal-1", "gui", 1, "user_instruction", {"text": "synthetic"}))
        director, runner, _ = self.director(inbox=inbox, runtime_store=store)
        self.assertEqual(director.start().status, "started")
        in_progress = Consumer(inbox, "director").next()
        self.assertIsNotNone(in_progress)
        result = director.stop()
        self.assertEqual(result.status, "stopped")
        self.assertEqual((result.in_flight_lease_ids, result.scan_complete), (("lease-1",), True))
        self.assertEqual((result.inbox_leased_ids, result.inbox_scan_complete), (("msg-1",), True))
        self.assertTrue(result.runner_stopped)
        self.assertEqual(runner.stops, ["session-1"])
        self.assertEqual(inbox.get("msg-1").state, "leased")
        stored = store.get(Lease, "lease-1")
        self.assertEqual((stored.value, stored.revision), (lease, 1))
        self.assertFalse(self.endpoint().exists())
        self.assertEqual(self.broker.revoked, ["grant-1"])
        self.assertEqual(director.stop().status, "not_running")
        events = [event.event for event in director.events()]
        self.assertLess(events.index("stop_requested"), events.index("stopped"))

    # ---------------------------------------------------- wake and health
    def test_idle_director_is_never_woken_and_new_work_wakes_once(self) -> None:
        inbox = self.inbox()
        director, runner, _ = self.director(inbox=inbox)
        self.assertIsNone(director.poll())  # not running yet
        director.start()
        for _ in range(50):
            self.assertIsNone(director.poll())
        self.assertEqual(runner.wakes, [])
        inbox.receive(InboxMessage("msg-1", "goal-1", "gui", 1, "user_instruction", {"text": "synthetic"}))
        self.assertIsNotNone(director.poll())
        self.assertIsNone(director.poll())
        self.assertEqual(runner.wakes, [1])
        director.stop()
        inbox.receive(InboxMessage("msg-2", "goal-1", "gui", 2, "user_instruction", {"text": "synthetic"}))
        self.assertIsNone(director.poll())
        self.assertEqual(runner.wakes, [1])

    def test_health_payload_is_bounded_and_secret_free(self) -> None:
        director, _, _ = self.director()
        stopped = director.health()
        self.assertEqual((stopped["state"], stopped["runner"], stopped["accepting_work"]),
                         ("STOPPED", "not_running", False))
        director.start()
        running = director.health()
        self.assertEqual((running["state"], running["runner"], running["model"], running["effort"]),
                         ("RUNNING", "alive", "synthetic-model", "high"))
        self.assertEqual(running["profile_revision"], 3)
        text = json.dumps(running)
        for word in ("secret", "password", "credential_ref"):
            self.assertNotIn(word, text.lower())

    # F7: lost audit records are visible to operators.
    def test_health_reports_event_sink_failures(self) -> None:
        def failing_sink(record):
            raise RuntimeError(SECRET_MARKER)

        director = Director(self.config(), FakeRunner(), FakeProfiles(), clock=fixed_clock,
                            event_sink=failing_sink)
        self.addCleanup(director.stop)
        self.assertEqual(director.health()["event_sink_failures"], 0)
        director.check()
        health = director.health()
        self.assertGreaterEqual(health["event_sink_failures"], 1)
        self.assertEqual(health["event_sink_failures"], director.sink_failures)
        self.assertNotIn(SECRET_MARKER, json.dumps(health))

    def test_invalid_configuration_and_runner_are_rejected(self) -> None:
        with self.assertRaises(InvalidDirectorConfig):
            DirectorConfig("bad/workspace")
        with self.assertRaises(InvalidDirectorConfig):
            DirectorConfig("ws-1", profile_id="bad id")
        with self.assertRaises(InvalidDirectorConfig):
            CredentialScope(object(), "adapter-vendor", "lease-1", 1)
        with self.assertRaises(InvalidDirectorConfig):
            Director(self.config(), object(), FakeProfiles())
        with self.assertRaises(InvalidDirectorConfig):
            Director(self.config(), FakeRunner(), object())


if __name__ == "__main__":
    unittest.main()
