"""PEER bridge tests with a fake AGS runner, plus one optional real-CLI contract class.

Fake tests use synthetic sessions and bodies only. The real contract class runs
only when an AGS checkout and node v24+ are present; otherwise it is skipped
with a NOT_RUN reason. Linux results are development verification only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_readmodel import ReadModel, from_peer_message  # noqa: E402
from aik_ags_peer import (  # noqa: E402
    AddressBook, AgsPeerError, AgsSessionCli, PeerBridge, decode, encode,
)
from aik_ags_peer.cli import STATE_DIR_ENV  # noqa: E402
from aik_ags_peer.envelope import MAX_BODY_BYTES  # noqa: E402


MARKER = "SYNTHETIC_PEER_BODY_MARKER_7f3c"
SECRET_ERROR = "SYNTHETIC_AGS_ERROR_DETAIL_MUST_NOT_LEAK"
T0 = "2030-01-01T00:00:00.000Z"
T1 = "2030-01-01T00:10:00.000Z"
ORCH = ("codex", "orchestrator-session-1")
WORKER = ("codex", "worker-session-1")
PEER_KEYS = {"event_id", "state", "ags_message_id", "ags_peer_id", "ags_session_id",
             "worker_address", "worker_id", "vm_attempt_id"}


def doc(identity):
    return {"host": identity[0], "sessionId": identity[1]}


class FakeResult:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def ok(data):
    return FakeResult(0, (json.dumps({"protocolVersion": "1.0.0", "ok": True, "data": data}) + "\n").encode())


def reject(text):
    return FakeResult(1, (json.dumps({"protocolVersion": "1.0.0", "ok": False, "error": text}) + "\n").encode())


class FakeAgs:
    """In-memory imitation of the observed AGS CLI contract (not AGS itself)."""

    def __init__(self):
        self.calls = []
        self.messages = {}
        self.faults = {}
        self.before_send = None
        self.counter = 0
        self.version = "v24.4.1"

    def operations(self):
        return [json.loads(call["input"])["operation"] for call in self.calls if call["input"]]

    def _new_id(self):
        self.counter += 1
        return f"00000000-0000-4000-8000-{self.counter:012d}"

    def inject(self, sender, target, body):
        message_id = self._new_id()
        self.messages[message_id] = {"sender": doc(sender), "target": doc(target), "body": body, "sent": True,
                                     "claimedAt": None, "acknowledgedAt": None, "firstDeliveredAt": None,
                                     "attempts": 0, "forgotten": False}
        return message_id

    def __call__(self, argv, *, input, env, timeout):
        self.calls.append({"argv": tuple(argv), "input": input, "env": dict(env), "timeout": timeout})
        if list(argv[1:]) == ["--version"]:
            return FakeResult(0, (self.version + "\n").encode())
        request = json.loads(input.decode("utf-8"))
        operation, payload = request["operation"], request["payload"]
        fault = self.faults.pop(operation, None)
        if isinstance(fault, BaseException):
            raise fault
        if fault is not None:
            return fault
        if operation == "prepare":
            if not set(payload) <= {"sender", "target", "body", "ttlSeconds"}:
                return reject("unexpected key")
            message_id = self._new_id()
            self.messages[message_id] = {"sender": payload["sender"], "target": payload["target"],
                                         "body": payload["body"], "sent": False, "claimedAt": None,
                                         "acknowledgedAt": None, "firstDeliveredAt": None, "attempts": 0,
                                         "forgotten": False}
            return ok({"messageId": message_id, "preparedAt": T0, "expiresAt": T1})
        if operation == "send":
            if self.before_send:
                self.before_send(payload["messageId"])
            message = self.messages.get(payload["messageId"])
            if message is None or message["sender"] != payload["sender"]:
                return reject("unknown message")
            duplicate = message["sent"]
            message["sent"] = True
            return ok({"messageId": payload["messageId"], "createdAt": T0, "expiresAt": T1,
                       "duplicate": duplicate, "autoWake": {}})
        if operation == "status":
            message = self.messages.get(payload["messageId"])
            if message is None or not message["sent"] or message["forgotten"]:
                return ok({"status": None, "guidance": "Delivery is unknown; compare saved receipts."})
            state = ("acknowledged" if message["acknowledgedAt"] else
                     "delivered" if message["claimedAt"] else "queued")
            return ok({"status": {"messageId": payload["messageId"], "target": message["target"],
                                  "createdAt": T0, "expiresAt": T1, "claimedAt": message["claimedAt"],
                                  "acknowledgedAt": message["acknowledgedAt"],
                                  "deliveryAttempts": message["attempts"],
                                  "firstDeliveredAt": message["firstDeliveredAt"], "state": state,
                                  "wake": None, "autoWake": None}})
        if operation == "pending":
            return ok({"count": sum(1 for m in self.messages.values()
                                    if m["sent"] and m["target"] == payload["target"] and not m["acknowledgedAt"])})
        if operation == "claim":
            claimed = []
            for message_id, message in self.messages.items():
                if (message["sent"] and message["target"] == payload["target"]
                        and message["claimedAt"] is None and not message["acknowledgedAt"]):
                    message["claimedAt"] = T0
                    message["firstDeliveredAt"] = T0
                    message["attempts"] += 1
                    claimed.append({"messageId": message_id, "sender": message["sender"],
                                    "recipient": message["target"], "body": message["body"], "createdAt": T0,
                                    "expiresAt": T1, "deliveryAttempt": message["attempts"],
                                    "firstDeliveredAt": T0})
            return ok({"messages": claimed})
        if operation == "acknowledge":
            count = 0
            for message_id in payload["messageIds"]:
                message = self.messages.get(message_id)
                if message and message["target"] == payload["target"] and message["claimedAt"] and not message["acknowledgedAt"]:
                    message["acknowledgedAt"] = T1
                    count += 1
            return ok({"acknowledged": count})
        return reject("Unsupported session message operation.")


class PeerBridgeFakeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-ags-peer-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli_file = self.root / "ags" / "session-message-cli.mjs"
        self.cli_file.parent.mkdir()
        self.cli_file.write_text("// synthetic placeholder; never executed\n", encoding="utf-8")
        self.state_dir = self.root / "ags-state"
        self.fake = FakeAgs()
        self.cli = self.cli_with(self.fake)
        self.addresses = AddressBook(self.root / "peer" / "addresses.sqlite3")
        self.journal_path = self.root / "peer" / "journal.sqlite3"
        self.bridge = PeerBridge(self.cli, self.addresses, self.journal_path, user_scope="user-local")
        self.worker = self.addresses.bind("worker-1", *WORKER, lease_id="lease-1", vm_attempt_id="vm-attempt-1")

    def cli_with(self, runner):
        return AgsSessionCli(Path(sys.executable).absolute(), self.cli_file, self.state_dir,
                             timeout=5.0, runner=runner)

    def task_body(self, marker=MARKER):
        return encode("task", "corr-1", {"instruction": marker})

    def test_argv_has_no_body_env_sets_state_dir_and_body_is_not_stored(self) -> None:
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        self.assertEqual(record.state, "sent")
        for call in self.fake.calls:
            self.assertFalse(any(MARKER in argument for argument in call["argv"]))
            self.assertEqual(call["argv"], (str(self.cli.node), str(self.cli_file)))
            self.assertEqual(call["env"][STATE_DIR_ENV], str(self.state_dir))
        self.assertIn(MARKER.encode(), self.fake.calls[0]["input"])
        self.assertNotIn(MARKER.encode(), self.journal_path.read_bytes())
        self.assertNotIn(MARKER.encode(), self.addresses.path.read_bytes())
        self.assertEqual(record.body_sha256, hashlib.sha256(self.task_body().encode()).hexdigest())

    def test_cli_failures_map_to_fixed_codes_and_outcomes(self) -> None:
        def raiser(error):
            def run(argv, *, input, env, timeout):
                raise error
            return run

        def returner(result):
            def run(argv, *, input, env, timeout):
                return result
            return run

        cases = (
            (raiser(subprocess.TimeoutExpired(["node"], 5)), "AGS_TIMEOUT", "unknown"),
            (raiser(FileNotFoundError("missing")), "AGS_UNAVAILABLE", "not_sent"),
            (raiser(PermissionError("denied")), "AGS_UNAVAILABLE", "not_sent"),
            (returner(FakeResult(0, b"not json\n")), "AGS_INVALID_RESPONSE", "unknown"),
            (returner(FakeResult(0, b'{"protocolVersion":"1.0.0","ok":true,"data":{}}\n{}\n')),
             "AGS_INVALID_RESPONSE", "unknown"),
            (returner(FakeResult(0, b'{"protocolVersion":"2.0.0","ok":true,"data":{}}\n')),
             "AGS_INVALID_RESPONSE", "unknown"),
            (returner(FakeResult(0, b"x" * (1024 * 1024 + 1))), "AGS_INVALID_RESPONSE", "unknown"),
        )
        for runner, code, outcome in cases:
            with self.subTest(code=code, outcome=outcome):
                with self.assertRaises(AgsPeerError) as caught:
                    self.cli_with(runner).call("pending", {"target": doc(WORKER)})
                self.assertEqual((caught.exception.code, caught.exception.outcome), (code, outcome))
        with self.assertRaises(AgsPeerError) as caught:
            self.cli_with(returner(reject(SECRET_ERROR))).call("pending", {"target": doc(WORKER)})
        error = caught.exception
        self.assertEqual((error.code, error.outcome), ("AGS_REJECTED", "rejected"))
        for text in (str(error), repr(error), repr(error.args)):
            self.assertNotIn(SECRET_ERROR, text)
        self.assertEqual(error.detail_digest, hashlib.sha256(SECRET_ERROR.encode()).hexdigest())
        self.assertEqual(error.detail_length, len(SECRET_ERROR))

    def test_unsupported_operations_are_rejected_before_any_process(self) -> None:
        for operation in ("wait", "reconcile-wake-observation", "drop", None):
            with self.subTest(operation=operation):
                with self.assertRaises(AgsPeerError) as caught:
                    self.cli.call(operation, {})
                self.assertEqual(caught.exception.code, "UNSUPPORTED_OPERATION")
        self.assertEqual(self.fake.calls, [])

    def test_node_version_requires_v24(self) -> None:
        self.assertEqual(self.cli.node_version(), "v24.4.1")
        self.fake.version = "v22.11.0"
        with self.assertRaises(AgsPeerError) as caught:
            self.cli.node_version()
        self.assertEqual(caught.exception.code, "NODE_UNSUPPORTED")

    def test_state_dir_symlink_is_rejected(self) -> None:
        real = self.root / "real-state"
        real.mkdir()
        alias = self.root / "alias-state"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        with self.assertRaises(AgsPeerError) as caught:
            AgsSessionCli(Path(sys.executable).absolute(), self.cli_file, alias, runner=self.fake)
        self.assertEqual(caught.exception.code, "INVALID_PATH")
        with self.assertRaises(AgsPeerError):
            AgsSessionCli("relative/node", self.cli_file, self.state_dir, runner=self.fake)

    def test_prepare_intent_is_recorded_before_send(self) -> None:
        seen = []
        self.fake.before_send = lambda mid: seen.append(tuple(r.state for r in self.bridge.send_history(mid)))
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        self.assertEqual(seen, [("intent",)])
        self.assertEqual(self.fake.operations(), ["prepare", "send"])
        self.assertEqual([r.state for r in self.bridge.send_history(record.ags_message_id)], ["intent", "sent"])
        self.assertEqual(record.worker_address, "aik-worker:worker-1")
        self.assertEqual(record.target, WORKER)

    def test_send_timeout_is_unknown_and_never_resent_automatically(self) -> None:
        self.fake.faults["send"] = subprocess.TimeoutExpired(["node"], 5)
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        self.assertEqual((record.state, record.error_code), ("send_unknown", "AGS_TIMEOUT"))
        self.assertEqual(self.fake.operations(), ["prepare", "send"])
        self.assertEqual([r.state for r in self.bridge.send_history(record.ags_message_id)],
                         ["intent", "send_unknown"])
        again = self.bridge.resend_known(record.ags_message_id)
        self.assertEqual(again.state, "sent")
        self.assertEqual(self.fake.operations(), ["prepare", "send", "send"])
        self.assertEqual(self.bridge.resend_known(record.ags_message_id).state, "duplicate")

    def test_send_rejection_is_recorded_and_raised(self) -> None:
        self.fake.faults["send"] = reject(SECRET_ERROR)
        with self.assertRaises(AgsPeerError) as caught:
            self.bridge.send(ORCH, "worker-1", self.task_body())
        self.assertEqual(caught.exception.code, "AGS_REJECTED")
        message_id = next(iter(self.fake.messages))
        self.assertEqual([r.state for r in self.bridge.send_history(message_id)], ["intent", "send_failed"])
        self.assertNotIn(SECRET_ERROR.encode(), self.journal_path.read_bytes())

    def test_unknown_worker_and_non_envelope_body_are_rejected_without_calls(self) -> None:
        with self.assertRaises(AgsPeerError) as caught:
            self.bridge.send(ORCH, "worker-missing", self.task_body())
        self.assertEqual(caught.exception.code, "UNKNOWN_WORKER")
        with self.assertRaises(AgsPeerError) as caught:
            self.bridge.send(ORCH, "worker-1", "plain text")
        self.assertEqual(caught.exception.code, "INVALID_ENVELOPE")
        self.assertEqual(self.fake.calls, [])

    def test_observe_records_delivered_claimed_acked_in_order_without_duplicates(self) -> None:
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        mid = record.ags_message_id
        first = self.bridge.observe(ORCH, mid)
        self.assertEqual((first.status, first.events), ("queued", ()))
        self.cli.call("claim", {"target": doc(WORKER)})
        second = self.bridge.observe(ORCH, mid)
        self.assertEqual([(e.state, e.cursor) for e in second.events], [("delivered", 1), ("claimed", 2)])
        self.assertEqual(self.bridge.observe(ORCH, mid).events, ())
        self.cli.call("acknowledge", {"target": doc(WORKER), "messageIds": [mid]})
        third = self.bridge.observe(ORCH, mid)
        self.assertEqual(third.status, "acknowledged")
        self.assertEqual([(e.state, e.cursor) for e in third.events], [("acked", 3)])
        self.assertEqual(self.bridge.observe(ORCH, mid).events, ())
        exported = self.bridge.events_after(0)
        self.assertEqual([(m["state"], c) for m, c in exported], [("delivered", 1), ("claimed", 2), ("acked", 3)])
        for message, _ in exported:
            self.assertEqual(set(message), PEER_KEYS)
            self.assertEqual(message["worker_address"], "aik-worker:worker-1")
            self.assertEqual(message["ags_peer_id"], "codex:worker-session-1")
        self.assertEqual(len(self.bridge.events_after(2)), 1)
        with self.assertRaises(AgsPeerError) as caught:
            self.bridge.observe(("codex", "someone-else"), mid)
        self.assertEqual(caught.exception.code, "SENDER_MISMATCH")

    def test_null_status_is_unknown_delivery_without_records_or_resend(self) -> None:
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        self.fake.messages[record.ags_message_id]["forgotten"] = True
        result = self.bridge.observe(ORCH, record.ags_message_id)
        self.assertTrue(result.unknown)
        self.assertEqual((result.status, result.events), ("UNKNOWN_DELIVERY", ()))
        self.assertEqual(self.bridge.events_after(0), ())
        self.assertEqual(self.fake.operations().count("send"), 1)

    def test_claim_results_classifies_results_and_never_acknowledges(self) -> None:
        result_id = self.fake.inject(WORKER, ORCH, encode("result", "corr-1", {"outcome": "synthetic"}))
        plain_id = self.fake.inject(WORKER, ORCH, "plain message from another host")
        task_id = self.fake.inject(WORKER, ORCH, encode("task", "corr-2", {}))
        unbound_id = self.fake.inject(("codex", "unbound-session"), ORCH, encode("result", "corr-3", {}))
        claimed = {m.message_id: m for m in self.bridge.claim_results(ORCH)}
        self.assertEqual(set(claimed), {result_id, plain_id, task_id, unbound_id})
        result = claimed[result_id]
        self.assertTrue(result.is_result)
        self.assertEqual(result.worker.worker_id, "worker-1")
        self.assertEqual((result.event.state, result.event.cursor), ("result", 1))
        self.assertIsNone(claimed[plain_id].envelope)
        self.assertIsNone(claimed[plain_id].event)
        self.assertEqual(claimed[task_id].envelope.kind, "task")
        self.assertIsNone(claimed[task_id].event)
        self.assertTrue(claimed[unbound_id].is_result)
        self.assertIsNone(claimed[unbound_id].worker)
        self.assertIsNone(claimed[unbound_id].event)
        self.assertEqual([m["state"] for m, _ in self.bridge.events_after(0)], ["result"])
        self.assertNotIn("acknowledge", self.fake.operations())
        self.assertTrue(all(m["acknowledgedAt"] is None for m in self.fake.messages.values()))
        self.assertEqual(self.bridge.acknowledge(ORCH, [result_id, plain_id]), 2)

    def test_address_binding_conflicts_and_peer_ids(self) -> None:
        again = self.addresses.bind("worker-1", *WORKER, lease_id="lease-1", vm_attempt_id="vm-attempt-1")
        self.assertEqual(again, self.worker)
        self.assertEqual(self.worker.ags_peer_id, "codex:worker-session-1")
        for args in (("worker-1", "codex", "other-session"), ("worker-2", *WORKER)):
            with self.subTest(args=args):
                with self.assertRaises(AgsPeerError) as caught:
                    self.addresses.bind(*args)
                self.assertEqual(caught.exception.code, "ADDRESS_CONFLICT")
        self.assertEqual(self.addresses.resolve_session(*WORKER).worker_address, "aik-worker:worker-1")
        self.assertIsNone(self.addresses.resolve_worker("worker-2"))
        slashed = self.addresses.bind("worker-3", "codex", "session/with/slash")
        colon = self.addresses.bind("worker-4", "host:part", "session")
        for address in (slashed, colon):
            self.assertTrue(address.ags_peer_id.startswith("peer-"))
            self.assertEqual(len(address.ags_peer_id), 45)

    def test_envelope_round_trip_and_non_convention_bodies(self) -> None:
        body = encode("result", "corr-9", {"b": [1, 2.5, None], "a": "텍스트"})
        envelope = decode(body)
        self.assertEqual((envelope.kind, envelope.correlation_id), ("result", "corr-9"))
        self.assertEqual(dict(envelope.payload), {"a": "텍스트", "b": [1, 2.5, None]})
        self.assertEqual(body, encode("result", "corr-9", {"a": "텍스트", "b": [1, 2.5, None]}))
        non_convention = (
            "plain text", "{}", "[]", 42,
            '{"schema":"aik.peer.v1","kind":"task","correlation_id":"c","payload":{},"extra":1}',
            '{"schema":"other.v1","kind":"task","correlation_id":"c","payload":{}}',
            '{"schema":"aik.peer.v1","kind":"bogus","correlation_id":"c","payload":{}}',
            '{"schema":"aik.peer.v1","schema":"aik.peer.v1","kind":"task","correlation_id":"c","payload":{}}',
            '{"schema":"aik.peer.v1","kind":"task","correlation_id":"c","payload":{"x":NaN}}',
            "x" * (MAX_BODY_BYTES + 1),
        )
        for value in non_convention:
            with self.subTest(value=str(value)[:40]):
                self.assertIsNone(decode(value))
        for args in (("bogus", "c", {}), ("task", "bad id!", {}), ("task", "c", {1: "x"}),
                     ("task", "c", {"x": "y" * MAX_BODY_BYTES})):
            with self.subTest(args=str(args)[:40]):
                with self.assertRaises(AgsPeerError):
                    encode(*args)

    def test_stores_are_append_only_and_reject_foreign_databases(self) -> None:
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        self.cli.call("claim", {"target": doc(WORKER)})
        self.bridge.observe(ORCH, record.ags_message_id)
        with sqlite3.connect(self.journal_path) as connection:
            for statement in ("UPDATE peer_events SET state='acked'", "DELETE FROM peer_events",
                              "UPDATE send_records SET phase='sent'", "DELETE FROM send_records"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(statement)
        with sqlite3.connect(self.addresses.path) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM worker_addresses")
        foreign = self.root / "foreign.sqlite3"
        with sqlite3.connect(foreign) as connection:
            connection.execute("PRAGMA application_id = 1234")
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
        for constructor in (lambda: AddressBook(foreign),
                            lambda: AddressBook(self.journal_path),
                            lambda: PeerBridge(self.cli, self.addresses, self.addresses.path, user_scope="u")):
            with self.assertRaises(AgsPeerError) as caught:
                constructor()
            self.assertEqual(caught.exception.code, "INVALID_STORE")

    def test_events_feed_aik_readmodel_peer_view(self) -> None:
        record = self.bridge.send(ORCH, "worker-1", self.task_body())
        self.cli.call("claim", {"target": doc(WORKER)})
        self.cli.call("acknowledge", {"target": doc(WORKER), "messageIds": [record.ags_message_id]})
        self.bridge.observe(ORCH, record.ags_message_id)
        self.fake.inject(WORKER, ORCH, encode("result", "corr-1", {"outcome": "synthetic"}))
        self.bridge.claim_results(ORCH)
        pairs = self.bridge.events_after(0)
        events = [from_peer_message(message, cursor, "user-local") for message, cursor in pairs]
        self.assertEqual([e.source for e in events], ["ags_peer"] * 4)
        self.assertEqual([e.cursor for e in events], [1, 2, 3, 4])
        self.assertEqual([e.state for e in events], ["delivered", "claimed", "acked", "result"])

        model = ReadModel(self.root / "readmodel" / "view.sqlite3", user_scope="user-local")
        report = model.apply(events)
        self.assertEqual((report.applied, report.conflicts, report.gaps_opened), (4, 0, 0))
        view = model.peer_view("codex:worker-session-1")
        self.assertTrue(view.found)
        self.assertFalse(view.authoritative)
        self.assertIn("aik-worker:worker-1", view.worker_addresses)
        self.assertIn("worker-1", view.worker_ids)
        self.assertEqual({d.state for d in view.deliveries}, {"acked", "result"})


class RealAgsContractTests(unittest.TestCase):
    """Runs the actual AGS 2.9.0 CLI only when available; otherwise NOT_RUN."""

    def setUp(self) -> None:
        ags_root = Path(os.environ.get("AIK_AGS_ROOT") or ROOT.parent / "agent-governance-suite")
        cli_path = ags_root / "mcp-server" / "dist" / "session-message-cli.mjs"
        if not cli_path.is_file():
            self.skipTest(f"NOT_RUN: AGS session-message CLI not found under {ags_root}")
        node = shutil.which("node")
        if not node:
            self.skipTest("NOT_RUN: node executable not found")
        temporary = tempfile.TemporaryDirectory(prefix="aik-ags-real-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli = AgsSessionCli(Path(node).resolve(), cli_path.resolve(), self.root / "ags-state", timeout=30.0)
        # Registered after the directory cleanup, so it runs first (LIFO).
        self.addCleanup(self.cli.stop_broker)
        try:
            self.cli.node_version()
        except AgsPeerError as error:
            self.skipTest(f"NOT_RUN: node v24+ unavailable ({error.code})")
        host = os.environ.get("AIK_AGS_TEST_HOST", "codex")
        self.orchestrator = (host, "aik-orchestrator-contract-test")
        self.worker = (host, "aik-worker-contract-test")
        self.addresses = AddressBook(self.root / "peer" / "addresses.sqlite3")
        self.addresses.bind("worker-real", *self.worker, vm_attempt_id="vm-attempt-real")
        self.bridge = PeerBridge(self.cli, self.addresses, self.root / "peer" / "journal.sqlite3",
                                 user_scope="user-contract-test")

    def test_task_and_result_round_trip_through_real_cli(self) -> None:
        record = self.bridge.send(self.orchestrator, "worker-real", encode("task", "corr-real", {"step": 1}))
        self.assertEqual(record.state, "sent")
        mid = record.ags_message_id
        claimed = self.cli.call("claim", {"target": doc(self.worker)})
        self.assertIn(mid, [m["messageId"] for m in claimed["messages"]])
        self.assertEqual(self.cli.call("acknowledge", {"target": doc(self.worker), "messageIds": [mid]})["acknowledged"], 1)
        observed = self.bridge.observe(self.orchestrator, mid)
        self.assertEqual([e.state for e in observed.events], ["delivered", "claimed", "acked"])

        prepared = self.cli.call("prepare", {"sender": doc(self.worker), "target": doc(self.orchestrator),
                                             "body": encode("result", "corr-real", {"outcome": "synthetic"})})
        sent = self.cli.call("send", {"sender": doc(self.worker), "messageId": prepared["messageId"]})
        self.assertFalse(sent["duplicate"])
        results = [m for m in self.bridge.claim_results(self.orchestrator) if m.message_id == prepared["messageId"]]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].event.state, "result")
        self.assertEqual(results[0].worker.worker_id, "worker-real")
        self.assertEqual(self.bridge.acknowledge(self.orchestrator, [prepared["messageId"]]), 1)
        self.assertEqual([m["state"] for m, _ in self.bridge.events_after(0)],
                         ["delivered", "claimed", "acked", "result"])


if __name__ == "__main__":
    unittest.main()
