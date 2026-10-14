"""Live check of the controller's optional JEV step (B2) against the real JEV service.

usage: python tools/probe_controller_jev.py REPORT.json [--base-url URL]

``Controller.start_goal`` runs with ``GoalClassifier`` in ON mode over the real
``JevBridge`` -> ``JevClient`` -> ``UrllibTransport``. The VM side is a local
recording stub: no VM installation is used, so this is not VM evidence. The
tool never reads a token; authentication must be injected by the egress
gateway. The journal is written to a temporary directory and deleted. The
report holds no RAW text, no token and no response body.

This is wire-level evidence, not qualification.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aik_controller import Controller  # noqa: E402
from aik_jev import (  # noqa: E402
    AgentSelection, CommonReq, GoalClassifier, JevBridge, JevClient, JevJournal, SkillCandidate,
)
from aik_jev.client import DEFAULT_BASE_URL, OBSERVED_CONTRACT_VERSION, UrllibTransport  # noqa: E402

GOAL_TEXT = "README의 설치 절차를 Windows 기준으로 다시 써 줘. 코드는 건드리지 마."
SKILLS = (
    ("write-tests", "Add or extend automated tests for existing code"),
    ("refactor", "Restructure code without changing behavior"),
    ("write-docs", "Write or update documentation"),
)


class RecordingVm:
    """Local stand-in for the VM public commands; it only records what it receives."""

    def __init__(self, order):
        self.order = order
        self.received = []

    def prepare(self, request):
        self.order.append("vm.prepare")
        self.received.append(request.source_request)
        return {"project_id": request.project_id}

    def _unused(self, request):
        raise AssertionError("not used by this probe")

    revise = run_once = observe = pause = cancel = status = replan = final_report = _unused


class AllowAll:
    def __init__(self, order):
        self.order = order

    def check(self, command, *, project_id, request_id, task_id=None):
        self.order.append("authority." + command.value)


class NoDelegation:
    def prepare_run(self, project_id, task, request_id):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    report = {
        "tool": "tools/probe_controller_jev.py", "observed_contract_version": OBSERVED_CONTRACT_VERSION,
        "base_url": args.base_url, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "authorization": "egress_gateway_injected", "vm": "local_recording_stub", "qualification": "NOT_RUN",
    }
    skills = tuple(SkillCandidate(skill_id, text, "agent_manifest") for skill_id, text in SKILLS)
    order: list = []
    vm = RecordingVm(order)
    client = JevClient(UrllibTransport(), base_url=args.base_url, timeout=60)
    with tempfile.TemporaryDirectory() as directory:
        bridge = JevBridge(JevJournal(Path(directory) / "jev.sqlite3"), client=client, mode="on",
                           vendor="anthropic")
        classifier = GoalClassifier(bridge, lambda raw: CommonReq(
            raw.sha256, "documentation", "README", skills, ("no code change",), "code_verified"))

        def traced(request_id, goal_text):
            order.append("classify")
            return classifier(request_id, goal_text)

        controller = Controller(vm, project_id="probe-project", delegation=NoDelegation(),
                                authority=AllowAll(order), run_id="probe-ctl", classifier=traced)
        controller.start_goal(GOAL_TEXT)
        (note,) = controller.goal_classifications
        selection = None
        if note.classification_id is not None:
            # The synthetic AGENT reports its own choice; the record stays separate from the classification.
            selection = bridge.record_agent_selection(
                note.classification_id, AgentSelection("probe-agent", "write-docs"))
            stored, selections = bridge.view(note.classification_id)
    goal_sha256 = hashlib.sha256(GOAL_TEXT.encode("utf-8")).hexdigest()
    report.update({
        "order": order,
        "goal_sha256": goal_sha256,
        "vm_received_sha256": [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in vm.received],
        "note": {"request_id": note.request_id, "command": note.command, "status": note.status,
                 "error_code": note.error_code},
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if selection is not None:
        report["classification"] = {
            "request_id": stored.request_id, "raw_sha256": stored.raw_sha256, "status": stored.status,
            "source": stored.source, "requested_model": stored.requested_model,
            "observed_model": stored.observed_model, "recommended_skill": stored.recommended_skill,
            "confidence": stored.confidence, "probabilities": [list(item) for item in stored.probabilities],
            "usage_input_tokens": stored.usage_input_tokens, "usage_output_tokens": stored.usage_output_tokens,
            "failure_code": stored.failure_code,
        }
        report["agent_selection"] = {
            "selected_skill_id": selection.selected_skill_id, "provenance": selection.provenance,
            "matches_recommendation": selection.matches_recommendation,
            "outside_candidates": selection.outside_candidates, "records": len(selections),
        }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report["note"], ensure_ascii=False))
    return 0 if note.status == "classified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
