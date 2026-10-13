"""Evidence-intake regressions; synthetic fixtures never prove Windows support.

Only the archived Server test reads an actual Windows execution. The other
fixtures are deliberately synthetic JSON and a one-pixel PNG, written by these
tests without launching Windows, an installer, a GUI, or any developer tool.
READY_FOR_REVIEW is an intake result, never support/product qualification.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_windows_client", ROOT / "tools/review_windows_client.py")
assert SPEC is not None and SPEC.loader is not None
reviewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewer)
ACTUAL = "installed-check/packaging/evidence/"
QUALIFICATION = {
    name: "NOT_RUN" for name in (
        "windows11_standard_user_clean_machine", "developer_tools_absence", "release_signing", "product_integration"
    )
}
ARCHIVED = ROOT / "evidence/windows-handoff/38226204128/artifact/handoff/evidence/8d415a8070b94a67aa92effb29e24801"
ARCHIVED_MANIFEST_SHA256 = "f71b452cb4c90543d84bca4e73b919f6714af5dd11309a2df02610832343957d"


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256(path.read_bytes())
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


class SyntheticBundle:
    """A bounded parser fixture; none of its Windows observations actually ran."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reset_fixture()

    def reset_fixture(self) -> None:
        marker = "SYNTHETIC parser regression only; no real Windows execution or clean-machine claim"
        self.manifest = {
            "schema_version": 1, "development_artifact": True, "source_commit": "1" * 40,
            "fixture_scope": marker,
            "installer": {"path": "installer/synthetic-dev-unsigned.exe", "bytes": 111, "sha256": "2" * 64,
                          "release_publisher_qualified": False},
            "runtime": {"engine_sha256": "3" * 64, "python_version": "3.14.8",
                        "python_runtime_libraries": [{"file": "python314.dll", "sha256": "4" * 64}]},
            "prior_validation": {"report_sha256": "5" * 64}, "qualification": dict(QUALIFICATION),
        }
        self.application = {
            "schema_version": 1, "status": "PASS", "platform": "win32", "packaged": True,
            "window_visible": True, "renderer_node_access": False, "product_integration": "NOT_RUN",
            "checks": {"health": {"status": "PASS", "frozen": True, "python_version": "3.14.8"},
                       "worker": {"status": "PASS"}, "vault": {"status": "PASS"}},
            "fixture_scope": marker,
        }
        self.screenshot = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        )
        host = {
            "platform": "win32", "architecture": "AMD64", "os_product_type": 1,
            "os_version": "10.0.26200", "os_build": "26200", "administrator": False,
            "logical_processors": 4, "memory_bytes": 16 * 1024 ** 3,
            "fixture_scope": marker,
        }
        self.installed = {
            "schema_version": 1, "status": "PASS", "source_commit": None, "development_artifact": True,
            "support_qualification": "NOT_RUN", "product_integration": "NOT_RUN",
            "started_at": "2026-10-12T12:00:02Z", "completed_at": "2026-10-12T12:00:30Z",
            "checks": {name: "PASS" for name in ("install", "runtime_bundle", "packaged_gui", "uninstall")},
            "application_exit_code": 0,
            "installer": {"file": "synthetic-dev-unsigned.exe", "bytes": 111, "sha256": "2" * 64,
                          "signature_status": "NotSigned", "release_publisher_qualified": False},
            "runtime_bundle": {"engine_sha256": "3" * 64,
                               "python_runtime_libraries": [{"file": "python314.dll", "sha256": "4" * 64}],
                               "installed_outside_source_tree": True, "unicode_and_space_paths": True},
            "environment": host, "fixture_scope": marker,
        }
        self.environment = {
            "schema_version": 1, "execution_id": "6" * 32, "observed_at": "2026-10-12T12:00:01Z",
            "status": "COMPLETE", "errors": [],
            "os": {"platform": "win32", "architecture": "AMD64", "product_type": 1,
                   "version": "10.0.26200", "build": 26200, "edition_id": "Professional", "display_version": "25H2"},
            "hardware": {"logical_processors": 4, "memory_bytes": 16 * 1024 ** 3},
            "user": {"elevated": False, "administrator_group_member": False},
            "tool_inventory": {
                "status": "COMPLETE", "detections": [],
                "probes": [{"name": name, "status": "PASS"} for name in (
                    "operating_system", "user_token", "hardware", "path_commands", "uninstall_registry", "known_locations"
                )],
            },
            "fixture_scope": marker,
        }
        self.handoff = {
            "schema_version": 2, "status": "PASS", "package_source_commit": "1" * 40,
            "execution_id": "6" * 32, "started_at": "2026-10-12T12:00:00Z",
            "completed_at": "2026-10-12T12:00:31Z", "qualification": dict(QUALIFICATION),
            "observed_execution": {"powershell_version": "5.1.26200.1", "powershell_edition": "Desktop",
                                   "is_64_bit_process": True, "github_sha": None, "github_actions": False},
            "checks": {name: "PASS" for name in (
                "package_member_hashes", "verifier_bom_conversion", "legacy_powershell_51", "delivered_script_parse",
                "installed_gui_worker_dpapi_uninstall", "installed_engine_identity", "installed_python_dll_identity",
                "actual_gui_evidence"
            )},
            "installed_report": ACTUAL + "installed-spike.json", "screenshot": ACTUAL + "screenshot.png",
            "verifier_exit_code": 0, "observed_windows_environment": copy.deepcopy(host),
            "client_environment_report": "client-environment.json", "client_environment_status": "COMPLETE",
            "fixture_scope": marker,
        }
        self.attachment = (marker + "\nSynthetic preparation record, not an operating-system image.\n").encode("utf-8")
        self.preparation = {
            "schema_version": 1, "execution_id": "6" * 32, "operator": "Synthetic fixture operator",
            "prepared_at": "2026-10-12T11:00:00Z", "method": "clean_vm_image", "description": marker,
            "development_tools_installed": False,
            "records": [{"path": "preparation/synthetic-image-record.txt", "sha256": sha256(self.attachment)}],
            "fixture_scope": marker,
        }
        self.write_fixture()

    def write_bytes(self, relative: str, raw: bytes) -> str:
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        return sha256(raw)

    def write_json(self, relative: str, value: object) -> str:
        return self.write_bytes(relative, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    def write_fixture(self) -> None:
        """Rebind child hashes so negative semantic cases reach their intended check."""
        self.manifest_sha256 = self.write_json("handoff-manifest.json", self.manifest)
        self.installed["application_report_sha256"] = self.write_json(ACTUAL + "application-report.json", self.application)
        self.installed["screenshot_sha256"] = self.write_bytes(ACTUAL + "screenshot.png", self.screenshot)
        self.handoff["installed_report_sha256"] = self.write_json(ACTUAL + "installed-spike.json", self.installed)
        self.environment["manifest_sha256"] = self.manifest_sha256
        environment_sha256 = self.write_json("client-environment.json", self.environment)
        self.handoff["manifest_sha256"] = self.manifest_sha256
        self.handoff["client_environment_sha256"] = environment_sha256
        self.write_json("handoff-validation.json", self.handoff)
        self.preparation["manifest_sha256"] = self.manifest_sha256
        self.preparation["client_environment_sha256"] = environment_sha256
        self.write_json("client-preparation.json", self.preparation)
        self.write_bytes("preparation/synthetic-image-record.txt", self.attachment)

    def review(self, preparation: str = "client-preparation.json") -> dict:
        return reviewer.review_bundle(self.root, self.manifest_sha256, preparation)


class WindowsClientReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="aik-synthetic-client-review-")
        self.addCleanup(self.temp.cleanup)
        self.fixture = SyntheticBundle(Path(self.temp.name) / "synthetic-evidence")

    def assert_fail(self, result: dict) -> None:
        self.assertEqual(result["status"], "FAIL", result)
        self.assertTrue(result["errors"], result)
        self.assertEqual(result["qualification"], QUALIFICATION)

    def assert_blocked(self, result: dict, blocker: str) -> None:
        self.assertEqual(result["status"], "BLOCKED", result)
        self.assertIn(blocker, result["blockers"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["qualification"], QUALIFICATION)

    def test_archived_server_administrator_execution_stays_blocked_and_unchanged(self) -> None:
        before = snapshot(ARCHIVED)
        self.assertEqual(before["handoff-manifest.json"], ARCHIVED_MANIFEST_SHA256)
        result = reviewer.review_bundle(ARCHIVED, ARCHIVED_MANIFEST_SHA256)
        self.assert_blocked(result, "NOT_WINDOWS_X64_CLIENT")
        self.assertIn("NOT_WINDOWS11_CANDIDATE", result["blockers"])
        self.assertIn("ELEVATED_OR_UNKNOWN_USER", result["blockers"])
        self.assertIn("MISSING_PREINSTALL_CLIENT_OBSERVATIONS", result["blockers"])
        self.assertEqual(snapshot(ARCHIVED), before)

    def test_complete_synthetic_fixture_is_ready_for_review_without_qualification_or_writes(self) -> None:
        before = snapshot(self.fixture.root)
        result = self.fixture.review()
        self.assertEqual(result["status"], "READY_FOR_REVIEW", result)
        self.assertEqual(result["qualification"], QUALIFICATION)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["execution_id"], "6" * 32)
        self.assertEqual(result["tool_detection_count"], 0)
        self.assertTrue(result["manual_review_required"])
        self.assertEqual({entry["path"] for entry in result["files"]}, set(before))
        self.assertEqual(snapshot(self.fixture.root), before)

    def test_aik022_auth_vault_probe_is_optional_but_must_fully_pass_when_reported(self) -> None:
        # Packages built before the probe (AIK-003 handoffs) still review; the field says ABSENT.
        self.assertEqual(self.fixture.review()["aik022_auth_vault"], "ABSENT")
        passing = {"status": "PASS", "backend": "windows-user-dpapi", "code": "AUTH_VAULT_FAILURE_PATHS_OK",
                   "cases": dict(reviewer.AUTH_VAULT_EXPECTED)}
        self.fixture.application["checks"]["auth_vault"] = passing
        self.fixture.write_fixture()
        result = self.fixture.review()
        self.assertEqual(result["status"], "READY_FOR_REVIEW", result)
        self.assertEqual(result["aik022_auth_vault"], "PASS_SYNTHETIC_SINGLE_USER")
        for broken in ({**passing, "status": "FAIL"},
                       {**passing, "cases": {**passing["cases"], "lock_contention": "NOT_RAISED"}},
                       {**passing, "cases": {**passing["cases"], "other_user_profile": "PASS"}}):
            with self.subTest(broken=broken):
                self.fixture.application["checks"]["auth_vault"] = broken
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_missing_preparation_cannot_be_replaced_by_an_empty_tool_inventory(self) -> None:
        (self.fixture.root / "client-preparation.json").unlink()
        self.assert_blocked(self.fixture.review(), "MISSING_CLEAN_PREPARATION_RECORD")

    def test_blank_or_incomplete_preparation_is_blocked(self) -> None:
        cases = (("operator", None), ("operator", "   "), ("description", ""), ("prepared_at", ""),
                 ("method", None), ("method", "restricted_PATH"), ("development_tools_installed", None),
                 ("development_tools_installed", True), ("records", []))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.fixture.reset_fixture()
                self.fixture.preparation[field] = value
                self.fixture.write_fixture()
                self.assert_blocked(self.fixture.review(), "INCOMPLETE_CLEAN_PREPARATION_RECORD")

    def test_detected_developer_tool_blocks_review_even_with_complete_probes(self) -> None:
        self.fixture.environment["tool_inventory"]["detections"] = [{"tool": "Python", "source": "synthetic PATH observation"}]
        self.fixture.write_fixture()
        result = self.fixture.review()
        self.assert_blocked(result, "DEVELOPER_TOOLS_DETECTED")
        self.assertEqual(result["tool_detection_count"], 1)

    def test_omitted_duplicate_failed_and_not_run_probes_are_blocked(self) -> None:
        for mutation in ("omitted", "duplicate", "failed", "not_run", "inventory_incomplete"):
            with self.subTest(mutation=mutation):
                self.fixture.reset_fixture()
                inventory = self.fixture.environment["tool_inventory"]
                if mutation == "omitted":
                    inventory["probes"].pop()
                elif mutation == "duplicate":
                    inventory["probes"][-1] = copy.deepcopy(inventory["probes"][0])
                elif mutation == "failed":
                    inventory["probes"][0]["status"] = "FAIL"
                elif mutation == "not_run":
                    inventory["probes"][0]["status"] = "NOT_RUN"
                else:
                    inventory["status"] = "INCOMPLETE"
                self.fixture.write_fixture()
                self.assert_blocked(self.fixture.review(), "INCOMPLETE_DEVELOPER_TOOL_INVENTORY")

    def test_observation_errors_or_incomplete_collection_are_blocked(self) -> None:
        for field, value in (("errors", ["SYNTHETIC registry probe access error"]), ("status", "INCOMPLETE")):
            with self.subTest(field=field):
                self.fixture.reset_fixture()
                self.fixture.environment[field] = value
                if field == "status":
                    self.fixture.handoff["client_environment_status"] = value
                self.fixture.write_fixture()
                self.assert_blocked(self.fixture.review(), "INCOMPLETE_HOST_OBSERVATIONS")

    def test_environment_status_disagreement_is_an_integrity_failure(self) -> None:
        self.fixture.handoff["client_environment_status"] = "INCOMPLETE"
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())

    def test_filtered_admin_and_unknown_or_elevated_user_are_not_standard_users(self) -> None:
        for field, value in (("administrator_group_member", True), ("administrator_group_member", None),
                             ("elevated", True), ("elevated", None)):
            with self.subTest(field=field, value=value):
                self.fixture.reset_fixture()
                self.fixture.environment["user"][field] = value
                self.fixture.write_fixture()
                self.assert_blocked(self.fixture.review(), "NOT_CONFIRMED_STANDARD_USER")

    def test_candidate_release_and_edition_follow_the_pinned_adr(self) -> None:
        for field, value in (("edition_id", "Enterprise"), ("display_version", "24H2")):
            with self.subTest(field=field):
                self.fixture.reset_fixture()
                self.fixture.environment["os"][field] = value
                self.fixture.write_fixture()
                self.assert_blocked(self.fixture.review(), "OS_EDITION_OR_RELEASE_OUTSIDE_ADR")
        self.fixture.reset_fixture()
        self.fixture.environment["os"].update(build=26100, version="10.0.26100", display_version="24H2")
        self.fixture.installed["environment"].update(os_build="26100", os_version="10.0.26100")
        self.fixture.handoff["observed_windows_environment"] = copy.deepcopy(self.fixture.installed["environment"])
        self.fixture.write_fixture()
        self.assert_blocked(self.fixture.review(), "NOT_WINDOWS11_CANDIDATE")

    def test_changed_bound_file_bytes_fail_without_modifying_input(self) -> None:
        for relative in ("handoff-manifest.json", ACTUAL + "installed-spike.json", ACTUAL + "application-report.json",
                         ACTUAL + "screenshot.png", "client-environment.json", "preparation/synthetic-image-record.txt"):
            with self.subTest(relative=relative):
                self.fixture.reset_fixture()
                path = self.fixture.root / relative
                path.write_bytes(path.read_bytes() + b"\n")
                before = snapshot(self.fixture.root)
                result = self.fixture.review()
                self.assert_fail(result)
                self.assertIn("hash mismatch", " ".join(result["errors"]).lower())
                self.assertEqual(snapshot(self.fixture.root), before)

    def test_installer_engine_and_dll_identity_mismatches_fail_after_rebinding_report(self) -> None:
        for mutation in ("installer_hash", "installer_bytes", "engine_hash", "dll_hash", "dll_name", "missing_dll"):
            with self.subTest(mutation=mutation):
                self.fixture.reset_fixture()
                installed = self.fixture.installed
                if mutation == "installer_hash":
                    installed["installer"]["sha256"] = "a" * 64
                elif mutation == "installer_bytes":
                    installed["installer"]["bytes"] += 1
                elif mutation == "engine_hash":
                    installed["runtime_bundle"]["engine_sha256"] = "a" * 64
                elif mutation == "dll_hash":
                    installed["runtime_bundle"]["python_runtime_libraries"][0]["sha256"] = "a" * 64
                elif mutation == "dll_name":
                    installed["runtime_bundle"]["python_runtime_libraries"][0]["file"] = "python313.dll"
                else:
                    installed["runtime_bundle"]["python_runtime_libraries"] = []
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_prior_report_replay_is_rejected_despite_matching_hashes(self) -> None:
        self.fixture.manifest["prior_validation"]["report_sha256"] = self.fixture.handoff["installed_report_sha256"]
        self.fixture.write_fixture()
        result = self.fixture.review()
        self.assert_fail(result)
        self.assertIn("reused", " ".join(result["errors"]))

    def test_source_and_execution_identity_mismatches_fail(self) -> None:
        cases = (("handoff", "package_source_commit", "a" * 40), ("handoff", "execution_id", "a" * 32),
                 ("environment", "execution_id", "a" * 32), ("preparation", "execution_id", "a" * 32),
                 ("handoff", "execution_id", "not-an-execution-id"))
        for document, field, value in cases:
            with self.subTest(document=document, field=field):
                self.fixture.reset_fixture()
                getattr(self.fixture, document)[field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_local_execution_does_not_require_synthetic_github_identity(self) -> None:
        self.assertIsNone(self.fixture.installed["source_commit"])
        self.assertIsNone(self.fixture.handoff["observed_execution"]["github_sha"])
        self.assertEqual(self.fixture.review()["status"], "READY_FOR_REVIEW")

    def test_conflicting_inherited_github_source_observations_fail(self) -> None:
        self.fixture.handoff["observed_execution"]["github_sha"] = "a" * 40
        self.fixture.installed["source_commit"] = "b" * 40
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())

    def test_timestamps_must_bind_preparation_observation_and_install_to_this_execution(self) -> None:
        cases = (("handoff", "started_at", "2026-10-12T12:00:03Z"),
                 ("handoff", "completed_at", "2026-10-12T12:00:29Z"),
                 ("installed", "completed_at", "2026-10-12T12:00:02Z"),
                 ("environment", "observed_at", "2026-10-12T11:59:59Z"),
                 ("environment", "observed_at", "2026-10-12T12:00:03Z"),
                 ("preparation", "prepared_at", "2026-10-12T12:00:01Z"),
                 ("installed", "started_at", "2026-10-12T12:00:02"),
                 ("environment", "observed_at", "invalid timestamp"))
        for document, field, value in cases:
            with self.subTest(document=document, field=field, value=value):
                self.fixture.reset_fixture()
                getattr(self.fixture, document)[field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_internally_ordered_future_execution_requires_clock_review(self) -> None:
        for document, fields in ((self.fixture.handoff, ("started_at", "completed_at")),
                                 (self.fixture.installed, ("started_at", "completed_at")),
                                 (self.fixture.environment, ("observed_at",)),
                                 (self.fixture.preparation, ("prepared_at",))):
            for field in fields:
                document[field] = document[field].replace("2026-", "2099-")
        self.fixture.write_fixture()
        self.assert_blocked(self.fixture.review(), "CLIENT_CLOCK_AHEAD_OF_REVIEW")

    def test_preinstall_host_and_hardware_must_match_installed_observations(self) -> None:
        for document, field, value in (("os", "build", 26300), ("hardware", "logical_processors", 8),
                                       ("hardware", "memory_bytes", 8 * 1024 ** 3)):
            with self.subTest(document=document, field=field):
                self.fixture.reset_fixture()
                self.fixture.environment[document][field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_actual_legacy_powershell_and_gui_checks_are_required(self) -> None:
        cases = (("powershell_version", "7.5.0"), ("powershell_edition", "Core"), ("is_64_bit_process", False))
        for field, value in cases:
            with self.subTest(field=field):
                self.fixture.reset_fixture()
                self.fixture.handoff["observed_execution"][field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())
        for document, field in (("installed", "uninstall"), ("application", "worker")):
            with self.subTest(document=document, field=field):
                self.fixture.reset_fixture()
                if document == "installed":
                    self.fixture.installed["checks"][field] = "FAIL"
                else:
                    self.fixture.application["checks"][field]["status"] = "FAIL"
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_foundation_not_pass_is_blocked_and_cannot_qualify_support(self) -> None:
        self.fixture.handoff["status"] = "FAIL"
        self.fixture.write_fixture()
        self.assert_blocked(self.fixture.review(), "FOUNDATION_EXECUTION_NOT_PASS")

    def test_changed_qualification_claims_are_rejected(self) -> None:
        for mutation in ("handoff", "installed", "application", "manifest_scope", "manifest_publisher",
                         "development_artifact", "publisher", "outside_source", "unicode_paths"):
            with self.subTest(mutation=mutation):
                self.fixture.reset_fixture()
                if mutation == "handoff":
                    self.fixture.handoff["qualification"]["windows11_standard_user_clean_machine"] = "PASS"
                elif mutation == "installed":
                    self.fixture.installed["support_qualification"] = "PASS"
                elif mutation == "application":
                    self.fixture.application["product_integration"] = "PASS"
                elif mutation == "manifest_scope":
                    self.fixture.manifest["qualification"]["product_integration"] = "PASS"
                elif mutation == "manifest_publisher":
                    self.fixture.manifest["installer"]["release_publisher_qualified"] = True
                elif mutation == "development_artifact":
                    self.fixture.installed["development_artifact"] = False
                elif mutation == "publisher":
                    self.fixture.installed["installer"]["release_publisher_qualified"] = True
                elif mutation == "outside_source":
                    self.fixture.installed["runtime_bundle"]["installed_outside_source_tree"] = False
                else:
                    self.fixture.installed["runtime_bundle"]["unicode_and_space_paths"] = False
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())

    def test_unsafe_preparation_attachment_paths_and_duplicates_are_rejected(self) -> None:
        for name in ("../outside.txt", "/outside.txt", "preparation/../outside.txt", "preparation//record.txt",
                     "preparation/./record.txt", "preparation/record.txt:stream", "preparation/..\\outside.txt",
                     "preparation/record\x00.txt"):
            with self.subTest(name=name):
                self.fixture.reset_fixture()
                self.fixture.preparation["records"][0]["path"] = name
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        self.fixture.preparation["records"] *= 2
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())

    def test_explicit_preparation_path_cannot_escape_bundle(self) -> None:
        for name in ("../client-preparation.json", "/client-preparation.json", "C:\\client-preparation.json", ""):
            with self.subTest(name=name):
                self.assert_fail(self.fixture.review(name))

    def test_symlinked_evidence_and_bundle_root_are_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside-record.txt"
        outside.write_bytes(self.fixture.attachment)
        attachment = self.fixture.root / "preparation/synthetic-image-record.txt"
        attachment.unlink()
        try:
            attachment.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        self.assert_fail(self.fixture.review())
        attachment.unlink()
        attachment.write_bytes(self.fixture.attachment)
        alias = Path(self.temp.name) / "symlinked-root"
        alias.symlink_to(self.fixture.root, target_is_directory=True)
        self.assert_fail(reviewer.review_bundle(alias, self.fixture.manifest_sha256))

    def test_malformed_json_duplicates_nonfinite_numbers_and_deep_nesting_fail(self) -> None:
        malformed = (b"{", b"[]", b"null", b' {"status":"PASS","status":"PASS"}', b'{"value":NaN}',
                     b'{"value":Infinity}', b"\xff", b'{"value":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}")
        for raw in malformed:
            with self.subTest(prefix=raw[:40]):
                self.fixture.reset_fixture()
                self.fixture.write_bytes("handoff-validation.json", raw)
                self.assert_fail(self.fixture.review())

    def test_malformed_nested_types_fail_without_uncaught_exceptions(self) -> None:
        cases = (("installed", "checks", []), ("environment", "os", None), ("environment", "user", []),
                 ("environment", "tool_inventory", "not an inventory"), ("preparation", "records", "not a list"))
        for document, field, value in cases:
            with self.subTest(document=document, field=field):
                self.fixture.reset_fixture()
                getattr(self.fixture, document)[field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())
        for field, value in (("probes", [None]), ("detections", None)):
            with self.subTest(field=field):
                self.fixture.reset_fixture()
                self.fixture.environment["tool_inventory"][field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        self.fixture.environment["tool_inventory"]["probes"][0]["name"] = []
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())

    def test_empty_oversized_and_nonregular_input_files_are_rejected(self) -> None:
        for size in (0, 2 * 1024 * 1024 + 1):
            with self.subTest(size=size):
                self.fixture.reset_fixture()
                with (self.fixture.root / "handoff-validation.json").open("wb") as stream:
                    stream.truncate(size)
                self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        with (self.fixture.root / "preparation/synthetic-image-record.txt").open("wb") as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
        self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        path = self.fixture.root / "handoff-validation.json"
        path.unlink()
        path.mkdir()
        self.assert_fail(self.fixture.review())

    def test_numeric_fields_reject_boolean_and_float_coercion(self) -> None:
        for document in ("manifest", "handoff", "installed", "application", "environment", "preparation"):
            for value in (True, 1.0):
                with self.subTest(document=document, schema_version=value):
                    self.fixture.reset_fixture()
                    getattr(self.fixture, document)["schema_version"] = value
                    self.fixture.write_fixture()
                    self.assert_fail(self.fixture.review())
        for document, field, value in (("handoff", "verifier_exit_code", False), ("installed", "application_exit_code", False)):
            with self.subTest(document=document, field=field):
                self.fixture.reset_fixture()
                getattr(self.fixture, document)[field] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        self.fixture.installed["installer"]["bytes"] = 111.0
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        self.fixture.environment["os"]["product_type"] = True
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())

    def test_equal_but_malformed_installer_hashes_do_not_establish_identity(self) -> None:
        for value in ("not-a-sha256", "A" * 64, None):
            with self.subTest(value=value):
                self.fixture.reset_fixture()
                self.fixture.manifest["installer"]["sha256"] = value
                self.fixture.installed["installer"]["sha256"] = value
                self.fixture.write_fixture()
                self.assert_fail(self.fixture.review())
        self.fixture.reset_fixture()
        self.fixture.manifest["prior_validation"]["report_sha256"] = "not-a-prior-report-hash"
        self.fixture.write_fixture()
        self.assert_fail(self.fixture.review())

    def test_cli_exit_codes_express_readiness_failure_and_blocking(self) -> None:
        script = ROOT / "tools/review_windows_client.py"
        for expected_code, mutation in ((0, "complete"), (2, "missing_preparation"), (1, "tampered_manifest")):
            with self.subTest(mutation=mutation):
                self.fixture.reset_fixture()
                if mutation == "missing_preparation":
                    (self.fixture.root / "client-preparation.json").unlink()
                elif mutation == "tampered_manifest":
                    self.fixture.write_bytes("handoff-manifest.json", b"{}\n")
                child = subprocess.run(
                    [sys.executable, str(script), "--evidence-root", str(self.fixture.root),
                     "--expected-manifest-sha256", self.fixture.manifest_sha256],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                self.assertEqual(child.returncode, expected_code, child.stdout + child.stderr)
                self.assertEqual(json.loads(child.stdout)["qualification"], QUALIFICATION)


if __name__ == "__main__":
    unittest.main()
