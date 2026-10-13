"""Governance gate projection over synthetic BridgeReceipts.

No VM, AGS, provider, credential or network is used. The receipts are built
directly from the bridge's public dataclasses with synthetic identifiers.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
import sys
from types import MappingProxyType
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_vm_bridge.bridge import BridgeReceipt, VmRefs  # noqa: E402
from aik_vm_bridge.errors import BridgeCode, VmBridgeError  # noqa: E402
from aik_vm_bridge.governance import (  # noqa: E402
    GATE_BLOCKER_CODES, UNREACHABLE_FACTS, GatePhase, GateState, GovernanceGateView,
    PluginTaskObservation, project_final_report, project_run_once, project_status_blocked_tasks,
)
from aik_vm_bridge.surface import Command  # noqa: E402


def receipt(command=Command.RUN_ONCE, payload=None, refs=None) -> BridgeReceipt:
    if refs is None:
        refs = VmRefs(project_id="project-1", task_id="task-1", attempt_id="attempt-1", history_cursor=7)
    return BridgeReceipt(command=command, request_id="request-1", project_id="project-1",
                         refs=refs, payload=MappingProxyType(dict(payload or {})))


def blocked(code, detail=None, **extra) -> BridgeReceipt:
    payload = {"action": "blocked", "blocker_code": code, **extra}
    if detail is not None:
        payload["detail"] = detail
    return receipt(payload=payload)


class RunOnceProjectionTests(unittest.TestCase):
    def test_four_gate_codes_map_to_state_and_phase(self) -> None:
        expected = {
            "GOVERNANCE_GATE_PENDING": (GateState.PENDING, GatePhase.UNREPORTED),
            "GOVERNANCE_GATE_BLOCKED": (GateState.BLOCKED, GatePhase.BEFORE_EXECUTION),
            "GOVERNANCE_GATE_FAILED": (GateState.FAILED, GatePhase.BEFORE_COMPLETION),
            "GOVERNANCE_CONTRACT_MISMATCH": (GateState.CONTRACT_MISMATCH, GatePhase.UNREPORTED),
        }
        self.assertEqual(dict(GATE_BLOCKER_CODES), expected)
        for code, (state, phase) in expected.items():
            with self.subTest(code=code):
                view = project_run_once(blocked(code))
                self.assertEqual((view.state, view.phase), (state, phase))
                self.assertEqual(view.vm_blocker_code, code)
                self.assertEqual(view.provenance, "vm_run_once")
                self.assertEqual((view.request_id, view.project_id, view.task_id, view.attempt_id,
                                  view.history_cursor),
                                 ("request-1", "project-1", "task-1", "attempt-1", 7))

    def test_unknown_governance_code_is_unrecognized_not_passing(self) -> None:
        view = project_run_once(blocked("GOVERNANCE_SOMETHING_NEW"))
        self.assertIsNotNone(view)
        self.assertEqual((view.state, view.phase), (GateState.UNRECOGNIZED, GatePhase.UNREPORTED))

    def test_non_governance_codes_and_non_blocked_actions_return_none(self) -> None:
        self.assertIsNone(project_run_once(blocked("STALE_EXECUTION_INPUT")))
        self.assertIsNone(project_run_once(blocked("EXTERNAL_EFFECT_UNKNOWN",
                                                   detail="GOVERNANCE_GATE_ERROR: synthetic")))
        for action in ("dispatched", "idle", None):
            with self.subTest(action=action):
                self.assertIsNone(project_run_once(receipt(payload={
                    "action": action, "blocker_code": "GOVERNANCE_GATE_BLOCKED"})))

    def test_blocked_with_missing_or_malformed_code_is_invalid_response(self) -> None:
        payloads = (
            {"action": "blocked"},
            {"action": "blocked", "blocker_code": None},
            {"action": "blocked", "blocker_code": 17},
            {"action": "blocked", "blocker_code": "governance_gate_blocked"},
            {"action": "blocked", "blocker_code": "G"},
            {"action": "blocked", "blocker_code": "GOVERNANCE_" + "X" * 100},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(VmBridgeError) as raised:
                    project_run_once(receipt(payload=payload))
                self.assertEqual(raised.exception.code, BridgeCode.INVALID_RESPONSE)
                self.assertEqual(raised.exception.subject, "blocker_code")

    def test_wrong_command_or_receipt_type_is_invalid_request(self) -> None:
        with self.assertRaises(VmBridgeError) as raised:
            project_run_once(receipt(command=Command.STATUS,
                                     payload={"action": "blocked", "blocker_code": "GOVERNANCE_GATE_BLOCKED"}))
        self.assertEqual((raised.exception.code, raised.exception.subject), (BridgeCode.INVALID_REQUEST, "command"))
        with self.assertRaises(VmBridgeError) as raised:
            project_run_once({"action": "blocked"})
        self.assertEqual(raised.exception.code, BridgeCode.INVALID_REQUEST)

    def test_refs_fall_back_to_payload_identifiers(self) -> None:
        view = project_run_once(receipt(refs=VmRefs(), payload={
            "action": "blocked", "blocker_code": "GOVERNANCE_GATE_FAILED",
            "project_id": "project-2", "task_id": "task-2", "attempt_id": "attempt-2"}))
        self.assertEqual((view.project_id, view.task_id, view.attempt_id, view.history_cursor),
                         ("project-2", "task-2", "attempt-2", None))

    def test_detail_reason_code_known_unknown_and_prefix(self) -> None:
        view = project_run_once(blocked("GOVERNANCE_GATE_BLOCKED", "GOVERNANCE_A2_PROFILE_REQUIRED: synthetic"))
        self.assertEqual(view.reason_code, "GOVERNANCE_A2_PROFILE_REQUIRED")
        self.assertEqual(view.detail, "GOVERNANCE_A2_PROFILE_REQUIRED: synthetic")
        view = project_run_once(blocked("GOVERNANCE_GATE_FAILED",
                                        "governance gate: GOVERNANCE_PROVIDER_UNSUPPORTED synthetic"))
        self.assertEqual(view.reason_code, "GOVERNANCE_PROVIDER_UNSUPPORTED")
        view = project_run_once(blocked("GOVERNANCE_GATE_PENDING", "GOVERNANCE_TIMEOUT"))
        self.assertEqual(view.reason_code, "GOVERNANCE_TIMEOUT")
        view = project_run_once(blocked("GOVERNANCE_GATE_BLOCKED", "GOVERNANCE_UNLISTED_REASON: synthetic"))
        self.assertIsNone(view.reason_code)
        self.assertEqual(view.detail, "GOVERNANCE_UNLISTED_REASON: synthetic")

    def test_contract_mismatch_check_ids(self) -> None:
        view = project_run_once(blocked(
            "GOVERNANCE_CONTRACT_MISMATCH",
            "GOVERNANCE_CONTRACT_MISMATCH: conformance:plan_shape: 기대 a, 관측 b"))
        self.assertEqual(view.reason_code, "GOVERNANCE_CONTRACT_MISMATCH")
        self.assertEqual(view.check_id, "conformance:plan_shape")
        self.assertEqual(view.conformance_check_id, "plan_shape")
        view = project_run_once(blocked(
            "GOVERNANCE_CONTRACT_MISMATCH", "GOVERNANCE_CONTRACT_MISMATCH: a2_profile: 기대 a, 관측 b"))
        self.assertEqual(view.check_id, "a2_profile")
        self.assertIsNone(view.conformance_check_id)

    def test_malformed_detail_yields_none_fields_without_error(self) -> None:
        cases = (
            "GOVERNANCE_CONTRACT_MISMATCH",
            "GOVERNANCE_CONTRACT_MISMATCH: no separator here",
            "GOVERNANCE_CONTRACT_MISMATCH:missing-space: x",
            "GOVERNANCE_CONTRACT_MISMATCHED: x: y",
            "synthetic free text GOVERNANCE_TIMEOUT",
            "",
        )
        for detail in cases:
            with self.subTest(detail=detail):
                view = project_run_once(blocked("GOVERNANCE_CONTRACT_MISMATCH", detail))
                self.assertIsNone(view.check_id)
                self.assertIsNone(view.conformance_check_id)
                self.assertEqual(view.state, GateState.CONTRACT_MISMATCH)
        view = project_run_once(receipt(payload={"action": "blocked", "blocker_code": "GOVERNANCE_GATE_BLOCKED",
                                                  "detail": 42}))
        self.assertIsNone(view.detail)
        self.assertIsNone(view.reason_code)

    def test_detail_never_changes_state(self) -> None:
        view = project_run_once(blocked(
            "GOVERNANCE_GATE_BLOCKED", "GOVERNANCE_CONTRACT_MISMATCH: conformance:plan_shape: 기대 a, 관측 b"))
        self.assertEqual((view.state, view.phase), (GateState.BLOCKED, GatePhase.BEFORE_EXECUTION))
        self.assertEqual(view.check_id, "conformance:plan_shape")
        view = project_run_once(blocked("GOVERNANCE_GATE_PENDING", "GOVERNANCE_GATE_FAILED: synthetic"))
        self.assertEqual(view.state, GateState.PENDING)

    def test_view_is_frozen(self) -> None:
        view = project_run_once(blocked("GOVERNANCE_GATE_BLOCKED"))
        self.assertIsInstance(view, GovernanceGateView)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.state = GateState.PENDING


class FinalReportProjectionTests(unittest.TestCase):
    def report(self, entries, *, include=True) -> BridgeReceipt:
        payload = {"project_id": "project-1"}
        if include:
            payload["governance_plugin_tasks"] = entries
        return receipt(command=Command.FINAL_REPORT, payload=payload)

    def test_entries_preserve_order_and_provenance(self) -> None:
        entries = (
            MappingProxyType({"task_id": "task-b", "task_ref": "T-2", "closure_tree_digest": "sha256:" + "b" * 64,
                              "plugin_version_label": "1.0-synthetic", "changed": True,
                              "changed_phases": ("before_completion",)}),
            {"task_id": "task-a", "task_ref": None, "closure_tree_digest": None,
             "plugin_version_label": None, "changed": False, "changed_phases": []},
        )
        observations = project_final_report(self.report(entries))
        self.assertEqual([item.task_id for item in observations], ["task-b", "task-a"])
        first = observations[0]
        self.assertIsInstance(first, PluginTaskObservation)
        self.assertEqual(first.changed_phases, ("before_completion",))
        self.assertTrue(first.changed)
        self.assertEqual(first.closure_provenance, "local_derived")
        self.assertEqual(first.label_provenance, "plugin_manifest_file")
        self.assertEqual(observations[1].changed_phases, ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.changed = False
        self.assertEqual(project_final_report(self.report([])), ())

    def test_missing_key_is_contract_mismatch(self) -> None:
        with self.assertRaises(VmBridgeError) as raised:
            project_final_report(self.report(None, include=False))
        self.assertEqual((raised.exception.code, raised.exception.subject),
                         (BridgeCode.CONTRACT_MISMATCH, "governance_plugin_tasks"))

    def test_malformed_entries_are_invalid_response(self) -> None:
        good = {"task_id": "task-1", "changed": False, "changed_phases": []}
        cases = (
            "not-a-list",
            None,
            ["not-a-mapping"],
            [dict(good, task_id="")],
            [dict(good, task_id=None)],
            [dict(good, changed="false")],
            [dict(good, changed_phases="before_execution")],
            [dict(good, changed_phases=["ok", 3])],
            [dict(good, plugin_version_label=1)],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(VmBridgeError) as raised:
                    project_final_report(self.report(entries))
                self.assertEqual((raised.exception.code, raised.exception.subject),
                                 (BridgeCode.INVALID_RESPONSE, "governance_plugin_tasks"))

    def test_wrong_command_is_invalid_request(self) -> None:
        with self.assertRaises(VmBridgeError) as raised:
            project_final_report(receipt(command=Command.RUN_ONCE, payload={"governance_plugin_tasks": []}))
        self.assertEqual(raised.exception.code, BridgeCode.INVALID_REQUEST)


class StatusProjectionTests(unittest.TestCase):
    def test_blocked_task_ids_in_original_order(self) -> None:
        payload = {"vm_tasks": [
            {"task_id": "task-3", "status": "blocked"},
            {"task_id": "task-1", "status": "running"},
            {"task_id": "task-2", "status": "blocked"},
        ]}
        self.assertEqual(project_status_blocked_tasks(receipt(command=Command.STATUS, payload=payload)),
                         ("task-3", "task-2"))
        self.assertEqual(project_status_blocked_tasks(receipt(command=Command.STATUS, payload={"vm_tasks": ()})), ())

    def test_missing_or_malformed_vm_tasks_is_invalid_response(self) -> None:
        for payload in ({}, {"vm_tasks": "x"}, {"vm_tasks": ["x"]}, {"vm_tasks": [{"task_id": "", "status": "blocked"}]},
                        {"vm_tasks": [{"task_id": "t", "status": None}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(VmBridgeError) as raised:
                    project_status_blocked_tasks(receipt(command=Command.STATUS, payload=payload))
                self.assertEqual((raised.exception.code, raised.exception.subject),
                                 (BridgeCode.INVALID_RESPONSE, "vm_tasks"))

    def test_wrong_command_is_invalid_request(self) -> None:
        with self.assertRaises(VmBridgeError) as raised:
            project_status_blocked_tasks(receipt(command=Command.FINAL_REPORT, payload={"vm_tasks": []}))
        self.assertEqual(raised.exception.code, BridgeCode.INVALID_REQUEST)


class ContractConstantTests(unittest.TestCase):
    def test_unreachable_facts_are_declared(self) -> None:
        self.assertEqual(UNREACHABLE_FACTS, (
            "contract_mismatch_phase", "operation_no_effect", "conformance_check_list",
            "gate_core_operations", "governance_blocked_history"))

    def test_module_does_not_import_vm_or_ags_internals(self) -> None:
        source = (ROOT / "src/aik_vm_bridge/governance.py").read_text(encoding="utf-8")
        for forbidden in ("flowmarshal", "vibemarshal", "agent_governance", "sqlite3"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
