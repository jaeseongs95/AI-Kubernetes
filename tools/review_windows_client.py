#!/usr/bin/env python3
"""Check AIK-003 client evidence before independent review (stdlib only).

Exit 0 means READY_FOR_REVIEW, 1 means inconsistent/unsafe evidence, and 2 means
missing evidence or an ineligible environment. No result qualifies support or
changes the backlog. Supply the manifest digest from the trusted handoff build,
not a digest taken only from the submitted evidence itself.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
EXECUTION_ID = re.compile(r"[0-9a-f]{32}\Z")
ACTUAL = "installed-check/packaging/evidence/"
HANDOFF_CHECKS = (
    "package_member_hashes", "verifier_bom_conversion", "legacy_powershell_51",
    "delivered_script_parse", "installed_gui_worker_dpapi_uninstall",
    "installed_engine_identity", "installed_python_dll_identity", "actual_gui_evidence",
)
PROBES = {"operating_system", "user_token", "hardware", "path_commands", "uninstall_registry", "known_locations"}
# Fixed case codes of spikes/windows-foundation/python/aik_spike/auth_vault.py (AIK-022).
AUTH_VAULT_EXPECTED = {
    "dpapi_round_trip": "ROUND_TRIP_OK", "lock_contention": "VAULT_BUSY",
    "malformed_reference": "INVALID_REFERENCE", "truncated_blob": "VAULT_CORRUPT",
    "foreign_blob_simulated": "ACCESS_DENIED", "deleted_reference": "CREDENTIAL_NOT_FOUND",
    "auth_clock_expiry": "EXPIRED_NOT_PLACEABLE", "credential_removed": "REFERENCE_UNAVAILABLE_NOT_PLACEABLE",
    "output_redaction": "REDACTED", "other_user_profile": "NOT_RUN_SINGLE_USER_SESSION",
}
WINDOWS_CANDIDATES = {26200: "25H2", 26300: "26H2"}  # Pinned AIK-003 ADR, not an evergreen support claim.
WINDOWS_EDITIONS = {"Core", "CoreN", "CoreSingleLanguage", "CoreCountrySpecific", "Professional", "ProfessionalN"}


class EvidenceError(ValueError):
    """The supplied bytes or their binding are inconsistent or unsafe."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        require(key not in value, f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def timestamp(value: Any) -> datetime:
    require(isinstance(value, str), "Missing or malformed execution timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("Invalid execution timestamp.") from exc
    require(result.tzinfo is not None, "Execution timestamps require a timezone.")
    return result


def digest(value: Any) -> str:
    require(isinstance(value, str) and SHA256.fullmatch(value) is not None, "Invalid SHA-256.")
    return value


def integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def libraries(value: Any) -> dict[str, str]:
    require(isinstance(value, list) and bool(value), "Missing Python DLL evidence.")
    result: dict[str, str] = {}
    for item in value:
        name = item["file"]
        require(isinstance(name, str) and re.fullmatch(r"python3[0-9]*\.dll", name, re.I) is not None,
                "Invalid Python DLL filename.")
        require(name.lower() not in result, "Duplicate Python DLL evidence.")
        result[name.lower()] = digest(item["sha256"])
    return result


class Bundle:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.files: list[dict[str, Any]] = []

    def path(self, relative: str) -> Path:
        require(isinstance(relative, str) and bool(relative) and "\\" not in relative and ":" not in relative,
                "Evidence paths must be relative POSIX paths.")
        parts = relative.split("/")
        require(not PurePosixPath(relative).is_absolute() and all(p not in ("", ".", "..") for p in parts),
                "Evidence paths must stay inside the bundle.")
        target = self.root.joinpath(*parts)
        for part in [*reversed(target.parents), target]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            require(not stat.S_ISLNK(info.st_mode) and
                    not (getattr(info, "st_file_attributes", 0) & 0x400),
                    "Evidence paths must not use symlinks or reparse points.")
        return target

    def read(self, relative: str, expected: str | None = None, *, limit: int = 2 * 1024 * 1024) -> bytes:
        path = self.path(relative)
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit, f"Invalid evidence file size/type: {relative}")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        require(len(raw) == info.st_size and len(raw) <= limit, f"Evidence changed while reading: {relative}")
        actual = hashlib.sha256(raw).hexdigest()
        if expected is not None:
            require(actual == digest(expected), f"Evidence hash mismatch: {relative}")
        self.files.append({"path": relative, "sha256": actual, "bytes": len(raw)})
        return raw

    def json(self, relative: str, expected: str | None = None) -> dict[str, Any]:
        raw = self.read(relative, expected)
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(EvidenceError("Nonfinite JSON number.")))
        require(isinstance(data, dict), f"Expected JSON object: {relative}")
        return data


