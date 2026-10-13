"""AIK-024 director bootstrap tests with a fake runner, fake profile gate and fake broker.

Supervisor ownership and the inbox are the real packages over OS temporary
directories. No model, provider, credential or network is used. Linux passing
is development verification, not Windows support.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


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


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"
STAMP = "2030-01-01T00:00:00Z"


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
        if self.mode == "worker":
            profile = RoleProfile("director-1", "ws-1", "worker", selected, 10)
            return ExecutionDecision("bound", profile, 1, selected, "primary", None, (), (), STAMP)
        profile = DirectorProfile("director-1", "ws-1", selected, 40)
        if self.mode == "rejected":
            attempt = CapabilityAttempt("primary", selected, False, ("EFFORT_NOT_SUPPORTED",), None)
            return ExecutionDecision("rejected", profile, 3, None, None, None, (attempt,),
                                     ("PRIMARY_REJECTED", "FALLBACK_NOT_APPROVED"), STAMP)
        return ExecutionDecision("bound", profile, 3, selected, "primary", None, (), (), STAMP)


class FakeRunner(DirectorRunner):
    def __init__(self, probe=None, observed=("synthetic-model", "high"), start_error=None):
        self.probe_result = ProbeResult("ok") if probe is None else probe
        self.observed = observed
        self.start_error = start_error
        self.probes = 0
        self.starts = []
        self.stops = []
        self.wakes = []

    def probe(self, binding):
        self.probes += 1
        return self.probe_result

    def start(self, request):
        if self.start_error is not None:
            raise self.start_error
        self.starts.append(request)
        return RunnerSession(f"session-{len(self.starts)}", *self.observed)

    def stop(self, session):
        self.stops.append(session.session_id)

    def health(self, session):
        return "alive"

    def wake(self, session, signal):
        # The only path where a real runner would run a model turn.
        self.wakes.append(signal.available)


class FakeBroker:
    def __init__(self, deny=None):
        self.deny = deny
        self.issued = []
        self.revoked = []

    def issue_grant(self, **request):
        if self.deny is not None:
            raise BrokerDenied(self.deny)
        self.issued.append(request)
        return SimpleNamespace(grant_id=f"grant-{len(self.issued)}")

    def revoke(self, grant_id):
        self.revoked.append(grant_id)
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
        health = director.health()
        self.assertEqual((health["observed_model"], health["observation_match"]), ("synthetic-model", True))

    def test_unobserved_model_is_unknown_not_matched(self) -> None:
        director, _, _ = self.director(runner=FakeRunner(observed=(None, None)))
        result = director.start()
        self.assertEqual(result.status, "started")
        self.assertIsNone(result.observation.matches)

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
