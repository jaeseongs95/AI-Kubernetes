"""AIK-023 profile schema, independent revisions and pre-execution capability checks.

All data is synthetic: no provider, credential or network is used. Clocks are
injected. The capability inventory is a real aik_capability.CapabilityInventory;
its resources are registered through a test-only harness (see make_inventory).
Some tests use a fake inventory that returns synthetic ValidationResults or
raises, to exercise fail-closed branches.
Linux runs are development verification, not Windows support evidence.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_capability import CapabilityError, CapabilityInventory, Provenance, ValidationResult  # noqa: E402
from aik_capability import inventory as capability_inventory  # noqa: E402
from aik_config import (  # noqa: E402
    AgentProfile, ConnectionProfile, CrossWorkspaceReference, InvalidDatabase, InvalidReference,
    RevisionConflict, Revisioned, SettingsStore, Workspace,
)
from aik_profiles import (  # noqa: E402
    CapabilityMismatch, DirectorProfile, ExecutionChoice, FallbackApproval, InvalidProfile,
    ProfileError, ProfileStore, RoleProfile, from_agent_profile, profile_document, profile_to_dict,
    to_agent_profile,
)
from aik_profiles import storage as profile_storage  # noqa: E402
from aik_profiles.cli import MAX_DOCUMENT_BYTES, main as cli_main  # noqa: E402


FIXED_NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
SECRET_MARKER = "SYNTHETIC SECRET MUST NOT BE STORED"
FULL_LISTS = {
    "conn-a": [{"model": "synthetic-model-alpha", "efforts": ["low", "high"], "tools": ["shell", "edit"]}],
    "conn-b": [{"model": "synthetic-model-beta", "efforts": ["medium"], "tools": ["shell"]}],
}
MISMATCH_LISTS = {
    "conn-a": [{"model": "synthetic-model-alpha", "efforts": ["low"], "tools": ["shell"]}],
    "conn-b": [{"model": "synthetic-model-beta", "efforts": ["medium"], "tools": ["shell"]}],
}


def fixed_clock() -> datetime:
    return FIXED_NOW


def failing_clock() -> datetime:
    raise RuntimeError("synthetic clock failure")


def make_inventory(lists: dict, resources=("conn-a", "conn-b")) -> CapabilityInventory:
    inventory = CapabilityInventory(clock=fixed_clock)
    for connection_id in resources:
        # Test harness only: register a synthetic resource without depending on
        # the provider package's ConnectionBinding constructor details.
        inventory._resources[connection_id] = capability_inventory._Resource(SimpleNamespace(
            connection_id=connection_id, provider="vendor-" + connection_id[-1], runtime="synthetic",
            auth_kind="none", revision=1,
        ))
    for connection_id, models in lists.items():
        inventory.load_list(connection_id, Provenance.PROVIDER_OBSERVED, json.dumps({"models": models}),
                            source_ref="synthetic-observation",
                            observed_at=FIXED_NOW)
    return inventory


def synthetic_result(connection_id, model, effort, tools, *, accepted=True,
                     supported_by=Provenance.PROVIDER_OBSERVED,
                     request_provenance=Provenance.REQUESTED) -> ValidationResult:
    return ValidationResult(
        connection_id=connection_id, connection_revision=1, resource_revision=1, model=model,
        effort=effort, tools=tools, request_provenance=request_provenance, accepted=accepted,
        supported_by=supported_by if accepted else None,
        evidence_digest=("0" * 64) if accepted else None, reasons=(),
        validated_at="2030-01-01T00:00:00Z",
    )


class FakeInventory:
    """Synthetic inventory returning a computed result or raising a given error."""

    def __init__(self, *, error=None, result=None):
        self.error = error
        self.result = result

    def validate(self, connection_id, model, effort=None, tools=()):
        if self.error is not None:
            raise self.error
        return self.result(connection_id, model, effort, tools)


def director_profile(**changes) -> DirectorProfile:
    values = dict(
        id="director-1", workspace_id="ws-1",
        primary=ExecutionChoice("conn-a", "synthetic-model-alpha", "high", ("shell", "edit")),
        max_calls=40,
        fallbacks=(ExecutionChoice("conn-b", "synthetic-model-beta", "medium", ("shell",)),),
        budget_limit=500,
    )
    values.update(changes)
    return DirectorProfile(**values)


def worker_profile() -> RoleProfile:
    return RoleProfile("worker-1", "ws-1", "worker",
                       ExecutionChoice("conn-b", "synthetic-model-beta", "medium", ("shell",)), max_calls=20)


def reviewer_profile() -> RoleProfile:
    return RoleProfile("reviewer-1", "ws-1", "reviewer",
                       ExecutionChoice("conn-a", "synthetic-model-alpha", "low"), max_calls=10)


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-profiles-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root / "settings.sqlite3")
        with self.settings.transaction() as tx:
            tx.save(Workspace("ws-1", "작업공간 1"))
            tx.save(Workspace("ws-2", "작업공간 2"))
            tx.save(ConnectionProfile("conn-a", "ws-1", "vendor-a"))
            tx.save(ConnectionProfile("conn-b", "ws-1", "vendor-b"))
            tx.save(ConnectionProfile("conn-x", "ws-2", "vendor-a"))
        self.path = self.root / "profiles.sqlite3"
        self.store = ProfileStore(self.path, settings=self.settings)

    # ----------------------------------------------------------- schema
    def test_multi_vendor_profiles_persist_with_ordered_fallbacks_across_reopen(self) -> None:
        director = director_profile(fallbacks=(
            ExecutionChoice("conn-b", "synthetic-model-beta", "medium", ("shell",)),
            ExecutionChoice("conn-a", "synthetic-model-alpha", "low"),
        ))
        for profile in (director, worker_profile(), reviewer_profile()):
            self.assertEqual(self.store.save(profile), Revisioned(profile, 1))
        reopened = ProfileStore(self.path, settings=self.settings)
        stored = reopened.get("director-1")
        self.assertEqual(stored, Revisioned(director, 1))
        self.assertEqual([choice.connection_id for choice in stored.value.fallbacks], ["conn-b", "conn-a"])
        self.assertEqual([entry.value.id for entry in reopened.list("ws-1")],
                         ["director-1", "reviewer-1", "worker-1"])
        self.assertEqual([entry.value.id for entry in reopened.list("ws-1", role="reviewer")], ["reviewer-1"])

    def test_invalid_profile_values_and_option_combinations_are_rejected(self) -> None:
        primary = ExecutionChoice("conn-a", "synthetic-model-alpha", "high")
        cases = {
            "duplicate tool": lambda: ExecutionChoice("conn-a", "m", tools=("shell", "SHELL")),
            "empty model": lambda: ExecutionChoice("conn-a", ""),
            "list tools": lambda: ExecutionChoice("conn-a", "m", tools=["shell"]),
            "zero calls": lambda: director_profile(max_calls=0),
            "bool calls": lambda: director_profile(max_calls=True),
            "director as role": lambda: RoleProfile("r", "ws-1", "director", primary, 1),
            "fallback repeats primary": lambda: director_profile(primary=primary, fallbacks=(primary,)),
            "fallback duplicate": lambda: director_profile(fallbacks=(
                ExecutionChoice("conn-b", "b", tools=("x", "y")), ExecutionChoice("conn-b", "b", tools=("y", "x")))),
            "too many fallbacks": lambda: director_profile(fallbacks=tuple(
                ExecutionChoice("conn-b", f"model-{n}") for n in range(9))),
            "unknown budget unit": lambda: director_profile(budget_unit="eur"),
            "negative budget": lambda: director_profile(budget_limit=-1),
            "fallbacks not tuple": lambda: director_profile(fallbacks=[]),
        }
        for name, build in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(InvalidProfile):
                    build()
        with self.assertRaises(TypeError):
            director_profile(api_key=SECRET_MARKER)

    def test_forged_frozen_profiles_are_rejected_without_storing_values(self) -> None:
        for field, value in (("max_calls", True), ("primary", "conn-a"), ("workspace_id", SECRET_MARKER)):
            with self.subTest(field=field):
                forged = director_profile()
                object.__setattr__(forged, field, value)
                with self.assertRaises(InvalidProfile) as rejected:
                    self.store.save(forged)
                self.assertNotIn(SECRET_MARKER, str(rejected.exception))
        self.assertEqual(self.store.list("ws-1"), ())
        self.assertNotIn(SECRET_MARKER.encode("ascii"), self.path.read_bytes())

    # --------------------------------------------- independent revisions
    def test_director_changes_never_overwrite_worker_or_reviewer_profiles(self) -> None:
        worker, reviewer = worker_profile(), reviewer_profile()
        for profile in (director_profile(), worker, reviewer):
            self.store.save(profile)
        first = director_profile(max_calls=50)
        second = director_profile(max_calls=50, fallbacks=())
        self.assertEqual(self.store.save(first, expected_revision=1).revision, 2)
        self.assertEqual(self.store.save(second, expected_revision=2).revision, 3)
        with self.assertRaises(RevisionConflict):
            self.store.save(director_profile(max_calls=7), expected_revision=1)
        # A director change cannot reuse a worker/reviewer row, and vice versa.
        with self.assertRaises(InvalidProfile):
            self.store.save(director_profile(id="worker-1"), expected_revision=1)
        with self.assertRaises(InvalidProfile):
            self.store.save(RoleProfile("director-1", "ws-1", "worker", worker.primary, 3), expected_revision=3)
        self.assertEqual(self.store.get("director-1"), Revisioned(second, 3))
        self.assertEqual(self.store.get("worker-1"), Revisioned(worker, 1))
        self.assertEqual(self.store.get("reviewer-1"), Revisioned(reviewer, 1))
        # Existing aik_config AgentProfile rows are neither written nor replaced.
        self.assertEqual(self.settings.list(AgentProfile), ())

    def test_worker_and_reviewer_changes_preserve_director_and_stale_second_store_conflicts(self) -> None:
        director, worker, reviewer = director_profile(), worker_profile(), reviewer_profile()
        for profile in (director, worker, reviewer):
            self.store.save(profile)
        second = ProfileStore(self.path, settings=self.settings)
        stale_worker = second.get("worker-1")
        updated_worker = replace(worker, max_calls=21)
        self.assertEqual(self.store.save(updated_worker, expected_revision=1).revision, 2)
        updated_reviewer = replace(reviewer, max_calls=11)
        self.assertEqual(second.save(updated_reviewer, expected_revision=1).revision, 2)
        with self.assertRaises(RevisionConflict):
            second.save(replace(worker, max_calls=99), expected_revision=stale_worker.revision)
        with self.assertRaises(RevisionConflict):
            second.delete("worker-1", expected_revision=stale_worker.revision)
        self.assertEqual(second.get("director-1"), Revisioned(director, 1))
        self.assertEqual(self.store.get("worker-1"), Revisioned(updated_worker, 2))
        self.assertEqual(self.store.get("reviewer-1"), Revisioned(updated_reviewer, 2))

    def test_delete_is_revision_checked_and_per_profile(self) -> None:
        self.store.save(director_profile())
        self.store.save(worker_profile())
        with self.assertRaises(RevisionConflict):
            self.store.delete("director-1", expected_revision=2)
        self.store.delete("director-1", expected_revision=1)
        self.assertIsNone(self.store.get("director-1"))
        self.assertEqual(self.store.get("worker-1"), Revisioned(worker_profile(), 1))

    def test_fallback_approval_cannot_be_reused_after_profile_delete_and_recreate(self) -> None:
        # F1 regression: a recreated ID must not restart at revision 1.
        self.store.save(director_profile())
        old_approval = FallbackApproval("director-1", 1, 0, "approval-1")
        self.store.delete("director-1", expected_revision=1)
        recreated = director_profile(fallbacks=(ExecutionChoice("conn-a", "synthetic-model-alpha", "low"),))
        self.assertEqual(self.store.save(recreated), Revisioned(recreated, 2))
        inventory = make_inventory(MISMATCH_LISTS)
        decision = self.store.prepare_execution("director-1", inventory, approval=old_approval, clock=fixed_clock)
        self.assertEqual(decision.status, "rejected")
        self.assertIsNone(decision.bound)
        self.assertIn("FALLBACK_APPROVAL_STALE_REVISION", decision.reasons)
        self.assertEqual(len(decision.attempts), 1)
        # An approval naming the new revision still works.
        fresh = self.store.prepare_execution("director-1", inventory,
                                             approval=FallbackApproval("director-1", 2, 0, "approval-2"),
                                             clock=fixed_clock)
        self.assertEqual(fresh.status, "bound")
        # The tombstone survives reopening, also when the workspace changes.
        reopened = ProfileStore(self.path, settings=self.settings)
        reopened.delete("director-1", expected_revision=2)
        moved = DirectorProfile("director-1", "ws-2", ExecutionChoice("conn-x", "synthetic-model-alpha"), 5)
        self.assertEqual(reopened.save(moved).revision, 3)
        for revision in (1, 2):
            stale = reopened.prepare_execution("director-1", make_inventory({}, resources=("conn-x",)),
                                               approval=FallbackApproval("director-1", revision, 0, "approval-x"),
                                               clock=fixed_clock)
            self.assertEqual(stale.status, "rejected")
            self.assertNotEqual(stale.bound_position, "fallback:0")

    def test_schema_one_profile_database_is_upgraded_in_place_with_records_preserved(self) -> None:
        legacy = self.root / "legacy-profiles.sqlite3"
        director = director_profile()
        with closing(sqlite3.connect(legacy, isolation_level=None)) as connection:
            profile_storage._create_schema(connection, 1)
            connection.execute(
                'INSERT INTO "profiles" ("id", "workspace_id", "role", "document", "revision") VALUES (?, ?, ?, ?, 3)',
                ("director-1", "ws-1", "director", profile_storage._json_text(profile_to_dict(director))),
            )
        store = ProfileStore(legacy, settings=self.settings)
        self.assertEqual(store.get("director-1"), Revisioned(director, 3))
        with closing(sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        store.delete("director-1", expected_revision=3)
        self.assertEqual(store.save(director).revision, 4)

        future = self.root / "future-profiles.sqlite3"
        with closing(sqlite3.connect(future, isolation_level=None)) as connection:
            profile_storage._create_schema(connection, 2)
            connection.execute("PRAGMA user_version = 99")
        before = future.read_bytes()
        with self.assertRaises(InvalidDatabase):
            ProfileStore(future, settings=self.settings)
        self.assertEqual(future.read_bytes(), before)

    def test_contract_violating_stored_profile_is_reported_as_invalid_database(self) -> None:
        self.store.save(director_profile())
        for document in (json.dumps({"id": "director-1"}), '{"id": "a", "id": "b"}'):
            with self.subTest(document=document):
                with closing(sqlite3.connect(self.path)) as connection:
                    connection.execute('UPDATE "profiles" SET "document" = ? WHERE "id" = ?',
                                       (document, "director-1"))
                    connection.commit()
                with self.assertRaises(InvalidDatabase):
                    self.store.get("director-1")
                with self.assertRaises(InvalidDatabase):
                    self.store.list("ws-1")

    # -------------------------------------------------------- references
    def test_missing_and_cross_workspace_references_are_rejected_at_save(self) -> None:
        cases = (
            (director_profile(workspace_id="ws-missing"), InvalidReference),
            (director_profile(primary=ExecutionChoice("conn-missing", "synthetic-model-alpha")), InvalidReference),
            (director_profile(fallbacks=(ExecutionChoice("conn-x", "synthetic-model-alpha"),)),
             CrossWorkspaceReference),
        )
        for profile, error in cases:
            with self.subTest(error=error.__name__):
                with self.assertRaises(error):
                    self.store.save(profile)
        self.assertIsNone(self.store.get("director-1"))

    # ------------------------------------------------ execution binding
    def test_execution_binds_primary_with_requested_profile_and_provenance(self) -> None:
        director = director_profile()
        self.store.save(director)
        inventory = make_inventory(FULL_LISTS)
        decision = self.store.prepare_execution("director-1", inventory, clock=fixed_clock)
        self.assertIs(decision.require(), decision)
        self.assertEqual(decision.status, "bound")
        self.assertEqual(decision.requested, director.primary)
        self.assertEqual(decision.bound, director.primary)
        self.assertEqual(decision.bound_position, "primary")
        validation = decision.attempts[0].validation
        self.assertIs(validation.supported_by, Provenance.PROVIDER_OBSERVED)
        self.assertEqual(validation.evidence_digest,
                         inventory.snapshot("conn-a", Provenance.PROVIDER_OBSERVED).digest)
        self.assertEqual(decision.decided_at, "2030-01-01T00:00:00Z")
        records = self.store.execution_records("director-1")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["requested_profile"], profile_to_dict(director))
        self.assertEqual(record["profile_revision"], 1)
        self.assertEqual(record["bound"]["connection_id"], "conn-a")
        self.assertEqual(record["binding_provenance"]["supported_by"], "provider_observed")
        self.assertEqual(record["binding_provenance"]["validated_at"], "2030-01-01T00:00:00Z")

    def test_option_mismatch_is_rejected_and_fallback_is_never_selected_automatically(self) -> None:
        self.store.save(director_profile())
        decision = self.store.prepare_execution("director-1", make_inventory(MISMATCH_LISTS), clock=fixed_clock)
        self.assertEqual(decision.status, "rejected")
        self.assertIsNone(decision.bound)
        self.assertEqual(len(decision.attempts), 1, "The valid fallback must not be tried without approval.")
        self.assertIn("provider_observed:EFFORT_NOT_SUPPORTED", decision.attempts[0].reasons)
        self.assertIn("provider_observed:TOOL_NOT_SUPPORTED:edit", decision.attempts[0].reasons)
        self.assertEqual(decision.reasons, ("PRIMARY_REJECTED", "FALLBACK_NOT_APPROVED"))
        with self.assertRaises(CapabilityMismatch) as failure:
            decision.require()
        self.assertIs(failure.exception.decision, decision)
        self.assertEqual(self.store.execution_records("director-1")[0]["status"], "rejected")

    def test_model_mismatch_is_rejected(self) -> None:
        self.store.save(worker_profile())
        lists = {"conn-b": [{"model": "synthetic-model-gamma", "efforts": ["medium"], "tools": ["shell"]}]}
        decision = self.store.prepare_execution("worker-1", make_inventory(lists), clock=fixed_clock)
        self.assertEqual(decision.status, "rejected")
        self.assertIn("provider_observed:MODEL_NOT_LISTED", decision.attempts[0].reasons)

    def test_explicitly_approved_fallback_is_used_and_recorded(self) -> None:
        director = director_profile()
        self.store.save(director)
        approval = FallbackApproval("director-1", 1, 0, "approval-1")
        decision = self.store.prepare_execution("director-1", make_inventory(MISMATCH_LISTS),
                                                approval=approval, clock=fixed_clock)
        self.assertEqual(decision.status, "bound")
        self.assertEqual(decision.requested, director.primary)
        self.assertEqual(decision.bound, director.fallbacks[0])
        self.assertEqual(decision.bound_position, "fallback:0")
        self.assertEqual(decision.approval, approval)
        self.assertEqual([attempt.accepted for attempt in decision.attempts], [False, True])
        record = self.store.execution_records("director-1")[-1]
        self.assertEqual(record["approval"]["approval_ref"], "approval-1")
        self.assertEqual(record["requested"]["connection_id"], "conn-a")
        self.assertEqual(record["bound"]["connection_id"], "conn-b")

    def test_invalid_stale_or_unsupported_approvals_do_not_bind(self) -> None:
        self.store.save(director_profile())
        inventory = make_inventory(MISMATCH_LISTS)
        cases = (
            (FallbackApproval("other-profile", 1, 0, "approval-1"), "FALLBACK_APPROVAL_PROFILE_MISMATCH"),
            (FallbackApproval("director-1", 2, 0, "approval-1"), "FALLBACK_APPROVAL_STALE_REVISION"),
            (FallbackApproval("director-1", 1, 3, "approval-1"), "FALLBACK_INDEX_OUT_OF_RANGE"),
        )
        for approval, reason in cases:
            with self.subTest(reason=reason):
                decision = self.store.prepare_execution("director-1", inventory, approval=approval, clock=fixed_clock)
                self.assertEqual(decision.status, "rejected")
                self.assertIn(reason, decision.reasons)
                self.assertEqual(len(decision.attempts), 1)
        self.store.save(director_profile(fallbacks=(ExecutionChoice("conn-b", "synthetic-model-beta", "high"),)),
                        expected_revision=1)
        decision = self.store.prepare_execution("director-1", inventory,
                                                approval=FallbackApproval("director-1", 2, 0, "approval-2"),
                                                clock=fixed_clock)
        self.assertEqual(decision.status, "rejected")
        self.assertIn("APPROVED_FALLBACK_REJECTED", decision.reasons)
        self.assertIn("provider_observed:EFFORT_NOT_SUPPORTED", decision.attempts[1].reasons)
        with self.assertRaises(InvalidProfile):
            self.store.prepare_execution("director-1", inventory, approval={"fallback_index": 0})

    def test_unknown_resource_and_missing_evidence_fail_closed(self) -> None:
        self.store.save(worker_profile())
        unknown = self.store.prepare_execution("worker-1", CapabilityInventory(clock=fixed_clock), clock=fixed_clock)
        self.assertEqual(unknown.status, "rejected")
        self.assertEqual(unknown.attempts[0].reasons, ("CAPABILITY_ERROR:UNKNOWN_RESOURCE",))
        no_lists = self.store.prepare_execution("worker-1", make_inventory({}), clock=fixed_clock)
        self.assertEqual(no_lists.status, "rejected")
        self.assertIn("NO_SUPPORT_EVIDENCE", no_lists.attempts[0].reasons)

    def test_inventory_failures_and_malformed_results_fail_closed_without_raw_text(self) -> None:
        self.store.save(worker_profile())
        cases = (
            ("exception", FakeInventory(error=RuntimeError(SECRET_MARKER)), ("INVENTORY_UNAVAILABLE",)),
            ("unclassified", FakeInventory(error=CapabilityError(SECRET_MARKER)),
             ("CAPABILITY_ERROR:UNCLASSIFIED",)),
            ("result mismatch", FakeInventory(result=lambda c, m, e, t: synthetic_result(
                c, "synthetic-model-other", e, t)), ("VALIDATION_RESULT_MISMATCH",)),
            ("not requested", FakeInventory(result=lambda c, m, e, t: synthetic_result(
                c, m, e, t, request_provenance=Provenance.PROVIDER_OBSERVED)), ("NO_SUPPORT_EVIDENCE",)),
            ("local derived support", FakeInventory(result=lambda c, m, e, t: synthetic_result(
                c, m, e, t, supported_by=Provenance.LOCAL_DERIVED)), ("NO_SUPPORT_EVIDENCE",)),
        )
        for name, inventory, reasons in cases:
            with self.subTest(case=name):
                decision = self.store.prepare_execution("worker-1", inventory, clock=fixed_clock)
                self.assertEqual(decision.status, "rejected")
                self.assertIsNone(decision.bound)
                for reason in reasons:
                    self.assertIn(reason, decision.attempts[0].reasons)
                with self.assertRaises(CapabilityMismatch):
                    decision.require()
        self.assertNotIn(SECRET_MARKER, json.dumps(self.store.execution_records("worker-1", limit=1000)))

    def test_configured_catalog_evidence_binds_and_is_recorded(self) -> None:
        self.store.save(worker_profile())
        inventory = make_inventory({})
        inventory.register_manual("conn-b", json.dumps({"models": FULL_LISTS["conn-b"]}), catalog_ref="catalog-1")
        decision = self.store.prepare_execution("worker-1", inventory, clock=fixed_clock)
        self.assertEqual(decision.status, "bound")
        self.assertIs(decision.bound_validation.supported_by, Provenance.CONFIGURED_CATALOG)
        record = self.store.execution_records("worker-1", limit=1, newest_first=True)[0]
        self.assertEqual(record["binding_provenance"]["supported_by"], "configured_catalog")

    def test_deleted_connection_rejects_execution_before_capability_check(self) -> None:
        self.store.save(worker_profile())
        self.settings.delete(ConnectionProfile, "conn-b", 1)
        decision = self.store.prepare_execution("worker-1", make_inventory(FULL_LISTS), clock=fixed_clock)
        self.assertEqual(decision.status, "rejected")
        self.assertEqual(decision.reasons, ("REFERENCE_MISSING",))
        self.assertEqual(decision.attempts, ())
        self.assertEqual(self.store.execution_records("worker-1")[0]["reasons"], ["REFERENCE_MISSING"])

    def test_connection_moved_to_another_workspace_rejects_execution(self) -> None:
        self.store.save(worker_profile())
        self.settings.delete(ConnectionProfile, "conn-b", 1)
        with self.settings.transaction() as tx:
            tx.save(ConnectionProfile("conn-b", "ws-2", "vendor-b"))
        decision = self.store.prepare_execution("worker-1", make_inventory(FULL_LISTS), clock=fixed_clock)
        self.assertEqual(decision.status, "rejected")
        self.assertEqual(decision.reasons, ("CROSS_WORKSPACE_REFERENCE",))
        self.assertEqual(decision.attempts, ())
        self.assertEqual(self.store.execution_records("worker-1")[0]["reasons"], ["CROSS_WORKSPACE_REFERENCE"])

    def test_clock_failure_appends_no_execution_record(self) -> None:
        self.store.save(worker_profile())
        with self.assertRaises(ProfileError):
            self.store.prepare_execution("worker-1", make_inventory(FULL_LISTS), clock=failing_clock)
        self.assertEqual(self.store.execution_records("worker-1"), ())

    def test_latest_execution_records_are_reachable_beyond_the_oldest_page(self) -> None:
        self.store.save(director_profile())
        mismatch = make_inventory(MISMATCH_LISTS)
        for _ in range(2):
            self.store.prepare_execution("director-1", mismatch, clock=fixed_clock)
        self.store.prepare_execution("director-1", make_inventory(FULL_LISTS), clock=fixed_clock)
        self.assertEqual(self.store.execution_records("director-1", limit=1)[0]["status"], "rejected")
        newest = self.store.execution_records("director-1", limit=1, newest_first=True)
        self.assertEqual(newest[0]["status"], "bound")
        first_page = self.store.execution_record_entries("director-1", limit=2)
        self.assertEqual([document["status"] for _, document in first_page], ["rejected", "rejected"])
        rest = self.store.execution_record_entries("director-1", limit=2, after_seq=first_page[-1][0])
        self.assertEqual([document["status"] for _, document in rest], ["bound"])
        older = self.store.execution_records("director-1", limit=10, before_seq=rest[0][0], newest_first=True)
        self.assertEqual([document["status"] for document in older], ["rejected", "rejected"])
        for arguments in ({"after_seq": -1}, {"before_seq": True}, {"newest_first": "yes"}):
            with self.subTest(arguments=arguments):
                with self.assertRaises(InvalidProfile):
                    self.store.execution_records("director-1", **arguments)

    # ------------------------------------------------- adapter and command
    def test_agent_profile_adapter_maps_without_writing_settings(self) -> None:
        self.assertEqual(to_agent_profile(director_profile()),
                         AgentProfile("director-1", "ws-1", "director", "conn-a", "synthetic-model-alpha", "high"))
        self.assertIsNone(to_agent_profile(reviewer_profile()))
        adapted = from_agent_profile(
            AgentProfile("worker-2", "ws-1", "worker", "conn-b", "synthetic-model-beta", "medium"),
            max_calls=5, tools=("shell",))
        self.assertEqual(adapted, RoleProfile("worker-2", "ws-1", "worker",
                                              ExecutionChoice("conn-b", "synthetic-model-beta", "medium", ("shell",)),
                                              5))

    def test_validation_command_reports_classifications_only(self) -> None:
        valid = self.root / "director.json"
        valid.write_text(json.dumps(profile_document(director_profile())), encoding="utf-8")
        document = profile_document(worker_profile())
        document["profile"]["api_key"] = SECRET_MARKER
        invalid = self.root / "invalid.json"
        invalid.write_text(json.dumps(document), encoding="utf-8")
        duplicate = self.root / "duplicate-key.json"
        duplicate.write_text('{"format": "aik.role-profile", "format": "x"}', encoding="utf-8")
        output = io.StringIO()
        code = cli_main(["validate", str(valid), str(invalid), str(duplicate), str(self.root / "absent.json")],
                        stdout=output)
        self.assertEqual(code, 1)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line["status"] for line in lines], ["valid", "invalid", "invalid", "invalid"])
        self.assertEqual(lines[0]["role"], "director")
        self.assertEqual(lines[3]["error"], "Unreadable")
        self.assertNotIn(SECRET_MARKER, output.getvalue())
        self.assertEqual(cli_main(["validate", str(valid)], stdout=io.StringIO()), 0)
        self.assertEqual(cli_main([], stdout=io.StringIO()), 2)

    def test_validation_command_rejects_oversized_and_directory_inputs(self) -> None:
        oversized = self.root / "oversized.json"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_DOCUMENT_BYTES + 10)
        output = io.StringIO()
        self.assertEqual(cli_main(["validate", str(oversized), str(self.root)], stdout=output), 1)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line["error"] for line in lines], ["DocumentTooLarge", "NotRegularFile"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are unavailable on this platform.")
    def test_validation_command_rejects_fifo_without_blocking(self) -> None:
        fifo = self.root / "profile.fifo"
        os.mkfifo(fifo)
        results = []
        output = io.StringIO()
        worker = threading.Thread(target=lambda: results.append(cli_main(["validate", str(fifo)], stdout=output)),
                                  daemon=True)
        worker.start()
        worker.join(5)
        if worker.is_alive():
            # Release the blocked reader so the failing case does not linger.
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            self.fail("The validation command blocked on a FIFO.")
        self.assertEqual(results, [1])
        self.assertEqual(json.loads(output.getvalue())["error"], "NotRegularFile")

    # ------------------------------------------------------------- files
    def test_settings_or_foreign_databases_are_rejected_and_preserved(self) -> None:
        with self.assertRaises(InvalidProfile):
            ProfileStore(self.settings.path, settings=self.settings)
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InvalidDatabase):
            ProfileStore(foreign, settings=self.settings)
        self.assertEqual(foreign.read_bytes(), before)

    def test_settings_companion_names_are_rejected_as_profile_paths(self) -> None:
        settings_path = Path(self.settings.path)
        for suffix in ("-journal", "-wal", "-shm", ".storage.lock"):
            with self.subTest(suffix=suffix):
                candidate = settings_path.with_name(settings_path.name + suffix)
                existed = candidate.exists()
                before = candidate.read_bytes() if existed else None
                with self.assertRaises(InvalidProfile):
                    ProfileStore(candidate, settings=self.settings)
                self.assertEqual(candidate.exists(), existed)
                if existed:
                    self.assertEqual(candidate.read_bytes(), before)

    def test_missing_profile_database_is_not_recreated_implicitly(self) -> None:
        self.path.unlink()
        with self.assertRaises(InvalidDatabase):
            self.store.get("director-1")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
