"""Minimal live diagnostic of the AIK-030 JEV bridge against the real service.

usage: python tools/probe_jev.py REPORT.json [--base-url URL]

It sends one synthetic RAW prompt and three synthetic skill candidates through
``JevBridge`` -> ``JevClient`` -> ``UrllibTransport``. The tool never reads a
token: authentication must be injected by the egress gateway. The journal is
written to a temporary directory and deleted. The report holds no RAW text, no
token and no response body; only observed fields, digests and usage.

This is wire-level evidence, not qualification.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aik_jev import (  # noqa: E402
    AgentSelection, CommonReq, JevBridge, JevClient, JevJournal, RawPrompt, SkillCandidate,
)
from aik_jev.client import DEFAULT_BASE_URL, OBSERVED_CONTRACT_VERSION, UrllibTransport  # noqa: E402
from aik_jev.errors import JevError  # noqa: E402

RAW_TEXT = "파서 모듈에 단위 테스트를 추가해 줘. 기존 동작은 바꾸지 마."
SKILLS = (
    ("write-tests", "Add or extend automated tests for existing code"),
    ("refactor", "Restructure code without changing behavior"),
    ("write-docs", "Write or update documentation"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    report = {
        "tool": "tools/probe_jev.py", "observed_contract_version": OBSERVED_CONTRACT_VERSION,
        "base_url": args.base_url, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "authorization": "egress_gateway_injected", "qualification": "NOT_RUN",
    }
    client = JevClient(UrllibTransport(), base_url=args.base_url, timeout=60)
    try:
        report["models"] = [{"name": m.name, "release_date": m.release_date} for m in client.list_models()]
    except JevError as error:
        report["models_error"] = error.code
    raw = RawPrompt(RAW_TEXT)
    skills = tuple(SkillCandidate(skill_id, text, "agent_manifest") for skill_id, text in SKILLS)
    req = CommonReq(raw.sha256, "development", "parser", skills, ("no behavior change",), "code_verified")
    with tempfile.TemporaryDirectory() as directory:
        bridge = JevBridge(JevJournal(Path(directory) / "jev.sqlite3"), client=client, mode="on",
                           vendor="anthropic")
        result = bridge.classify("probe-1", raw, req)
        # The synthetic AGENT deliberately picks a different skill to show the two records stay separate.
        selection = bridge.record_agent_selection(
            result.classification_id, AgentSelection("probe-agent", "refactor"))
        stored, selections = bridge.view(result.classification_id)
    report.update({
        "raw_sha256": raw.sha256, "raw_bytes": raw.byte_length, "req_sha256": req.sha256,
        "classification": {
            "status": stored.status, "source": stored.source, "requested_model": stored.requested_model,
            "observed_model": stored.observed_model, "recommended_skill": stored.recommended_skill,
            "confidence": stored.confidence, "probabilities": [list(item) for item in stored.probabilities],
            "usage_input_tokens": stored.usage_input_tokens, "usage_output_tokens": stored.usage_output_tokens,
            "failure_code": stored.failure_code,
        },
        "agent_selection": {
            "selected_skill_id": selection.selected_skill_id, "provenance": selection.provenance,
            "matches_recommendation": selection.matches_recommendation,
            "outside_candidates": selection.outside_candidates, "records": len(selections),
        },
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report["classification"], ensure_ascii=False))
    return 0 if stored.status == "classified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
