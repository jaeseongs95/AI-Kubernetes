"""B2: the optional JEV step in the controller's goal preparation (AIK-030 / AIK-026).

The real Controller, VmBridge (over the synthetic VM of test_controller),
JevBridge, JevClient and JevJournal are used. JEV is reached only through the
fake transport of test_jev; no network, token or VM installation is used.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_controller as ctl  # noqa: E402
import test_jev as jev_tests  # noqa: E402
from aik_http.transport import TransportTimeout  # noqa: E402
from aik_authority import AuthorityCode, AuthorityDenied  # noqa: E402
from aik_controller import Controller, GoalClassificationNote, InvalidControllerConfig  # noqa: E402
from aik_jev import (  # noqa: E402
    AgentSelection, CommonReq, GoalClassifier, JevBridge, JevClient, JevError, JevJournal, RawPrompt,
)
from aik_vm_bridge import Command, VmBridge  # noqa: E402


GOAL = jev_tests.RAW_TEXT  # whitespace, quotes and CR/LF must reach both VM and JEV unchanged
REVISED = "Revise: also cover the error path."


class OrderedVm(ctl.FakeVm):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def prepare(self, project_id, *, source_request, candidate_count=None):
        self.order.append("vm.prepare")
        return super().prepare(project_id, source_request=source_request, candidate_count=candidate_count)

    def revise(self, project_id, *, source_request, candidate_count=None):
        self.order.append("vm.revise")
        self.revised = source_request
        return super().revise(project_id, source_request=source_request, candidate_count=candidate_count)


class OrderedAuthority(ctl.FakeAuthority):
    def __init__(self, order, **kwargs):
        super().__init__(**kwargs)
        self.order = order

    def check(self, command, *, project_id, request_id, task_id=None):
        self.order.append("authority." + Command(command).value)
        return super().check(command, project_id=project_id, request_id=request_id, task_id=task_id)


class ControllerJevTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-controller-jev-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.order: list = []
        self.req_calls: list = []

    def req_for_raw(self, raw):
        self.req_calls.append(raw.sha256)
        return CommonReq.for_raw(raw, work_mode="implementation", target="src/example",
                                 skills=jev_tests.SKILLS)

    def jev(self, responses=(), *, mode="on"):
        self.transport = jev_tests.FakeTransport(responses)
        self.journal = JevJournal(self.root / "jev.sqlite3")
        bridge = JevBridge(self.journal, client=JevClient(self.transport), mode=mode, vendor="anthropic")
        classifier = GoalClassifier(bridge, self.req_for_raw)

        def traced(request_id, goal_text):
            self.order.append("classify")
            return classifier(request_id, goal_text)

        return bridge, traced

    def controller(self, classifier=None, **authority):
        self.vm = OrderedVm(self.order)
        self.authority = OrderedAuthority(self.order, **authority)
        return Controller(VmBridge(self.vm, ctl.SURFACE, now=lambda: ctl.NOW), project_id=ctl.PROJECT,
                          delegation=ctl.FakeDelegation(), authority=self.authority, sleep=lambda seconds: None,
                          run_id="run-jev", classifier=classifier)

    def prepared_text(self):
        return [details["source_request"] for name, details in self.vm.calls if name == "prepare"]

    def test_default_has_no_classifier_step(self) -> None:
        controller = self.controller()
        controller.start_goal(GOAL)
        self.assertEqual(self.order, ["authority.prepare", "vm.prepare"])
        self.assertEqual(self.prepared_text(), [GOAL])
        self.assertEqual(controller.goal_classifications, ())

    def test_off_mode_records_without_calling_jev(self) -> None:
        bridge, classifier = self.jev(mode="off")
        controller = self.controller(classifier)
        controller.start_goal(GOAL)
        self.assertEqual(self.order, ["authority.prepare", "classify", "vm.prepare"])
        self.assertEqual(self.transport.requests, [])
        (note,) = controller.goal_classifications
        self.assertEqual((note.request_id, note.command, note.status, note.error_code),
                         ("run-jev-000001", "prepare", "off", None))
        stored, selections = bridge.view(note.classification_id)
        self.assertEqual((stored.request_id, stored.source, stored.mode), ("run-jev-000001", "none", "off"))
        self.assertEqual(selections, ())
        self.assertEqual(self.prepared_text(), [GOAL])

    def test_on_mode_classifies_the_unchanged_goal_before_prepare(self) -> None:
        bridge, classifier = self.jev([jev_tests.json_response(jev_tests.success_payload())])
        controller = self.controller(classifier)
        controller.start_goal(GOAL)
        self.assertEqual(self.order, ["authority.prepare", "classify", "vm.prepare"])
        (request,) = self.transport.requests
        body = json.loads(request.body.decode("utf-8"))
        self.assertEqual(body["state"]["raw"], GOAL)
        self.assertNotIn(GOAL, json.dumps(body["state"]["req"], ensure_ascii=False))
        self.assertEqual(self.prepared_text(), [GOAL])
        (note,) = controller.goal_classifications
        self.assertEqual((note.status, note.error_code), ("classified", None))
        stored, _ = bridge.view(note.classification_id)
        self.assertEqual((stored.source, stored.recommended_skill), ("jev", "test-writer"))
        # The AGENT's final choice stays a separate record; the controller never makes it.
        selection = bridge.record_agent_selection(note.classification_id, AgentSelection("agent-1", "code-review"))
        self.assertIs(selection.matches_recommendation, False)
        self.assertEqual(bridge.view(note.classification_id)[0], stored)

    def test_unavailable_jev_does_not_block_prepare(self) -> None:
        _, classifier = self.jev([TransportTimeout()])
        controller = self.controller(classifier)
        controller.start_goal(GOAL)
        (note,) = controller.goal_classifications
        self.assertEqual((note.status, note.error_code), ("unavailable", None))
        self.assertEqual(self.prepared_text(), [GOAL])
        self.assertEqual(len(self.transport.requests), 1)  # no retry

    def test_classifier_exception_is_noted_and_prepare_still_runs(self) -> None:
        bridge, _ = self.jev(mode="off")

        def mismatched(raw):  # a REQ bound to another RAW: the bridge refuses it
            return CommonReq.for_raw(RawPrompt("other text"), work_mode="implementation",
                                     skills=jev_tests.SKILLS)

        controller = self.controller(GoalClassifier(bridge, mismatched))
        controller.start_goal(GOAL)
        self.assertEqual(controller.goal_classifications,
                         (GoalClassificationNote("run-jev-000001", "prepare", None, None, "RAW_REQ_MISMATCH"),))
        self.assertEqual(self.prepared_text(), [GOAL])

        def broken(request_id, goal_text):
            raise RuntimeError("synthetic classifier failure")

        def wrong(request_id, goal_text):
            return object()

        for classifier, code in ((broken, "CLASSIFIER_FAILED"), (wrong, "CLASSIFIER_RESULT_INVALID")):
            controller = self.controller(classifier)
            controller.start_goal(GOAL)
            self.assertEqual([note.error_code for note in controller.goal_classifications], [code])
            self.assertEqual(self.prepared_text(), [GOAL])

    def test_denied_prepare_sends_nothing_to_jev(self) -> None:
        _, classifier = self.jev()
        controller = self.controller(classifier, deny={Command.PREPARE: AuthorityCode.POLICY_UNKNOWN})
        with self.assertRaises(AuthorityDenied):
            controller.start_goal(GOAL)
        self.assertEqual(self.order, ["authority.prepare"])
        self.assertEqual((self.transport.requests, self.req_calls, controller.goal_classifications), ([], [], ()))

    def test_revise_is_classified_under_its_own_request(self) -> None:
        bridge, classifier = self.jev([jev_tests.json_response(jev_tests.success_payload()),
                                       jev_tests.json_response(jev_tests.success_payload("doc-writer"))])
        controller = self.controller(classifier)
        controller.start_goal(GOAL)
        controller.revise_goal(REVISED)
        first, second = controller.goal_classifications
        self.assertEqual([(note.request_id, note.command) for note in (first, second)],
                         [("run-jev-000001", "prepare"), ("run-jev-000002", "revise")])
        self.assertEqual(bridge.view(second.classification_id)[0].recommended_skill, "doc-writer")
        self.assertEqual(self.vm.revised, REVISED)
        self.assertEqual(self.order[-3:], ["authority.revise", "classify", "vm.revise"])

    def test_invalid_configuration_is_rejected(self) -> None:
        bridge, _ = self.jev(mode="off")
        with self.assertRaises(InvalidControllerConfig):
            self.controller(classifier="not callable")
        with self.assertRaises(JevError):
            GoalClassifier(object(), self.req_for_raw)
        with self.assertRaises(JevError):
            GoalClassifier(bridge, None)
        with self.assertRaises(JevError) as caught:
            GoalClassifier(bridge, lambda raw: "not a REQ")("request-1", GOAL)
        self.assertEqual(caught.exception.code, "INVALID_REQ")


if __name__ == "__main__":
    unittest.main()
