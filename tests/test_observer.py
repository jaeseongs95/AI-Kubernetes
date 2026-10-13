"""AIK-036 observer tests with synthetic events, fake ports and a fake budget.

No provider, credential, network or VM/AGS installation is used. Passing on
Linux is development verification, not Windows support.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_budget import UNKNOWN, Usage, confirmed  # noqa: E402
from aik_provider import AuthKind, ConnectionBinding, JobReceipt, JobRequest, JobState  # noqa: E402
from aik_observer import (  # noqa: E402
    NOT_PROVIDED, EventConflict, GapRecord, InvalidEvent, ObserverError, ProviderEvent,
    UnknownObservation, WorkerObserver,
)


FINGERPRINT = "a" * 64
SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_BE_STORED"
BINDING = ConnectionBinding("synthetic", "synthetic-runtime", "connection-1", 1, AuthKind.API_KEY,
                            "secret://local/test-ref", FINGERPRINT)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@dataclass
class FakeSettlement:
    kind: str


class FakeBudget:
    """Mimics the budget rule: first settlement, any later report is late."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Usage]] = []

    def settle(self, reservation_id: str, usage: Usage) -> FakeSettlement:
        self.calls.append((reservation_id, usage))
        count = sum(1 for rid, _ in self.calls if rid == reservation_id)
        return FakeSettlement("settlement" if count == 1 else "late")


class FakeSource:
    def __init__(self, events, *, drop_once=(), drop_always=(), fail_on_call=None, ignore_cursor=False):
        self.events = list(events)
        self.drop_once = set(drop_once)
        self.drop_always = set(drop_always)
        self.fail_on_call = fail_on_call
        self.ignore_cursor = ignore_cursor
        self.calls: list[int | None] = []

    def read(self, after_sequence, limit):
        self.calls.append(after_sequence)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise ConnectionError("synthetic stream disconnect")
        start = 0 if self.ignore_cursor or after_sequence is None else after_sequence
        result, dropped = [], set()
        for event in self.events:
            if event.sequence <= start or event.sequence in self.drop_always:
                continue
            if event.sequence in self.drop_once:
                dropped.add(event.sequence)
                continue
            result.append(event)
            if len(result) >= limit:
                break
        self.drop_once -= dropped
        return tuple(result)


class FakePort:
    def __init__(self, states=()) -> None:
        self.states = list(states)
        self.calls = 0
        self.job_override = None

    def observe(self, request, receipt):
        self.calls += 1
        state = self.states.pop(0) if self.states else receipt.state
        observed = replace(receipt, state=state)
        if self.job_override:
            observed = replace(observed, provider_job_id=self.job_override)
        return observed


def event(sequence, kind="output_delta", payload=None, provenance="provider_observed", event_id=None):
    return ProviderEvent(sequence, event_id or f"event-{sequence}", kind,
                         payload if payload is not None else {"text": f"chunk {sequence}"}, provenance)


class ObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-observer-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "observer" / "observer.sqlite3"
        self.budget = FakeBudget()
        self.observer = WorkerObserver(self.database, budget=self.budget, clock=FakeClock())

    def start(self, lease_id="lease-1", job="job-1"):
        request = JobRequest(BINDING, f"request-{lease_id}", "route-1", "vm-attempt-1", "synthetic-model-large")
        receipt = JobReceipt(request.request_id, job, "connection-1", 1, FINGERPRINT, JobState.ACCEPTED)
        return self.observer.start(lease_id, vm_attempt_id="vm-attempt-1", provider_job_id=job,
                                   requested_model="synthetic-model-large", requested_effort="high",
                                   request=request, receipt=receipt)

    def test_stream_disconnect_then_reconnect_resumes_from_cursor(self) -> None:
        self.start()
        events = [event(n) for n in range(1, 11)]
        first = FakeSource(events, fail_on_call=3)
        result = self.observer.ingest("lease-1", first, limit=3)
        self.assertTrue(result.disconnected)
        self.assertEqual((result.applied, result.cursor), (6, 6))
        second = FakeSource(events)
        result = self.observer.ingest("lease-1", second, limit=3)
        self.assertFalse(result.disconnected)
        self.assertEqual(second.calls[0], 6)
        self.assertEqual((result.applied, result.cursor, result.stream_state), (4, 10, "ok"))
        view = self.observer.snapshot("lease-1")
        self.assertEqual(view.output_events, 10)
        self.assertFalse(view.terminal)

    def test_duplicate_events_are_ignored(self) -> None:
        self.start()
        events = [event(1), event(2), event(3)]
        self.observer.ingest("lease-1", FakeSource(events))
        result = self.observer.ingest("lease-1", FakeSource(events, ignore_cursor=True))
        self.assertEqual((result.applied, result.duplicates, result.cursor), (0, 3, 3))
        self.start("lease-2")
        same = event(1)
        result = self.observer.ingest("lease-2", FakeSource([same, same, event(2)]))
        self.assertEqual((result.applied, result.duplicates), (2, 1))
        self.assertEqual(self.observer.snapshot("lease-1").output_events, 3)

    def test_same_sequence_with_different_content_is_a_fail_closed_conflict(self) -> None:
        self.start()
        self.observer.ingest("lease-1", FakeSource([event(1), event(2)]))
        changed = FakeSource([event(1), event(2, payload={"text": "different"})], ignore_cursor=True)
        with self.assertRaises(EventConflict):
            self.observer.ingest("lease-1", changed)
        view = self.observer.snapshot("lease-1")
        self.assertEqual((view.conflicts, view.stream_state, view.cursor), (1, "conflict", 2))
        with self.assertRaises(EventConflict):
            self.observer.ingest("lease-1", FakeSource([event(3)]))
        self.start("lease-2")
        with self.assertRaises(EventConflict):
            self.observer.ingest("lease-2", FakeSource([event(1), event(1, payload={"text": "x"})]))
        self.assertEqual(self.observer.snapshot("lease-2").cursor, 0)

    def test_gap_is_restored_by_requery(self) -> None:
        self.start()
        source = FakeSource([event(n) for n in range(1, 6)], drop_once={3})
        result = self.observer.ingest("lease-1", source)
        self.assertEqual((result.applied, result.gaps, result.cursor, result.stream_state), (5, 1, 5, "ok"))
        self.assertEqual(source.calls, [None, 2, 5])
        self.assertEqual(self.observer.snapshot("lease-1").gaps, (GapRecord(3, 3, True),))

    def test_unresolved_gap_blocks_later_events_until_recovered(self) -> None:
        self.start()
        events = [event(1), event(2), event(3), event(4, "usage", {"output_tokens": 50}), event(5)]
        result = self.observer.ingest("lease-1", FakeSource(events, drop_always={3}))
        self.assertEqual((result.applied, result.gaps, result.cursor, result.stream_state),
                         (2, 1, 2, "gap_unresolved"))
        view = self.observer.snapshot("lease-1")
        self.assertEqual(view.gaps, (GapRecord(3, 3, False),))
        self.assertEqual(view.usage.output_tokens, UNKNOWN)
        port = FakePort([JobState.SUCCEEDED])
        view = self.observer.poll("lease-1", port)
        self.assertTrue(view.terminal)
        self.assertEqual(self.budget.calls, [("lease-1", Usage())])
        result = self.observer.ingest("lease-1", FakeSource(events))
        self.assertEqual((result.applied, result.cursor, result.stream_state), (3, 5, "ok"))
        self.assertEqual(self.observer.snapshot("lease-1").gaps, (GapRecord(3, 3, True),))
        view = self.observer.poll("lease-1", port)
        self.assertEqual(self.budget.calls[1][1].output_tokens, confirmed(50))
        self.assertEqual(tuple(s.kind for s in view.settlements), ("settlement", "late"))

    def test_partial_output_or_accepted_request_is_not_completion(self) -> None:
        self.start()
        self.observer.ingest("lease-1", FakeSource([event(1), event(2, "tool_call", {"name": "t"})]))
        port = FakePort([JobState.ACCEPTED, JobState.RUNNING, JobState.UNKNOWN])
        for _ in range(3):
            view = self.observer.poll("lease-1", port)
            self.assertFalse(view.terminal)
        self.assertEqual(view.job_state, JobState.UNKNOWN)
        self.assertEqual((view.output_events, view.tool_calls), (1, 1))
        self.assertEqual(self.budget.calls, [])
        self.assertEqual(view.settlements, ())

    def test_missing_usage_differs_from_actual_zero(self) -> None:
        self.start("lease-zero")
        self.start("lease-none")
        self.observer.ingest("lease-zero", FakeSource([
            event(1, "usage", {"input_tokens": 0, "api_cost_cents": 0, "output_tokens": None}),
        ]))
        zero = self.observer.snapshot("lease-zero").usage
        none = self.observer.snapshot("lease-none").usage
        self.assertEqual(zero.input_tokens, confirmed(0))
        self.assertEqual(zero.api_cost_cents, confirmed(0))
        self.assertEqual(zero.output_tokens, UNKNOWN)
        self.assertEqual(zero.quota_used, UNKNOWN)
        self.assertEqual(none, Usage())
        self.observer.poll("lease-none", FakePort([JobState.FAILED]))
        self.assertEqual(self.budget.calls, [("lease-none", Usage())])

    def test_later_usage_report_restates_the_total(self) -> None:
        self.start()
        self.observer.ingest("lease-1", FakeSource([
            event(1, "usage", {"output_tokens": 5}),
            event(2, "usage", {"output_tokens": 9, "input_tokens": 3}),
            event(3, "usage", {"api_cost_cents": 4}),
            event(4, "usage", {"output_tokens": 999}, provenance="model_reported"),
        ]))
        usage = self.observer.snapshot("lease-1").usage
        self.assertEqual((usage.output_tokens, usage.input_tokens, usage.api_cost_cents),
                         (confirmed(9), confirmed(3), confirmed(4)))

    def test_observed_settings_stay_separate_from_requested_and_model_reported(self) -> None:
        self.start()
        self.observer.ingest("lease-1", FakeSource([
            event(1, "model_reported", {"model": "claimed-model-x", "effort": "max"}, provenance="model_reported"),
            event(2, "state", {"model": "another-claim"}, provenance="model_reported"),
        ]))
        view = self.observer.snapshot("lease-1")
        self.assertIsNone(view.observed.model.value)
        self.assertEqual(view.observed.model.reason, NOT_PROVIDED)
        self.assertEqual(view.observed.effort.reason, NOT_PROVIDED)
        self.assertEqual((view.model_reported.model.value, view.model_reported.model.source_sequence),
                         ("another-claim", 2))
        self.assertEqual(view.model_reported.effort.value, "max")
        self.observer.ingest("lease-1", FakeSource([
            event(3, "state", {"model": "synthetic-model-small", "effort": "medium"}),
        ]))
        view = self.observer.snapshot("lease-1")
        self.assertEqual((view.observed.model.value, view.observed.model.source_sequence,
                          view.observed.model.provenance), ("synthetic-model-small", 3, "provider_observed"))
        self.assertEqual(view.observed.effort.value, "medium")
        self.assertEqual((view.requested.model, view.requested.effort, view.requested.provenance),
                         ("synthetic-model-large", "high", "client_requested"))

    def test_terminal_settles_once_and_later_usage_is_late(self) -> None:
        self.start()
        port = FakePort([JobState.SUCCEEDED])
        self.observer.poll("lease-1", port)
        self.observer.poll("lease-1", port)
        self.assertEqual(len(self.budget.calls), 1)
        self.assertEqual(port.calls, 1)
        self.observer.ingest("lease-1", FakeSource([event(1, "usage", {"output_tokens": 12})]))
        view = self.observer.poll("lease-1", port)
        self.assertEqual(port.calls, 1)
        self.assertEqual(len(self.budget.calls), 2)
        self.assertEqual(tuple(s.kind for s in view.settlements), ("settlement", "late"))
        self.assertEqual(view.settlements[1].usage.output_tokens, confirmed(12))

    def test_mismatched_receipt_is_rejected_without_state_change(self) -> None:
        self.start()
        port = FakePort([JobState.SUCCEEDED])
        port.job_override = "job-other"
        with self.assertRaises(ObserverError):
            self.observer.poll("lease-1", port)
        self.assertEqual(self.observer.snapshot("lease-1").job_state, JobState.ACCEPTED)
        self.assertEqual(self.budget.calls, [])

    def test_invalid_and_forged_events_are_rejected(self) -> None:
        for payload in ({"x": {1, 2}}, {"x": (1, 2)}, {1: "a"}):
            with self.assertRaises(InvalidEvent):
                event(1, payload=payload)
        for payload in ({"input_tokens": -1}, {"input_tokens": True}, {"quota_used": 3}):
            with self.assertRaises(InvalidEvent):
                event(1, "usage", payload)
        self.start()
        forged = event(1)
        object.__setattr__(forged, "kind", "unknown-kind")
        with self.assertRaises(InvalidEvent):
            self.observer.ingest("lease-1", FakeSource([forged]))
        self.assertEqual(self.observer.snapshot("lease-1").cursor, 0)

    def test_persistence_append_only_and_no_raw_output_stored(self) -> None:
        self.start()
        self.observer.ingest("lease-1", FakeSource([event(1, payload={"text": SECRET_MARKER})]))
        before = self.observer.snapshot("lease-1")
        reopened = WorkerObserver(self.database, budget=FakeBudget(), clock=FakeClock())
        self.assertEqual(reopened.snapshot("lease-1"), before)
        self.assertNotIn(SECRET_MARKER.encode("ascii"), self.database.read_bytes())
        with closing(sqlite3.connect(self.database)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE events SET digest='x'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM events")
        with self.assertRaises(UnknownObservation):
            reopened.snapshot("lease-missing")

    def test_foreign_database_and_inconsistent_start_are_rejected(self) -> None:
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(ObserverError):
            WorkerObserver(foreign, budget=FakeBudget())
        self.assertEqual(foreign.read_bytes(), before)
        request = JobRequest(BINDING, "request-x", "route-1", "vm-attempt-1", "synthetic-model-large")
        receipt = JobReceipt("request-x", "job-x", "connection-1", 1, FINGERPRINT, JobState.ACCEPTED)
        with self.assertRaises(ObserverError):
            self.observer.start("lease-x", vm_attempt_id="vm-attempt-1", provider_job_id="job-x",
                                requested_model="other-model", request=request, receipt=receipt)
        with self.assertRaises(ObserverError):
            self.observer.start("lease-x", vm_attempt_id="vm-attempt-2", provider_job_id="job-x",
                                requested_model="synthetic-model-large", request=request, receipt=receipt)
        self.start()
        with self.assertRaises(ObserverError):
            self.start(job="job-different")


if __name__ == "__main__":
    unittest.main()
