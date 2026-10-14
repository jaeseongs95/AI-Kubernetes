"""Recovery resend wired to the real ApiToolLoop.resume across a crash boundary.

Each scenario crashes the real ApiToolLoop (SimulatedCrash from the loop's
budget gate or from a DevTools call), then a fresh RecoveryReconciler and
ToolEffectAdapter over the same files decide what to do. The resend goes
through LoopResendScheduler into ApiToolLoop.resume, which replays the
journaled result without re-executing the effect.

"Controller restart" keeps the DevTools instance (the worker process lives).
"Process restart" uses a new DevTools: its effect-ID replay memory is gone,
the authority (VM's gateway, a fake here) still remembers the effect.
No provider, credential, network or VM/AGS installation is used. Passing on
Linux is development verification, not Windows qualification.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent))

import test_dispatch as dispatch_tests  # noqa: E402
from test_dispatch import (  # noqa: E402
    CONTENT, INPUT_REVISION, PYTHON, USER, WORKSPACE, CrashOn, FakeModel, SimulatedCrash, create_turn,
)
from test_recovery_adapter import FakeJobs, FakeVm  # noqa: E402

from aik_budget import BudgetStore  # noqa: E402
from aik_devtools import DevToolPolicy, DevTools, VerificationCommand  # noqa: E402
from aik_dispatch import ApiToolLoop, DispatchJournal, ModelToolCall, assistant_message  # noqa: E402
from aik_lease import LeaseManager  # noqa: E402
from aik_observer import WorkerObserver  # noqa: E402
from aik_recovery import (  # noqa: E402
    LoopResendScheduler, RecoveryError, RecoveryJournal, RecoveryReconciler, ResumeContext, ToolEffectAdapter,
)
from aik_runtime import RuntimeStore  # noqa: E402


class ResendResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        # ApiToolLoopTests.setUp builds the real WorkspaceManager, DevTools, FakeAuthority and DispatchJournal.
        # An instance (not a module-level class) so its own tests are not collected again here.
        self.h = dispatch_tests.ApiToolLoopTests("test_create_file_result_returns_to_the_model_then_end_turn")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.state = self.h.base / "recovery-state"
        self.state.mkdir()
        self.contexts: dict = {}
        self.models: list = []

    # -- helpers -------------------------------------------------------------

    def crash(self, turn, crash_on: int) -> None:
        with self.assertRaises(SimulatedCrash):
            self.h.run_loop(self.h.loop(FakeModel(turn), budget_ok=CrashOn(crash_on)))

    def context(self, turn, tools=None) -> FakeModel:
        """Register the caller-owned resume inputs for req-api; returns the model the resumed loop calls."""
        model = FakeModel()
        loop = ApiToolLoop(model, tools or self.h.tools, DispatchJournal(self.h.base / "dispatch.sqlite3"),
                           max_model_calls=8, max_tool_calls=8, budget_ok=lambda: True)
        self.contexts["req-api"] = ResumeContext(loop, self.h.identity, self.h.session, INPUT_REVISION, 1,
                                                 (USER, assistant_message(turn)))
        return model

    def reconcile(self, vm_state: str = "active"):
        """Fresh instances over the same files, as a restarted controller sees them."""
        runtime = RuntimeStore(self.state / "runtime.sqlite3")
        budget = BudgetStore(self.state / "budget.sqlite3")
        leases = LeaseManager(runtime, budget, ttl_seconds=60)
        observer = WorkerObserver(self.state / "observer.sqlite3", budget=budget)
        journal = DispatchJournal(self.h.base / "dispatch.sqlite3")
        scheduler = LoopResendScheduler(journal, self.contexts.get)
        reconciler = RecoveryReconciler(
            runtime, leases, observer, vm_port=FakeVm({"vm-attempt-1": vm_state}), job_port=FakeJobs(),
            effect_port=ToolEffectAdapter(journal, resend=scheduler),
            journal=RecoveryJournal(self.state / "recovery.sqlite3"),
        )
        return reconciler.reconcile()

    @staticmethod
    def outcomes(report) -> list:
        return [(d.subject_id.split("/")[1], d.action, d.outcome, d.reasons[-1]) for d in report.decisions]

    def states(self, call_id: str = "call-1") -> list:
        return self.h.tool_states(call_id)

    # -- scenarios -----------------------------------------------------------

    def test_receipt_crash_resends_the_same_result_through_resume_once(self) -> None:
        turn = create_turn()
        self.crash(turn, 3)  # receipt journaled, crash before ``returned``
        self.assertEqual(self.states(), ["intent", "receipt"])
        model = self.context(turn)

        report = self.reconcile()
        self.assertEqual(self.outcomes(report), [
            ("call-1", "resend_result", "planned", "VM_ATTEMPT_ACTIVE"),
            ("call-1", "resend_result", "applied", "VM_ATTEMPT_ACTIVE"),
        ])
        self.assertEqual(report.failures, ())
        self.assertEqual(self.states(), ["intent", "receipt", "returned"])
        self.assertEqual(len(model.calls), 1)
        resent = model.calls[0][1][-1]
        self.assertEqual((resent["role"], resent["tool_call_id"], resent["result"]["ok"]), ("tool", "call-1", True))
        self.assertEqual(len(self.h.authority.intents), 1)  # the effect ran once
        self.assertEqual((self.h.root / "src" / "app.txt").read_bytes(), CONTENT.encode("utf-8"))

        again = self.reconcile()  # nothing is open any more
        self.assertEqual((again.decisions, again.failures), ((), ()))
        self.assertEqual(len(model.calls), 1)

    def test_denied_crash_resends_the_error_without_devtools(self) -> None:
        turn = create_turn(ModelToolCall("call-1", "delete_file", {"path": "src/app.txt"}))
        self.crash(turn, 3)
        self.assertEqual(self.states(), ["intent", "denied"])
        unused = mock.Mock(spec=DevTools)
        model = self.context(turn, tools=unused)

        report = self.reconcile()
        self.assertEqual(self.outcomes(report)[-1], ("call-1", "resend_result", "applied", "VM_ATTEMPT_ACTIVE"))
        self.assertEqual(self.states(), ["intent", "denied", "returned"])
        self.assertEqual(model.calls[0][1][-1]["result"], {"ok": False, "error": "TOOL_NOT_ALLOWED"})
        self.assertEqual(unused.mock_calls, [])
        self.assertEqual(self.h.authority.requests, [])

    def test_two_pending_calls_of_one_request_resume_once(self) -> None:
        turn = create_turn(ModelToolCall("call-1", "create_file", {"path": "src/app.txt", "content": CONTENT}),
                           ModelToolCall("call-2", "delete_file", {"path": "src/app.txt"}))
        self.crash(turn, 4)
        self.assertEqual((self.states("call-1"), self.states("call-2")),
                         (["intent", "receipt"], ["intent", "denied"]))
        model = self.context(turn)

        report = self.reconcile()
        applied = [entry for entry in self.outcomes(report) if entry[2] == "applied"]
        self.assertEqual(sorted(applied), [("call-1", "resend_result", "applied", "VM_ATTEMPT_ACTIVE"),
                                           ("call-2", "resend_result", "applied", "VM_ATTEMPT_ACTIVE")])
        self.assertEqual(len(model.calls), 1)  # one resume, not one per call
        sent = [message["tool_call_id"] for message in model.calls[0][1] if message.get("role") == "tool"]
        self.assertEqual(sent, ["call-1", "call-2"])
        self.assertEqual((self.states("call-1")[-1], self.states("call-2")[-1]), ("returned", "returned"))
        self.assertEqual(len(self.h.authority.intents), 1)

    def test_sibling_without_receipt_blocks_the_resend_and_is_recorded_failed(self) -> None:
        turn = create_turn(ModelToolCall("call-1", "create_file", {"path": "src/a.txt", "content": "a"}),
                           ModelToolCall("call-2", "create_file", {"path": "src/b.txt", "content": "b"}))
        original = DevTools.create_file
        calls = []

        def first_real_then_crash(tools, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise SimulatedCrash()
            return original(tools, *args, **kwargs)

        with mock.patch.object(DevTools, "create_file", autospec=True, side_effect=first_real_then_crash):
            with self.assertRaises(SimulatedCrash):
                self.h.run_loop(self.h.loop(FakeModel(turn)))
        self.assertEqual((self.states("call-1"), self.states("call-2")), (["intent", "receipt"], ["intent"]))
        model = self.context(turn)

        report = self.reconcile()
        results = {(entry[0], entry[1], entry[2]): entry[3] for entry in self.outcomes(report)}
        self.assertEqual(results[("call-1", "resend_result", "failed")], "RESEND_NOT_DELIVERED")
        self.assertNotIn(("call-1", "resend_result", "applied"), results)
        self.assertIn(("call-2", "handoff_vm_unknown", "applied"), results)
        self.assertEqual(model.calls, [])  # resume stopped with effect_unknown before any model call
        self.assertEqual(self.states("call-1"), ["intent", "receipt"])
        self.assertEqual(self.states("call-2")[-1], "effect_unknown")
        self.assertEqual(len(self.h.authority.intents), 1)
        self.assertFalse((self.h.root / "src" / "b.txt").exists())

    def test_process_restart_without_replay_memory_fails_closed(self) -> None:
        turn = create_turn()
        self.crash(turn, 3)
        # New process: a new DevTools (no replay memory) over the same authority and workspace.
        fresh = DevTools(authority=self.h.authority, workspaces=self.h.manager, policy=DevToolPolicy(WORKSPACE, (
            VerificationCommand("unit", (PYTHON, "-c", "import sys; sys.exit(0)"), timeout_seconds=30),
        ), analysis_only_paths=("fixtures",)))
        model = self.context(turn, tools=fresh)

        report = self.reconcile()
        self.assertEqual(self.outcomes(report)[-1], ("call-1", "resend_result", "failed", "RESEND_REPLAY_MISMATCH"))
        self.assertEqual(model.calls, [])
        self.assertEqual(self.states(), ["intent", "receipt"])
        self.assertEqual((self.h.root / "src" / "app.txt").read_bytes(), CONTENT.encode("utf-8"))
        # Known limitation, pinned: without replay memory the new DevTools re-observes the idempotent
        # create (same file, no new intent) and forwards a second receipt; the loop then refuses the
        # non-replayed result. The effect intent stays single.
        self.assertEqual(len(self.h.authority.intents), 1)
        self.assertEqual(self.h.authority.requests, ["call-1", "call-1"])
        self.assertEqual([status for _, status, _ in self.h.authority.receipts], ["succeeded", "succeeded"])

    def test_missing_or_mismatched_context_is_failed_without_journal_change(self) -> None:
        turn = create_turn()
        self.crash(turn, 3)
        before = self.h.journal.tool_events("req-api")

        report = self.reconcile()
        self.assertEqual(self.outcomes(report)[-1], ("call-1", "resend_result", "failed", "RESUME_CONTEXT_MISSING"))
        self.assertEqual(self.h.journal.tool_events("req-api"), before)

        self.context(turn)
        other = self.contexts["req-api"]
        self.contexts["req-api"] = ResumeContext(other.loop, self.h.identity.__class__.from_dict(
            {**self.h.identity.to_dict(), "request_id": "req-other"}), other.session, other.input_revision,
            other.authorization_revision, other.messages)
        report = self.reconcile()
        self.assertEqual(self.outcomes(report)[-1], ("call-1", "resend_result", "failed", "RESUME_CONTEXT_MISMATCH"))
        self.assertEqual(self.h.journal.tool_events("req-api"), before)

    def test_long_lived_scheduler_looks_up_a_context_registered_later(self) -> None:
        turn = create_turn()
        self.crash(turn, 3)
        scheduler = LoopResendScheduler(self.h.journal, self.contexts.get)
        with self.assertRaises(RecoveryError) as raised:
            scheduler("req-api", "call-1")
        self.assertEqual(raised.exception.code, "RESUME_CONTEXT_MISSING")
        model = self.context(turn)
        scheduler("req-api", "call-1")
        scheduler("req-api", "call-1")  # already returned: no second resume
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.states(), ["intent", "receipt", "returned"])

    def test_scheduler_rejects_bad_construction(self) -> None:
        with self.assertRaises(RecoveryError):
            LoopResendScheduler(object(), lambda request_id: None)
        with self.assertRaises(RecoveryError):
            LoopResendScheduler(self.h.journal, None)


if __name__ == "__main__":
    unittest.main()
