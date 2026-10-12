#!/usr/bin/env python3
"""Package an already verified Windows foundation installer using only stdlib.

This packages existing evidence; it does not execute Windows or qualify support.
The original installer verifier remains untouched in the checkout. Its delivered
copy receives a UTF-8 BOM so Windows PowerShell 5.1 decodes Korean paths correctly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any
import zipfile


BOM = b"\xef\xbb\xbf"
INSTALLER_MEMBER = "installer/AIK-Foundation-Spike-win-x64-dev-unsigned.exe"
VERIFIER_SOURCE = "scripts/windows/verify-installed-spike.ps1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SOURCE_FILES = (
    VERIFIER_SOURCE,
    "scripts/windows/build-sidecar.ps1",
    "packaging/electron-builder.cjs",
    "packaging/requirements-build.windows.lock",
    "spikes/windows-foundation/package.json",
    "spikes/windows-foundation/package-lock.json",
    "tools/build_windows_handoff.py",
    "tools/windows_handoff/run-validation.ps1",
    "tools/windows_handoff/run-validation.cmd",
    "tools/windows_handoff/README.ko.md",
    ".github/workflows/windows-client-handoff.yml",
)


class HandoffError(ValueError):
    """An input is incomplete, inconsistent, or unsafe to package."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffError(f"Invalid UTF-8 JSON: {path}") from exc
    if not isinstance(data, dict):
        raise HandoffError(f"Expected a JSON object: {path}")
    return data, raw


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HandoffError(message)


def runtime_libraries(items: Any) -> list[dict[str, str]]:
    require(isinstance(items, list) and bool(items), "CPython runtime library evidence is missing.")
    normalized: dict[str, dict[str, str]] = {}
    for item in items:
        require(isinstance(item, dict), "Malformed runtime library evidence.")
        name, digest = item.get("file"), item.get("sha256")
        require(isinstance(name, str) and re.fullmatch(r"python3[0-9]*\.dll", name, re.I) is not None,
                "Runtime library names must be plain CPython DLL filenames.")
        require(isinstance(digest, str) and SHA256.fullmatch(digest) is not None,
                "Runtime library SHA-256 is invalid.")
        require(name.lower() not in normalized, "Duplicate runtime library filename.")
        normalized[name.lower()] = {"file": name.lower(), "sha256": digest}
    return [normalized[key] for key in sorted(normalized)]


def utf8_bom_copy(raw: bytes) -> tuple[bytes, str]:
    """Preserve every source byte; add exactly one BOM only when absent."""
    raw.decode("utf-8-sig", errors="strict")
    if raw.startswith(BOM):
        return raw, "identity; source already has UTF-8 BOM"
    return BOM + raw, "prepend UTF-8 BOM; all original bytes unchanged"