def review_bundle(root: Path, expected_manifest_sha256: str, preparation: str = "client-preparation.json") -> dict[str, Any]:
    bundle = Bundle(root)
    blockers: list[str] = []
    result: dict[str, Any] = {
        "schema_version": 1, "task_id": "AIK-003", "status": "FAIL",
        "scope": "Evidence integrity and readiness for independent clean-client review",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "expected_manifest_sha256": expected_manifest_sha256,
        "files": bundle.files, "blockers": blockers, "errors": [],
        "qualification": {key: "NOT_RUN" for key in (
            "windows11_standard_user_clean_machine", "developer_tools_absence", "release_signing", "product_integration")},
        "manual_review_required": [
            "Verify the operator's clean-machine preparation records and image provenance.",
            "Inspect the fresh screenshot and actual installation results.",
            "Review hardware, current Windows support/security updates, paths and offline conditions against the pinned ADR.",
            "Record independent AIK-003 acceptance evidence before changing task status.",
        ],
    }
    try:
        manifest_sha = digest(expected_manifest_sha256)
        manifest = bundle.json("handoff-manifest.json", manifest_sha)
        handoff = bundle.json("handoff-validation.json")
        require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1 and manifest["development_artifact"] is True,
                "Unsupported handoff manifest.")
        require(type(handoff["schema_version"]) is int and handoff["schema_version"] in (1, 2), "Unsupported handoff evidence schema.")
        require(handoff["manifest_sha256"] == manifest_sha, "Handoff manifest binding mismatch.")
        require(re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"]) is not None and
                handoff["package_source_commit"] == manifest["source_commit"], "Source commit mismatch.")
        result["package_source_commit"] = manifest["source_commit"]
        require(all(handoff["qualification"].get(key) == "NOT_RUN" for key in result["qualification"]),
                "Foundation evidence must not claim support or product qualification.")
        require(all(manifest["qualification"].get(key) == "NOT_RUN" for key in result["qualification"]) and
                manifest["installer"].get("release_publisher_qualified") is False,
                "Manifest must retain the development artifact qualification scope.")
        if handoff.get("status") != "PASS":
            blockers.append("FOUNDATION_EXECUTION_NOT_PASS")
            result["status"] = "BLOCKED"
            return result
        require(all(handoff["checks"].get(key) == "PASS" for key in HANDOFF_CHECKS), "Incomplete handoff checks.")
        execution = handoff["observed_execution"]
        require(str(execution["powershell_version"]).startswith("5.1.") and
                execution["powershell_edition"] == "Desktop" and execution["is_64_bit_process"] is True,
                "Evidence must come from actual x64 Windows PowerShell 5.1.")
        require(handoff["installed_report"] == ACTUAL + "installed-spike.json" and
                handoff["screenshot"] == ACTUAL + "screenshot.png", "Fresh execution paths were replaced.")
        installed = bundle.json(ACTUAL + "installed-spike.json", handoff["installed_report_sha256"])
        require(handoff["installed_report_sha256"] != digest(manifest["prior_validation"]["report_sha256"]),
                "Prior installation evidence was reused as a new execution.")
        require(type(installed["schema_version"]) is int and installed["schema_version"] == 1 and
                installed["status"] == "PASS" and type(handoff["verifier_exit_code"]) is int and handoff["verifier_exit_code"] == 0 and
                type(installed["application_exit_code"]) is int and installed["application_exit_code"] == 0 and
                all(installed["checks"].get(k) == "PASS" for k in ("install", "runtime_bundle", "packaged_gui", "uninstall")),
                "Incomplete actual installation checks.")
        require(installed["product_integration"] == "NOT_RUN" and installed["support_qualification"] == "NOT_RUN",
                "The installation report changed qualification scope.")
        require(installed.get("development_artifact") is True and
                installed["installer"].get("release_publisher_qualified") is False and
                installed["runtime_bundle"].get("installed_outside_source_tree") is True and
                installed["runtime_bundle"].get("unicode_and_space_paths") is True,
                "Installation evidence must preserve development, publisher and path claims.")
        require(installed.get("source_commit") == execution.get("github_sha"),
                "Inherited GitHub source observations disagree.")
        require(integer(manifest["installer"]["bytes"], 1) and integer(installed["installer"]["bytes"], 1),
                "Installer byte counts must be positive integers.")
        digest(manifest["installer"]["sha256"])
        digest(installed["installer"]["sha256"])
        for key in ("sha256", "bytes"):
            require(installed["installer"][key] == manifest["installer"][key], "Installer identity mismatch.")
        require(digest(installed["runtime_bundle"]["engine_sha256"]) == digest(manifest["runtime"]["engine_sha256"]),
                "Installed engine identity mismatch.")
        require(libraries(installed["runtime_bundle"]["python_runtime_libraries"]) ==
                libraries(manifest["runtime"]["python_runtime_libraries"]), "Installed Python DLL identity mismatch.")
        application = bundle.json(ACTUAL + "application-report.json", installed["application_report_sha256"])
        screenshot = bundle.read(ACTUAL + "screenshot.png", installed["screenshot_sha256"], limit=20 * 1024 * 1024)
        require(screenshot.startswith(b"\x89PNG\r\n\x1a\n"), "Missing PNG screenshot header.")
        require(type(application["schema_version"]) is int and application["schema_version"] == 1 and
                application["status"] == "PASS" and application["platform"] == "win32" and
                application["packaged"] is True and application["window_visible"] is True and
                application["renderer_node_access"] is False and application["product_integration"] == "NOT_RUN" and
                all(application["checks"][key]["status"] == "PASS" for key in ("health", "worker", "vault")) and
                application["checks"]["health"]["frozen"] is True and
                application["checks"]["health"]["python_version"] == manifest["runtime"]["python_version"],
                "Incomplete actual GUI/worker/DPAPI evidence.")
        # AIK-022 synthetic auth/vault failure paths: optional so packages built before the
        # probe still review for AIK-003, but when reported it must be a complete PASS.
        auth_vault = application["checks"].get("auth_vault")
        if auth_vault is None:
            result["aik022_auth_vault"] = "ABSENT"
        else:
            cases = auth_vault.get("cases") if isinstance(auth_vault, dict) else None
            require(isinstance(cases, dict) and auth_vault.get("status") == "PASS"
                    and auth_vault.get("code") == "AUTH_VAULT_FAILURE_PATHS_OK"
                    and auth_vault.get("backend") == "windows-user-dpapi" and cases == AUTH_VAULT_EXPECTED,
                    "The AIK-022 auth/vault failure-path probe did not pass every case.")
            result["aik022_auth_vault"] = "PASS_SYNTHETIC_SINGLE_USER"
        start, end = timestamp(handoff["started_at"]), timestamp(handoff["completed_at"])
        install_start, install_end = timestamp(installed["started_at"]), timestamp(installed["completed_at"])
        require(start <= install_start < install_end <= end, "Installation timestamps fall outside this execution.")
        if end > timestamp(result["checked_at"]) + timedelta(minutes=5):
            blockers.append("CLIENT_CLOCK_AHEAD_OF_REVIEW")
        host = installed["environment"]
        require(handoff["observed_windows_environment"] == host, "Host observations differ from installed evidence.")
        if host.get("platform") != "win32" or host.get("architecture") != "AMD64" or type(host.get("os_product_type")) is not int or host["os_product_type"] != 1:
            blockers.append("NOT_WINDOWS_X64_CLIENT")
        if not str(host.get("os_version", "")).startswith("10.0.") or not str(host.get("os_build", "")).isdigit() or int(host["os_build"]) not in WINDOWS_CANDIDATES:
            blockers.append("NOT_WINDOWS11_CANDIDATE")
        if host.get("administrator") is not False:
            blockers.append("ELEVATED_OR_UNKNOWN_USER")
        if not handoff.get("client_environment_report"):
            blockers.append("MISSING_PREINSTALL_CLIENT_OBSERVATIONS")
            result["status"] = "BLOCKED"
            return result
        require(handoff["client_environment_report"] == "client-environment.json", "Unexpected environment evidence path.")
        env_sha = digest(handoff["client_environment_sha256"])
        env = bundle.json("client-environment.json", env_sha)
        require(handoff.get("client_environment_status") == env.get("status"), "Environment status observations disagree.")
        run_id = handoff["execution_id"]
        require(isinstance(run_id, str) and EXECUTION_ID.fullmatch(run_id) is not None and env["execution_id"] == run_id,
                "Client execution identity mismatch.")
        require(type(env["schema_version"]) is int and env["schema_version"] == 1 and env["manifest_sha256"] == manifest_sha, "Client manifest binding mismatch.")
        require(start <= timestamp(env["observed_at"]) <= install_start, "Environment was not collected before installation.")
        result["execution_id"] = run_id
        if env.get("status") != "COMPLETE" or env.get("errors") != []:
            blockers.append("INCOMPLETE_HOST_OBSERVATIONS")
        os_info = env["os"]
        require(integer(os_info.get("build")) and integer(os_info.get("product_type")), "Invalid typed OS observations.")
        for field, observed in (("platform", host["platform"]), ("architecture", host["architecture"]),
                                ("product_type", host["os_product_type"]), ("version", host["os_version"]),
                                ("build", int(host["os_build"]))):
            require(os_info.get(field) == observed, f"Preinstall and installed OS observations disagree: {field}")
        if os_info.get("edition_id") not in WINDOWS_EDITIONS or os_info.get("display_version") != WINDOWS_CANDIDATES.get(os_info.get("build")):
            blockers.append("OS_EDITION_OR_RELEASE_OUTSIDE_ADR")
        hardware = env["hardware"]
        if not all(integer(h.get(k), 1) for h in (hardware, host) for k in ("logical_processors", "memory_bytes")):
            blockers.append("INCOMPLETE_HARDWARE_OBSERVATIONS")
        require(hardware.get("logical_processors") == host.get("logical_processors") and
                hardware.get("memory_bytes") == host.get("memory_bytes"), "Hardware observations disagree.")
        if env["user"].get("elevated") is not False or env["user"].get("administrator_group_member") is not False:
            blockers.append("NOT_CONFIRMED_STANDARD_USER")
        inventory = env["tool_inventory"]
        probes = inventory.get("probes", [])
        require(isinstance(probes, list) and all(isinstance(p, dict) for p in probes), "Invalid probe inventory.")
        if inventory.get("status") != "COMPLETE" or len(probes) != len(PROBES) or {p.get("name") for p in probes} != PROBES or any(p.get("status") != "PASS" for p in probes):
            blockers.append("INCOMPLETE_DEVELOPER_TOOL_INVENTORY")
        detections = inventory.get("detections")
        require(isinstance(detections, list), "Missing developer tool detections.")
        if detections:
            blockers.append("DEVELOPER_TOOLS_DETECTED")
        result["tool_detection_count"] = len(detections)
        result["tool_inventory_limit"] = "No detections in targeted probes is not proof of a clean machine."
        prep_path = bundle.path(preparation)
        if not prep_path.exists():
            blockers.append("MISSING_CLEAN_PREPARATION_RECORD")
        else:
            prep = bundle.json(preparation)
            require(type(prep["schema_version"]) is int and prep["schema_version"] == 1 and prep["execution_id"] == run_id and
                    prep["manifest_sha256"] == manifest_sha and prep["client_environment_sha256"] == env_sha,
                    "Preparation record belongs to a different execution.")
            complete = all(isinstance(prep.get(k), str) and bool(prep[k].strip()) for k in ("operator", "description", "prepared_at"))
            if not complete or prep.get("method") not in ("clean_install", "clean_vm_image") or prep.get("development_tools_installed") is not False or not prep.get("records"):
                blockers.append("INCOMPLETE_CLEAN_PREPARATION_RECORD")
            else:
                require(timestamp(prep["prepared_at"]) <= start, "Machine preparation occurred after execution began.")
                records = prep["records"]
                require(isinstance(records, list) and 1 <= len(records) <= 16, "Invalid preparation attachment inventory.")
                seen: set[str] = set()
                for record in records:
                    name = record["path"]
                    require(isinstance(name, str) and name.startswith("preparation/") and name not in seen,
                            "Preparation attachments need distinct preparation/ paths.")
                    seen.add(name)
                    bundle.read(name, record["sha256"], limit=20 * 1024 * 1024)
                result["operator_claims"] = {key: prep[key] for key in ("operator", "prepared_at", "method", "description", "development_tools_installed")}
        result["status"] = "BLOCKED" if blockers else "READY_FOR_REVIEW"
    except (EvidenceError, OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        result["errors"].append(str(exc))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--preparation-record", default="client-preparation.json", help="Relative path inside the evidence bundle")
    args = parser.parse_args()
    result = review_bundle(args.evidence_root, args.expected_manifest_sha256, args.preparation_record)
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return {"READY_FOR_REVIEW": 0, "FAIL": 1, "BLOCKED": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
