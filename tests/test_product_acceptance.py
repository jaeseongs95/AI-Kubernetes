"""AIK-083 contract and independent-file oracle regression tests (stdlib only)."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from product_acceptance import (ContractError, blob_sha, check_oracle, load_json,
                                release_preflight, validate_contract)


class AcceptanceContractTests(unittest.TestCase):
    def test_contract_maps_all_twelve_journeys_and_original_release_gates(self):
        spec = validate_contract()
        self.assertEqual([item["id"] for item in spec["journeys"]],
                         [f"P{i:02d}" for i in range(1, 13)])
        self.assertEqual([spec["phases"][p]["gate"] for p in ("M4", "M5", "M6")],
                         ["AIK-064", "AIK-089", "AIK-090"])
        self.assertEqual(spec["decision_status"], "proposed")

    def test_git_blob_source_binding_is_correct_and_sensitive_to_bytes(self):
        spec = validate_contract()
        matrix = ROOT / spec["matrix"]["path"]
        original = matrix.read_bytes()
        self.assertEqual(blob_sha(original), spec["matrix"]["git_blob_sha"])
        self.assertNotEqual(blob_sha(original + b" "), spec["matrix"]["git_blob_sha"])

    def _candidate_root(self, temporary: Path) -> Path:
        files = ["contracts/product-acceptance/v1.json",
                 "contracts/upstream/support-matrix.json",
                 "contracts/product-acceptance/fixture/project/orders.csv",
                 "contracts/product-acceptance/fixture/project/policy.json",
                 "contracts/product-acceptance/fixture/project/settlement.py",
                 "docs/development-backlog.ko.md"]
        for name in files:
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        return temporary

    def test_missing_journey_cannot_pass_a_document_only_audit(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._candidate_root(Path(d))
            filename = root / "contracts/product-acceptance/v1.json"
            data = json.loads(filename.read_text(encoding="utf-8"))
            data["journeys"] = data["journeys"][:-1]
            filename.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "JOURNEY_COUNT"):
                validate_contract(root)

    def test_missing_gui_owner_or_wrong_gate_fails(self):
        for mutation in ("gui", "gate"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                root = self._candidate_root(Path(d))
                filename = root / "contracts/product-acceptance/v1.json"
                data = json.loads(filename.read_text(encoding="utf-8"))
                if mutation == "gui":
                    data["journeys"][0]["owners"]["gui"] = []
                else:
                    data["journeys"][0]["gate"] = "AIK-089"
                filename.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ContractError):
                    validate_contract(root)

    def test_matrix_bytes_or_fixture_input_change_never_pass(self):
        for name in ("contracts/upstream/support-matrix.json",
                     "contracts/product-acceptance/fixture/project/orders.csv"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root = self._candidate_root(Path(d))
                with (root / name).open("ab") as handle:
                    handle.write(b" ")
                with self.assertRaises(ContractError):
                    validate_contract(root)

    def _copy_project(self, path: Path):
        shutil.copytree(ROOT / "contracts/product-acceptance/fixture/project", path)

    def test_starter_project_does_not_already_satisfy_the_oracle(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / "한글 공백 프로젝트"
            self._copy_project(project)
            with self.assertRaisesRegex(ContractError, "ARTIFACT_MISSING"):
                check_oracle(project)
            process = subprocess.run([sys.executable, str(project / "settlement.py")],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            with self.assertRaisesRegex(ContractError, "ARTIFACT_SCHEMA"):
                check_oracle(project)

    def test_literal_independent_expected_values_are_exact(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / "한글 공백 프로젝트"
            self._copy_project(project)
            example_result = {
                "rows": [
                    {"order_id": "A-001", "net_krw": 10000, "vat_krw": 1000, "gross_krw": 11000},
                    {"order_id": "B-002", "net_krw": 20005, "vat_krw": 2001, "gross_krw": 22006},
                    {"order_id": "C-003", "net_krw": 11505, "vat_krw": 1151, "gross_krw": 12656}
                ],
                "totals": {"net_krw": 41510, "vat_krw": 4152, "gross_krw": 45662}
            }
            artifact = project / "settlement.json"
            artifact.write_text(json.dumps(example_result, ensure_ascii=False), encoding="utf-8")
            receipt = check_oracle(project)
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["gross_krw"], 45662)
            self.assertEqual(receipt["artifact_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
            self.assertEqual(receipt["governed_agent_e2e"], "NOT_RUN")

            example_result["rows"][1]["vat_krw"] = True
            artifact.write_text(json.dumps(example_result), encoding="utf-8")
            with self.assertRaises(ContractError):
                check_oracle(project)
            example_result["rows"][1]["vat_krw"] = 2001
            example_result["totals"]["vat_krw"] = 4151
            artifact.write_text(json.dumps(example_result), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "ARTIFACT_TOTAL_MISMATCH"):
                check_oracle(project)

    def test_symlink_or_modified_input_cannot_reuse_fixture_qualification(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / "workspace"
            self._copy_project(project)
            with (project / "orders.csv").open("ab") as stream:
                stream.write(b"x")
            with self.assertRaisesRegex(ContractError, "FIXTURE_INPUT_CHANGED"):
                check_oracle(project)

    def test_duplicate_json_keys_cannot_hide_fake_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            filename = Path(d) / "duplicate.json"
            filename.write_text('{"status":"PASS","status":"FAIL"}', encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "DUPLICATE_JSON_KEY"):
                load_json(filename)

    def test_release_stays_blocked_even_for_self_attested_green_json(self):
        spec = validate_contract()
        report = {
            "scope": "M4", "gate": "AIK-064",
            "contract_blob_sha": blob_sha((ROOT / "contracts/product-acceptance/v1.json").read_bytes()),
            "matrix_blob_sha": spec["matrix"]["git_blob_sha"],
            "build_commit": "a" * 40, "installer_sha256": "b" * 64,
            "critical_or_high_defects": 0,
            "host_qualification": {"status": "PASS", "os": "Windows 11 26H2 x64",
                                   "standard_user": True, "development_tools_absent": True},
            "provenance": {"independent_review": True, "review_evidence_ref": "synthetic"},
            "journeys": {
                f"P{i:02d}": {"status": "PASS", "build_commit": "a" * 40,
                              "installer_sha256": "b" * 64,
                              "matrix_blob_sha": spec["matrix"]["git_blob_sha"],
                              "evidence_ref": "synthetic", "independent_oracle": True}
                for i in range(1, 11)
            },
            "metrics": {key.replace("_max", ""): 0 for key in
                        spec["fixed_measurement_candidate"]["thresholds"]},
            "threshold_approval_evidence_ref": "synthetic",
            "live_required_routes_qualified": True,
            "selected_optional_dependencies_qualified": True,
        }
        result = release_preflight(report, spec)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("UNAPPROVED_SCOPE_OR_MATRIX", result["reasons"])
        self.assertFalse(result["release_approved"])
        report["journeys"]["P04"]["build_commit"] = "c" * 40
        self.assertIn("JOURNEY_P04_NOT_QUALIFIED",
                      release_preflight(report, spec)["reasons"])

    def test_preflight_rejects_missing_evidence_and_unqualified_client(self):
        spec = validate_contract()
        report = {"scope": "M4", "gate": "AIK-064", "host_qualification": {"status": "NOT_RUN"}}
        actual = release_preflight(report, spec)
        self.assertEqual(actual["status"], "BLOCKED")
        self.assertIn("CLEAN_CLIENT_NOT_PROVEN", actual["reasons"])
        self.assertIn("JOURNEY_EVIDENCE_COVERAGE", actual["reasons"])

    def test_cli_contract_runs_without_products_or_secrets(self):
        proc = subprocess.run([sys.executable, str(ROOT / "tools/product_acceptance.py"), "validate"],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "VALID_DRAFT")


if __name__ == "__main__":
    unittest.main()
