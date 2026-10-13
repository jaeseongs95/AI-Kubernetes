"""AIK-043 read model against disposable SQLite files and synthetic events only.

VM receipts and AGS peer messages are synthetic stand-ins; no VM History or
AGS PEER collector, provider, credential or network is used. The clock is
injected. Passing on Linux is development verification; Windows is NOT_RUN.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_vm_bridge.bridge import BridgeReceipt, JobObservation, JobPhase, VmRefs  # noqa: E402
from aik_vm_bridge.surface import Command  # noqa: E402
from aik_readmodel import (  # noqa: E402
    InvalidEvent, InvalidReadModelDatabase, ReadModel, ScopeMismatch, SourceEvent,
    from_bridge_receipt, from_lease, from_peer_message,
)
from aik_runtime import Lease, Placement, Pool, RuntimeStore, UnsafeStoragePath, Versioned  # noqa: E402


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
SCOPE = "os-user-synthetic-1"
OTHER_SCOPE = "os-user-synthetic-2"


def ev(source, cursor, kind, refs, state=None, *, event_id=None, scope=SCOPE):
    payload = {} if state is None else {"state": state}
    return SourceEvent(source, cursor, event_id or f"{source.replace('_', '-')}-{cursor}", scope, kind, refs, payload)


def placement(number=1):
    return Placement(f"placement-{number}", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                     f"vm-task-{number}", f"vm-attempt-{number}", ags_session_id=f"ags-session-{number}",
                     state="assigned")


def lease(number=1, revision=2, state="active"):
    return Versioned(Lease(f"lease-{number}", f"placement-{number}", "pool-1", "worker-1", 1,
                           "2030-01-01T00:00:00Z", state=state, provider_job_id=f"job-{number}"), revision)


def peer(cursor, state, message="msg-1", session="ags-session-1"):
    return from_peer_message({"event_id": f"peer-event-{cursor}", "state": state, "ags_message_id": message,
                              "ags_peer_id": "peer-1", "ags_session_id": session,
                              "worker_address": "local-peer://worker-1", "worker_id": "worker-1"},
                             cursor, SCOPE)


def dispatch(cursor, state, number=1, **extra):
    refs = {"request_id": f"request-{number}", "lease_id": f"lease-{number}",
            "vm_attempt_id": f"vm-attempt-{number}", **extra}
    return ev("dispatch", cursor, state, refs, state)



def vm_receipt(cursor, *, command=None, status=None, goal="vm-goal-1", task="vm-task-1"):
    """A real aik_vm_bridge BridgeReceipt carrying synthetic VM IDs."""
    refs = VmRefs(project_id="vm-project-1", goal_id=goal, task_id=task, attempt_id="vm-attempt-1",
                  history_cursor=cursor)
    job = None if status is None else JobObservation(JobPhase.RUNNING, status, "vm-job-1")
    return BridgeReceipt(command=command or Command.RUN_ONCE, request_id="bridge-req-1",
                         project_id="vm-project-1", refs=refs, payload=MappingProxyType({}), job=job)

class ReadModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aik-readmodel-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state" / "readmodel-non-authoritative.sqlite3"
        self.model = ReadModel(self.path, user_scope=SCOPE, clock=lambda: NOW)

    def chain_events(self):
        vm = from_bridge_receipt(vm_receipt(1, status="in_progress"), SCOPE)
        return [vm, from_lease(lease(), placement(), SCOPE, cursor=1),
                dispatch(1, "sent_ack", provider_job_id="job-1"), peer(1, "delivered")]

    def test_attempt_links_vm_placement_lease_dispatch_peer_and_worker_address(self):
        report = self.model.apply(self.chain_events())
        self.assertEqual((report.stored, report.applied, report.gaps_opened), (4, 4, 0))
        view = self.model.attempt_view("vm-attempt-1")
        self.assertTrue(view.found)
        self.assertFalse(view.authoritative)
        self.assertFalse(view.stale)
        expected = {
            "vm_project_id": ("vm-project-1",), "vm_goal_id": ("vm-goal-1",), "vm_task_id": ("vm-task-1",),
            "vm_attempt_id": ("vm-attempt-1",), "placement_id": ("placement-1",), "lease_id": ("lease-1",),
            "request_id": ("request-1",), "provider_job_id": ("job-1",), "ags_session_id": ("ags-session-1",),
            "ags_peer_id": ("peer-1",), "ags_message_id": ("msg-1",),
            "worker_address": ("local-peer://worker-1",), "worker_id": ("worker-1",),
        }
        self.assertEqual(dict(view.ids), expected)
        self.assertEqual(view.states["vm"].state, "in_progress")
        self.assertEqual(view.states["aik_runtime"].state, "active")
        self.assertEqual(view.states["dispatch"].kind, "sent_ack")
        self.assertEqual(view.states["ags_peer"].state, "delivered")

    def test_shared_worker_does_not_merge_another_attempt(self):
        self.model.apply(self.chain_events())
        self.model.apply([from_lease(lease(2), placement(2), SCOPE, cursor=2)])
        view = self.model.attempt_view("vm-attempt-1")
        self.assertEqual(view.ids["lease_id"], ("lease-1",))
        self.assertEqual(view.ids["vm_attempt_id"], ("vm-attempt-1",))
        other = self.model.attempt_view("vm-attempt-2")
        self.assertEqual(other.ids["lease_id"], ("lease-2",))
        self.assertFalse(self.model.attempt_view("vm-attempt-missing").found)

    def test_duplicate_is_ignored_and_conflict_keeps_existing_projection(self):
        first = dispatch(1, "reserved")
        self.model.apply([first])
        digest = self.model.projection_digest()
        report = self.model.apply([first])
        self.assertEqual((report.duplicates, report.stored, report.applied), (1, 0, 0))
        forged = dispatch(1, "send_unknown", number=9)
        report = self.model.apply([forged])
        self.assertEqual((report.conflicts, report.stored), (1, 0))
        reused = ev("dispatch", 2, "started", {"lease_id": "lease-1"}, event_id="dispatch-1")
        self.assertEqual(self.model.apply([reused]).conflicts, 1)
        self.assertEqual(self.model.projection_digest(), digest)
        conflicts = self.model.conflicts()
        self.assertEqual([item.code for item in conflicts], ["CURSOR_CONFLICT", "EVENT_ID_CONFLICT"])
        self.assertEqual((conflicts[0].existing_digest, conflicts[0].observed_digest), (first.digest, forged.digest))
        self.assertEqual(self.model.attempt_view("vm-attempt-1").states["dispatch"].kind, "reserved")
        self.assertEqual(self.model.cursor_status()["dispatch"].conflicts, 2)

    def test_out_of_order_cursor_opens_gap_then_fills_and_applies(self):
        self.model.apply([dispatch(1, "reserved")])
        report = self.model.apply([dispatch(3, "started", provider_job_id="job-3")])
        self.assertEqual((report.stored, report.applied, report.gaps_opened), (1, 0, 1))
        status = self.model.cursor_status()["dispatch"]
        self.assertEqual((status.applied_cursor, status.max_seen_cursor, status.open_gaps), (1, 3, ((2, 2),)))
        self.assertTrue(status.stale)
        view = self.model.attempt_view("vm-attempt-1")
        self.assertTrue(view.stale)
        self.assertNotIn("provider_job_id", view.ids)
        report = self.model.apply([dispatch(2, "sent_ack")])
        self.assertEqual((report.applied, report.gaps_closed, report.gaps_opened), (2, 1, 0))
        status = self.model.cursor_status()["dispatch"]
        self.assertEqual((status.applied_cursor, status.open_gaps, status.stale), (3, (), False))
        view = self.model.attempt_view("vm-attempt-1")
        self.assertFalse(view.stale)
        self.assertEqual(view.ids["provider_job_id"], ("job-3",))
        self.assertEqual((view.states["dispatch"].cursor, view.states["dispatch"].kind), (3, "started"))

    def test_partial_fill_splits_a_gap(self):
        self.model.apply([dispatch(1, "reserved"), dispatch(5, "started")])
        report = self.model.apply([dispatch(3, "sent_ack")])
        self.assertEqual((report.applied, report.gaps_closed, report.gaps_opened), (0, 1, 2))
        self.assertEqual(self.model.cursor_status()["dispatch"].open_gaps, ((2, 2), (4, 4)))

    def test_other_user_scope_is_rejected_without_recording(self):
        with self.assertRaises(ScopeMismatch):
            self.model.apply([dispatch(1, "reserved"), ev("dispatch", 2, "sent_ack", {}, scope=OTHER_SCOPE)])
        self.assertEqual(dict(self.model.cursor_status()), {})
        with self.assertRaises(ScopeMismatch):
            ReadModel(self.path, user_scope=OTHER_SCOPE)

    def test_rebuild_reproduces_projection_and_event_log_is_append_only(self):
        events = self.chain_events() + [peer(3, "acked"), dispatch(2, "started")]
        self.model.apply(events)
        before = self.model.projection_digest()
        self.assertEqual(self.model.rebuild(), before)
        self.assertEqual(ReadModel(self.path, user_scope=SCOPE).projection_digest(), before)
        ordered = ReadModel(self.root / "ordered" / "readmodel.sqlite3", user_scope=SCOPE)
        ordered.apply(sorted(events, key=lambda item: (item.source, item.cursor)))
        self.assertEqual(ordered.projection_digest(), before)
        with closing(sqlite3.connect(self.path)) as connection:
            for statement in ('UPDATE "event_log" SET "digest" = \'x\'', 'DELETE FROM "event_log"',
                              'DELETE FROM "meta"', 'UPDATE "meta" SET "value" = \'other\''):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(statement)
            connection.execute('DELETE FROM "links"')
            connection.commit()
        self.assertNotEqual(self.model.projection_digest(), before)
        self.assertEqual(self.model.rebuild(), before)

    def test_peer_delivery_states_are_kept_in_source_cursor_order(self):
        self.model.apply([peer(1, "delivered"), peer(3, "acked"), peer(4, "result")])
        view = self.model.peer_view("peer-1")
        self.assertEqual([item.state for item in view.history], ["delivered"])
        self.assertTrue(view.stale)
        self.model.apply([peer(2, "claimed")])
        view = self.model.peer_view("peer-1")
        self.assertFalse(view.stale)
        self.assertFalse(view.authoritative)
        self.assertEqual([(item.cursor, item.state) for item in view.history],
                         [(1, "delivered"), (2, "claimed"), (3, "acked"), (4, "result")])
        self.assertEqual([(item.ags_message_id, item.state) for item in view.deliveries], [("msg-1", "result")])
        self.assertEqual(view.worker_addresses, ("local-peer://worker-1",))
        self.assertEqual(view.ags_session_ids, ("ags-session-1",))
        self.assertFalse(self.model.peer_view("peer-unknown").found)

    def test_adapters_copy_values_and_reject_invalid_input(self):
        self.assertIsNone(from_bridge_receipt(vm_receipt(None), SCOPE))
        with self.assertRaises(InvalidEvent):
            from_bridge_receipt(vm_receipt(True), SCOPE)
        with self.assertRaises(InvalidEvent):
            from_bridge_receipt(SimpleNamespace(history_cursor=7, attempt_id="vm-attempt-1"), SCOPE)
        vm = from_bridge_receipt(vm_receipt(7, command=Command.STATUS, goal=None, task=None), SCOPE)
        self.assertEqual((vm.source, vm.cursor, vm.kind, dict(vm.refs), vm.state),
                         ("vm", 7, "status", {"vm_project_id": "vm-project-1", "vm_attempt_id": "vm-attempt-1"},
                          None))
        with self.assertRaises(InvalidEvent):
            from_lease(lease(), placement(2), SCOPE, cursor=1)
        with self.assertRaises(InvalidEvent):
            from_lease(lease().value, placement(), SCOPE, cursor=1)
        runtime = from_lease(lease(), placement(), SCOPE, cursor=4)
        self.assertEqual((runtime.payload["lease_revision"], runtime.state), (2, "active"))
        with self.assertRaises(InvalidEvent):
            from_peer_message({"event_id": "e-1", "state": "delivered", "ags_message_id": "m",
                               "ags_peer_id": "p", "body": "synthetic message body"}, 1, SCOPE)
        with self.assertRaises(InvalidEvent):
            from_peer_message({"event_id": "e-1", "state": "completed", "ags_message_id": "m",
                               "ags_peer_id": "p"}, 1, SCOPE)
        with self.assertRaises(InvalidEvent):
            ev("dispatch", 1, "reserved", {"raw_secret": "x"})
        with self.assertRaises(InvalidEvent):
            ev("dispatch", 0, "reserved", {})
        self.assertEqual(dispatch(1, "reserved").digest, dispatch(1, "reserved").digest)
        self.assertNotEqual(dispatch(1, "reserved").digest, dispatch(1, "sent_ack").digest)

    def test_foreign_database_and_relative_path_are_rejected(self):
        runtime_path = self.root / "runtime" / "runtime.sqlite3"
        RuntimeStore(runtime_path).save(Pool("pool-1", "synthetic", "connection-1"), expected_revision=0)
        before = runtime_path.read_bytes()
        with self.assertRaises(InvalidReadModelDatabase):
            ReadModel(runtime_path, user_scope=SCOPE)
        self.assertEqual(runtime_path.read_bytes(), before)
        with self.assertRaises(UnsafeStoragePath):
            ReadModel("relative-readmodel.sqlite3", user_scope=SCOPE)


if __name__ == "__main__":
    unittest.main()
