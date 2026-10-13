"""Regression guards for packaging; synthetic fixtures never prove Windows support.

The fixture .exe is deliberately not executable. Tests either package bytes or
reject damaged members before installation. Real GUI/worker/DPAPI evidence comes
from windows-client-handoff.yml, with the actual built Windows installer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_windows_handoff", ROOT / "tools/build_windows_handoff.py")
assert SPEC is not None and SPEC.loader is not None
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)
COMMIT = "1" * 40


class WindowsHandoffRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aik-handoff-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fixture-source"
        self.root.mkdir()
        for name in handoff.SOURCE_FILES:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        self.installer = self.root / "fixture-dev-unsigned.exe"
        self.installer.write_bytes(b"SYNTHETIC PACKAGING FIXTURE; NOT AN EXECUTABLE\n")
        self.build = {
            "schema_version": 1, "source_commit": COMMIT, "target": "win32-x64", "python": "3.14.8",
            "engine_sha256": "2" * 64, "runtime_libraries": [{"file": "python314.dll", "sha256": "3" * 64}],
            "product_integration": "NOT_RUN",
        }
        self.application = {
            "schema_version": 1, "status": "PASS", "platform": "win32", "packaged": True,
            "window_visible": True, "renderer_node_access": False, "product_integration": "NOT_RUN",
            "checks": {"health": {"status": "PASS", "frozen": True, "python_version": "3.14.8"},
                       "worker": {"status": "PASS"}, "vault": {"status": "PASS"},
                       "auth_vault": {"status": "PASS"}},
            "fixture_scope": "packaging regression only; no Windows execution",
        }
        self.screenshot = b"\x89PNG\r\n\x1a\nSYNTHETIC IMAGE HEADER; NO GUI EXECUTION"
        self.installed = {
            "schema_version": 1, "source_commit": COMMIT, "status": "PASS", "development_artifact": True,
            "product_integration": "NOT_RUN", "support_qualification": "NOT_RUN",
            "checks": {name: "PASS" for name in ("install", "runtime_bundle", "packaged_gui", "uninstall")},
            "installer": {"file": self.installer.name, "bytes": self.installer.stat().st_size,
                          "sha256": handoff.sha256_file(self.installer), "signature_status": "NotSigned",
                          "release_publisher_qualified": False},
            "runtime_bundle": {"engine_sha256": "2" * 64,
                               "python_runtime_libraries": [{"file": "python314.dll", "sha256": "3" * 64}],
                               "installed_outside_source_tree": True, "unicode_and_space_paths": True},
            "workflow_run_id": None, "workflow_attempt": None,
            "environment": {"fixture_scope": "no Windows environment was executed"},
        }
        self.output = Path(self.temp.name) / "handoff.zip"
        self.write_evidence()

    def write_evidence(self) -> None:
        build_path = self.root / "packaging/build/sidecar-build.json"
        evidence = self.root / "packaging/evidence"
        build_path.parent.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=True)
        app_raw = (json.dumps(self.application) + "\n").encode("utf-8")
        self.installed["application_report_sha256"] = hashlib.sha256(app_raw).hexdigest()
        self.installed["screenshot_sha256"] = hashlib.sha256(self.screenshot).hexdigest()
        build_path.write_text(json.dumps(self.build), encoding="utf-8")
        (evidence / "installed-spike.json").write_text(json.dumps(self.installed), encoding="utf-8")
        (evidence / "application-report.json").write_bytes(app_raw)
        (evidence / "screenshot.png").write_bytes(self.screenshot)

    def package(self) -> dict:
        return handoff.build_handoff(self.root, self.installer, COMMIT, self.output)

    def test_package_preserves_original_bytes_bom_conversion_and_member_hashes(self) -> None:
        result = self.package()
        self.assertEqual(result["windows_execution"], "NOT_RUN")
        self.assertEqual(result["sha256"], handoff.sha256_file(self.output))
        with zipfile.ZipFile(self.output) as archive:
            manifest = json.loads(archive.read("handoff-manifest.json"))
            original = archive.read("provenance/verify-installed-spike.original.ps1")
            self.assertEqual(original, (ROOT / handoff.VERIFIER_SOURCE).read_bytes())
            self.assertEqual(archive.read("verify-installed-spike.ps1"), handoff.BOM + original)
            self.assertTrue(archive.read("run-validation.ps1").isascii())
            self.assertTrue(archive.read("collect-client-environment.ps1").isascii())
            self.assertTrue(archive.read("run-validation.cmd").isascii())
            self.assertEqual(set(archive.namelist()), {item["path"] for item in manifest["files"]} | {"handoff-manifest.json"})
            for item in manifest["files"]:
                content = archive.read(item["path"])
                self.assertEqual(item["bytes"], len(content))
                self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
            self.assertIsNone(manifest["prior_validation"]["workflow_run_id"])
            self.assertTrue(all(value == "NOT_RUN" for value in manifest["qualification"].values()))

    def test_changed_installer_is_rejected_before_output(self) -> None:
        self.installer.write_bytes(self.installer.read_bytes() + b"changed")
        with self.assertRaisesRegex(handoff.HandoffError, "installer bytes"):
            self.package()
        self.assertFalse(self.output.exists())

    def test_installed_engine_and_python_dll_mismatches_are_rejected(self) -> None:
        for field in ("engine", "dll"):
            with self.subTest(field=field):
                self.installed["runtime_bundle"]["engine_sha256"] = "4" * 64 if field == "engine" else "2" * 64
                self.installed["runtime_bundle"]["python_runtime_libraries"][0]["sha256"] = "4" * 64 if field == "dll" else "3" * 64
                self.write_evidence()
                with self.assertRaises(handoff.HandoffError):
                    self.package()
                self.assertFalse(self.output.exists())

    def test_uninstalled_failure_and_unpacked_gui_cannot_be_handed_off(self) -> None:
        self.installed["checks"]["uninstall"] = "FAIL"
        self.write_evidence()
        with self.assertRaisesRegex(handoff.HandoffError, "uninstall evidence"):
            self.package()
        self.installed["checks"]["uninstall"] = "PASS"
        self.application["packaged"] = False
        self.write_evidence()
        with self.assertRaisesRegex(handoff.HandoffError, "Actual packaged GUI"):
            self.package()

    def test_new_packages_require_the_aik022_auth_vault_probe(self) -> None:
        for broken in ({"status": "FAIL"}, None):
            with self.subTest(auth_vault=broken):
                if broken is None:
                    del self.application["checks"]["auth_vault"]
                else:
                    self.application["checks"]["auth_vault"] = broken
                self.write_evidence()
                with self.assertRaisesRegex(handoff.HandoffError, "Actual packaged GUI"):
                    self.package()

    def test_source_and_raw_gui_evidence_must_match(self) -> None:
        self.build["source_commit"] = "9" * 40
        self.write_evidence()
        with self.assertRaisesRegex(handoff.HandoffError, "source commit"):
            self.package()
        self.build["source_commit"] = COMMIT
        self.write_evidence()
        screenshot = self.root / "packaging/evidence/screenshot.png"
        screenshot.write_bytes(screenshot.read_bytes() + b"changed")
        with self.assertRaisesRegex(handoff.HandoffError, "screenshot bytes"):
            self.package()

    def test_existing_output_and_checksum_are_never_overwritten(self) -> None:
        self.output.write_bytes(b"user-owned existing output")
        with self.assertRaisesRegex(handoff.HandoffError, "overwrite"):
            self.package()
        self.assertEqual(self.output.read_bytes(), b"user-owned existing output")
        self.output.unlink()
        checksum = self.output.with_name(self.output.name + ".sha256")
        checksum.write_bytes(b"user-owned checksum")
        with self.assertRaisesRegex(handoff.HandoffError, "overwrite"):
            self.package()
        self.assertEqual(checksum.read_bytes(), b"user-owned checksum")
        self.assertFalse(self.output.exists())

    def test_bom_is_not_duplicated_and_invalid_utf8_is_rejected(self) -> None:
        raw = handoff.BOM + "Write-Output '한글'\r\n".encode("utf-8")
        self.assertEqual(handoff.utf8_bom_copy(raw)[0], raw)
        with self.assertRaises(UnicodeDecodeError):
            handoff.utf8_bom_copy(b"\xff\xfe")

    def test_tampered_delivered_script_is_rejected_before_any_install(self) -> None:
        self.check_tampered_member("verify-installed-spike.ps1")

    def test_tampered_environment_collector_is_rejected_before_execution(self) -> None:
        self.check_tampered_member("collect-client-environment.ps1")

    def check_tampered_member(self, name: str) -> None:
        executable = os.environ.get("AIK_HANDOFF_TEST_POWERSHELL") or shutil.which("powershell") or shutil.which("pwsh")
        if not executable:
            self.skipTest("No PowerShell executable available for the real wrapper negative test.")
        self.package()
        extracted = Path(self.temp.name) / "extracted"
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(extracted)
        delivered = extracted / name
        delivered.write_bytes(delivered.read_bytes() + b"\nthrow 'tampered'\n")
        evidence_parent = Path(self.temp.name) / "negative-evidence"
        child = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(extracted / "run-validation.ps1"), "-EvidenceParent", str(evidence_parent)],
            capture_output=True, text=True, timeout=40, check=False,
        )
        self.assertEqual(child.returncode, 1, child.stdout + child.stderr)
        reports = list(evidence_parent.glob("*/handoff-validation.json"))
        self.assertEqual(len(reports), 1, child.stdout + child.stderr)
        report = json.loads(reports[0].read_text(encoding="utf-8-sig"))
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("Changed package member: " + name, report["error"])
        self.assertEqual(report["checks"]["installed_gui_worker_dpapi_uninstall"], "NOT_RUN")
        self.assertNotIn("verifier_pid", report)

    def test_actual_powershell_environment_collector_contract(self) -> None:
        executable = os.environ.get("AIK_HANDOFF_TEST_POWERSHELL") or shutil.which("powershell") or shutil.which("pwsh")
        if not executable:
            self.skipTest("No PowerShell executable available for the collector contract test.")
        harness = Path(self.temp.name) / "collector-harness.ps1"
        harness.write_text(
            "param([string] $CollectorPath)\n. $CollectorPath\n"
            "Get-AikClientEnvironment -ExecutionId ('1' * 32) -ManifestSha256 ('2' * 64) | ConvertTo-Json -Depth 16\n",
            encoding="ascii",
        )
        child = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness),
             "-CollectorPath", str(ROOT / "tools/windows_handoff/collect-client-environment.ps1")],
            capture_output=True, text=True, timeout=90, check=False,
        )
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        observation = json.loads(child.stdout)
        self.assertEqual(observation["schema_version"], 1)
        self.assertEqual(observation["execution_id"], "1" * 32)
        self.assertEqual(observation["manifest_sha256"], "2" * 64)
        self.assertNotIn("qualification", observation)
        probes = observation["tool_inventory"]["probes"]
        self.assertEqual({p["name"] for p in probes}, {
            "operating_system", "user_token", "hardware", "path_commands", "uninstall_registry", "known_locations"})
        if os.name == "nt":
            self.assertEqual(observation["os"]["platform"], "win32")
            self.assertIn(observation["status"], ("COMPLETE", "INCOMPLETE"))
        else:
            self.assertEqual(observation["status"], "UNSUPPORTED")
            self.assertEqual(observation["tool_inventory"]["status"], "INCOMPLETE")
            self.assertIsNone(observation["user"]["elevated"])
            self.assertIsNone(observation["user"]["administrator_group_member"])
            self.assertTrue(observation["errors"])


if __name__ == "__main__":
    unittest.main()
