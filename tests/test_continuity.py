"""Director continuity against disposable SQLite files and a fake VM read port.

All IDs, directives and summaries are synthetic. No provider, credential,
network or VM installation is used. Linux runs are development verification,
not Windows support evidence.
"""
from __future__ import annotations

from contextlib import closing
import copy
from dataclasses import fields, replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_continuity import (  # noqa: E402
    BridgeStatusPort, Checkpoint, ConfigRevision, ContextSummary, ContinuityConflict, ContinuityStore,
    Directive, InvalidContinuityDatabase, InvalidContinuityRecord, Resumer, RevisionRegression, TaskRef,
)
from aik_runtime.errors import UnsafeStoragePath  # noqa: E402


SECRET = "sk-SYNTHETICSECRETVALUE1234567890"


def vm_payload() -> dict:
    return {
        "project_id": "project-1",
        "goal_id": "goal-1",
        "goal_revision_id": "goal-rev-2",
        "plan_revision_id": "plan-rev-3",
        "authorization_revision_id": "auth-rev-1",
        "goal_state": "active",
        "tasks": [
            {"task_id": "task-a", "state": "done", "attempt_id": "attempt-a1", "evidence": ["evidence-a1"]},
            {"task_id": "task-b", "state": "running", "attempt_id": "attempt-b1", "evidence": []},
            {"task_id": "task-c", "state": "pending", "attempt_id": None},
        ],
    }


