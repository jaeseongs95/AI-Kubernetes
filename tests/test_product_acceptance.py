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
import sqlite3
from contextlib import closing
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
        self.assertEqual(spec["phases"]["M4"]["platforms"], ["WIN-X64", "LINUX-X64"])

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
                 "docs/development-backlog.ko.md", "planning/backlog.sqlite3"]
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
        self.assertIn("PLATFORM_EVIDENCE_COVERAGE", result["reasons"])
        report["platforms"] = {"WIN-X64": dict(report, platform_id="WIN-X64")}
        self.assertIn("WIN-X64:JOURNEY_P04_NOT_QUALIFIED",
                      release_preflight(report, spec)["reasons"])

    def test_preflight_rejects_missing_evidence_and_unqualified_client(self):
        spec = validate_contract()
        report = {"scope": "M4", "gate": "AIK-064", "host_qualification": {"status": "NOT_RUN"}}
        actual = release_preflight(report, spec)
        self.assertEqual(actual["status"], "BLOCKED")
        self.assertIn("PLATFORM_EVIDENCE_COVERAGE", actual["reasons"])
        report["platforms"] = {"WIN-X64": dict(report, platform_id="WIN-X64")}
        reasons = release_preflight(report, spec)["reasons"]
        self.assertIn("WIN-X64:CLEAN_CLIENT_NOT_PROVEN", reasons)
        self.assertIn("WIN-X64:JOURNEY_EVIDENCE_COVERAGE", reasons)

    def test_approved_windows_only_report_still_blocks_linux_release(self):
        # Original v1 accepted this fully green Windows-only receipt when approved.
        spec = copy.deepcopy(validate_contract())
        spec["decision_status"] = spec["matrix"]["decision_status"] = "confirmed"
        spec["authority"] = "qualified"
        report = self._green_platform(spec, "WIN-X64", "Windows 11 26H2 x64", "b" * 64)
        report.update(scope="M4", contract_blob_sha=blob_sha((ROOT / "contracts/product-acceptance/v1.json").read_bytes()),
                      matrix_blob_sha=spec["matrix"]["git_blob_sha"])
        result = release_preflight(report, spec)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("PLATFORM_EVIDENCE_COVERAGE", result["reasons"])

    def _green_platform(self, spec, platform_id, os_name, installer):
        return {"platform_id": platform_id, "gate": "AIK-064", "build_commit": "a" * 40,
                "installer_sha256": installer, "critical_or_high_defects": 0,
                "host_qualification": {"status": "PASS", "platform_id": platform_id,
                    "family": "windows" if platform_id == "WIN-X64" else "linux", "architecture": "x64",
                    "os": os_name, "standard_user": True, "development_tools_absent": True,
                    "support_definition_evidence_ref": "synthetic-supported-host"},
                "provenance": {"independent_review": True, "review_evidence_ref": "synthetic-review"},
                "journeys": {f"P{i:02d}": {"status": "PASS", "platform_id": platform_id,
                    "build_commit": "a" * 40, "installer_sha256": installer,
                    "matrix_blob_sha": spec["matrix"]["git_blob_sha"], "evidence_ref": "synthetic",
                    "independent_oracle": True} for i in range(1, 11)},
                "metrics": {k.replace("_max", ""): 0 for k in spec["fixed_measurement_candidate"]["thresholds"]},
                "threshold_approval_evidence_ref": "synthetic-thresholds",
                "live_required_routes_qualified": True, "selected_optional_dependencies_qualified": True}

    def _two_platform_report(self):
        spec = copy.deepcopy(validate_contract())
        spec["decision_status"] = spec["matrix"]["decision_status"] = "confirmed"
        spec["authority"] = "qualified"
        for platform_id, os_name in (("WIN-X64", "Windows 11 26H2 x64"), ("LINUX-X64", "Synthetic Linux x64")):
            spec["platforms"][platform_id].update(support_status="confirmed", supported_os_candidates=[os_name])
        report = {"scope": "M4", "gate": "AIK-064", "build_commit": "a" * 40,
                  "contract_blob_sha": blob_sha((ROOT / "contracts/product-acceptance/v1.json").read_bytes()),
                  "matrix_blob_sha": spec["matrix"]["git_blob_sha"], "platforms": {
                      "WIN-X64": self._green_platform(spec, "WIN-X64", "Windows 11 26H2 x64", "b" * 64),
                      "LINUX-X64": self._green_platform(spec, "LINUX-X64", "Synthetic Linux x64", "c" * 64)}}
        return spec, report

    def test_each_platform_requires_its_own_host_installer_journeys_and_metrics(self):
        spec, report = self._two_platform_report()
        self.assertEqual(release_preflight(report, spec)["status"], "READY_FOR_INDEPENDENT_RELEASE_REVIEW")
        for field, value, code in (("platform_id", "WIN-X64", "PLATFORM_BUILD_IDENTITY"),
                                   ("build_commit", "d" * 40, "PLATFORM_BUILD_IDENTITY"),
                                   ("host_qualification", report["platforms"]["WIN-X64"]["host_qualification"], "CLEAN_CLIENT_NOT_PROVEN"),
                                   ("journeys", report["platforms"]["WIN-X64"]["journeys"], "JOURNEY_P01_NOT_QUALIFIED"),
                                   ("metrics", {}, "PERFORMANCE_NOT_QUALIFIED"),
                                   ("live_required_routes_qualified", False, "REQUIRED_ROUTES_UNQUALIFIED")):
            with self.subTest(field=field):
                bad = copy.deepcopy(report)
                bad["platforms"]["LINUX-X64"][field] = value
                result = release_preflight(bad, spec)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertIn("LINUX-X64:" + code, result["reasons"])
        spec["platforms"]["LINUX-X64"].update(support_status="pending_AIK-003", supported_os_candidates=[])
        self.assertIn("LINUX-X64:PLATFORM_SUPPORT_NOT_CONFIRMED", release_preflight(report, spec)["reasons"])

    def test_missing_linux_contract_or_phase_coverage_is_rejected(self):
        for mutation in ("platforms", "phase"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                root = self._candidate_root(Path(d))
                filename = root / "contracts/product-acceptance/v1.json"
                data = json.loads(filename.read_text(encoding="utf-8"))
                if mutation == "platforms":
                    del data["platforms"]["LINUX-X64"]
                else:
                    data["phases"]["M4"]["platforms"] = ["WIN-X64"]
                filename.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ContractError):
                    validate_contract(root)

    def test_contract_checks_the_current_ledger_platform_scope(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._candidate_root(Path(d))
            with closing(sqlite3.connect(root / "planning/backlog.sqlite3")) as conn:
                conn.execute("DELETE FROM platforms WHERE id='LINUX-X64'")
                conn.commit()
            with self.assertRaisesRegex(ContractError, "LEDGER_PLATFORM_SCOPE"):
                validate_contract(root)

    def test_platform_failure_or_missing_previous_gate_blocks_the_whole_release(self):
        spec, report = self._two_platform_report()
        for status in ("FAIL", "NOT_RUN", "BLOCKED", "SKIP", "UNKNOWN"):
            with self.subTest(status=status):
                bad = copy.deepcopy(report)
                bad["platforms"]["LINUX-X64"]["journeys"]["P01"]["status"] = status
                self.assertEqual(release_preflight(bad, spec)["status"], "BLOCKED")
        report.update(scope="M5", gate="AIK-089")
        for platform_id, row in report["platforms"].items():
            row.update(gate="AIK-089", previous_gate_status="PASS", previous_gate_on_same_build=True)
            row["journeys"]["P11"] = dict(row["journeys"]["P01"])
        self.assertEqual(release_preflight(report, spec)["status"], "READY_FOR_INDEPENDENT_RELEASE_REVIEW")
        report["platforms"]["LINUX-X64"]["previous_gate_on_same_build"] = False
        self.assertIn("LINUX-X64:PREVIOUS_GATE_NOT_PROVEN", release_preflight(report, spec)["reasons"])

    def test_cli_contract_runs_without_products_or_secrets(self):
        proc = subprocess.run([sys.executable, str(ROOT / "tools/product_acceptance.py"), "validate"],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "VALID_DRAFT")


if __name__ == "__main__":
    unittest.main()
