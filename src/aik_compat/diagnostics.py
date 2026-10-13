"""AIK-008 installed AGS/VM compatibility diagnosis.

This is an analysis of *observed* installed artifacts, never a replacement
for the VM/AGS governance gate or a provider/model/effort qualification.
A bundled inspection is historical even if every check passed. An actual live
inspection is still non-invasive and does not execute a provider.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

EXPECTED_CHECKS = (
    "ags_bytes", "vm_bytes", "vm_schema", "vm_non_editable", "vm_wheel",
    "vm_distribution", "vm_created_ledger", "ags_mcp_inventory",
    "ags_vm_a2_namespace", "vm_ags_compatibility",
)
REQUIRED_COMMANDS = ("vm_metadata", "vm_help", "vm_project_init", "vm_status",
                     "vm_governance_check_plugin")
EXPECTED_BLOCKERS = frozenset((
    "AGS_HOST_INTEGRATION_MANIFEST_MISSING", "AGS_A2_NAMESPACE_UNAVAILABLE",
    "AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED", "VM_RUNTIME_OWNER_WINDOWS_ONLY",
))
KNOWN_CODES = frozenset((
    "AGS_ARTIFACT_MISMATCH", "VM_ARTIFACT_MISMATCH", "UPSTREAM_IDENTITY_MISMATCH",
    "INSPECTION_INCOMPLETE", "A2_NAMESPACE_UNAVAILABLE", "PLUGIN_INCOMPATIBLE",
    "RUNTIME_OWNER_NOT_VERIFIED", "DURABLE_BINDING_NOT_IMPLEMENTED",
    "ROUTE_MATRIX_STALE", "ROUTE_AUTH_NOT_CHECKED", "ROUTE_MODEL_PROBE_NOT_RUN",
    "ROUTE_GOVERNED_QUALIFICATION_NOT_RUN", "ROUTE_ADAPTER_NOT_QUALIFIED",
    "INCOMPLETE_PROVENANCE", "UPSTREAM_GATE_BLOCKED",
))
HEX = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SCOPE = {"openai", "anthropic"}
ROLES = {"director", "developer", "reviewer"}
AUTHS = {"api_key", "native_subscription"}


class DiagnosticError(ValueError):
    """Sanitized diagnostic failure; never echo file payload or exception."""


def read_json(path: Path, *, maximum: int = 2_000_000) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    if len(raw) > maximum or not raw:
        raise DiagnosticError("INVALID_DOCUMENT")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in items:
            if key in obj:
                raise DiagnosticError("DUPLICATE_JSON_KEY")
            obj[key] = value
        return obj
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(DiagnosticError("INVALID_NUMBER")))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise DiagnosticError("INVALID_DOCUMENT") from None
    if type(obj) is not dict:
        raise DiagnosticError("INVALID_DOCUMENT")
    return obj, raw


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _listed(value: Any) -> list[Any]:
    return value if type(value) is list else []


def _sha(value: Any) -> bool:
    return type(value) is str and HEX.fullmatch(value) is not None


def _git_commit(value: Any) -> bool:
    return type(value) is str and COMMIT.fullmatch(value) is not None


def _observed_at(value: Any) -> str | None:
    if type(value) is not str:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            return None
    except (ValueError, OverflowError):
        return None
    return value


def _manifest_consistent(expected: Any, observed: Any) -> bool:
    if type(expected) is not list or not expected or type(observed) is not list:
        return False
    def normalized(rows: list[Any], *, actual: bool) -> dict[str, str] | None:
        result: dict[str, str] = {}
        for entry in rows:
            if type(entry) is not dict:
                return None
            path, sha = entry.get("path"), entry.get("sha256")
            if type(path) is not str or not path or path in result or not _sha(sha):
                return None
            if actual and entry.get("status") != "PASS":
                return None
            result[path] = sha
        return result
    exp, act = normalized(expected, actual=False), normalized(observed, actual=True)
    return exp is not None and exp == act


def _status(value: Any) -> str:
    return value if type(value) is str and value in {"PASS", "BLOCKED", "FAIL", "NOT_RUN"} else "NOT_RUN"


def _epoch_ok(check: dict[str, Any]) -> bool:
    hello = _mapping(check.get("response"))
    result = _mapping(hello.get("result"))
    epoch = result.get("serverEpoch")
    return type(epoch) is str and len(epoch) >= 32 and "error" not in hello


def _command_ok(report: dict[str, Any], name: str) -> bool:
    command = _mapping(_mapping(report.get("commands")).get(name))
    return type(command.get("exit_code")) is int and command.get("exit_code") == 0 and command.get("timed_out") is False


def inspect(
    inspection: dict[str, Any], inspection_bytes: bytes,
    baseline: dict[str, Any], baseline_bytes: bytes,
    matrix: dict[str, Any], matrix_bytes: bytes,
    integration: dict[str, Any], integration_bytes: bytes,
    *, source: str = "historical",
) -> dict[str, Any]:
    """Return JSON-serializable, no-secret summary of evidence and route blockers."""
    if source not in ("historical", "live"):
        raise DiagnosticError("INVALID_SOURCE_KIND")
    if (inspection.get("format") != "aik-upstream-inspection-v1"
            or baseline.get("format") != "aik-upstream-baseline-v1"
            or matrix.get("format") != "aik-support-matrix-v1"
            or integration.get("format") != "aik-upstream-integration-contract-v1"):
        raise DiagnosticError("INVALID_CONTRACT_FORMAT")
    checks = _mapping(inspection.get("checks"))
    installed = _mapping(inspection.get("vm_installed"))
    ags = _mapping(baseline.get("ags"))
    vm = _mapping(baseline.get("vm"))
    if not (_git_commit(ags.get("revision")) and _git_commit(vm.get("revision"))
            and _sha(ags.get("archive_sha256")) and _sha(vm.get("wheel_sha256"))):
        raise DiagnosticError("INVALID_UPSTREAM_LOCK")
    routes = _listed(matrix.get("routes"))
    if len(routes) == 0 or len(routes) > 128 or len({
        row.get("route_id") for row in routes if type(row) is dict
    }) != len(routes):
        raise DiagnosticError("INVALID_SUPPORT_MATRIX")
    if type(matrix.get("revision")) is not str or type(integration.get("revision")) is not str:
        raise DiagnosticError("INVALID_CONTRACT_REVISION")
    observed = _observed_at(inspection.get("observed_at"))
    reasons: set[str] = set()
    if observed is None or not all(name in checks for name in EXPECTED_CHECKS):
        reasons.add("INSPECTION_INCOMPLETE")
    if not all(_command_ok(inspection, cmd) for cmd in REQUIRED_COMMANDS):
        reasons.add("INSPECTION_INCOMPLETE")

    ags_bytes = _mapping(checks.get("ags_bytes"))
    vm_bytes = _mapping(checks.get("vm_bytes"))
    if not _manifest_consistent(ags.get("files"), ags_bytes.get("files")):
        reasons.add("AGS_ARTIFACT_MISMATCH")
    if not _manifest_consistent(vm.get("files"), vm_bytes.get("files")):
        reasons.add("VM_ARTIFACT_MISMATCH")
    if _status(ags_bytes.get("status")) != "PASS":
        reasons.add("AGS_ARTIFACT_MISMATCH")
    if _status(vm_bytes.get("status")) != "PASS":
        reasons.add("VM_ARTIFACT_MISMATCH")
    if (installed.get("version") != vm.get("version_label") or
            installed.get("distribution") != vm.get("distribution") or
            installed.get("sqlite_schema_revision") != vm.get("sqlite_schema_revision") or
            installed.get("domain_schema_version") != vm.get("domain_schema_version") or
            _mapping(checks.get("vm_wheel")).get("observed_sha256") != vm.get("wheel_sha256") or
            _status(_mapping(checks.get("vm_wheel")).get("status")) != "PASS" or
            any(_status(_mapping(checks.get(n)).get("status")) != "PASS" for n in
                ("vm_schema", "vm_non_editable", "vm_distribution", "vm_created_ledger"))):
        reasons.add("VM_ARTIFACT_MISMATCH")

    provenance = _mapping(inspection.get("artifact_provenance"))
    if (provenance.get("ags_source_archive_sha256") != ags.get("archive_sha256")
            or provenance.get("vm_wheel_sha256") != vm.get("wheel_sha256")):
        reasons.add("UPSTREAM_IDENTITY_MISMATCH")
    if inspection.get("qualification") != "NOT_RUN" or inspection.get("provider_calls") != 0:
        reasons.add("INSPECTION_INCOMPLETE")

    hello = _mapping(checks.get("ags_vm_a2_namespace"))
    a2_valid = _epoch_ok(hello)
    if not a2_valid or _status(hello.get("status")) != "PASS":
        reasons.add("A2_NAMESPACE_UNAVAILABLE")
    plugin = _mapping(checks.get("vm_ags_compatibility"))
    if _status(plugin.get("status")) != "PASS" or not _command_ok(inspection, "vm_governance_check_plugin"):
        reasons.add("PLUGIN_INCOMPATIBLE")
    if installed.get("runtime_owner_platform_supported") is not True:
        reasons.add("RUNTIME_OWNER_NOT_VERIFIED")
    # An installed artifact check is never live model/effort observation.
    checks_for_integrity = {"ags_bytes", "vm_bytes", "vm_schema", "vm_non_editable",
                            "vm_wheel", "vm_distribution", "vm_created_ledger",
                            "ags_mcp_inventory", "ags_vm_a2_namespace", "vm_ags_compatibility"}
    if any(_status(_mapping(checks.get(k)).get("status")) != "PASS"
           for k in checks_for_integrity):
        reasons.add("INSPECTION_INCOMPLETE")
    # Durable route binding is not supplied by the adapter/inspection alone.
    seam = [x for x in _listed(integration.get("seams"))
            if type(x) is dict and x.get("id") == "aik-bound-runtime"]
    if not seam or seam[0].get("observed") != "implemented_and_qualified":
        reasons.add("DURABLE_BINDING_NOT_IMPLEMENTED")

    expected_unresolved = {row.get("code") for row in _listed(integration.get("unresolved_gates"))
                           if type(row) is dict}
    blocker_codes = sorted(expected_unresolved & EXPECTED_BLOCKERS)
    if not expected_unresolved <= EXPECTED_BLOCKERS:
        reasons.add("INCOMPLETE_PROVENANCE")

    route_reports = []
    baseline_sha = digest(baseline_bytes)
    for row in routes:
        if type(row) is not dict:
            raise DiagnosticError("INVALID_SUPPORT_MATRIX")
        identity = (row.get("route_id"), row.get("provider"), row.get("auth_kind"), row.get("role"))
        if not all(type(v) is str for v in identity):
            raise DiagnosticError("INVALID_ROUTE_IDENTITY")
        route_id, provider, auth, role = identity
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", route_id)
                or provider not in SCOPE or auth not in AUTHS or role not in ROLES):
            raise DiagnosticError("INVALID_ROUTE_IDENTITY")
        flags: set[str] = set()
        if (row.get("baseline_sha256") != baseline_sha or row.get("ags_revision") != ags["revision"]
                or row.get("vm_revision") != vm["revision"]
                or row.get("revision") != matrix["revision"]):
            flags.add("ROUTE_MATRIX_STALE")
        if ("A2_NAMESPACE_UNAVAILABLE" in reasons or "PLUGIN_INCOMPATIBLE" in reasons or
                "AGS_ARTIFACT_MISMATCH" in reasons or "VM_ARTIFACT_MISMATCH" in reasons):
            flags.add("UPSTREAM_GATE_BLOCKED")
        if row.get("authentication_status") != "PASS":
            flags.add("ROUTE_AUTH_NOT_CHECKED")
        if row.get("probe_status") != "PASS":
            flags.add("ROUTE_MODEL_PROBE_NOT_RUN")
        if row.get("qualification_status") not in ("PASS", "qualified"):
            flags.add("ROUTE_GOVERNED_QUALIFICATION_NOT_RUN")
        if row.get("adapter_status") != "implemented":
            flags.add("ROUTE_ADAPTER_NOT_QUALIFIED")
        if row.get("operationally_advertised") is True and flags:
            flags.add("ROUTE_MATRIX_STALE")
        route_reports.append({
            "route_id": route_id, "provider": provider, "auth_kind": auth,
            "role": role, "required": row.get("required") is True,
            "state": "BLOCKED" if flags else "UNVERIFIED",
            "codes": sorted(flags), "qualified": False,
        })
    if any("ROUTE_MATRIX_STALE" in row["codes"] for row in route_reports):
        reasons.add("ROUTE_MATRIX_STALE")
    identity_failure = bool(reasons & {"AGS_ARTIFACT_MISMATCH", "VM_ARTIFACT_MISMATCH",
                                      "UPSTREAM_IDENTITY_MISMATCH", "ROUTE_MATRIX_STALE"})
    incompatible = bool(reasons & {"A2_NAMESPACE_UNAVAILABLE", "PLUGIN_INCOMPATIBLE"})
    compatibility = "INCOMPATIBLE" if identity_failure or incompatible else "UNVERIFIED"
    # This diagnostic cannot produce a governance PASS. A separate installed
    # verifier and governed qualification must be run on an approved target.
    return {
        "format": "aik-upstream-diagnostic-v1",
        "task_id": "AIK-008",
        "source_kind": source,
        "observed_at": observed,
        "inspection_sha256": digest(inspection_bytes),
        "baseline_sha256": baseline_sha,
        "matrix_sha256": digest(matrix_bytes),
        "integration_sha256": digest(integration_bytes),
        "ags_revision": ags["revision"],
        "vm_revision": vm["revision"],
        "matrix_revision": matrix["revision"],
        "compatibility": compatibility,
        "status": "BLOCKED",
        "qualification": "NOT_QUALIFIED",
        "installed_conformance": "NOT_RUN" if source == "historical" else "REQUIRES_REVIEW",
        "model_effort_live_probe": "NOT_RUN",
        "provider_calls": 0,
        "codes": sorted(reasons),
        "upstream_blockers": blocker_codes,
        "routes": route_reports,
        "gui": {
            "ready_for_dispatch": False,
            "primary_code": ("UPSTREAM_IDENTITY_MISMATCH" if identity_failure else
                             "A2_NAMESPACE_UNAVAILABLE" if incompatible else
                             "INSPECTION_INCOMPLETE"),
            "next_step": "REQUALIFY_INSTALLED_UPSTREAM",
        },
    }


__all__ = ["DiagnosticError", "inspect", "read_json"]
