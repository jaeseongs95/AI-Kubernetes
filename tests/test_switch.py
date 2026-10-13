"""Director switch state machine against fake inventory, fake runner and fake profiles.

Only synthetic IDs and model names are used. No provider is contacted and no
credential is read. The continuity store and switch journal are real SQLite
files in an OS temporary directory. Linux success is not Windows evidence.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


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
    SwitchConflict, SwitchError, SwitchJournal, SwitchPending, SwitchPolicy, SwitchRequest,
    binding_to_dict, make_binding,
)
from aik_switch import journal as journal_module  # noqa: E402


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


def reviewer_profile() -> RoleProfile:
    return RoleProfile("reviewer-1", "ws-1", "reviewer", ExecutionChoice("conn-r", "synthetic-reviewer-model"), 10)


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
        self.invalid_result = False

    def activate(self, binding, handoff):
        self.activations.append((binding, handoff))
        if self.fail:
            raise RuntimeError("synthetic runner failure")
        if self.invalid_result:
            return "synthetic-not-a-session"
        return RunnerSession(
            f"session-{len(self.activations)}",
            observed_model=self.model_override or binding.model,
            observed_effort=binding.effort if self.observe_effort else None,
        )


class FakeBudget:
    """Synthetic remaining-budget source read at deferred application points."""

    def __init__(self, value=100):
        self.value = value
        self.calls = []

    def remaining(self, project_id):
        self.calls.append(project_id)
        return self.value


class StubContinuity:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error

    def handoff(self, project_id):
        if self.error is not None:
            raise self.error
        return self.result


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
        self.budget = FakeBudget(100)

    def make(self, *, policy="default", journal=None, **overrides) -> DirectorSwitch:
        values = dict(
            profiles=self.profiles, profile_id="director-1", inventory=self.inventory, runner=self.runner,
            continuity=self.continuity, project_id="project-1", journal=journal or self.journal,
            policy=make_policy() if policy == "default" else policy,
            clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc), budget=self.budget,
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

    # -------------------------------------------------- review regressions (F1)
    def test_lost_apply_record_is_reconciled_before_resume_and_survives_restart(self) -> None:
        real_append = self.journal.append
        faults = {"apply": True}

        def failing_append(document):
            if document.get("phase") == "apply" and faults["apply"]:
                raise SwitchError("synthetic journal write failure")
            return real_append(document)

        switch = self.make()
        with mock.patch.object(self.journal, "append", side_effect=failing_append):
            with self.assertRaises(SwitchError):
                switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
            self.assertEqual(switch.state, "decision_required")
            self.assertEqual(switch.block_codes, ("JOURNAL_WRITE_FAILED",))
            self.assertEqual(switch.binding.position, "fallback:0")
            self.assertIsNone(self.journal.get("evt-1", "apply"))
            # While the record still cannot be written, the block is not released.
            with self.assertRaises(SwitchError):
                switch.resume_current("evt-resume")
            self.assertEqual(switch.block_codes, ("JOURNAL_WRITE_FAILED",))
            self.assertIsNone(self.journal.get("evt-resume", "request"))
            faults["apply"] = False
            resumed = switch.resume_current("evt-resume")
        self.assertEqual(resumed.status, "resumed")
        self.assertEqual(resumed.record["reconciled_apply_event_id"], "evt-1")
        apply = self.journal.get("evt-1", "apply")
        self.assertIsNotNone(apply)
        self.assertEqual(apply["status"], "applied")
        self.assertEqual(apply["new"]["position"], "fallback:0")
        self.assertEqual(apply["observed_effort"], "medium")
        self.assertEqual(apply["checkpoint"]["revision"], 1)
        restarted = self.make(journal=SwitchJournal(self.journal.path))
        self.assertEqual(restarted.binding.position, "fallback:0")
        self.assertEqual(restarted.state, "active")

    def test_resume_record_with_unrecorded_binding_change_fails_closed_on_restart(self) -> None:
        self.make()
        # Simulates a journal written by the earlier code: the apply record was
        # lost, but a later resume released the changed binding.
        fallback = binding_to_dict(make_binding(director_profile(), 3, "fallback:0"))
        self.journal.append({
            "event_id": "evt-old-resume", "phase": "request", "kind": "resume", "status": "resumed",
            "director_id": "director-1", "project_id": "project-1", "recorded_at": "2030-01-01T00:00:00Z",
            "reason": "resume_current", "previous": fallback, "recheck": None, "reasons": [],
            "state_after": "active", "block_codes": [],
        })
        restarted = self.make(journal=SwitchJournal(self.journal.path))
        self.assertEqual(restarted.state, "decision_required")
        self.assertEqual(restarted.block_codes, ("RESUMED_BINDING_MISMATCH",))
        self.assertEqual(restarted.binding.position, "primary")
        with self.assertRaises(SwitchBlocked):
            restarted.begin_call("call-1")

    # -------------------------------------------------- review regressions (F2)
    def test_deferred_apply_rechecks_budget_from_budget_port(self) -> None:
        switch = self.make()
        switch.begin_call("call-1")
        pending = switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 25))
        self.assertEqual(pending.status, "pending")
        self.budget.value = 15  # the in-flight call consumed budget
        outcome = switch.observe(CallEvent("evt-done", "call-1", "completed")).switch
        self.assertEqual(outcome.status, "recheck_failed")
        self.assertEqual(outcome.apply_record["reasons"], ["RECHECK_REJECTED", "BUDGET_EXCEEDED"])
        self.assertEqual(outcome.apply_record["budget_source"], "budget_port")
        self.assertEqual(outcome.apply_record["budget_remaining_at_apply"], 15)
        self.assertEqual(self.budget.calls, ["project-1"])
        self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(self.runner.activations, [])

    def test_deferred_apply_without_budget_source_fails_closed(self) -> None:
        switch = self.make(budget=None)
        switch.begin_call("call-1")
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        outcome = switch.observe(CallEvent("evt-done", "call-1", "completed")).switch
        self.assertEqual(outcome.status, "recheck_failed")
        self.assertEqual(outcome.apply_record["reasons"], ["RECHECK_REJECTED", "BUDGET_UNKNOWN"])
        self.assertEqual(outcome.apply_record["budget_source"], "NO_BUDGET_SOURCE")
        self.assertEqual(self.runner.activations, [])
        # Immediate application happens at the request instant and uses its value.
        immediate = switch.request_switch(SwitchRequest("evt-2", "user_request", "fallback:0", 100))
        self.assertEqual(immediate.status, "applied")
        self.assertEqual(immediate.apply_record["budget_source"], "request")

    # -------------------------------------------------- review regressions (F3)
    def test_event_id_reuse_with_different_content_conflicts(self) -> None:
        switch = self.make()
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.assertEqual(switch.request_switch(
            SwitchRequest("evt-1", "user_request", "fallback:0", 100)).status, "duplicate")
        with self.assertRaises(SwitchConflict):
            switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:2", 100))
        with self.assertRaises(SwitchConflict):
            switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 50))
        with self.assertRaises(SwitchConflict):
            switch.resume_current("evt-1")
        self.assertEqual(switch.resume_current("evt-r").status, "rejected")
        with self.assertRaises(SwitchConflict):
            switch.request_switch(SwitchRequest("evt-r", "user_request", "fallback:2", 100))
        switch.begin_call("call-1")
        switch.observe(CallEvent("evt-c", "call-1", "completed"))
        switch.begin_call("call-2")
        with self.assertRaises(SwitchConflict):
            switch.observe(CallEvent("evt-c", "call-2", "completed"))
        self.assertEqual(switch.binding.position, "fallback:0")
        self.assertEqual(len(self.runner.activations), 1)

    # -------------------------------------------------- review regressions (F4)
    def test_terminal_event_before_registration_is_not_consumed(self) -> None:
        switch = self.make()
        early = switch.observe(CallEvent("evt-early", "call-1", "completed"))
        self.assertEqual(early.status, "unknown_call")
        switch.begin_call("call-1")
        again = switch.observe(CallEvent("evt-early", "call-1", "completed"))
        self.assertEqual(again.status, "recorded")
        self.assertEqual(switch.in_flight_call_ids, ())

    # -------------------------------------------------- review regressions (F6)
    def test_reauth_ref_is_recorded_and_conflicts_on_reuse(self) -> None:
        switch = self.make()
        switch.request_switch(SwitchRequest("evt-1", "auth_expired"))
        with self.assertRaises(InvalidSwitch):
            switch.resume_current("evt-relogin", reauth_ref="not a valid ref")
        resumed = switch.resume_current("evt-relogin", reauth_ref="relogin-ref-1")
        self.assertEqual(resumed.status, "resumed")
        self.assertEqual(resumed.record["reauth_ref"], "relogin-ref-1")
        self.assertEqual(switch.resume_current("evt-relogin", reauth_ref="relogin-ref-1").status, "duplicate")
        with self.assertRaises(SwitchConflict):
            switch.resume_current("evt-relogin", reauth_ref="relogin-ref-2")

    # -------------------------------------------------- review regressions (F7)
    def test_fingerprint_detects_accidental_mismatch_but_is_not_tamper_proof(self) -> None:
        self.assertNotIn("tampered", InvalidSwitchJournal.__doc__)
        self.make().request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        path = self.journal.path
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            seq, text = connection.execute(
                "SELECT seq, document FROM switch_records WHERE event_id = 'evt-1' AND phase = 'request'").fetchone()
        document = json.loads(text)
        document["budget_remaining"] = 999
        forged = journal_module._canonical(document)

        def rewrite(fingerprint=None):
            with closing(sqlite3.connect(path, isolation_level=None)) as connection:
                connection.execute("DROP TRIGGER switch_records_no_update")
                if fingerprint is None:
                    connection.execute("UPDATE switch_records SET document = ? WHERE seq = ?", (forged, seq))
                else:
                    connection.execute("UPDATE switch_records SET document = ?, fingerprint = ? WHERE seq = ?",
                                       (forged, fingerprint, seq))
                connection.execute(journal_module._STATEMENTS[2])

        rewrite()
        with self.assertRaises(InvalidSwitchJournal):
            SwitchJournal(path)
        rewrite(journal_module._fingerprint("evt-1", "request", "director-1", "accepted", forged))
        # A recomputed unkeyed fingerprint is accepted: documented limitation.
        self.assertEqual(SwitchJournal(path).get("evt-1")["budget_remaining"], 999)

    # -------------------------------------------------- review regressions (F9)
    def test_clock_failure_at_application_point_changes_nothing_and_can_retry(self) -> None:
        clock_state = {"fail": False}

        def clock():
            if clock_state["fail"]:
                raise RuntimeError("synthetic clock failure")
            return datetime(2030, 1, 1, tzinfo=timezone.utc)

        switch = self.make(clock=clock)
        switch.begin_call("call-1")
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        clock_state["fail"] = True
        with self.assertRaises(SwitchError):
            switch.observe(CallEvent("evt-done", "call-1", "completed"))
        self.assertEqual(switch.state, "switch_pending")
        self.assertEqual(switch.pending_event_id, "evt-1")
        self.assertEqual(switch.in_flight_call_ids, ("call-1",))
        self.assertEqual(self.runner.activations, [])
        clock_state["fail"] = False
        retried = switch.observe(CallEvent("evt-done", "call-1", "completed"))
        self.assertEqual(retried.status, "recorded")
        self.assertEqual(retried.switch.status, "applied")
        self.assertEqual(switch.binding.position, "fallback:0")

    # -------------------------------------------------- test-gap coverage (F8)
    def test_activation_failures_for_handoff_and_runner_results_keep_binding(self) -> None:
        cases = (
            ("continuity-error", StubContinuity(error=RuntimeError("synthetic outage")), False,
             "CONTINUITY_UNAVAILABLE"),
            ("invalid-handoff", StubContinuity(result=object()), False, "INVALID_HANDOFF"),
            ("invalid-runner", self.continuity, True, "INVALID_RUNNER_RESULT"),
        )
        for name, continuity, invalid_result, code in cases:
            with self.subTest(case=name):
                self.runner.invalid_result = invalid_result
                switch = self.make(continuity=continuity)
                outcome = switch.request_switch(SwitchRequest(f"evt-{name}", "user_request", "fallback:0", 100))
                self.assertEqual(outcome.status, "activation_failed")
                self.assertEqual(outcome.apply_record["reasons"], [code])
                self.assertEqual(switch.binding.position, "primary")
                self.assertEqual(switch.state, "decision_required")
        self.assertEqual(len(self.runner.activations), 1)

    def test_inventory_change_while_pending_fails_the_recheck(self) -> None:
        switch = self.make()
        switch.begin_call("call-1")
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        self.inventory.supported.pop(("conn-b", "synthetic-model-beta"))
        outcome = switch.observe(CallEvent("evt-done", "call-1", "completed")).switch
        self.assertEqual(outcome.status, "recheck_failed")
        self.assertEqual(outcome.apply_record["reasons"][:2], ["RECHECK_REJECTED", "CAPABILITY_REJECTED"])
        self.assertEqual(switch.binding.position, "primary")
        self.assertEqual(self.runner.activations, [])

    def test_failed_call_is_an_application_point_without_rerun(self) -> None:
        switch = self.make()
        switch.begin_call("call-1")
        switch.request_switch(SwitchRequest("evt-1", "user_request", "fallback:0", 100))
        outcome = switch.observe(CallEvent("evt-failed", "call-1", "failed"))
        self.assertEqual(outcome.switch.status, "applied")
        self.assertEqual(switch.binding.position, "fallback:0")
        self.assertEqual(len(self.runner.activations), 1)

    def test_restart_restores_blocked_state_and_detects_changed_profile(self) -> None:
        self.make().request_switch(SwitchRequest("evt-1", "auth_expired"))
        restarted = self.make(journal=SwitchJournal(self.journal.path))
        self.assertEqual(restarted.state, "reauth_required")
        self.assertEqual(restarted.block_codes, ("REAUTH_REQUIRED",))
        self.assertEqual(restarted.resume_current("evt-r").status, "resumed")
        self.assertEqual(restarted.request_switch(
            SwitchRequest("evt-2", "user_request", "fallback:0", 100)).status, "applied")
        self.profiles.entries["director-1"] = Revisioned(director_profile(), 4)
        changed = self.make(journal=SwitchJournal(self.journal.path))
        self.assertEqual(changed.state, "decision_required")
        self.assertEqual(changed.block_codes, ("PROFILE_CHANGED_SINCE_LAST_SWITCH",))

    def test_reviewer_profile_is_rejected(self) -> None:
        self.profiles.entries["reviewer-1"] = Revisioned(reviewer_profile(), 2)
        with self.assertRaises(InvalidSwitch):
            self.make(profile_id="reviewer-1", policy=None)


if __name__ == "__main__":
    unittest.main()