def build_handoff(
    repository_root: Path,
    installer_path: Path,
    source_commit: str,
    output_path: Path,
    *,
    build_manifest_path: Path | None = None,
    verification_report_path: Path | None = None,
) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    installer = installer_path.resolve(strict=True)
    output = output_path.absolute()
    checksum_path = output.with_name(output.name + ".sha256")
    require(COMMIT.fullmatch(source_commit) is not None, "An exact 40-character source commit is required.")
    require(output.suffix.lower() == ".zip", "The handoff output must be a .zip file.")
    require(not output.exists() and not checksum_path.exists(), "Refusing to overwrite an existing handoff or checksum.")
    require(installer.is_file() and installer.suffix.lower() == ".exe", "A built Windows installer .exe is required.")
    build_path = build_manifest_path or root / "packaging/build/sidecar-build.json"
    installed_path = verification_report_path or root / "packaging/evidence/installed-spike.json"
    build, build_raw = load_json(build_path)
    installed, installed_raw = load_json(installed_path)
    application_path = installed_path.parent / "application-report.json"
    application, application_raw = load_json(application_path)
    screenshot_path = installed_path.parent / "screenshot.png"
    screenshot_raw = screenshot_path.read_bytes()
    require(build.get("schema_version") == 1 and build.get("target") == "win32-x64"
            and build.get("python") == "3.14.8", "A verified Windows x64 CPython 3.14.8 build manifest is required.")
    require(build.get("source_commit") == installed.get("source_commit") == source_commit,
            "Build and installed evidence must identify the exact requested source commit.")
    require(installed.get("schema_version") == 1 and installed.get("status") == "PASS"
            and installed.get("development_artifact") is True,
            "Only a passed development-installer validation may be handed off.")
    checks = installed.get("checks", {})
    require(isinstance(checks, dict) and all(checks.get(key) == "PASS" for key in
            ("install", "runtime_bundle", "packaged_gui", "uninstall")),
            "Install, runtime, GUI, and uninstall evidence must all pass.")
    require(installed.get("product_integration") == "NOT_RUN"
            and installed.get("support_qualification") == "NOT_RUN"
            and build.get("product_integration") == "NOT_RUN",
            "The foundation handoff must preserve unqualified product/support status.")
    installer_record = installed.get("installer", {})
    installer_sha = sha256_file(installer)
    installer_size = installer.stat().st_size
    require(installer_record.get("file") == installer.name
            and installer_record.get("sha256") == installer_sha
            and installer_record.get("bytes") == installer_size,
            "The installer bytes do not match the installer actually validated.")
    require(installer_record.get("release_publisher_qualified") is False,
            "This handoff must not claim a qualified release publisher.")
    runtime = installed.get("runtime_bundle", {})
    engine_sha = build.get("engine_sha256")
    require(isinstance(engine_sha, str) and SHA256.fullmatch(engine_sha) is not None
            and runtime.get("engine_sha256") == engine_sha,
            "Installed engine identity differs from the frozen build.")
    libraries = runtime_libraries(build.get("runtime_libraries"))
    require(runtime_libraries(runtime.get("python_runtime_libraries")) == libraries,
            "Installed CPython DLL identities differ from the frozen build.")
    require(runtime.get("installed_outside_source_tree") is True
            and runtime.get("unicode_and_space_paths") is True,
            "Prior installed verification must have used independent Unicode paths.")
    require(installed.get("application_report_sha256") == hashlib.sha256(application_raw).hexdigest()
            and installed.get("screenshot_sha256") == hashlib.sha256(screenshot_raw).hexdigest(),
            "Application report or screenshot bytes differ from the installed evidence.")
    require(screenshot_raw.startswith(b"\x89PNG\r\n\x1a\n"), "The captured GUI evidence is not a PNG.")
    app_checks = application.get("checks", {})
    require(application.get("schema_version") == 1 and application.get("status") == "PASS"
            and application.get("platform") == "win32" and application.get("window_visible") is True
            and application.get("packaged") is True
            and application.get("renderer_node_access") is False
            and application.get("product_integration") == "NOT_RUN"
            and all(app_checks.get(key, {}).get("status") == "PASS" for key in ("health", "worker", "vault"))
            and app_checks.get("health", {}).get("frozen") is True
            and app_checks.get("health", {}).get("python_version") == "3.14.8",
            "Actual packaged GUI, frozen sidecar, worker, and DPAPI evidence is incomplete.")

    original = (root / VERIFIER_SOURCE).read_bytes()
    delivered, transformation = utf8_bom_copy(original)
    wrapper = (root / "tools/windows_handoff/run-validation.ps1").read_bytes()
    launcher = (root / "tools/windows_handoff/run-validation.cmd").read_bytes()
    require(wrapper.isascii() and launcher.isascii(), "The legacy PowerShell wrapper and launcher must be ASCII only.")
    members: dict[str, bytes | Path] = {
        "run-validation.ps1": wrapper,
        "run-validation.cmd": launcher,
        "README.ko.md": (root / "tools/windows_handoff/README.ko.md").read_bytes(),
        INSTALLER_MEMBER: installer,
        "verify-installed-spike.ps1": delivered,
        "provenance/verify-installed-spike.original.ps1": original,
        "provenance/sidecar-build.json": build_raw,
        "provenance/installed-spike.json": installed_raw,
        "provenance/application-report.json": application_raw,
        "provenance/screenshot.png": screenshot_raw,
    }
    file_records = []
    for name, content in sorted(members.items()):
        file_records.append({"path": name, "bytes": installer_size if isinstance(content, Path) else len(content),
                             "sha256": installer_sha if isinstance(content, Path) else hashlib.sha256(content).hexdigest()})
    source_records = [{"path": name, "sha256": sha256_file(root / name)} for name in SOURCE_FILES]
    manifest = {
        "schema_version": 1,
        "scope": "AIK-003 independent Windows client handoff of the verified AIK-002 foundation spike",
        "source_commit": source_commit,
        "source_identity": {
            "binding": "source_commit identifies the already-built installer and engine; it is matched to supplied build and installed evidence",
            "file_observation": "files below are current workspace inputs read by this packaging tool; they are not asserted to belong to source_commit, and no offline Git revision lookup is performed",
            "files": source_records,
        },
        "development_artifact": True,
        "installer": {"path": INSTALLER_MEMBER, "original_file": installer.name,
                      "bytes": installer_size, "sha256": installer_sha,
                      "observed_signature_status": installer_record.get("signature_status"),
                      "release_publisher_qualified": False},
        "runtime": {"python_version": "3.14.8", "engine_sha256": engine_sha,
                    "python_runtime_libraries": libraries},
        "verifier": {"source_path": VERIFIER_SOURCE,
                     "original_package_path": "provenance/verify-installed-spike.original.ps1",
                     "original_sha256": hashlib.sha256(original).hexdigest(),
                     "delivered_path": "verify-installed-spike.ps1",
                     "delivered_sha256": hashlib.sha256(delivered).hexdigest(),
                     "transformation": transformation},
        "prior_validation": {"workflow_run_id": installed.get("workflow_run_id"),
                             "workflow_attempt": installed.get("workflow_attempt"),
                             "status": installed["status"],
                             "environment": installed.get("environment"),
                             "report_sha256": hashlib.sha256(installed_raw).hexdigest()},
        "files": file_records,
        "qualification": {"windows11_standard_user_clean_machine": "NOT_RUN",
                          "developer_tools_absence": "NOT_RUN", "product_integration": "NOT_RUN",
                          "release_signing": "NOT_RUN"},
        "limits": [
            "Packaging prior PASS evidence is not a new Windows execution.",
            "Manifest hashes detect changed members; this unsigned ZIP is not a publisher trust mechanism.",
            "The wrapper records actual execution provenance and never creates GitHub identity variables.",
            "A Server administrator run with a restricted child PATH is not a clean Windows 11 standard-user run.",
        ],
    }
    manifest_raw = (json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode("ascii")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".aik-handoff-", suffix=".zip", dir=output.parent, delete=False) as temp:
            temp_name = temp.name
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for name, content in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED if isinstance(content, Path) else zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                digest = hashlib.sha256()
                with archive.open(info, "w", force_zip64=True) as target:
                    if isinstance(content, Path):
                        with content.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                target.write(chunk)
                                digest.update(chunk)
                        require(digest.hexdigest() == installer_sha,
                                "Installer changed during handoff packaging.")
                    else:
                        target.write(content)
            archive.writestr("handoff-manifest.json", manifest_raw)
        package_sha = sha256_file(Path(temp_name))
        # Exclusive destination creation also rejects a file created after preflight.
        with output.open("xb") as target, Path(temp_name).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
        with checksum_path.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{package_sha}  {output.name}\n")
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return {"schema_version": 1, "status": "PASS", "scope": "handoff packaging and input identity checks only",
            "source_commit": source_commit, "file": str(output), "bytes": output.stat().st_size,
            "sha256": package_sha, "checksum_file": str(checksum_path),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(), "member_count": len(members) + 1,
            "windows_execution": "NOT_RUN", "windows11_standard_user_clean_machine": "NOT_RUN"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--build-manifest", type=Path)
    parser.add_argument("--verification-report", type=Path)
    args = parser.parse_args()
    try:
        result = build_handoff(args.repository_root, args.installer, args.source_commit, args.output,
                               build_manifest_path=args.build_manifest,
                               verification_report_path=args.verification_report)
    except (HandoffError, OSError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
