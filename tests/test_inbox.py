"""Inbox/outbox/wake tests on disposable SQLite files with synthetic data only.

No provider, credential, network or model is used. The restart test kills an
actual child process holding a lease, and the hot-journal test kills a child
inside an uncommitted write transaction. Linux passing is development
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
INBOX_CRASH_WRITER = r'''
from pathlib import Path
import sqlite3
import sys
import time

# Fault harness only: a raw writer forces uncommitted pages to spill to disk
# so that its death leaves a hot rollback journal beside the inbox file.
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA synchronous=FULL")
connection.execute("PRAGMA cache_size=4")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
for number in range(512):
    connection.execute(
        "INSERT INTO inbox (message_id, goal_id, source, event_cursor, kind, payload, fingerprint, "
        "order_status, state, attempts, available_ms, received_ms) "
        "VALUES (?, 'goal-1', 'vm-events', ?, 'system', ?, 'synthetic', 'in_order', 'received', 0, 0, 0)",
        (f"msg-uncommitted-{number:04d}", number + 100, '{"summary":"' + "x" * 200 + '"}'),
    )
connection.execute("UPDATE meta SET value = value + 1000 WHERE name = 'change_counter'")
Path(sys.argv[2]).write_text("inserts reached; COMMIT has not run", encoding="utf-8")
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

    def test_same_source_cursor_and_content_with_new_message_id_is_duplicate(self) -> None:
        store = self.store()
        calls = []
        policy = WakePolicy(store, calls.append)
        payload = {"status_code": "synthetic"}
        first = InboxMessage("m-a", "goal-1", "vm-events", 5, "execution_result", payload)
        self.assertEqual(store.receive(first).status, "accepted")
        self.assertIsNotNone(policy.poll())
        retried = InboxMessage("m-b", "goal-1", "vm-events", 5, "execution_result", dict(payload))
        result = store.receive(retried)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(result.record.message, first)
        self.assertIsNone(store.get("m-b"))
        self.assertIsNone(policy.poll())
        leased = store.lease_next("director")
        self.assertEqual(leased.record.message.message_id, "m-a")
        store.ack("m-a", leased.lease_token)
        after_ack = store.receive(retried)
        self.assertEqual((after_ack.status, after_ack.record.state), ("duplicate", "acked"))
        self.assertIsNone(store.lease_next("director"))
        self.assertEqual(len(calls), 1)
        # A different event that reuses the cursor is still stored and flagged.
        changed = InboxMessage("m-c", "goal-1", "vm-events", 5, "execution_result", {"status_code": "different"})
        self.assertEqual(store.receive(changed).record.order, "out_of_order")
        self.assertEqual([entry.message.message_id for entry in store.list_messages()], ["m-a", "m-c"])

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

    def test_fail_and_ack_reject_wrong_expired_or_non_leased_tokens(self) -> None:
        store = self.store()
        store.receive(message("msg-target", 1))
        with self.assertRaises(LeaseLost):
            store.ack("msg-target", "0" * 32)
        with self.assertRaises(LeaseLost):
            store.fail("msg-target", "0" * 32, "synthetic_failure")
        with self.assertRaises(LeaseLost):
            store.ack("msg-absent", "0" * 32)
        self.assertEqual(store.get("msg-target").state, "received")

        leased = store.lease_next("director", lease_seconds=10)
        with self.assertRaises(LeaseLost):
            store.fail("msg-target", "f" * 32, "synthetic_failure")
        with self.assertRaises(LeaseLost):
            store.fail("msg-target", "not-a-token", "synthetic_failure")
        self.assertEqual(store.get("msg-target").state, "leased")
        self.clock.advance(11)
        with self.assertRaises(LeaseLost):
            store.fail("msg-target", leased.lease_token, "synthetic_failure")
        with self.assertRaises(LeaseLost):
            store.ack("msg-target", leased.lease_token)

        renewed = store.lease_next("director")
        self.assertEqual(store.fail("msg-target", renewed.lease_token, "invalid_input", permanent=True).state, "dead")
        with self.assertRaises(LeaseLost):
            store.ack("msg-target", renewed.lease_token)
        with self.assertRaises(LeaseLost):
            store.fail("msg-target", renewed.lease_token, "synthetic_failure")
        self.assertEqual(store.get("msg-target").state, "dead")
        self.assertEqual(store.pending_outbox(), ())

        store.receive(message("msg-acked", 2))
        acked = store.lease_next("director")
        store.ack("msg-acked", acked.lease_token)
        with self.assertRaises(LeaseLost):
            store.ack("msg-acked", "e" * 32)

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

    def test_process_death_mid_transaction_rolls_back_hot_journal_on_reopen(self) -> None:
        store = self.store()
        committed = message("msg-committed", 1)
        store.receive(committed)
        committed_bytes = self.database.read_bytes()
        marker = self.root / "inbox-transaction.marker"
        process = subprocess.Popen(
            [sys.executable, "-c", INBOX_CRASH_WRITER, str(self.database), str(marker)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.communicate(timeout=5)))
        deadline = time.monotonic() + 10
        while not marker.exists():
            if process.poll() is not None:
                self.fail("Writer exited before its uncommitted writes: " + "".join(process.communicate(timeout=5)))
            if time.monotonic() >= deadline:
                self.fail("Writer did not reach the pre-COMMIT marker within ten seconds.")
            time.sleep(0.01)
        journal = self.database.with_name(self.database.name + "-journal")
        self.assertTrue(journal.is_file(), "The crash harness did not produce a rollback journal.")
        self.assertGreater(journal.stat().st_size, 512)
        self.assertNotEqual(self.database.read_bytes(), committed_bytes, "No uncommitted pages reached disk.")
        process.kill()
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)

        reopened = self.store()
        self.assertFalse(journal.exists())
        self.assertEqual([entry.message.message_id for entry in reopened.list_messages(limit=1000)],
                         ["msg-committed"])
        self.assertEqual(reopened.get("msg-committed").message, committed)
        self.assertEqual(reopened.receive(message("msg-after", 2)).status, "accepted")
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

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

    def test_wake_does_not_signal_for_expired_lease_at_max_attempts(self) -> None:
        store = self.store(max_attempts=1)
        calls = []
        policy = WakePolicy(store, calls.append)
        store.receive(message("msg-last", 1))
        self.assertIsNotNone(policy.poll())
        leased = store.lease_next("director", lease_seconds=10)
        self.assertEqual(leased.record.attempts, 1)
        self.clock.advance(11)
        self.assertIsNone(policy.poll())
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.get("msg-last").state, "dead")
        self.assertIsNone(store.lease_next("director"))
        self.assertIsNone(policy.poll())
        self.assertEqual(len(calls), 1)

    def test_wake_signals_once_for_reclaimable_expired_lease(self) -> None:
        store = self.store()
        calls = []
        policy = WakePolicy(store, calls.append)
        store.receive(message("msg-reclaim", 1))
        self.assertIsNotNone(policy.poll())
        store.lease_next("director", lease_seconds=10)
        self.assertIsNone(policy.poll())
        self.clock.advance(11)
        self.assertEqual(policy.poll().available, 1)
        for _ in range(20):
            self.assertIsNone(policy.poll())
        self.assertEqual(len(calls), 2)
        self.assertEqual(store.get("msg-reclaim").state, "received")

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

    def test_failing_wake_callback_is_retried_no_faster_than_min_interval(self) -> None:
        store = self.store()
        attempts = []
        behaviour = {"raise": True}

        def wake(signal) -> None:
            attempts.append(signal.available)
            if behaviour["raise"]:
                raise RuntimeError("synthetic callback failure")

        policy = WakePolicy(store, wake, min_interval=10)
        store.receive(message("msg-1", 1))
        with self.assertRaises(RuntimeError):
            policy.poll()
        for _ in range(50):
            self.assertIsNone(policy.poll())
        self.assertEqual(len(attempts), 1)
        self.clock.advance(10)
        with self.assertRaises(RuntimeError):
            policy.poll()
        self.assertEqual(len(attempts), 2)
        behaviour["raise"] = False
        self.clock.advance(10)
        self.assertEqual(policy.poll().available, 1)
        self.clock.advance(100)
        self.assertIsNone(policy.poll())
        self.assertEqual(len(attempts), 3)

    def test_wake_reset_signals_an_unchanged_queue_again(self) -> None:
        store = self.store()
        calls = []
        policy = WakePolicy(store, calls.append)
        store.receive(message("msg-1", 1))
        self.assertIsNotNone(policy.poll())
        self.assertIsNone(policy.poll())
        policy.reset()
        self.assertEqual(policy.poll().available, 1)
        self.assertIsNone(policy.poll())
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

    def test_repeated_ack_cannot_add_new_outbox_items(self) -> None:
        store = self.store()
        store.receive(message("msg-1", 1, kind="user_instruction", source="gui"))
        leased = store.lease_next("director")
        request = OutboxMessage("out-1", "goal-1", "vm_request", {"action": "synthetic follow-up"})
        store.ack("msg-1", leased.lease_token, [request])
        # Repeating the identical ACK remains idempotent.
        self.assertEqual(store.ack("msg-1", leased.lease_token, [request]).outbox_ids, ("out-1",))
        self.assertEqual(store.ack("msg-1", leased.lease_token).outbox_ids, ())
        extra = OutboxMessage("out-2", "goal-1", "vm_request", {"action": "synthetic duplicate"})
        for outbox in ([extra], [request, extra]):
            with self.subTest(items=len(outbox)):
                with self.assertRaises(MessageConflict):
                    store.ack("msg-1", leased.lease_token, outbox)
                self.assertIsNone(store.get_outbox("out-2"))
        self.assertEqual([item.message.outbox_id for item in store.pending_outbox()], ["out-1"])
        self.assertEqual(store.get("msg-1").state, "acked")

    def test_out_of_range_after_seq_is_classified_invalid(self) -> None:
        store = self.store()
        for call in (store.list_messages, store.pending_outbox):
            for value in (2**63, 2**70):
                with self.subTest(call=call.__name__, value=value):
                    with self.assertRaises(InvalidMessage):
                        call(after_seq=value)
        self.assertEqual(store.list_messages(after_seq=2**63 - 1), ())
        self.assertEqual(store.pending_outbox(after_seq=2**63 - 1), ())

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
