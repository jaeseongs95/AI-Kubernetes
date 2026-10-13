"""AIK-008 historical installed evidence and fail-closed route diagnosis."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from aik_compat.diagnostics import DiagnosticError, inspect, read_json


def sources():
    folders = ROOT / "contracts" / "upstream"
    return [read_json(folders / name) for name in (
        "installed-inspection.json", "baseline.json", "support-matrix.json",
        "integration-contract.json",
    )]


def diagnose(*, modify=None, source="historical"):
    data = sources()
    if modify:
        modify(data)
    return inspect(*[value for entry in data for value in entry], source=source)


class UpstreamDiagnosticTests(unittest.TestCase):
    def test_real_pinned_inspection_exposes_a2_and_plugin_blockers(self):
        result = diagnose()
        self.assertEqual(result["format"], "aik-upstream-diagnostic-v1")
        self.assertEqual(result["compatibility"], "INCOMPATIBLE")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["qualification"], "NOT_QUALIFIED")
        self.assertEqual(result["source_kind"], "historical")
        self.assertEqual(result["installed_conformance"], "NOT_RUN")
        self.assertIn("A2_NAMESPACE_UNAVAILABLE", result["codes"])
        self.assertIn("PLUGIN_INCOMPATIBLE", result["codes"])
        self.assertIn("AGS_A2_NAMESPACE_UNAVAILABLE", result["upstream_blockers"])
        self.assertIn("AGS_HOST_INTEGRATION_MANIFEST_MISSING", result["upstream_blockers"])
        self.assertFalse(result["gui"]["ready_for_dispatch"])

    def test_route_statuses_and_names_remain_separate_and_unqualified(self):
        report = diagnose()
        entries = report["routes"]
        self.assertEqual(len(entries), 12)
        self.assertEqual(len({row["route_id"] for row in entries}), len(entries))
        self.assertEqual({row["provider"] for row in entries}, {"openai", "anthropic"})
        self.assertEqual({row["auth_kind"] for row in entries}, {"api_key", "native_subscription"})
        self.assertEqual({row["role"] for row in entries}, {"director", "developer", "reviewer"})
        for row in entries:
            self.assertTrue(row["required"])
            self.assertFalse(row["qualified"])
            self.assertEqual(row["state"], "BLOCKED")
            self.assertIn("ROUTE_MODEL_PROBE_NOT_RUN", row["codes"])
            self.assertIn("ROUTE_GOVERNED_QUALIFICATION_NOT_RUN", row["codes"])
            self.assertIn("ROUTE_ADAPTER_NOT_QUALIFIED", row["codes"])
            self.assertNotIn("ROUTE_MATRIX_STALE", row["codes"])

    def test_reference_sha_and_pinned_versions_are_bound(self):
        result = diagnose()
        self.assertEqual(len(result["baseline_sha256"]), 64)
        self.assertEqual(len(result["inspection_sha256"]), 64)
        self.assertEqual(len(result["matrix_sha256"]), 64)
        self.assertEqual(result["matrix_revision"], "1")
        self.assertEqual(result["ags_revision"], "56fef8bd189377b80f4020f506e717ab292b260a")
        self.assertEqual(result["vm_revision"], "32bb0f9dd9f024045d24487312b50f5b703573a3")

    def test_corrupt_observed_ags_bytes_block_even_when_status_is_forged_pass(self):
        def mutate(data):
            data[0][0]["checks"]["ags_bytes"]["files"][0]["sha256"] = "f" * 64
            data[0][0]["checks"]["ags_bytes"]["status"] = "PASS"
            data[0][0]["status"] = "PASS"
        report = diagnose(modify=mutate)
        self.assertIn("AGS_ARTIFACT_MISMATCH", report["codes"])
        self.assertEqual(report["status"], "BLOCKED")

    def test_forged_vm_wheel_and_distribution_status_cannot_override_identity(self):
        def mutate(data):
            data[0][0]["checks"]["vm_wheel"]["status"] = "PASS"
            data[0][0]["checks"]["vm_wheel"]["observed_sha256"] = "0" * 64
            data[0][0]["vm_installed"]["version"] = "999.0"
        report = diagnose(modify=mutate)
        self.assertIn("VM_ARTIFACT_MISMATCH", report["codes"])

    def test_forged_a2_pass_without_hello_epoch_stays_incompatible(self):
        def mutate(data):
            inspection = data[0][0]
            inspection["checks"]["ags_vm_a2_namespace"]["status"] = "PASS"
            inspection["status"] = "PASS"
        report = diagnose(modify=mutate)
        self.assertIn("A2_NAMESPACE_UNAVAILABLE", report["codes"])
        self.assertEqual(report["compatibility"], "INCOMPATIBLE")

    def test_missing_conformance_command_is_not_confused_with_success(self):
        def mutate(data):
            data[0][0]["commands"].pop("vm_metadata")
        report = diagnose(modify=mutate)
        self.assertIn("INSPECTION_INCOMPLETE", report["codes"])
        self.assertFalse(report["gui"]["ready_for_dispatch"])

    def test_stale_route_revision_is_scoped_to_that_route(self):
        def mutate(data):
            data[2][0]["routes"][0]["baseline_sha256"] = "1" * 64
        report = diagnose(modify=mutate)
        flagged = [r for r in report["routes"] if "ROUTE_MATRIX_STALE" in r["codes"]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["route_id"], "openai-api_key-director-v1")
        self.assertTrue(all(not route["qualified"] for route in report["routes"]))

    def test_fixture_cannot_turn_route_on_with_status_strings(self):
        def mutate(data):
            row = data[2][0]["routes"][0]
            row.update({"adapter_status": "implemented", "authentication_status": "PASS",
                        "probe_status": "PASS", "qualification_status": "PASS",
                        "operationally_advertised": True})
        report = diagnose(modify=mutate)
        first = report["routes"][0]
        self.assertFalse(first["qualified"])
        self.assertEqual(first["state"], "BLOCKED")
        self.assertIn("UPSTREAM_GATE_BLOCKED", first["codes"])

    def test_changing_observation_source_does_not_grant_qualification(self):
        report = diagnose(source="live")
        self.assertEqual(report["source_kind"], "live")
        self.assertEqual(report["installed_conformance"], "REQUIRES_REVIEW")
        self.assertEqual(report["qualification"], "NOT_QUALIFIED")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["provider_calls"], 0)

    def test_duplicate_json_and_untrusted_large_inputs_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "inspection.json"
            for sample in (b'{"format":"a","format":"b"}', b"[]" , b"{" + b"a" * 2000001 + b"}"):
                file.write_bytes(sample)
                with self.assertRaises(DiagnosticError):
                    read_json(file)

    def test_cli_offline_exits_blocked_and_has_gui_safe_codes(self):
        proc = subprocess.run([sys.executable, str(ROOT / "tools/diagnose_upstream.py")],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["gui"]["ready_for_dispatch"])
        self.assertNotIn("installed-ags", proc.stdout)
        self.assertNotIn("/opt/codex/", proc.stdout)
        ok = subprocess.run([sys.executable, str(ROOT / "tools/diagnose_upstream.py"), "--allow-blocked"],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["status"], "BLOCKED")

    def test_cli_rejects_live_invocation_without_actual_installed_inputs(self):
        proc = subprocess.run([sys.executable, str(ROOT / "tools/diagnose_upstream.py"), "--live"],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--ags-root", proc.stderr)

    def test_report_does_not_reflect_raw_plugin_error_payload(self):
        def mutate(data):
            data[0][0]["commands"]["vm_governance_check_plugin"]["stderr"] = "secret-canary-in-raw-log"
            data[0][0]["checks"]["ags_vm_a2_namespace"]["response"]["error"]["message"] = "secret-canary-in-raw-log"
        report = diagnose(modify=mutate)
        self.assertNotIn("secret-canary-in-raw-log", json.dumps(report))
        self.assertEqual(report["gui"]["primary_code"], "A2_NAMESPACE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
