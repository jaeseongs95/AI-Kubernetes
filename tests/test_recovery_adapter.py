"""ToolEffectAdapter: RecoveryReconciler's effect port over the real DispatchJournal.

Crash states are written as tool events in a real DispatchJournal (API agent
loop path), then reconciled by the real RecoveryReconciler on fresh instances.
VM and provider ports are fakes with no leases to reconcile. No provider,
credential, network or VM/AGS installation is used. Passing on Linux is
development verification, not Windows qualification.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_budget import BudgetStore  # noqa: E402
from aik_dispatch import DispatchIdentity, DispatchJournal  # noqa: E402
from aik_lease import LeaseManager  # noqa: E402
from aik_observer import WorkerObserver  # noqa: E402
from aik_provider.contracts import AuthKind, ConnectionBinding  # noqa: E402
from aik_recovery import (  # noqa: E402
    UNKNOWN_CODE, EffectRef, RecoveryError, RecoveryJournal, RecoveryReconciler, ToolEffectAdapter,
)
from aik_runtime import RuntimeStore  # noqa: E402


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
BINDING = ConnectionBinding("synthetic", "synthetic-runtime", "connection-1", 1,
                            next(kind for kind in AuthKind if kind.value == "api_key"),
                            "secret://local/test-key", "0123456789abcdef" * 4)


class FakeVm:
    def __init__(self, states):
        self.states = dict(states)

    def attempt_state(self, vm_attempt_id):
        return self.states.get(vm_attempt_id, "absent")


class FakeJobs:
    def observe(self, request, receipt):  # no leases exist, so this must not be reached
        raise AssertionError("unexpected provider observation")

    def start_job(self, request):
        raise AssertionError("relaunch attempted")


def identity(request_id: str, attempt: str, base: Path) -> DispatchIdentity:
    return DispatchIdentity(
        request_id=request_id, contract_digest="sha256:" + "1" * 64, vm_project_id="vm-project-1",
        vm_task_id="vm-task-" + attempt, vm_attempt_id=attempt, lease_id="lease-" + attempt,
        fencing_token=1, pool_id="pool-1", route_id="route-1", binding=BINDING,
        canonical_cwd=str(base / "workspace"), input_digest="sha256:" + "2" * 64, path="api",
    )


class ToolEffectAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-recovery-adapter-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = self.base / "state"
        self.clock = lambda: NOW
        self.journal = DispatchJournal(self.state / "dispatch.sqlite3", clock=self.clock)
        self.resent = []
        # Crash states left by ApiAgentLoop (intent -> receipt|denied|effect_unknown -> returned).
        self.journal.register(identity("req-a", "attempt-a", self.base))
        self.journal.register(identity("req-b", "attempt-b", self.base))
        self.tool("req-a", "call-intent", "create_file", ("intent",))
        self.tool("req-a", "call-read", "read_file", ("intent",))
        self.tool("req-a", "call-receipt", "update_file", ("intent", "receipt"))
        self.tool("req-a", "call-denied", "unsupported", ("intent", "denied"))
        self.tool("req-a", "call-done", "create_file", ("intent", "receipt", "returned"))
        self.tool("req-b", "call-unknown", "run_verification", ("intent", "effect_unknown"))
        self.tool("req-b", "call-late", "create_file", ("intent", "receipt"))
        self.adapter = self.make_adapter(self.journal)

    def tool(self, request_id, call_id, tool, states) -> None:
        data = {
            "intent": {"tool": tool, "arguments_digest": "sha256:" + "3" * 64, "effect_id": None,
                       "input_revision": "rev-1", "authorization_revision": 1},
            "receipt": {"kind": "file", "result_digest": "sha256:" + "4" * 64, "replayed": False},
            "denied": {"code": "TOOL_NOT_ALLOWED"},
            "effect_unknown": {"code": "INTERRUPTED_BEFORE_RECEIPT"},
            "returned": {},
        }
        for state in states:
            self.journal.tool_event(request_id, call_id, state, data[state])

    def make_adapter(self, journal) -> ToolEffectAdapter:
        return ToolEffectAdapter(journal, resend=lambda request_id, call_id: self.resent.append((request_id, call_id)))

    def states(self, request_id, call_id, journal=None) -> list:
        journal = journal or self.journal
        return [event.state for event in journal.tool_events(request_id) if event.tool_call_id == call_id]

    def reconciler(self, vm_states) -> tuple:
        """Fresh instances over the same files, as a restarted process sees them."""
        runtime = RuntimeStore(self.state / "runtime.sqlite3")
        budget = BudgetStore(self.state / "budget.sqlite3", clock=self.clock)
        leases = LeaseManager(runtime, budget, ttl_seconds=60, clock=self.clock)
        observer = WorkerObserver(self.state / "observer.sqlite3", budget=budget, clock=self.clock)
        journal = DispatchJournal(self.state / "dispatch.sqlite3", clock=self.clock)
        reconciler = RecoveryReconciler(
            runtime, leases, observer, vm_port=FakeVm(vm_states), job_port=FakeJobs(),
            effect_port=self.make_adapter(journal), journal=RecoveryJournal(self.state / "recovery.sqlite3"),
            clock=self.clock,
        )
        return reconciler, journal

    def test_inflight_lists_only_calls_whose_result_was_not_returned(self) -> None:
        refs = {ref.tool_call_id: ref for ref in self.adapter.inflight()}
        self.assertEqual(set(refs), {"call-intent", "call-read", "call-receipt", "call-denied", "call-unknown",
                                     "call-late"})
        self.assertEqual(self.journal.open_tool_requests(), ("req-a", "req-b"))
        # No receipt: intent only, or already marked unknown.
        for call in ("call-intent", "call-read", "call-unknown"):
            self.assertFalse(refs[call].has_receipt, call)
            self.assertIsNone(refs[call].receipt_ref)
        # Receipt or denial journaled, result not yet returned to the model.
        for call in ("call-receipt", "call-denied", "call-late"):
            self.assertTrue(refs[call].has_receipt, call)
            self.assertFalse(refs[call].model_result_returned)
            self.assertTrue(refs[call].receipt_ref.startswith("tool-receipt-"))
        self.assertEqual(refs["call-intent"].vm_attempt_id, "attempt-a")
        self.assertEqual(refs["call-unknown"].vm_attempt_id, "attempt-b")
        self.assertEqual(refs["call-intent"].operation, "create_file")
        self.assertEqual(refs["call-read"].idempotency, "read_only")
        self.assertEqual(refs["call-denied"].idempotency, "read_only")
        for call in ("call-intent", "call-receipt", "call-unknown", "call-late"):
            self.assertEqual(refs[call].idempotency, "non_idempotent", call)

    def test_mark_unknown_appends_effect_unknown_once_and_never_executes(self) -> None:
        ref = next(ref for ref in self.adapter.inflight() if ref.tool_call_id == "call-intent")
        self.adapter.mark_unknown(ref)
        self.adapter.mark_unknown(ref)  # idempotent
        self.assertEqual(self.states("req-a", "call-intent"), ["intent", "effect_unknown"])
        event = [e for e in self.journal.tool_events("req-a") if e.state == "effect_unknown"][0]
        self.assertEqual(event.data["code"], UNKNOWN_CODE)
        self.assertEqual(self.resent, [])

    def test_mark_unknown_refuses_when_a_receipt_arrived_after_the_scan(self) -> None:
        ref = next(ref for ref in self.adapter.inflight() if ref.tool_call_id == "call-intent")
        self.journal.tool_event("req-a", "call-intent", "receipt", {"kind": "file"})
        with self.assertRaises(RecoveryError) as raised:
            self.adapter.mark_unknown(ref)
        self.assertEqual(raised.exception.code, "EFFECT_STATE_CHANGED")
        self.assertEqual(self.states("req-a", "call-intent"), ["intent", "receipt"])

    def test_resend_hands_off_without_writing_returned(self) -> None:
        ref = next(ref for ref in self.adapter.inflight() if ref.tool_call_id == "call-receipt")
        self.adapter.resend_result(ref)
        self.assertEqual(self.resent, [("req-a", "call-receipt")])
        self.assertEqual(self.states("req-a", "call-receipt"), ["intent", "receipt"])
        no_receipt = next(ref for ref in self.adapter.inflight() if ref.tool_call_id == "call-intent")
        with self.assertRaises(RecoveryError) as raised:
            self.adapter.resend_result(no_receipt)
        self.assertEqual(raised.exception.code, "EFFECT_STATE_CHANGED")

    def test_call_returned_since_the_scan_is_not_resent(self) -> None:
        # One resume sends every pending call of a request; a sibling found returned is already sent.
        ref = next(ref for ref in self.adapter.inflight() if ref.tool_call_id == "call-denied")
        self.journal.tool_event("req-a", "call-denied", "returned", {})
        self.adapter.resend_result(ref)
        self.assertEqual(self.resent, [])
        self.assertEqual(self.states("req-a", "call-denied"), ["intent", "denied", "returned"])

    def test_unknown_or_returned_refs_are_refused(self) -> None:
        stale = EffectRef("attempt-a", "call-done", "create_file", True, False, "non_idempotent")
        with self.assertRaises(RecoveryError) as raised:
            self.adapter.resend_result(stale)
        self.assertEqual(raised.exception.code, "EFFECT_NOT_OPEN")
        with self.assertRaises(RecoveryError):
            self.adapter.mark_unknown("not a ref")
        with self.assertRaises(RecoveryError):
            ToolEffectAdapter(object(), resend=lambda *args: None)
        with self.assertRaises(RecoveryError):
            ToolEffectAdapter(self.journal, resend=None)

    def test_cancelled_reader_marks_refs_and_must_return_bool(self) -> None:
        adapter = ToolEffectAdapter(self.journal, resend=lambda *args: None,
                                    is_cancelled=lambda attempt: attempt == "attempt-b")
        cancelled = {ref.tool_call_id: ref.cancelled for ref in adapter.inflight()}
        self.assertTrue(cancelled["call-late"])
        self.assertFalse(cancelled["call-receipt"])
        broken = ToolEffectAdapter(self.journal, resend=lambda *args: None, is_cancelled=lambda attempt: "yes")
        with self.assertRaises(RecoveryError):
            broken.inflight()

    def test_ambiguous_attempt_tool_call_is_refused(self) -> None:
        self.journal.register(identity("req-c", "attempt-a", self.base))
        self.tool("req-c", "call-intent", "create_file", ("intent",))
        with self.assertRaises(RecoveryError) as raised:
            self.adapter.inflight()
        self.assertEqual(raised.exception.code, "AMBIGUOUS_EFFECT")

    def test_real_reconciler_after_restart_marks_unknown_resends_and_records_late_receipts(self) -> None:
        reconciler, journal = self.reconciler({"attempt-a": "active", "attempt-b": "terminal"})
        report = reconciler.reconcile()
        actions = {decision.subject_id.split("/")[1]: decision.action for decision in report.decisions
                   if decision.subject_kind == "effect" and decision.outcome in ("applied", "recorded")}
        self.assertEqual(actions["call-intent"], "handoff_vm_unknown")
        self.assertEqual(actions["call-read"], "handoff_vm_unknown")
        self.assertEqual(actions["call-unknown"], "handoff_vm_unknown")
        self.assertEqual(actions["call-receipt"], "resend_result")
        self.assertEqual(actions["call-denied"], "resend_result")
        self.assertEqual(actions["call-late"], "record_late_receipt")  # VM attempt terminal
        self.assertEqual(report.failures, ())
        self.assertEqual(self.states("req-a", "call-intent", journal), ["intent", "effect_unknown"])
        self.assertEqual(self.states("req-b", "call-unknown", journal), ["intent", "effect_unknown"])
        self.assertEqual(sorted(self.resent), [("req-a", "call-denied"), ("req-a", "call-receipt")])
        # Nothing was executed and no result was marked returned by recovery.
        self.assertEqual(self.states("req-a", "call-receipt", journal), ["intent", "receipt"])

        second, _ = self.reconciler({"attempt-a": "active", "attempt-b": "terminal"})
        again = second.reconcile()
        self.assertEqual(sorted(self.resent), [("req-a", "call-denied"), ("req-a", "call-receipt")],
                         "an applied resend is not repeated")
        self.assertFalse([d for d in again.decisions if d.outcome == "applied"])


if __name__ == "__main__":
    unittest.main()
