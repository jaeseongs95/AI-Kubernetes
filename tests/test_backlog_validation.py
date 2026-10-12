"""Regression tests for the audit's invalid-database counterexamples.

Every test builds a small disposable planning fixture. No product code,
upstream project, real credentials, or checked-in backlog data are modified.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backlog_validation_under_test", ROOT / "tools" / "backlog.py")
backlog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backlog)
NOW = "2026-10-12T00:00:00Z"


class BacklogValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aik-validation-")
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "fixture.sqlite3"
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript((ROOT / "planning" / "schema.sql").read_text(encoding="utf-8"))
        self.conn.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "schema_version": "1", "project": "AI-Kubernetes", "created_at": NOW,
            "updated_at": NOW, "baseline_commit": "a" * 40,
            "canonical_data": "planning/backlog.sqlite3", "review_snapshot": "planning/backlog.sql",
            "ownership_policy": "VM tasks; AIK placements",
        }.items())
        self.conn.execute("INSERT INTO sources VALUES ('S-USER','Test request','user','','','')")
        self.conn.execute("INSERT INTO requirements VALUES ('U01','user','Fixture requirement','Fixture description')")
        self.conn.execute("INSERT INTO milestones VALUES ('M0','Fixture',0,'first_release','Fixture exit')")
        self.conn.execute("INSERT INTO epics VALUES ('E01','Fixture epic','Fixture description')")
        for number in (1, 2):
            task_id = f"AIK-{number:03d}"
            self.conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, f"Task {number}", "Description", "Deliverable", "Verification", "E01", "M0",
                 "P0", "todo", "feature", "AIK", number, NOW, NOW),
            )
            self.conn.execute("INSERT INTO task_requirements VALUES (?, 'U01')", (task_id,))
            self.conn.execute("INSERT INTO task_sources VALUES (?, 'S-USER')", (task_id,))
            self.conn.executemany("INSERT INTO acceptance_criteria VALUES (?, ?, ?)",
                                  [(task_id, 1, "First observed result"), (task_id, 2, "Second observed result")])
            self.conn.execute(
                "INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
                "VALUES (?,'created',NULL,'todo','Fixture created',?)", (task_id, NOW),
            )
        self.conn.execute("INSERT INTO task_dependencies VALUES ('AIK-002','AIK-001')")
        self.conn.commit()

    def assert_invalid(self, expected):
        issues = backlog.validate_connection(self.conn)
        self.assertTrue(any(expected in issue for issue in issues), issues)

    def change_status(self, task_id, new_status):
        old_status = self.conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, task_id))
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
            "VALUES (?,'status_change',?,?,?,?)", (task_id, old_status, new_status, "Fixture transition", NOW),
        )

    def add_evidence(self, task_id):
        self.conn.execute("INSERT INTO task_evidence VALUES (?,?,?,?,?,?)",
                          (f"EV-{task_id}", task_id, "test", "fixture-result", "Observed fixture result", NOW))

    def test_baseline_and_schema_version(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(backlog.validate_connection(self.conn), [])
        self.assertEqual([row[0] for row in self.conn.execute("SELECT id FROM v_ready_tasks")], ["AIK-001"])

    def test_unknown_user_version(self):
        self.conn.execute("PRAGMA user_version=999")
        self.assert_invalid("Unsupported schema user_version")

    def test_inconsistent_metadata_version(self):
        self.conn.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        self.assert_invalid("metadata.schema_version")

    def test_missing_metadata(self):
        self.conn.execute("DELETE FROM metadata")
        self.assert_invalid("Missing or empty required metadata")

    def test_blank_metadata(self):
        self.conn.execute("UPDATE metadata SET value='   ' WHERE key='updated_at'")
        self.assert_invalid("Missing or empty required metadata: updated_at")

    def test_invalid_enum_is_detected_even_with_checks_disabled(self):
        self.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.conn.execute("UPDATE tasks SET status='invented' WHERE id='AIK-001'")
        self.assert_invalid("invalid status 'invented'")

    def test_blank_required_text_is_detected_even_with_checks_disabled(self):
        self.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.conn.execute("UPDATE tasks SET title=' ' WHERE id='AIK-001'")
        self.assert_invalid("title must be nonempty text")

    def test_negative_ordinal_is_detected_even_with_checks_disabled(self):
        self.conn.execute("PRAGMA ignore_check_constraints=ON")
        self.conn.execute("UPDATE acceptance_criteria SET ordinal=0 WHERE task_id='AIK-001' AND ordinal=1")
        self.assert_invalid("ordinal must be an integer >= 1")

    def test_fractional_sequence_is_not_an_execution_order(self):
        self.conn.execute("UPDATE tasks SET sequence=0.5 WHERE id='AIK-001'")
        self.assert_invalid("sequence must be an integer >= 0")

    def test_ready_view_cannot_advertise_all_todo_tasks(self):
        self.conn.execute("DROP VIEW v_ready_tasks")
        self.conn.execute("CREATE VIEW v_ready_tasks AS SELECT * FROM v_backlog WHERE status='todo'")
        self.assert_invalid("Schema definition differs from version 1: v_ready_tasks")

    def test_same_named_table_without_constraints_is_rejected(self):
        self.conn.execute("DROP TABLE metadata")
        self.conn.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
        self.assert_invalid("Schema definition differs from version 1: metadata")

    def test_unexpected_trigger_is_rejected(self):
        self.conn.execute("CREATE TRIGGER hidden_change AFTER UPDATE ON tasks BEGIN DELETE FROM task_events; END")
        self.assert_invalid("Unexpected schema object for version 1: hidden_change")

    def test_user_object_with_similar_sqlite_prefix_is_not_ignored(self):
        self.conn.execute("CREATE TRIGGER sqliteExtra AFTER UPDATE ON tasks BEGIN DELETE FROM task_events; END")
        self.assert_invalid("Unexpected schema object for version 1: sqliteExtra")

    def test_missing_history(self):
        self.conn.execute("DELETE FROM task_events")
        self.assert_invalid("expected one created event, found 0")

    def test_duplicate_creation(self):
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
            "VALUES ('AIK-001','created',NULL,'todo','Duplicate',?)", (NOW,),
        )
        self.assert_invalid("expected one created event, found 2")

    def test_invalid_initial_state(self):
        self.conn.execute("UPDATE task_events SET new_status='done' WHERE task_id='AIK-001'")
        self.assert_invalid("history must begin with a created event from null to todo")

    def test_task_state_and_history_must_agree(self):
        self.conn.execute("UPDATE tasks SET status='in_progress' WHERE id='AIK-001'")
        self.assert_invalid("differs from last event status")

    def test_event_old_status_must_match_previous_state(self):
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
            "VALUES ('AIK-001','status_change','done','todo','Incorrect prior state',?)", (NOW,),
        )
        self.assert_invalid("broken status transition")

    def test_review_event_cannot_change_state(self):
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
            "VALUES ('AIK-001','review_refined','todo','in_progress','Incorrect review event',?)", (NOW,),
        )
        self.assert_invalid("non-status event")

    def test_no_op_review_event_is_valid(self):
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
            "VALUES ('AIK-001','review_refined','todo','todo','Reviewed description',?)", (NOW,),
        )
        self.assertEqual(backlog.validate_connection(self.conn), [])

    def test_start_requires_completed_prerequisites(self):
        self.change_status("AIK-002", "in_progress")
        self.assert_invalid("AIK-002: in_progress while prerequisite AIK-001 is todo")

    def test_completion_requires_completed_prerequisites(self):
        self.change_status("AIK-002", "done")
        self.add_evidence("AIK-002")
        self.assert_invalid("AIK-002: done while prerequisite AIK-001 is todo")

    def test_completion_requires_evidence(self):
        self.change_status("AIK-001", "done")
        self.assert_invalid("done task has no recorded evidence")

    def test_valid_completion_unlocks_and_allows_next_task(self):
        self.change_status("AIK-001", "done")
        self.add_evidence("AIK-001")
        self.assertEqual(backlog.validate_connection(self.conn), [])
        self.assertEqual([row[0] for row in self.conn.execute("SELECT id FROM v_ready_tasks")], ["AIK-002"])
        self.change_status("AIK-002", "in_progress")
        self.assertEqual(backlog.validate_connection(self.conn), [])

    def test_blocked_does_not_require_completed_prerequisites(self):
        self.change_status("AIK-002", "blocked")
        self.assertEqual(backlog.validate_connection(self.conn), [])

    def test_cancelled_does_not_unlock_dependents(self):
        self.change_status("AIK-001", "cancelled")
        self.assertEqual(backlog.validate_connection(self.conn), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v_ready_tasks WHERE id='AIK-002'").fetchone()[0], 0)

    def test_cycle_is_still_rejected(self):
        self.conn.execute("INSERT INTO task_dependencies VALUES ('AIK-001','AIK-002')")
        self.assert_invalid("Dependency cycle")

    def test_missing_requirement_link_is_still_rejected(self):
        self.conn.execute("DELETE FROM task_requirements WHERE task_id='AIK-001'")
        self.assert_invalid("AIK-001: no linked requirement")

    def test_missing_source_link_is_still_rejected(self):
        self.conn.execute("DELETE FROM task_sources WHERE task_id='AIK-001'")
        self.assert_invalid("AIK-001: no linked source")

    def test_missing_acceptance_criteria_are_still_rejected(self):
        self.conn.execute("DELETE FROM acceptance_criteria WHERE task_id='AIK-001'")
        self.assert_invalid("AIK-001: fewer than two acceptance criteria")


if __name__ == "__main__":
    unittest.main()