class FakeVm:
    """Read port fake. Effectful methods exist only to prove they are never called."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls = []

    def status(self, project_id):
        self.calls.append(("status", project_id))
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.payload)

    def prepare(self, *args, **kwargs):
        self.calls.append(("prepare",))
        raise AssertionError("resume must not create a Goal")

    def run_once(self, *args, **kwargs):
        self.calls.append(("run_once",))
        raise AssertionError("resume must not create an Attempt")


class ContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-continuity-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "상태" / "continuity.sqlite3"
        self.now = [1_800_000_000.0]

    def clock(self) -> float:
        self.now[0] += 1
        return self.now[0]

    def store(self, path: Path | None = None) -> ContinuityStore:
        return ContinuityStore(path or self.path, clock=self.clock)

    def seed(self, store: ContinuityStore):
        d1 = store.append_directive(Directive("dir-1", "project-1", "instruction", "합성 보고서 A를 작성하라")).record
        d2 = store.append_directive(Directive("dir-2", "project-1", "instruction", "결과를 CSV로도 내보내라")).record
        c1 = store.append_directive(Directive("cor-1", "project-1", "correction", "A 대신 보고서 B를 작성하라",
                                              target_id="dir-1", scope="replace")).record
        c2 = store.append_directive(Directive("cor-2", "project-1", "correction", "보고서 B에 표를 포함하라",
                                              target_id="cor-1", scope="supplement")).record
        c3 = store.append_directive(Directive("cor-3", "project-1", "correction", "CSV 내보내기는 필요 없다",
                                              target_id="dir-2", scope="withdraw")).record
        return d1, d2, c1, c2, c3

    def checkpoint(self, directive_seq: int, **changes) -> Checkpoint:
        base = Checkpoint(
            project_id="project-1", goal_id="goal-1", goal_revision_id="goal-rev-2",
            plan_revision_id="plan-rev-3",
            tasks=(TaskRef("task-a", "attempt-a1", ("evidence-a1",)), TaskRef("task-b", "attempt-b1"),
                   TaskRef("task-c", None)),
            evidence_refs=("evidence-a1",), authorization_revision_id="auth-rev-1",
            directive_seq=directive_seq,
        )
        return replace(base, **changes)

    def seeded_with_checkpoint(self):
        store = self.store()
        records = self.seed(store)
        store.record_config(ConfigRevision("director-settings", 1, "a" * 64))
        store.record_summary(ContextSummary("summary-1", "project-1", "모델 요약: task-b가 완료된 것으로 보인다",
                                            covers_through_seq=records[-1].seq))
        saved = store.save_checkpoint(self.checkpoint(records[-1].seq, config_refs=(("director-settings", 1),),
                                                      summary_id="summary-1"), 0)
        return store, records, saved

    # ------------------------------------------------------------ directives
    def test_directives_and_corrections_keep_order_and_scope_across_reopen(self) -> None:
        store = self.store()
        records = self.seed(store)
        store.append_directive(Directive("dir-other", "project-2", "instruction", "다른 프로젝트 지시"))
        self.assertEqual([record.seq for record in records], sorted(record.seq for record in records))
        reopened = self.store()
        effective = reopened.effective_directives("project-1")
        self.assertEqual([item.directive_id for item in effective], ["dir-1", "dir-2", "cor-1", "cor-2", "cor-3"])
        status = {item.directive_id: item for item in effective}
        self.assertEqual((status["dir-1"].status, status["dir-1"].superseded_by), ("replaced", "cor-1"))
        self.assertEqual((status["dir-2"].status, status["dir-2"].superseded_by), ("withdrawn", "cor-3"))
        self.assertEqual(status["cor-1"].status, "active")
        self.assertEqual(status["cor-1"].supplemented_by, ("cor-2",))
        self.assertEqual(status["cor-2"].status, "active")
        self.assertEqual(status["cor-3"].status, "applied")
        self.assertEqual(status["dir-1"].text, "합성 보고서 A를 작성하라")  # original text preserved
        self.assertEqual(reopened.handoff("project-1").active_directive_ids, ("cor-1", "cor-2"))
        page = reopened.directives("project-1", limit=2)
        self.assertEqual([item.directive.directive_id for item in page], ["dir-1", "dir-2"])
        self.assertEqual([item.directive.directive_id for item in reopened.directives(
            "project-1", after_seq=page[-1].seq)], ["cor-1", "cor-2", "cor-3"])

    def test_correction_target_rules_duplicates_and_conflicts(self) -> None:
        store = self.store()
        first = store.append_directive(Directive("dir-1", "project-1", "instruction", "합성 지시"))
        store.append_directive(Directive("dir-x", "project-2", "instruction", "다른 프로젝트"))
        self.assertEqual(first.status, "accepted")
        with self.assertRaises(InvalidContinuityRecord):
            store.append_directive(Directive("cor-1", "project-1", "correction", "정정", "dir-missing", "replace"))
        with self.assertRaises(InvalidContinuityRecord):
            store.append_directive(Directive("cor-2", "project-1", "correction", "정정", "dir-x", "replace"))
        for arguments in (("cor-3", "project-1", "correction", "정정", "cor-3", "replace"),
                          ("dir-3", "project-1", "instruction", "지시", "dir-1", "replace"),
                          ("cor-4", "project-1", "correction", "정정", "dir-1", None),
                          ("cor-5", "project-1", "correction", "정정", "dir-1", "rewrite"),
                          ("dir-6", "project-1", "instruction", "   ")):
            with self.subTest(arguments=arguments[0]):
                with self.assertRaises(InvalidContinuityRecord):
                    Directive(*arguments)
        again = store.append_directive(Directive("dir-1", "project-1", "instruction", "합성 지시"))
        self.assertEqual((again.status, again.record.seq), ("duplicate", first.record.seq))
        with self.assertRaises(ContinuityConflict):
            store.append_directive(Directive("dir-1", "project-1", "instruction", "바뀐 내용"))
        self.assertEqual(len(store.directives("project-1")), 1)

    def test_append_only_triggers_reject_raw_update_and_delete(self) -> None:
        store, _, _ = self.seeded_with_checkpoint()
        with closing(sqlite3.connect(store.path)) as connection:
            for statement in ("UPDATE directives SET text = 'x'", "DELETE FROM directives",
                              "UPDATE checkpoints SET body = '{}'", "DELETE FROM summaries",
                              "UPDATE config_revisions SET digest = 'x'"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(statement)
        self.assertEqual(len(self.store().directives("project-1")), 5)

    def test_secret_like_text_is_rejected_without_echo_or_storage(self) -> None:
        store = self.store()
        store.append_directive(Directive("dir-1", "project-1", "instruction", "합성 지시"))
        for build in (lambda: Directive("dir-2", "project-1", "instruction", "이 키를 써라 " + SECRET),
                      lambda: Directive("dir-3", "project-1", "instruction", "Authorization: Bearer abc"),
                      lambda: ContextSummary("summary-x", "project-1", "요약 " + SECRET)):
            with self.assertRaises(InvalidContinuityRecord) as raised:
                build()
            self.assertNotIn(SECRET, str(raised.exception))
        packet = json.dumps(store.handoff("project-1").to_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET, packet)
        self.assertNotIn(SECRET.encode("ascii"), store.path.read_bytes())

    # ------------------------------------------------------------- revisions
    def test_config_revisions_reject_regression_and_conflicting_duplicates(self) -> None:
        store = self.store()
        self.assertEqual(store.record_config(ConfigRevision("director-settings", 1, "a" * 64)).status, "accepted")
        self.assertEqual(store.record_config(ConfigRevision("director-settings", 3, "b" * 64)).status, "accepted")
        self.assertEqual(store.record_config(ConfigRevision("director-settings", 1, "a" * 64)).status, "duplicate")
        with self.assertRaises(ContinuityConflict):
            store.record_config(ConfigRevision("director-settings", 3, "c" * 64))
        with self.assertRaises(RevisionRegression):
            store.record_config(ConfigRevision("director-settings", 2, "d" * 64))
        with self.assertRaises(InvalidContinuityRecord):
            ConfigRevision("director-settings", 4, "NOT-A-DIGEST")
        latest = self.store().latest_config("director-settings")
        self.assertEqual((latest.config.revision, latest.config.digest), (3, "b" * 64))

    def test_checkpoint_revisions_are_optimistic_idempotent_and_monotonic(self) -> None:
        store = self.store()
        d1 = store.append_directive(Directive("dir-1", "project-1", "instruction", "합성 지시")).record
        other = store.append_directive(Directive("dir-o", "project-2", "instruction", "다른 지시")).record
        store.record_config(ConfigRevision("director-settings", 1, "a" * 64))
        first = self.checkpoint(d1.seq, config_refs=(("director-settings", 1),))
        saved = store.save_checkpoint(first, 0)
        self.assertEqual(saved.revision, 1)
        self.assertEqual(store.save_checkpoint(first, 0), saved)  # retry after a lost reply
        with self.assertRaises(ContinuityConflict):
            store.save_checkpoint(replace(first, plan_revision_id="plan-rev-x"), 0)
        with self.assertRaises(ContinuityConflict):
            store.save_checkpoint(first, 5)
        for invalid in (replace(first, directive_seq=999), replace(first, directive_seq=other.seq),
                        replace(first, summary_id="summary-missing"),
                        replace(first, config_refs=(("director-settings", 9),))):
            with self.subTest(invalid=invalid.directive_seq):
                with self.assertRaises(InvalidContinuityRecord):
                    store.save_checkpoint(invalid, 1)
        d2 = store.append_directive(Directive("dir-2", "project-1", "instruction", "후속 지시")).record
        second = store.save_checkpoint(replace(first, directive_seq=d2.seq), 1)
        self.assertEqual(second.revision, 2)
        with self.assertRaises(RevisionRegression):
            store.save_checkpoint(replace(first, directive_seq=d1.seq), 2)
        store.record_config(ConfigRevision("director-settings", 2, "b" * 64))
        third = store.save_checkpoint(replace(first, directive_seq=d2.seq, config_refs=(("director-settings", 2),)), 2)
        self.assertEqual(third.revision, 3)
        with self.assertRaises(RevisionRegression):
            store.save_checkpoint(replace(first, directive_seq=d2.seq), 3)
        self.assertEqual([item.revision for item in self.store().checkpoints("project-1")], [1, 2, 3])

    def test_checkpoint_holds_only_vm_references(self) -> None:
        store, _, saved = self.seeded_with_checkpoint()
        self.assertEqual({field.name for field in fields(Checkpoint)}, {
            "project_id", "goal_id", "goal_revision_id", "plan_revision_id", "tasks", "evidence_refs",
            "authorization_revision_id", "directive_seq", "config_refs", "summary_id"})
        self.assertEqual({field.name for field in fields(TaskRef)}, {"task_id", "attempt_id", "evidence"})
        with closing(sqlite3.connect(store.path)) as connection:
            body = json.loads(connection.execute("SELECT body FROM checkpoints").fetchone()[0])
        self.assertNotIn("goal_state", body)
        self.assertTrue(all(set(task) == {"task_id", "attempt_id", "evidence"} for task in body["tasks"]))
        self.assertEqual(saved.checkpoint.goal_id, "goal-1")

    # ---------------------------------------------------------------- resume
    def test_context_compaction_and_restart_resume_same_goal_without_new_goal(self) -> None:
        store, records, saved = self.seeded_with_checkpoint()
        conversation = [record.directive.text for record in records]
        # Simulate context compaction and process restart: in-memory context is gone.
        del conversation, store
        restarted = self.store()
        packet = restarted.handoff("project-1")
        data = packet.to_dict()
        self.assertEqual(data["summary"]["authority"], "model_reported")
        self.assertEqual([item["directive_id"] for item in data["directives"]],
                         ["dir-1", "dir-2", "cor-1", "cor-2", "cor-3"])
        self.assertEqual(data["checkpoint"]["goal_id"], "goal-1")
        self.assertEqual(data["configs"], [["director-settings", 1, "a" * 64]])
        vm = FakeVm(vm_payload())
        result = Resumer(restarted, vm).resume("project-1")
        self.assertEqual(result.status, "continue")
        self.assertEqual(result.handoff, "continue_existing")
        self.assertEqual(result.continuation["goal_id"], "goal-1")
        self.assertEqual(result.continuation["goal_revision_id"], "goal-rev-2")
        self.assertEqual(result.continuation["plan_revision_id"], "plan-rev-3")
        self.assertEqual(result.continuation["checkpoint_revision"], saved.revision)
        self.assertEqual(result.continuation["tasks"],
                         (("task-a", "attempt-a1"), ("task-b", "attempt-b1"), ("task-c", None)))
        # The summary claimed task-b looked done; VM authority still lists it open.
        self.assertEqual(result.open_task_ids, ("task-b", "task-c"))
        self.assertEqual(result.vm_evidence_refs, ("evidence-a1",))
        self.assertEqual(vm.calls, [("status", "project-1")])
        following = restarted.save_checkpoint(saved.checkpoint, result.checkpoint.revision - 1)
        self.assertEqual((following.revision, following.checkpoint.goal_id), (saved.revision, "goal-1"))
        progressed = restarted.save_checkpoint(
            replace(saved.checkpoint, tasks=saved.checkpoint.tasks[:2] + (TaskRef("task-c", "attempt-c1"),)),
            saved.revision)
        self.assertEqual((progressed.revision, progressed.checkpoint.goal_id), (2, "goal-1"))

    def test_vm_progress_since_checkpoint_still_continues(self) -> None:
        store, _, _ = self.seeded_with_checkpoint()
        payload = vm_payload()
        payload["tasks"][1].update(state="done", evidence=["evidence-b1"])
        payload["tasks"][2].update(state="running", attempt_id="attempt-c1")
        result = Resumer(store, FakeVm(payload)).resume("project-1")
        self.assertEqual(result.status, "continue")
        self.assertEqual(result.open_task_ids, ("task-c",))

    def test_stale_references_stop_for_user_confirmation(self) -> None:
        store, _, _ = self.seeded_with_checkpoint()

        def change(**updates):
            payload = vm_payload()
            payload.update(updates)
            return payload

        def task_change(index, **updates):
            payload = vm_payload()
            payload["tasks"][index].update(updates)
            return payload

        without_a = vm_payload()
        del without_a["tasks"][0]
        cases = {
            ("GOAL_REVISION_CHANGED", None): change(goal_revision_id="goal-rev-3"),
            ("PLAN_REVISION_CHANGED", None): change(plan_revision_id="plan-rev-4"),
            ("GOAL_MISMATCH", None): change(goal_id="goal-2"),
            ("PROJECT_MISMATCH", None): change(project_id="project-9"),
            ("GOAL_CANCELLED", None): change(goal_state="cancelled"),
            ("AUTHORIZATION_CHANGED", None): change(authorization_revision_id="auth-rev-2"),
            ("ATTEMPT_CHANGED", "task-b"): task_change(1, attempt_id="attempt-b2"),
            ("EVIDENCE_MISSING", "task-a"): task_change(0, evidence=[]),
            ("TASK_MISSING", "task-a"): without_a,
        }
        for expected, payload in cases.items():
            with self.subTest(code=expected[0]):
                vm = FakeVm(payload)
                result = Resumer(store, vm).resume("project-1")
                self.assertEqual(result.status, "confirmation_required")
                self.assertEqual(result.handoff, "confirm_with_user")
                self.assertIn(expected, result.mismatches)
                self.assertIsNone(result.continuation)
                self.assertEqual(vm.calls, [("status", "project-1")])
        self.assertEqual(store.latest_checkpoint("project-1").revision, 1)

    def test_unreadable_or_unavailable_vm_status_is_not_continued(self) -> None:
        store, _, _ = self.seeded_with_checkpoint()
        missing_revision = vm_payload()
        del missing_revision["goal_revision_id"]
        missing_authorization = vm_payload()
        del missing_authorization["authorization_revision_id"]
        cases = {
            "GOAL_REVISION_UNREADABLE": missing_revision,
            "AUTHORIZATION_UNREADABLE": missing_authorization,
            "VM_STATE_UNKNOWN": dict(vm_payload(), goal_state="exploded"),
            "PLAN_REVISION_UNREADABLE": dict(vm_payload(), plan_revision_id=None),
            "VM_STATUS_INVALID": ["not", "a", "mapping"],
        }
        for code, payload in cases.items():
            with self.subTest(code=code):
                result = Resumer(store, FakeVm(payload)).resume("project-1")
                self.assertEqual((result.status, result.handoff), ("unverifiable", "inspect_vm"))
                self.assertIn((code, None), result.mismatches)
                self.assertIsNone(result.continuation)
        broken = Resumer(store, FakeVm(error=RuntimeError("synthetic failure " + SECRET))).resume("project-1")
        self.assertEqual((broken.status, broken.handoff), ("unavailable", "retry"))
        self.assertNotIn(SECRET, repr(broken))

    def test_no_checkpoint_reports_start_goal_without_creating(self) -> None:
        store = self.store()
        vm = FakeVm(vm_payload())
        result = Resumer(store, vm).resume("project-1")
        self.assertEqual((result.status, result.handoff), ("no_checkpoint", "start_goal"))
        self.assertEqual(vm.calls, [])
        self.assertIsNone(store.latest_checkpoint("project-1"))

    def test_bridge_status_port_uses_read_only_status_query(self) -> None:
        from aik_vm_bridge import ProjectQuery

        class FakeBridge:
            def __init__(self):
                self.queries = []

            def status(self, query):
                self.queries.append(query)
                return SimpleNamespace(payload=vm_payload())

        bridge = FakeBridge()
        port = BridgeStatusPort(bridge)
        self.assertEqual(port.status("project-1")["goal_id"], "goal-1")
        self.assertEqual(len(bridge.queries), 1)
        self.assertIsInstance(bridge.queries[0], ProjectQuery)
        with self.assertRaises(InvalidContinuityRecord):
            BridgeStatusPort(object())

    # ---------------------------------------------------------- fail-closed
    def test_corrupt_foreign_and_tampered_databases_fail_closed(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"SQLite format 3\0SYNTHETIC DAMAGED DATABASE" * 12)
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        store, _, _ = self.seeded_with_checkpoint()
        tampered = store.path
        with closing(sqlite3.connect(tampered)) as connection:
            connection.execute("INSERT INTO checkpoints (project_id, revision, body, fingerprint, recorded_ms) "
                               "VALUES ('project-1', 2, 'not json', 'x', 1)")
            connection.commit()
        gap = self.root / "gap" / "continuity.sqlite3"
        gap_store = self.store(gap)
        d1 = gap_store.append_directive(Directive("dir-1", "project-1", "instruction", "합성 지시")).record
        gap_store.save_checkpoint(self.checkpoint(d1.seq), 0)
        with closing(sqlite3.connect(gap)) as connection:
            body, digest = connection.execute("SELECT body, fingerprint FROM checkpoints").fetchone()
            connection.execute("INSERT INTO checkpoints (project_id, revision, body, fingerprint, recorded_ms) "
                               "VALUES ('project-1', 3, ?, ?, 1)", (body, digest))
            connection.commit()
        for path in (corrupt, foreign, tampered, gap):
            with self.subTest(source=path.name if path is not gap else "gap"):
                before = path.read_bytes()
                with self.assertRaises(InvalidContinuityDatabase):
                    self.store(path)
                self.assertEqual(path.read_bytes(), before)

    def test_repository_destination_is_rejected(self) -> None:
        project = self.root / "source-project"
        (project / ".git").mkdir(parents=True)
        target = project / "state" / "continuity.sqlite3"
        with self.assertRaises(UnsafeStoragePath):
            self.store(target)
        self.assertFalse(target.parent.exists())
        with self.assertRaises(UnsafeStoragePath):
            ContinuityStore("relative/continuity.sqlite3")


if __name__ == "__main__":
    unittest.main()
