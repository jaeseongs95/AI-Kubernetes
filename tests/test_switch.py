"""Director switch state machine against fake inventory, fake runner and fake profiles.

Only synthetic IDs and model names are used. No provider is contacted and no
credential is read. The continuity store and switch journal are real SQLite
files in an OS temporary directory. Linux success is not Windows evidence.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_capability import CapabilityError, Provenance, ValidationResult  # noqa: E402
from aik_config.models import Revisioned  # noqa: E402
from aik_continuity import Checkpoint, ContinuityStore, Directive  # noqa: E402
from aik_director.models import RunnerSession  # noqa: E402
from aik_profiles import DirectorProfile, ExecutionChoice, RoleProfile  # noqa: E402
from aik_runtime import UnsafeStoragePath  # noqa: E402
from aik_switch import (  # noqa: E402
    CallEvent, DirectorSwitch, FallbackRule, InvalidSwitch, InvalidSwitchJournal, SwitchBlocked,
    SwitchJournal, SwitchPending, SwitchPolicy, SwitchRequest,
)


DIGEST = "a" * 64
DIRECTIVE_TEXT = "합성 보고서를 작성하라 SYNTHETIC-DIRECTIVE-MARKER"


def director_profile() -> DirectorProfile:
    return DirectorProfile(
        "director-1", "ws-1",
        ExecutionChoice("conn-a", "synthetic-model-alpha", "high", ("shell", "files")),
        40,
        fallbacks=(
            ExecutionChoice("conn-b", "synthetic-model-beta", "medium", ("shell", "files")),
            ExecutionChoice("conn-c", "synthetic-model-gamma", "high", ("shell",)),
            ExecutionChoice("conn-a", "synthetic-model-delta", "low", ("shell", "files")),
        ),
    )


def worker_profile() -> RoleProfile:
    return RoleProfile("worker-1", "ws-1", "worker", ExecutionChoice("conn-w", "synthetic-worker-model"), 10)


def make_policy(**overrides) -> SwitchPolicy:
    values = dict(
        profile_id="director-1", profile_revision=3, approval_ref="approval-1", data_scope="ws-1",
        rules=(FallbackRule("primary", 90, 10, ("ws-1",)), FallbackRule("fallback:0", 80, 20, ("ws-1",)),
               FallbackRule("fallback:1", 70, 5, ("ws-1",)), FallbackRule("fallback:2", 85, 5, ("ws-1",))),
        min_quality=75, required_tools=("shell", "files"),
    )
    values.update(overrides)
    return SwitchPolicy(**values)


class FakeProfiles:
    """Read-only: deliberately has no save/delete, so no profile can be written."""

    def __init__(self, entries):
        self.entries = dict(entries)

    def get(self, profile_id):
        return self.entries.get(profile_id)


class FakeInventory:
    def __init__(self):
        self.supported = {}
        self.calls = []
        self.fail = False
        self.error = None

    def allow(self, connection_id, model, efforts, tools):
        self.supported[(connection_id, model)] = (efforts, tools)

    def validate(self, connection_id, model, effort=None, tools=(), *, min_context=None):
        self.calls.append((connection_id, model))
        if self.error is not None:
            raise self.error
        if self.fail:
            raise RuntimeError("synthetic inventory outage")
        entry = self.supported.get((connection_id, model))
        reasons = []
        if entry is None:
            reasons.append("provider_observed:MODEL_NOT_LISTED")
        else:
            efforts, supported_tools = entry
            if effort is not None and effort not in efforts:
                reasons.append("provider_observed:EFFORT_NOT_SUPPORTED")
            for tool in tools:
                if tool not in supported_tools:
                    reasons.append("provider_observed:TOOL_NOT_SUPPORTED:" + tool)
        accepted = not reasons
        return ValidationResult(
            connection_id, 1, 1, model, effort, tuple(tools), Provenance.REQUESTED, accepted,
            Provenance.PROVIDER_OBSERVED if accepted else None, DIGEST if accepted else None,
            tuple(reasons), "2030-01-01T00:00:00Z", min_context,
        )


class FakeRunner:
    def __init__(self):
        self.activations = []
        self.observe_effort = True
        self.model_override = None
        self.fail = False

    def activate(self, binding, handoff):
        self.activations.append((binding, handoff))
        if self.fail:
            raise RuntimeError("synthetic runner failure")
        return RunnerSession(
            f"session-{len(self.activations)}",
            observed_model=self.model_override or binding.model,
            observed_effort=binding.effort if self.observe_effort else None,
        )


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-switch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        state = self.root / "state"
        self.continuity = ContinuityStore(state / "continuity.sqlite3", clock=lambda: 1_900_000_000.0)
        directive = self.continuity.append_directive(
            Directive("dir-1", "project-1", "instruction", DIRECTIVE_TEXT)).record
        self.continuity.save_checkpoint(
            Checkpoint("project-1", "goal-1", "goal-rev-1", "plan-rev-1", directive_seq=directive.seq), 0)
        self.journal = SwitchJournal(state / "switch.sqlite3")
        self.worker = Revisioned(worker_profile(), 5)
        self.profiles = FakeProfiles({"director-1": Revisioned(director_profile(), 3), "worker-1": self.worker})
        self.inventory = FakeInventory()
        for choice in (director_profile().primary, *director_profile().fallbacks):
            self.inventory.allow(choice.connection_id, choice.model, ("low", "medium", "high"), ("shell", "files"))
        self.runner = FakeRunner()

    def make(self, *, policy="default", journal=None, **overrides) -> DirectorSwitch:
        values = dict(
            profiles=self.profiles, profile_id="director-1", inventory=self.inventory, runner=self.runner,
            continuity=self.continuity, project_id="project-1", journal=journal or self.journal,
            policy=make_policy() if policy == "default" else policy,
            clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        values.update(overrides)
        return DirectorSwitch(**values)

    # -------------------------------------------------- application point
    def test_switch_during_call_waits_for_terminal_event_and_never_double_applies(self) -> None:
        switch = self.make()
        started = switch.begin_call("call-1")
        self.assertEqual(started.position, "primary")
        outcome = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(outcome.status, "pending")
        self.assertEqual(switch.state, "switch_pending")
        self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(outcome.record["waiting_call_ids"], ["call-1"])
        self.assertEqual(outcome.record["applies"], "after_calls")
        self.assertEqual(self.runner.activations, [])
        with self.assertRaises(SwitchPending):
            switch.begin_call("call-2")

        done = switch.observe(CallEvent("evt-call-1-done", "call-1", "completed"))
        self.assertEqual(done.status, "recorded")
        self.assertEqual(done.switch.status, "applied")
        self.assertEqual(switch.binding.position, "fallback:0")
        self.assertEqual(switch.state, "active")
        self.assertEqual(len(self.runner.activations), 1)

        self.assertEqual(switch.observe(CallEvent("evt-call-1-done", "call-1", "completed")).status, "duplicate")
        self.assertEqual(switch.observe(CallEvent("evt-call-1-late", "call-1", "failed")).status,
                         "already_terminal")
        again = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.apply_record["status"], "applied")
        self.assertEqual(len(self.runner.activations), 1)
        self.assertEqual([(doc["event_id"], doc["phase"]) for _, doc in switch.history()],
                         [("evt-1", "request"), ("evt-1", "apply")])
        self.assertEqual(switch.begin_call("call-2").position, "fallback:0")

    def test_cancelled_calls_are_application_points_and_second_request_is_rejected(self) -> None:
        switch = self.make()
        switch.begin_call("call-1")
        switch.begin_call("call-2")
        self.assertEqual(switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100)).status,
                         "pending")
        rejected = switch.request_switch(SwitchRequest("evt-2", "user_request", "fallback:2", 100))
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.record["reasons"], ["SWITCH_ALREADY_PENDING"])
        first = switch.observe(CallEvent("evt-c1", "call-1", "cancelled"))
        self.assertIsNone(first.switch)
        self.assertEqual(switch.state, "switch_pending")
        second = switch.observe(CallEvent("evt-c2", "call-2", "cancelled"))
        self.assertEqual(second.switch.status, "applied")
        self.assertEqual(switch.binding.position, "fallback:0")
        self.assertEqual(switch.observe(CallEvent("evt-x", "call-unknown", "completed")).status, "unknown_call")
        with self.assertRaises(InvalidSwitch):
            switch.begin_call("call-1")

    # -------------------------------------------------- records and handoff
    def test_idle_switch_applies_with_continuity_handoff_and_checkpoint_record(self) -> None:
        switch = self.make()
        outcome = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(outcome.status, "applied")
        self.assertEqual(outcome.record["status"], "accepted")
        self.assertEqual(outcome.record["applies"], "immediately")
        record = outcome.apply_record
        self.assertEqual(record["reason"], "user_request")
        self.assertEqual(record["previous"]["model"], "synthetic-model-alpha")
        self.assertEqual(record["new"]["model"], "synthetic-model-beta")
        self.assertEqual(record["requested_effort"], "medium")
        self.assertEqual(record["observed_effort"], "medium")
        self.assertIsNone(record["observed_effort_reason"])
        self.assertIs(record["observation_matches"], True)
        self.assertEqual(record["checkpoint"]["project_id"], "project-1")
        self.assertEqual(record["checkpoint"]["revision"], 1)
        self.assertEqual(record["approval"], {"profile_id": "director-1", "profile_revision": 3,
                                              "fallback_index": 0, "approval_ref": "approval-1"})
        self.assertEqual(record["handoff"]["active_directive_ids"], ["dir-1"])
        binding, packet = self.runner.activations[0]
        self.assertEqual(binding.model, "synthetic-model-beta")
        self.assertEqual(packet.checkpoint.revision, record["checkpoint"]["revision"])
        self.assertEqual(packet.checkpoint.checkpoint.goal_id, "goal-1")
        self.assertEqual(packet.directives[0].text, DIRECTIVE_TEXT)
        self.assertNotIn("SYNTHETIC-DIRECTIVE-MARKER".encode(), self.journal.path.read_bytes())

    def test_unobserved_effort_is_null_with_reason_and_never_promoted(self) -> None:
        self.runner.observe_effort = False
        record = self.make().request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100)).apply_record
        self.assertEqual(record["requested_effort"], "medium")
        self.assertIsNone(record["observed_effort"])
        self.assertEqual(record["observed_effort_reason"], "NOT_OBSERVED")
        self.assertIsNone(record["observation_matches"])

    def test_observed_model_mismatch_blocks_new_calls(self) -> None:
        self.runner.model_override = "synthetic-model-other"
        switch = self.make()
        outcome = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(outcome.status, "applied")
        self.assertEqual(switch.state, "decision_required")
        self.assertEqual(switch.block_codes, ("OBSERVED_BINDING_MISMATCH",))
        self.assertEqual(outcome.apply_record["observed_model"], "synthetic-model-other")
        with self.assertRaises(SwitchBlocked):
            switch.begin_call("call-1")

    # -------------------------------------------------- tools and candidates
    def test_missing_tools_reject_candidates_and_auto_fallback_takes_first_qualifying(self) -> None:
        switch = self.make()
        configured = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:1", 100))
        self.assertEqual(configured.status, "decision_required")
        self.assertIn("TOOL_NOT_CONFIGURED:files", configured.record["candidates"][0]["reasons"])
        self.assertEqual(switch.state, "active")
        self.assertNotIn(("conn-c", "synthetic-model-gamma"), self.inventory.calls)

        self.inventory.allow("conn-b", "synthetic-model-beta", ("medium",), ("shell",))
        unsupported = switch.request_switch(SwitchRequest("evt-2", "user_request", "fallback:0", 100))
        reasons = unsupported.record["candidates"][0]["reasons"]
        self.assertIn("CAPABILITY_REJECTED", reasons)
        self.assertIn("CAPABILITY:provider_observed:TOOL_NOT_SUPPORTED:files", reasons)

        automatic = switch.request_switch(SwitchRequest("evt-3", "provider_unavailable", budget_remaining=100))
        self.assertEqual(automatic.status, "applied")
        self.assertEqual([item["position"] for item in automatic.record["candidates"]],
                         ["fallback:0", "fallback:1", "fallback:2"])
        self.assertEqual(switch.binding.position, "fallback:2")
        self.assertEqual(self.profiles.entries["worker-1"], self.worker)

    def test_no_qualifying_candidate_stops_for_a_user_decision(self) -> None:
        switch = self.make()
        self.inventory.fail = True
        outcome = switch.request_switch(SwitchRequest("evt-1", "provider_unavailable", budget_remaining=100))
        self.assertEqual(outcome.status, "decision_required")
        self.assertEqual(switch.state, "decision_required")
        self.assertEqual(outcome.record["candidates"][0]["reasons"], ["INVENTORY_UNAVAILABLE"])
        self.assertEqual(self.runner.activations, [])
        with self.assertRaises(SwitchBlocked):
            switch.begin_call("call-1")
        self.inventory.fail = False
        self.inventory.error = CapabilityError("UNKNOWN_RESOURCE")
        self.assertEqual(switch.resume_current("evt-resume-1").status, "decision_required")
        self.inventory.error = None
        resumed = switch.resume_current("evt-resume-2")
        self.assertEqual(resumed.status, "resumed")
        self.assertEqual(switch.state, "active")
        self.assertEqual(switch.binding.position, "primary")

    # -------------------------------------------------- authentication
    def test_auth_failure_without_permission_requires_reauthentication(self) -> None:
        switch = self.make()
        outcome = switch.request_switch(SwitchRequest("evt-1", "auth_expired"))
        self.assertEqual(outcome.status, "reauth_required")
        self.assertEqual(outcome.record["reasons"], ["REAUTH_REQUIRED"])
        self.assertEqual(switch.state, "reauth_required")
        self.assertEqual(self.runner.activations, [])
        with self.assertRaises(SwitchBlocked):
            switch.begin_call("call-1")
        self.assertEqual(switch.resume_current("evt-relogin").status, "resumed")
        self.assertEqual(switch.begin_call("call-1").position, "primary")

    def test_auth_fallback_excludes_the_failed_connection(self) -> None:
        switch = self.make(policy=make_policy(fallback_on_auth=True))
        self.inventory.supported.pop(("conn-b", "synthetic-model-beta"))
        blocked = switch.request_switch(SwitchRequest("evt-1", "auth_locked", budget_remaining=100))
        self.assertEqual(blocked.status, "reauth_required")
        reasons = {item["position"]: item["reasons"] for item in blocked.record["candidates"]}
        self.assertIn("CAPABILITY_REJECTED", reasons["fallback:0"])
        self.assertIn("SAME_CONNECTION_AUTH_FAILED", reasons["fallback:2"])
        self.inventory.allow("conn-b", "synthetic-model-beta", ("medium",), ("shell", "files"))
        applied = switch.request_switch(SwitchRequest("evt-2", "auth_locked", budget_remaining=100))
        self.assertEqual(applied.status, "applied")
        self.assertEqual(switch.binding.connection_id, "conn-b")
        self.assertEqual(switch.state, "active")

    # -------------------------------------------------- policy limits
    def test_budget_quality_scope_and_policy_limits_prevent_switching(self) -> None:
        cases = (
            ("budget-exceeded", make_policy(), 15, "BUDGET_EXCEEDED"),
            ("budget-unknown", make_policy(), None, "BUDGET_UNKNOWN"),
            ("cost-limit", make_policy(max_cost_per_call=10), 100, "OVER_COST_LIMIT"),
            ("quality", make_policy(min_quality=85), 100, "QUALITY_BELOW_FLOOR"),
            ("scope", make_policy(rules=(FallbackRule("fallback:0", 80, 20, ("ws-other",)),)), 100,
             "DATA_SCOPE_NOT_ALLOWED"),
            ("not-allowed", make_policy(rules=(FallbackRule("fallback:2", 85, 5, ("ws-1",)),)), 100,
             "NOT_ALLOWED_BY_POLICY"),
            ("stale", make_policy(profile_revision=2), 100, "POLICY_STALE"),
            ("no-policy", None, 100, "NO_FALLBACK_POLICY"),
        )
        for name, policy, budget, code in cases:
            with self.subTest(case=name):
                switch = self.make(policy=policy)
                outcome = switch.request_switch(SwitchRequest(f"evt-{name}", "user_request", "fallback:0", budget))
                self.assertEqual(outcome.status, "decision_required")
                self.assertIn(code, outcome.record["candidates"][0]["reasons"])
                self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(self.runner.activations, [])

    def test_profile_change_while_pending_fails_the_apply_time_recheck(self) -> None:
        switch = self.make()
        switch.begin_call("call-1")
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.profiles.entries["director-1"] = Revisioned(director_profile(), 4)
        outcome = switch.observe(CallEvent("evt-done", "call-1", "completed")).switch
        self.assertEqual(outcome.status, "recheck_failed")
        self.assertEqual(outcome.apply_record["reasons"], ["PROFILE_CHANGED"])
        self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(switch.state, "decision_required")
        self.assertEqual(self.runner.activations, [])

    def test_missing_checkpoint_blocks_application_unless_policy_permits(self) -> None:
        switch = self.make(project_id="project-empty")
        outcome = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(outcome.status, "activation_failed")
        self.assertEqual(outcome.apply_record["reasons"], ["CHECKPOINT_REQUIRED"])
        self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(self.runner.activations, [])
        permissive = self.make(project_id="project-empty", policy=make_policy(require_checkpoint=False))
        applied = permissive.request_switch(SwitchRequest("evt-2", "user_request", "fallback:0", 100))
        self.assertEqual(applied.status, "applied")
        self.assertIsNone(applied.apply_record["checkpoint"])
        self.assertEqual(applied.apply_record["checkpoint_reason"], "NO_CHECKPOINT")

    def test_runner_failure_keeps_previous_binding_and_requires_decision(self) -> None:
        self.runner.fail = True
        switch = self.make()
        outcome = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(outcome.status, "activation_failed")
        self.assertEqual(outcome.apply_record["reasons"], ["RUNNER_UNAVAILABLE"])
        self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(switch.state, "decision_required")

    # -------------------------------------------------- worker profiles
    def test_worker_profiles_are_never_switched_or_written(self) -> None:
        with self.assertRaises(InvalidSwitch):
            self.make(profile_id="worker-1", policy=None)
        with self.assertRaises(InvalidSwitch):
            self.make(policy=make_policy(profile_id="worker-1"))
        switch = self.make()
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertIs(self.profiles.entries["worker-1"], self.worker)
        self.assertEqual({doc["director_id"] for _, doc in switch.history()}, {"director-1"})

    # -------------------------------------------------- persistence
    def test_restart_restores_applied_binding_and_keeps_event_idempotency(self) -> None:
        self.make().request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        restarted = self.make(journal=SwitchJournal(self.journal.path))
        self.assertEqual(restarted.binding.position, "fallback:0")
        self.assertEqual(restarted.state, "active")
        self.assertEqual(restarted.request_switch(
            SwitchRequest("evt-1", "user_request", "fallback:0", 100)).status, "duplicate")
        self.assertEqual(len(self.runner.activations), 1)

    def test_pending_switch_lost_on_restart_fails_closed(self) -> None:
        switch = self.make()
        switch.begin_call("call-1")
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        restarted = self.make(journal=SwitchJournal(self.journal.path))
        self.assertEqual(restarted.state, "decision_required")
        self.assertEqual(restarted.block_codes, ("PENDING_SWITCH_LOST_ON_RESTART",))
        self.assertEqual(restarted.binding.position, "primary")

    def test_journal_is_append_only_and_rejects_foreign_or_repository_paths(self) -> None:
        self.make().request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        with closing(sqlite3.connect(self.journal.path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE switch_records SET status = 'applied'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM switch_records")
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InvalidSwitchJournal):
            SwitchJournal(foreign)
        self.assertEqual(foreign.read_bytes(), before)
        (self.root / "repo" / ".git").mkdir(parents=True)
        with self.assertRaises(UnsafeStoragePath):
            SwitchJournal(self.root / "repo" / "switch.sqlite3")
        with self.assertRaises(UnsafeStoragePath):
            SwitchJournal("relative-switch.sqlite3")

    def test_invalid_requests_and_events_are_rejected(self) -> None:
        with self.assertRaises(InvalidSwitch):
            SwitchRequest("evt-1", "user_request")
        with self.assertRaises(InvalidSwitch):
            SwitchRequest("evt-1", "auth_expired", target="fallback:0")
        with self.assertRaises(InvalidSwitch):
            SwitchRequest("evt-1", "user_request", "fallback:9")
        with self.assertRaises(InvalidSwitch):
            SwitchRequest("evt-1", "unknown_reason")
        with self.assertRaises(InvalidSwitch):
            CallEvent("evt-1", "call-1", "interrupted")
        with self.assertRaises(InvalidSwitch):
            FallbackRule("primary", 0)
        with self.assertRaises(InvalidSwitch):
            make_policy(rules=(FallbackRule("primary", 50), FallbackRule("primary", 60)))
        switch = self.make()
        switch.begin_call("call-1")
        with self.assertRaises(InvalidSwitch):
            switch.begin_call("call-1")
        same_position = switch.request_switch(SwitchRequest("evt-same", "user_request", "primary", 100))
        self.assertIn("SAME_AS_CURRENT", same_position.record["candidates"][0]["reasons"])
        self.assertEqual(replace(make_policy(), min_quality=80).min_quality, 80)


if __name__ == "__main__":
    unittest.main()
