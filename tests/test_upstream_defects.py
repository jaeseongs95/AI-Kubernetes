"""AIK-008 upstream defect list, route separation and AIK-081/082 linkage.

Inputs are the committed contract fixtures plus in-memory mutations. No AGS/VM
installer, provider, credential or network is used. Passing on Linux is
development verification, not installed Windows conformance.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_compat.diagnostics import KNOWN_CODES, DiagnosticError, inspect, read_json  # noqa: E402
from aik_compat import upstream_defects  # noqa: E402
from aik_compat.upstream_defects import build_defect_report, diagnose_defects  # noqa: E402

CANARY = "secret-canary-defect-list"
CONTRACT_GATES = {
    "AGS_HOST_INTEGRATION_MANIFEST_MISSING", "AGS_A2_NAMESPACE_UNAVAILABLE",
    "AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED", "VM_RUNTIME_OWNER_WINDOWS_ONLY",
}


def load():
    folder = ROOT / "contracts" / "upstream"
    return [read_json(folder / name) for name in (
        "installed-inspection.json", "baseline.json", "support-matrix.json",
        "integration-contract.json",
    )]


def run(modify=None, source="historical"):
    data = load()
    if modify:
        modify(data)
    return diagnose_defects(*[value for entry in data for value in entry], source=source)


def diagnostic_and_sources():
    data = load()
    diagnostic = inspect(*[value for entry in data for value in entry])
    (_, _), (baseline, bb), (matrix, mb), (integration, ib) = data
    return diagnostic, baseline, bb, matrix, mb, integration, ib


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, "-m", "aik_compat.upstream_defects", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)


def error_code(error):
    candidates = [getattr(error, "code", None)]
    if error.args:
        candidates.append(error.args[0])
    candidates.append(str(error))
    for candidate in candidates:
        if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate):
            return candidate
    return None


def hide_incompatibility(diagnostic):
    """Forgery F-3(a): erase every observed blocker from a saved diagnostic."""
    forged = copy.deepcopy(diagnostic)
    forged["codes"] = []
    forged["compatibility"] = "UNVERIFIED"
    for route in forged["routes"]:
        route["state"] = "UNVERIFIED"
        route["codes"] = []
    return forged


class UpstreamDefectTests(unittest.TestCase):
    def test_report_records_state_versions_manifest_and_digests(self):
        report = run()
        self.assertEqual(report["format"], "aik-upstream-defects-v1")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["qualification"], "NOT_QUALIFIED")
        self.assertEqual(report["compatibility"], "INCOMPATIBLE")
        self.assertEqual(report["compatibility_state"], "incompatible")
        baseline = load()[1][0]
        ags, vm = report["identity"]["ags"], report["identity"]["vm"]
        self.assertEqual(ags["version_label"], baseline["ags"]["version_label"])
        self.assertEqual(ags["archive_sha256"], baseline["ags"]["archive_sha256"])
        self.assertEqual(ags["host_integration_manifest"], "ABSENT")
        self.assertEqual(ags["manifest_file_count"], len(baseline["ags"]["files"]))
        self.assertEqual(vm["version_label"], baseline["vm"]["version_label"])
        self.assertEqual(vm["distribution"], baseline["vm"]["distribution"])
        self.assertEqual(vm["wheel_sha256"], baseline["vm"]["wheel_sha256"])
        self.assertEqual(vm["sqlite_schema_revision"], 4)
        self.assertEqual(vm["manifest_file_count"], len(baseline["vm"]["files"]))
        for value in (ags["manifest_sha256"], vm["manifest_sha256"], report["diagnostic_sha256"],
                      report["identity"]["baseline_sha256"], report["identity"]["matrix_sha256"]):
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_defect_list_classifies_owner_and_never_resolves(self):
        report = run()
        defects = {d["code"]: d for d in report["defects"]}
        self.assertTrue(CONTRACT_GATES <= set(defects))
        for code in ("AGS_A2_NAMESPACE_UNAVAILABLE", "AGS_HOST_INTEGRATION_MANIFEST_MISSING"):
            self.assertEqual(defects[code]["status"], "OBSERVED")
            self.assertEqual(defects[code]["owner"], "AGS")
            self.assertTrue(defects[code]["upstream"])
            self.assertEqual(defects[code]["follow_up_task"], "AIK-081")
            self.assertEqual(defects[code]["observation_source"], "historical")
        durable = defects["AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED"]
        self.assertEqual(durable["owner"], "AIK")
        self.assertFalse(durable["upstream"])
        for defect in report["defects"]:
            self.assertIn(defect["status"], {"OBSERVED", "DECLARED"})
            self.assertEqual(defect["resolution"], "REQUIRES_INSTALLED_CONFORMANCE")

    def test_a2_plugin_and_runtime_observations_link_to_081_and_082(self):
        report = run()
        a2 = report["linkage"]["a2_plugin"]
        self.assertEqual(a2["task"], "AIK-081")
        self.assertEqual(a2["state"], "BLOCKED")
        self.assertEqual(set(a2["codes"]), {"A2_NAMESPACE_UNAVAILABLE", "PLUGIN_INCOMPATIBLE"})
        self.assertFalse(a2["qualified"])
        groups = report["linkage"]["model_effort"]
        self.assertEqual({g["runtime"] for g in groups}, {
            "openai_api_tool_loop", "codex_app_server", "anthropic_api_tool_loop", "claude_code_cli"})
        for group in groups:
            self.assertEqual(group["task"], "AIK-082")
            self.assertEqual(len(group["route_ids"]), 3)
            self.assertEqual(group["model_observation"], "NOT_RUN")
            self.assertEqual(group["effort_observation"], "NOT_RUN")
            self.assertEqual(group["live_probe"], "NOT_RUN")
            self.assertFalse(group["qualified"])

    def test_stale_route_is_isolated_from_other_routes(self):
        baseline_report = run()

        def mutate(data):
            data[2][0]["routes"][0]["baseline_sha256"] = "1" * 64
        report = run(mutate)
        first = report["routes"][0]
        self.assertIn("ROUTE_MATRIX_STALE", first["codes"])
        self.assertIn("AIK-008", first["code_tasks"])
        self.assertNotIn("AIK-008", baseline_report["routes"][0]["code_tasks"])
        self.assertEqual(report["routes"][1:], baseline_report["routes"][1:])
        self.assertTrue(all(not r["qualified"] and not r["dispatchable"] for r in report["routes"]))

    def test_document_pass_strings_cannot_produce_observation_or_qualification(self):
        def mutate(data):
            for row in data[2][0]["routes"]:
                if row["runtime"] == "codex_app_server":
                    row.update({"probe_status": "PASS", "authentication_status": "PASS",
                                "qualification_status": "PASS", "adapter_status": "implemented"})
        report = run(mutate)
        for group in report["linkage"]["model_effort"]:
            expected = "REPORTED_REQUIRES_LIVE_EVIDENCE" if group["runtime"] == "codex_app_server" else "NOT_RUN"
            self.assertEqual(group["model_observation"], expected)
            self.assertNotEqual(group["model_observation"], "PASS")
            self.assertFalse(group["qualified"])
        self.assertTrue(all(not r["qualified"] for r in report["routes"]))
        self.assertEqual(report["status"], "BLOCKED")

    def test_forged_diagnostic_claims_are_rejected(self):
        source = diagnostic_and_sources()
        forgeries = {
            "status": lambda d: d.update(status="PASS"),
            "qualification": lambda d: d.update(qualification="QUALIFIED"),
            "ready": lambda d: d["gui"].update(ready_for_dispatch=True),
            "route": lambda d: d["routes"][0].update(qualified=True),
            "unknown_code": lambda d: d["codes"].append("TOTALLY_PASS"),
            "provider_calls": lambda d: d.update(provider_calls=1),
            "conformance": lambda d: d.update(installed_conformance="PASS"),
        }
        for name, forge in forgeries.items():
            with self.subTest(forgery=name):
                diagnostic = copy.deepcopy(source[0])
                forge(diagnostic)
                with self.assertRaises(DiagnosticError):
                    build_defect_report(diagnostic, *source[1:])

    def test_contract_bytes_must_match_diagnostic_digests(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        with self.assertRaises(DiagnosticError):
            build_defect_report(diagnostic, baseline, bb, matrix, mb + b" ", integration, ib)
        with self.assertRaises(DiagnosticError):
            build_defect_report(diagnostic, baseline, bb + b" ", matrix, mb, integration, ib)

    def test_unknown_contract_gate_is_listed_unclassified_and_invalid_gate_fails(self):
        def add_gate(data):
            data[3][0]["unresolved_gates"].append({"code": "NEW_UPSTREAM_GAP", "blocks": ["AIK-008"]})
        report = run(add_gate)
        gap = next(d for d in report["defects"] if d["code"] == "NEW_UPSTREAM_GAP")
        self.assertEqual(gap["owner"], "UNCLASSIFIED")
        self.assertEqual(gap["status"], "DECLARED")
        self.assertIn("INCOMPLETE_PROVENANCE", {f["code"] for f in report["findings"]})

        def bad_gate(data):
            data[3][0]["unresolved_gates"].append({"code": "lowercase-gap", "blocks": []})
        with self.assertRaises(DiagnosticError):
            run(bad_gate)

    def test_gui_reasons_use_fixed_keys_and_never_reflect_raw_payload(self):
        def mutate(data):
            data[0][0]["commands"]["vm_governance_check_plugin"]["stderr"] = CANARY
            data[0][0]["vm_installed"]["package_root"] = "/" + CANARY + "/flowmarshal"
        report = run(mutate)
        self.assertNotIn(CANARY, json.dumps(report, ensure_ascii=False))
        reasons = report["gui"]["reasons"]
        self.assertTrue(reasons)
        self.assertFalse(report["gui"]["ready_for_dispatch"])
        self.assertEqual(len({r["code"] for r in reasons}), len(reasons))
        for reason in reasons:
            self.assertRegex(reason["message_key"], r"^aik\.upstream\.[a-z0-9_]+$")
            for task in reason["linked_tasks"]:
                self.assertIsNotNone(re.fullmatch(r"AIK-\d{3}", task))

    def test_live_source_still_requires_review_and_stays_blocked(self):
        report = run(source="live")
        self.assertEqual(report["source_kind"], "live")
        self.assertEqual(report["installed_conformance"], "REQUIRES_REVIEW")
        self.assertEqual(report["status"], "BLOCKED")
        observed = [d for d in report["defects"] if d["status"] == "OBSERVED"]
        self.assertTrue(observed)
        self.assertTrue(all(d["observation_source"] == "live" for d in observed))

    def test_cli_offline_blocked_exit_codes_and_output_location(self):
        blocked = cli()
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["status"], "BLOCKED")
        allowed = cli("--allow-blocked")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        inside = ROOT / "aik-defects-must-not-exist.json"
        rejected = cli("--output", str(inside))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("output", rejected.stderr)
        self.assertFalse(inside.exists())
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nested" / "defects.json"
            written = cli("--allow-blocked", "--output", str(target))
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["format"], "aik-upstream-defects-v1")

    def test_cli_consumes_saved_diagnostic_and_rejects_forged_file(self):
        diagnostic = diagnostic_and_sources()[0]
        with tempfile.TemporaryDirectory() as temporary:
            saved = Path(temporary) / "diagnostic.json"
            saved.write_text(json.dumps(diagnostic), encoding="utf-8")
            proc = cli("--diagnostic", str(saved))
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["source_kind"], "historical")
            forged = dict(diagnostic, status="PASS")
            saved.write_text(json.dumps(forged), encoding="utf-8")
            proc = cli("--diagnostic", str(saved))
            self.assertEqual(proc.returncode, 1)
            failure = json.loads(proc.stdout)
            self.assertEqual(failure["code"], "INVALID_DEFECT_INPUT")
            self.assertNotIn(temporary, proc.stdout)

    # --- F-1: identity is the baseline expectation, not an installed observation
    def test_identity_is_labelled_baseline_expectation_and_never_claims_installed_match(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        report = build_defect_report(diagnostic, baseline, bb, matrix, mb, integration, ib)
        identity = report["identity"]
        self.assertEqual(identity["identity_source"], "baseline_contract")
        for component, code in (("ags", "AGS_ARTIFACT_MISMATCH"), ("vm", "VM_ARTIFACT_MISMATCH")):
            expected = "MISMATCH_REPORTED" if code in diagnostic["codes"] else "NOT_CONFIRMED"
            self.assertEqual(identity[component]["installed_match"], expected)
        if "AGS_ARTIFACT_MISMATCH" not in KNOWN_CODES:
            self.skipTest("AGS_ARTIFACT_MISMATCH is not a known diagnostic code")
        mismatched = copy.deepcopy(diagnostic)
        if "AGS_ARTIFACT_MISMATCH" not in mismatched["codes"]:
            mismatched["codes"].append("AGS_ARTIFACT_MISMATCH")
        report = build_defect_report(mismatched, baseline, bb, matrix, mb, integration, ib)
        self.assertEqual(report["identity"]["identity_source"], "baseline_contract")
        self.assertEqual(report["identity"]["ags"]["installed_match"], "MISMATCH_REPORTED")
        for component in ("ags", "vm"):
            self.assertNotEqual(report["identity"][component]["installed_match"], "MATCH")

    # --- F-2: AIK-082 linkage keeps per-route state separate from the aggregate
    def test_model_effort_keeps_per_route_states_separate_from_aggregate(self):
        baseline_report = run()

        def mutate(data):
            data[2][0]["routes"][0]["baseline_sha256"] = "1" * 64
        report = run(mutate)
        stale_id = report["routes"][0]["route_id"]
        before = {g["runtime"]: g for g in baseline_report["linkage"]["model_effort"]}
        found_stale = False
        for group in report["linkage"]["model_effort"]:
            self.assertEqual(group["state_scope"], "AGGREGATE_OF_ROUTES")
            self.assertEqual([s["route_id"] for s in group["route_states"]], group["route_ids"])
            previous = {s["route_id"]: s for s in before[group["runtime"]]["route_states"]}
            for entry in group["route_states"]:
                if entry["route_id"] == stale_id:
                    found_stale = True
                    self.assertIn("ROUTE_MATRIX_STALE", entry["codes"])
                else:
                    self.assertEqual(entry, previous[entry["route_id"]])
                    self.assertNotIn("ROUTE_MATRIX_STALE", entry["codes"])
        self.assertTrue(found_stale)

    # --- F-3: saved diagnostics are recomputed from their inspection record
    def test_saved_diagnostic_recomputation_rejects_hidden_incompatibility(self):
        data = load()
        (inspection, ibytes), (baseline, bb), (matrix, mb), (integration, ib) = data
        diagnostic = inspect(inspection, ibytes, baseline, bb, matrix, mb, integration, ib)
        sources = (inspection, ibytes, baseline, bb, matrix, mb, integration, ib)
        upstream_defects.verify_saved_diagnostic(json.loads(json.dumps(diagnostic)), *sources)
        hidden = hide_incompatibility(diagnostic)
        # Without recomputation the structural check accepts this forgery.
        build_defect_report(hidden, baseline, bb, matrix, mb, integration, ib)
        with self.assertRaises(DiagnosticError) as rejected:
            upstream_defects.verify_saved_diagnostic(hidden, *sources)
        self.assertEqual(error_code(rejected.exception), "DIAGNOSTIC_INPUT_MISMATCH")
        live = dict(copy.deepcopy(diagnostic), source_kind="live", installed_conformance="REQUIRES_REVIEW")
        with self.assertRaises(DiagnosticError) as rejected:
            upstream_defects.verify_saved_diagnostic(live, *sources, require_source="historical")
        self.assertEqual(error_code(rejected.exception), "DIAGNOSTIC_INPUT_MISMATCH")
        if "inspection_sha256" in diagnostic:
            altered = dict(copy.deepcopy(diagnostic), inspection_sha256="0" * 64)
            if altered["inspection_sha256"] != diagnostic["inspection_sha256"]:
                with self.assertRaises(DiagnosticError):
                    upstream_defects.verify_saved_diagnostic(altered, *sources)

    def test_cli_rejects_saved_diagnostic_that_hides_defects_or_forges_source(self):
        diagnostic = diagnostic_and_sources()[0]
        forgeries = {
            "hidden": hide_incompatibility(diagnostic),
            "source": dict(copy.deepcopy(diagnostic), source_kind="live",
                           installed_conformance="REQUIRES_REVIEW"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            saved = Path(temporary) / "diagnostic.json"
            for name, forged in forgeries.items():
                with self.subTest(forgery=name):
                    saved.write_text(json.dumps(forged), encoding="utf-8")
                    proc = cli("--diagnostic", str(saved))
                    self.assertEqual(proc.returncode, 1, proc.stdout)
                    failure = json.loads(proc.stdout)
                    self.assertEqual(failure["code"], "INVALID_DEFECT_INPUT")
                    self.assertEqual(failure["cause_code"], "DIAGNOSTIC_INPUT_MISMATCH")
                    self.assertNotIn(temporary, proc.stdout)

    # --- F-4: untrusted revision/timestamp text is not reflected
    def test_report_fields_do_not_reflect_untrusted_revision_or_timestamp_text(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        forged = copy.deepcopy(diagnostic)
        forged["observed_at"] = "/" + CANARY + "/observed"
        forged["matrix_revision"] = "/" + CANARY + "/matrix"
        poisoned = copy.deepcopy(baseline)
        poisoned["ags"]["revision"] = "/" + CANARY + "/revision"
        forged["ags_revision"] = poisoned["ags"]["revision"]
        report = build_defect_report(forged, poisoned, bb, matrix, mb, integration, ib)
        self.assertNotIn(CANARY, json.dumps(report, ensure_ascii=False))
        self.assertIsNone(report["observed_at"])
        self.assertIsNone(report["identity"]["matrix_revision"])
        self.assertIsNone(report["identity"]["ags"]["revision"])
        naive = dict(copy.deepcopy(diagnostic), observed_at="2030-01-02T03:04:05")
        self.assertIsNone(build_defect_report(naive, baseline, bb, matrix, mb, integration, ib)["observed_at"])
        aware = dict(copy.deepcopy(diagnostic), observed_at="2030-01-02T03:04:05+00:00")
        self.assertEqual(build_defect_report(aware, baseline, bb, matrix, mb, integration, ib)["observed_at"],
                         "2030-01-02T03:04:05+00:00")

    # --- F-5: CLI failures keep a fixed cause code and GUI reasons
    def test_cli_failure_reports_fixed_cause_code_and_gui_reasons(self):
        diagnostic = diagnostic_and_sources()[0]
        with tempfile.TemporaryDirectory() as temporary:
            saved = Path(temporary) / "diagnostic.json"
            saved.write_text(json.dumps(dict(diagnostic, status="PASS")), encoding="utf-8")
            untrusted = cli("--diagnostic", str(saved))
            matrix_copy = Path(temporary) / "support-matrix.json"
            matrix_copy.write_bytes((ROOT / "contracts/upstream/support-matrix.json").read_bytes() + b" ")
            saved_valid = Path(temporary) / "valid.json"
            saved_valid.write_text(json.dumps(diagnostic), encoding="utf-8")
            mismatch = cli("--diagnostic", str(saved_valid), "--matrix", str(matrix_copy))
            for proc, cause in ((untrusted, "UNTRUSTED_DIAGNOSTIC"), (mismatch, "DIAGNOSTIC_INPUT_MISMATCH")):
                with self.subTest(cause=cause):
                    self.assertEqual(proc.returncode, 1)
                    failure = json.loads(proc.stdout)
                    self.assertEqual(failure["code"], "INVALID_DEFECT_INPUT")
                    self.assertEqual(failure["cause_code"], cause)
                    self.assertFalse(failure["gui"]["ready_for_dispatch"])
                    self.assertEqual(failure["gui"]["next_step"], "FIX_DEFECT_INPUT")
                    self.assertEqual([r["code"] for r in failure["gui"]["reasons"]], [cause])
                    self.assertEqual(failure["gui"]["reasons"][0]["message_key"], "aik.upstream." + cause.lower())
                    self.assertNotIn(temporary, proc.stdout)

    # --- F-6: previously untested paths
    def test_unverified_compatibility_path_stays_blocked_without_pass(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        report = build_defect_report(hide_incompatibility(diagnostic), baseline, bb, matrix, mb, integration, ib)
        self.assertEqual(report["compatibility"], "UNVERIFIED")
        self.assertEqual(report["compatibility_state"], "unverified")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["qualification"], "NOT_QUALIFIED")
        self.assertEqual(report["linkage"]["a2_plugin"]["state"], "UNVERIFIED")
        self.assertTrue(all(d["status"] == "DECLARED" and d["observation_source"] is None
                            for d in report["defects"]))
        for group in report["linkage"]["model_effort"]:
            self.assertEqual(group["state"], "UNVERIFIED")
            self.assertFalse(group["qualified"])
        self.assertFalse(report["gui"]["ready_for_dispatch"])

    def test_observed_defect_missing_from_contract_is_listed_undeclared(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        if "A2_NAMESPACE_UNAVAILABLE" not in diagnostic["codes"]:
            self.skipTest("fixture diagnostic does not observe A2_NAMESPACE_UNAVAILABLE")
        trimmed = copy.deepcopy(integration)
        trimmed["unresolved_gates"] = [g for g in trimmed["unresolved_gates"]
                                       if g.get("code") != "AGS_A2_NAMESPACE_UNAVAILABLE"]
        report = build_defect_report(diagnostic, baseline, bb, matrix, mb, trimmed, ib)
        defect = next(d for d in report["defects"] if d["code"] == "AGS_A2_NAMESPACE_UNAVAILABLE")
        self.assertFalse(defect["declared_in_contract"])
        self.assertEqual(defect["status"], "OBSERVED")
        self.assertEqual(defect["owner"], "AGS")
        self.assertEqual(defect["linked_tasks"], [])
        self.assertIsNone(defect["evidence_ref"])

    def test_integration_bytes_must_match_diagnostic_digest(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        with self.assertRaises(DiagnosticError) as rejected:
            build_defect_report(diagnostic, baseline, bb, matrix, mb, integration, ib + b" ")
        self.assertEqual(error_code(rejected.exception), "DIAGNOSTIC_INPUT_MISMATCH")

    def test_runtime_group_with_mixed_provider_or_auth_is_rejected(self):
        diagnostic, baseline, bb, matrix, mb, integration, ib = diagnostic_and_sources()
        mixed = copy.deepcopy(matrix)
        rows = mixed["routes"]
        first = rows[0]
        other = next((r for r in rows if (r.get("provider"), r.get("auth_kind"))
                      != (first.get("provider"), first.get("auth_kind"))), None)
        if other is None:
            self.skipTest("fixture matrix has a single provider/auth pair")
        first["runtime"] = other["runtime"]
        with self.assertRaises(DiagnosticError) as rejected:
            build_defect_report(diagnostic, baseline, bb, mixed, mb, integration, ib)
        self.assertEqual(error_code(rejected.exception), "INVALID_SUPPORT_MATRIX")


if __name__ == "__main__":
    unittest.main()
