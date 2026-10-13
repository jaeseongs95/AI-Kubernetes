"""Inbox/outbox/wake tests on disposable SQLite files with synthetic data only.

No provider, credential, network or model is used. The restart test kills an
actual child process holding a lease. Linux passing is development
verification, not evidence of Windows support.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_inbox import (  # noqa: E402
    APPLICATION_ID, Consumer, InboxMessage, InboxStore, InvalidInboxDatabase, InvalidMessage,
    LeaseLost, MessageConflict, OutboxMessage, WakePolicy,
)
from aik_runtime import RuntimeStore, StorageBusy, UnsafeStoragePath  # noqa: E402
from aik_runtime import paths  # noqa: E402


SECRET = "SYNTHETIC_SECRET_MUST_NOT_BE_STORED"
LEASE_HOLDER = r'''
from pathlib import Path
import sys
import time
sys.path.insert(0, sys.argv[1])
from aik_inbox import InboxMessage, InboxStore

store = InboxStore(Path(sys.argv[2]))
store.receive(InboxMessage("msg-crash", "goal-1", "vm-events", 1, "execution_result", {"status_code": "synthetic"}))
leased = store.lease_next("director-old", lease_seconds=60)
assert leased is not None
Path(sys.argv[3]).write_text("leased; ACK has not run", encoding="utf-8")
time.sleep(30)
'''


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def message(message_id: str, cursor: int, kind: str = "execution_result", goal: str = "goal-1",
            source: str = "vm-events", payload: dict | None = None) -> InboxMessage:
    return InboxMessage(message_id, goal, source, cursor, kind,
                        payload if payload is not None else {"summary": f"synthetic {message_id}"})


class InboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-inbox-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "사용자 공간" / "inbox data.sqlite3"
        self.clock = FakeClock()

    def store(self, **options) -> InboxStore:
        return InboxStore(self.database, clock=options.pop("clock", self.clock), **options)

    def test_messages_persist_with_ids_cursor_and_survive_reopen(self) -> None:
        store = self.store()
        original = message("msg-1", 7, kind="user_instruction", source="gui")
        result = store.receive(original)
        self.assertEqual(result.status, "accepted")
        self.assertEqual((result.record.state, result.record.order, result.record.seq), ("received", "in_order", 1))
        reopened = self.store()
        stored = reopened.get("msg-1")
        self.assertEqual(stored.message, original)
        self.assertEqual((stored.message.goal_id, stored.message.event_cursor), ("goal-1", 7))
        self.assertEqual([entry.message.message_id for entry in reopened.list_messages()], ["msg-1"])

    def test_duplicate_message_id_is_idempotent_even_after_ack_and_conflicts_are_rejected(self) -> None:
        store = self.store()
        first = message("msg-dup", 1)
        store.receive(first)
        self.assertEqual(store.receive(first).status, "duplicate")
        leased = store.lease_next("director")
        store.ack("msg-dup", leased.lease_token)
        again = store.receive(first)
        self.assertEqual((again.status, again.record.state), ("duplicate", "acked"))
        self.assertIsNone(store.lease_next("director"))
        with self.assertRaises(MessageConflict):
            store.receive(message("msg-dup", 2, payload={"summary": "different"}))
        self.assertEqual(store.get("msg-dup").message, first)
        self.assertEqual(len(store.list_messages()), 1)

    def test_out_of_order_cursor_is_detected_per_source_and_still_delivered(self) -> None:
        store = self.store()
        self.assertEqual(store.receive(message("vm-1", 1)).record.order, "in_order")
        self.assertEqual(store.receive(message("vm-3", 3)).record.order, "in_order")
        self.assertEqual(store.receive(message("vm-2", 2)).record.order, "out_of_order")
        self.assertEqual(store.receive(message("vm-3b", 3)).record.order, "out_of_order")
        self.assertEqual(store.receive(message("ags-1", 1, source="ags-events")).record.order, "in_order")
        self.assertEqual(store.receive(message("vm-4", 4)).record.order, "in_order")
        consumer = Consumer(store, "director")
        delivered = []
        while (leased := consumer.next()) is not None:
            delivered.append((leased.record.message.message_id, leased.record.order))
            consumer.ack(leased)
        self.assertEqual(delivered, [("vm-1", "in_order"), ("vm-3", "in_order"), ("vm-2", "out_of_order"),
                                     ("vm-3b", "out_of_order"), ("ags-1", "in_order"), ("vm-4", "in_order")])

    def test_lease_excludes_others_and_ack_is_not_goal_completion(self) -> None:
        store = self.store()
        store.receive(message("msg-1", 1))
        leased = store.lease_next("director-a", lease_seconds=30)
        self.assertEqual((leased.record.state, leased.record.attempts, leased.record.lease_owner),
                         ("leased", 1, "director-a"))
        self.assertIsNone(store.lease_next("director-b"))
        with self.assertRaises(LeaseLost):
            store.ack("msg-1", "0" * 32)
        result = store.ack("msg-1", leased.lease_token)
        self.assertEqual(result.record.state, "acked")
        self.assertEqual(result.goal_completion, "not_determined")
        self.assertEqual(store.ack("msg-1", leased.lease_token).record.state, "acked")
        self.assertIsNone(store.lease_next("director-b"))

    def test_expired_lease_cannot_ack_and_is_reclaimed_by_next_lease(self) -> None:
        store = self.store()
        store.receive(message("msg-1", 1))
        old = store.lease_next("director-a", lease_seconds=10)
        self.clock.advance(11)
        with self.assertRaises(LeaseLost):
            store.ack("msg-1", old.lease_token)
        new = store.lease_next("director-b")
        self.assertEqual((new.record.message.message_id, new.record.attempts), ("msg-1", 2))
        self.assertEqual(new.record.last_error, "lease_expired")
        with self.assertRaises(LeaseLost):
            store.ack("msg-1", old.lease_token)
        self.assertEqual(store.ack("msg-1", new.lease_token).record.state, "acked")

    def test_restart_reclaims_expired_leases_and_optionally_all_leases(self) -> None:
        store = self.store()
        store.receive(message("msg-short", 1))
        store.receive(message("msg-long", 2))
        store.lease_next("director-old", lease_seconds=30)
        store.lease_next("director-old", lease_seconds=300)
        self.clock.advance(31)
        restarted = self.store()
        self.assertEqual(restarted.last_recovery.reclaimed, ("msg-short",))
        self.assertEqual(restarted.get("msg-long").state, "leased")
        sole_owner = self.store(reclaim_all_leases=True)
        self.assertEqual(sole_owner.last_recovery.reclaimed, ("msg-long",))
        self.assertEqual([entry.message.message_id for entry in sole_owner.list_messages("received")],
                         ["msg-short", "msg-long"])

    def test_actual_process_kill_then_restart_recovers_unacked_item(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        marker = self.root / "leased.marker"
        process = subprocess.Popen(
            [sys.executable, "-c", LEASE_HOLDER, str(ROOT / "src"), str(self.database), str(marker)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.communicate(timeout=5)))
        deadline = time.monotonic() + 10
        while not marker.exists():
            if process.poll() is not None:
                self.fail("Lease holder exited early: " + "".join(process.communicate(timeout=5)))
            if time.monotonic() >= deadline:
                self.fail("Lease holder did not reach its marker within ten seconds.")
            time.sleep(0.01)
        process.kill()
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)

        unexpired = InboxStore(self.database)
        self.assertEqual(unexpired.last_recovery.reclaimed, ())
        self.assertIsNone(unexpired.lease_next("director-new"))
        later = InboxStore(self.database, clock=lambda: time.time() + 120)
        self.assertEqual(later.last_recovery.reclaimed, ("msg-crash",))
        leased = later.lease_next("director-new")
        self.assertEqual((leased.record.message.message_id, leased.record.attempts), ("msg-crash", 2))
        self.assertEqual(later.ack("msg-crash", leased.lease_token).record.state, "acked")

    def test_failures_retry_with_backoff_and_become_dead_after_max_attempts(self) -> None:
        store = self.store(max_attempts=2)
        store.receive(message("msg-retry", 1))
        first = store.lease_next("director")
        failed = store.fail("msg-retry", first.lease_token, "synthetic_failure", retry_after=5)
        self.assertEqual((failed.state, failed.last_error), ("failed", "synthetic_failure"))
        self.assertIsNone(store.lease_next("director"))
        self.clock.advance(5)
        second = store.lease_next("director")
        self.assertEqual(second.record.attempts, 2)
        self.assertEqual(store.fail("msg-retry", second.lease_token, "synthetic_failure").state, "dead")
        self.assertIsNone(store.lease_next("director"))

        store.receive(message("msg-permanent", 2))
        leased = store.lease_next("director")
        self.assertEqual(store.fail("msg-permanent", leased.lease_token, "invalid_input", permanent=True).state, "dead")

        store.receive(message("msg-expire", 3))
        store.lease_next("director", lease_seconds=1)
        self.clock.advance(2)
        self.assertEqual(store.lease_next("director", lease_seconds=1).record.attempts, 2)
        self.clock.advance(2)
        self.assertEqual(store.recover().dead, ("msg-expire",))
        self.assertEqual(store.get("msg-expire").state, "dead")

    def test_wake_only_when_unprocessed_items_exist_and_never_on_idle_queue(self) -> None:
        store = self.store()
        calls = []
        policy = WakePolicy(store, calls.append)
        for _ in range(1000):
            self.assertIsNone(policy.poll())
        self.assertEqual(calls, [])
        for cursor, kind in enumerate(("user_instruction", "execution_result", "error"), start=1):
            item = message(f"msg-{kind}", cursor, kind=kind, source="mixed-events")
            store.receive(item)
            signal = policy.poll()
            self.assertIsNotNone(signal)
            self.assertEqual(dict(signal.kinds), {kind: 1})
            self.assertEqual(store.receive(item).status, "duplicate")
            for _ in range(50):
                self.assertIsNone(policy.poll())
            leased = store.lease_next("director")
            self.assertIsNone(policy.poll())
            store.ack(leased.record.message.message_id, leased.lease_token)
            self.assertIsNone(policy.poll())
        self.assertEqual(len(calls), 3)

        store.receive(message("msg-retry", 10, kind="error", source="mixed-events"))
        self.assertIsNotNone(policy.poll())
        leased = store.lease_next("director")
        store.fail("msg-retry", leased.lease_token, "synthetic_failure", retry_after=5)
        self.assertIsNone(policy.poll())
        self.clock.advance(5)
        self.assertIsNotNone(policy.poll())
        for _ in range(100):
            self.assertIsNone(policy.poll())
        self.assertEqual(len(calls), 5)

    def test_wake_min_interval_rate_limits_signals(self) -> None:
        store = self.store()
        calls = []
        policy = WakePolicy(store, calls.append, min_interval=10)
        store.receive(message("msg-1", 1))
        self.assertIsNotNone(policy.poll())
        store.receive(message("msg-2", 2))
        self.assertIsNone(policy.poll())
        self.clock.advance(10)
        self.assertEqual(policy.poll().available, 2)
        self.assertEqual(len(calls), 2)

    def test_ack_with_outbox_is_atomic_and_delivery_is_not_goal_completion(self) -> None:
        store = self.store()
        store.receive(message("msg-1", 1, kind="user_instruction", source="gui"))
        leased = store.lease_next("director")
        request = OutboxMessage("out-1", "goal-1", "vm_request", {"action": "synthetic follow-up"})
        result = store.ack("msg-1", leased.lease_token, [request])
        self.assertEqual((result.outbox_ids, result.goal_completion), (("out-1",), "not_determined"))
        pending = store.pending_outbox()
        self.assertEqual([(item.message, item.source_message_id, item.state) for item in pending],
                         [(request, "msg-1", "pending")])
        self.assertEqual(store.mark_delivered("out-1").state, "delivered")
        self.assertEqual(store.pending_outbox(), ())
        self.assertEqual(store.get("msg-1").state, "acked")

        store.receive(message("msg-2", 2, kind="user_instruction", source="gui"))
        second = store.lease_next("director")
        conflicting = OutboxMessage("out-1", "goal-1", "vm_request", {"action": "different"})
        with self.assertRaises(MessageConflict):
            store.ack("msg-2", second.lease_token, [OutboxMessage("out-2", "goal-1", "user_notice"), conflicting])
        self.assertEqual(store.get("msg-2").state, "leased")
        self.assertIsNone(store.get_outbox("out-2"))
        self.assertEqual(store.get_outbox("out-1").message, request)

    def test_payload_contract_rejects_secrets_oversize_and_non_json(self) -> None:
        store = self.store()
        nested: dict = {}
        cursor = nested
        for _ in range(20):
            cursor["level"] = {}
            cursor = cursor["level"]
        payloads = (
            {"api_key": SECRET}, {"nested": {"Password": SECRET}}, {"session_token": SECRET},
            {"note": "Bearer " + SECRET}, {"blob": "x" * 20000}, {"value": float("nan")},
            {"value": b"bytes"}, {"value": {1, 2}}, {"value": 2**70}, nested, [SECRET],
        )
        for number, payload in enumerate(payloads):
            with self.subTest(case=number):
                with self.assertRaises(InvalidMessage) as rejected:
                    InboxMessage(f"msg-{number}", "goal-1", "vm-events", 1, "system", payload)
                self.assertNotIn(SECRET, str(rejected.exception))
        for arguments in (("msg-x", "goal-1", "vm", 0, "system"), ("msg-x", "goal-1", "vm", True, "system"),
                          ("msg-x", "goal-1", "vm", 1, "goal_completed"), ("../msg", "goal-1", "vm", 1, "system")):
            with self.subTest(arguments=arguments[3:]):
                with self.assertRaises(InvalidMessage):
                    InboxMessage(*arguments)
        forged = message("msg-forged", 1)
        object.__setattr__(forged, "payload", {"api_key": SECRET})
        with self.assertRaises(InvalidMessage):
            store.receive(forged)
        self.assertEqual(store.list_messages(), ())
        self.assertNotIn(SECRET.encode("ascii"), self.database.read_bytes())

    def test_paths_reject_ledger_repository_relative_and_foreign_databases(self) -> None:
        for path in (self.root / "backlog.sqlite3", Path("relative/inbox.sqlite3")):
            with self.subTest(path=str(path)):
                with self.assertRaises(UnsafeStoragePath):
                    InboxStore(path)
        project = self.root / "project"
        (project / ".git").mkdir(parents=True)
        with self.assertRaises(UnsafeStoragePath):
            InboxStore(project / "state" / "inbox.sqlite3")
        self.assertFalse((project / "state").exists())

        runtime = self.root / "runtime.sqlite3"
        RuntimeStore(runtime)
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"SQLite format 3\0SYNTHETIC DAMAGED DATABASE" * 12)
        empty = self.root / "empty.sqlite3"
        empty.write_bytes(b"")
        for path in (runtime, corrupt, empty):
            with self.subTest(source=path.name):
                before = path.read_bytes()
                with self.assertRaises(InvalidInboxDatabase):
                    InboxStore(path)
                self.assertEqual(path.read_bytes(), before)

    def test_application_id_is_distinct_and_default_location_is_per_user(self) -> None:
        self.assertNotIn(APPLICATION_ID, (0x41494B52, 0x41494B43))
        self.store()
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
        windows_base, development_base = self.root / "windows-profile", self.root / "xdg-state"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(windows_base), "XDG_STATE_HOME": str(development_base)}):
            store = InboxStore(clock=self.clock)
        expected = (windows_base / "AIK/runtime/inbox.sqlite3" if os.name == "nt"
                    else development_base / "aik/inbox.sqlite3")
        self.assertEqual(store.path, expected.resolve())
        self.assertTrue(expected.is_file())

    def test_held_storage_lock_times_out_without_writes(self) -> None:
        store = self.store(timeout=0.05)
        with paths.database_lock(store.path, 1.0):
            with self.assertRaises(StorageBusy):
                store.receive(message("msg-busy", 1))
        self.assertIsNone(store.get("msg-busy"))
        self.assertEqual(store.receive(message("msg-busy", 1)).status, "accepted")


if __name__ == "__main__":
    unittest.main()
