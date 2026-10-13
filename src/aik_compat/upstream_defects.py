"""AIK-008 upstream defect list, route separation and AIK-081/082 linkage.

This module consumes a diagnostic report produced by ``diagnostics.inspect``.
It does not run an installer, contact a provider or read credentials. Its
output never contains a governance PASS, a qualified route or a COMPATIBLE
state. ``compatible`` stays reserved for a separate installed conformance run
and governed qualification on an approved target.

Only fixed codes, contract identifiers, bounded revisions, version labels,
timezone-aware timestamps and digests are returned. Raw stdout/stderr, local
paths and provider text from inspection reports are not copied into the
result. Free-form diagnostic text in revision/timestamp fields is dropped.

``identity`` holds the baseline contract expectation, not values observed on
an installation. A saved diagnostic consumed by the CLI is recomputed from its
inspection record before use (``verify_saved_diagnostic``).
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from .diagnostics import KNOWN_CODES, DiagnosticError, digest, inspect, read_json

FORMAT = "aik-upstream-defects-v1"
DIAGNOSTIC_FORMAT = "aik-upstream-diagnostic-v1"
_REPO = Path(__file__).resolve().parents[2]
_CONTRACTS = _REPO / "contracts" / "upstream"
_HISTORICAL_INSPECTION = _CONTRACTS / "installed-inspection.json"

_TASK = re.compile(r"AIK-\d{3}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_RUNTIME = re.compile(r"[a-z0-9][a-z0-9_]{0,63}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE = re.compile(r"[A-Za-z0-9._/#-]{1,200}\Z")

_SOURCES = {"historical": "NOT_RUN", "live": "REQUIRES_REVIEW"}
_ROUTE_STATES = frozenset(("BLOCKED", "UNVERIFIED"))
# Fields excluded from the saved-diagnostic recomputation comparison. The
# timestamp is format-checked separately instead of being trusted verbatim.
_VOLATILE_DIAGNOSTIC_KEYS = frozenset(("observed_at",))

# A diagnostic finding that reproduces a declared integration-contract gate.
REASON_TO_DEFECT = {
    "A2_NAMESPACE_UNAVAILABLE": "AGS_A2_NAMESPACE_UNAVAILABLE",
    "PLUGIN_INCOMPATIBLE": "AGS_HOST_INTEGRATION_MANIFEST_MISSING",
    "DURABLE_BINDING_NOT_IMPLEMENTED": "AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED",
    "RUNTIME_OWNER_NOT_VERIFIED": "VM_RUNTIME_OWNER_WINDOWS_ONLY",
}

# Top-level diagnostic code -> (category, follow-up tasks).
FINDINGS = {
    "AGS_ARTIFACT_MISMATCH": ("installation", ("AIK-008",)),
    "VM_ARTIFACT_MISMATCH": ("installation", ("AIK-008",)),
    "UPSTREAM_IDENTITY_MISMATCH": ("installation", ("AIK-008",)),
    "INSPECTION_INCOMPLETE": ("evidence", ("AIK-008",)),
    "INCOMPLETE_PROVENANCE": ("evidence", ("AIK-008",)),
    "ROUTE_MATRIX_STALE": ("evidence", ("AIK-008",)),
    "A2_NAMESPACE_UNAVAILABLE": ("upstream", ("AIK-081",)),
    "PLUGIN_INCOMPATIBLE": ("upstream", ("AIK-081",)),
    "UPSTREAM_GATE_BLOCKED": ("upstream", ("AIK-081",)),
    "RUNTIME_OWNER_NOT_VERIFIED": ("platform", ("AIK-086",)),
    "DURABLE_BINDING_NOT_IMPLEMENTED": ("aik", ("AIK-031",)),
    "ROUTE_AUTH_NOT_CHECKED": ("route", ("AIK-022",)),
    "ROUTE_MODEL_PROBE_NOT_RUN": ("route", ("AIK-082",)),
    "ROUTE_GOVERNED_QUALIFICATION_NOT_RUN": ("route", ("AIK-086",)),
    "ROUTE_ADAPTER_NOT_QUALIFIED": ("route", ("AIK-031", "AIK-035")),
}

# Route code -> follow-up tasks (route-local; never propagated to siblings).
ROUTE_CODE_TASKS = {
    "ROUTE_MATRIX_STALE": ("AIK-008",),
    "UPSTREAM_GATE_BLOCKED": ("AIK-081",),
    "ROUTE_AUTH_NOT_CHECKED": ("AIK-022",),
    "ROUTE_MODEL_PROBE_NOT_RUN": ("AIK-082",),
    "ROUTE_GOVERNED_QUALIFICATION_NOT_RUN": ("AIK-086",),
    "ROUTE_ADAPTER_NOT_QUALIFIED": ("AIK-031", "AIK-035"),
}


def _fail(code: str) -> DiagnosticError:
    return DiagnosticError(code)


def _error_code(error: BaseException) -> str:
    """Return a fixed code carried by a DiagnosticError, never free text."""
    candidates = [getattr(error, "code", None)]
    if error.args:
        candidates.append(error.args[0])
    candidates.append(str(error))
    for candidate in candidates:
        if type(candidate) is str and _CODE.fullmatch(candidate):
            return candidate
    return "UNSPECIFIED_DIAGNOSTIC_ERROR"


def _mapping(value: Any) -> dict[str, Any]:
    return value if type(value) is dict else {}


def _classify(code: str) -> tuple[str, bool | None, str, str]:
    """Return (owner, upstream, kind, follow-up task) for a gate code."""
    if code.startswith("AGS_"):
        return "AGS", True, "upstream_defect", "AIK-081"
    if code.startswith("VM_"):
        return "VM", True, "upstream_platform_limit", "AIK-086"
    if code.startswith("AIK_"):
        return "AIK", False, "aik_gap", "AIK-031"
    return "UNCLASSIFIED", None, "unclassified", "AIK-008"


def _message_key(code: str) -> str:
    return "aik.upstream." + code.lower()


def _tasks(values: Any) -> list[str]:
    if type(values) is not list:
        raise _fail("INVALID_TASK_REFERENCE")
    result = set()
    for value in values:
        if type(value) is not str or _TASK.fullmatch(value) is None:
            raise _fail("INVALID_TASK_REFERENCE")
        result.add(value)
    return sorted(result)


def _label(value: Any) -> str | None:
    return value if type(value) is str and _LABEL.fullmatch(value) else None


def _revision_value(value: Any) -> str | int | None:
    """A revision is a bounded label or a nonnegative integer; else dropped."""
    if type(value) is int and value >= 0:
        return value
    return _label(value)


def _timestamp(value: Any) -> str | None:
    """Keep only a bounded, timezone-aware ISO 8601 timestamp."""
    if type(value) is not str or not 1 <= len(value) <= 64:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return value


def _sha_or_none(value: Any) -> str | None:
    return value if type(value) is str and _HEX.fullmatch(value) else None


def _bounded_text(value: Any) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= 500:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    return value


def _manifest(files: Any) -> tuple[int, str]:
    if type(files) is not list or not files:
        raise _fail("INVALID_UPSTREAM_LOCK")
    rows = []
    seen = set()
    for entry in files:
        entry = _mapping(entry)
        path, sha = entry.get("path"), entry.get("sha256")
        if type(path) is not str or not path or path in seen or _sha_or_none(sha) is None:
            raise _fail("INVALID_UPSTREAM_LOCK")
        seen.add(path)
        rows.append([path, sha])
    rows.sort()
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(rows), digest(canonical)


def _code_list(values: Any) -> list[str]:
    if type(values) is not list:
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    if any(type(code) is not str or code not in KNOWN_CODES for code in values):
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    if len(set(values)) != len(values):
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    return sorted(values)


def _validate_diagnostic(diagnostic: Any, baseline_bytes: bytes, matrix_bytes: bytes,
                         integration_bytes: bytes) -> None:
    """Reject diagnostics that claim more than diagnostics.inspect can produce."""
    if type(diagnostic) is not dict:
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    source = diagnostic.get("source_kind")
    provider_calls = diagnostic.get("provider_calls")
    if (diagnostic.get("format") != DIAGNOSTIC_FORMAT
            or diagnostic.get("task_id") != "AIK-008"
            or diagnostic.get("status") != "BLOCKED"
            or diagnostic.get("qualification") != "NOT_QUALIFIED"
            or type(provider_calls) is not int or provider_calls != 0
            or diagnostic.get("model_effort_live_probe") != "NOT_RUN"
            or type(source) is not str or source not in _SOURCES
            or diagnostic.get("installed_conformance") != _SOURCES[source]
            or diagnostic.get("compatibility") not in ("INCOMPATIBLE", "UNVERIFIED")):
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    gui = diagnostic.get("gui")
    if (type(gui) is not dict or gui.get("ready_for_dispatch") is not False
            or type(gui.get("primary_code")) is not str or gui.get("primary_code") not in KNOWN_CODES
            or gui.get("next_step") != "REQUALIFY_INSTALLED_UPSTREAM"):
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    _code_list(diagnostic.get("codes"))
    blockers = diagnostic.get("upstream_blockers")
    if type(blockers) is not list or any(type(c) is not str or _CODE.fullmatch(c) is None for c in blockers):
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    routes = diagnostic.get("routes")
    if type(routes) is not list or not routes:
        raise _fail("UNTRUSTED_DIAGNOSTIC")
    for row in routes:
        if (type(row) is not dict or row.get("qualified") is not False
                or row.get("state") not in _ROUTE_STATES
                or type(row.get("required")) is not bool
                or any(type(row.get(key)) is not str for key in ("route_id", "provider", "auth_kind", "role"))):
            raise _fail("UNTRUSTED_DIAGNOSTIC")
        codes = _code_list(row.get("codes"))
        if row["state"] == "BLOCKED" and not codes:
            raise _fail("UNTRUSTED_DIAGNOSTIC")
    for key, raw in (("baseline_sha256", baseline_bytes), ("matrix_sha256", matrix_bytes),
                     ("integration_sha256", integration_bytes)):
        if type(raw) is not bytes or diagnostic.get(key) != digest(raw):
            raise _fail("DIAGNOSTIC_INPUT_MISMATCH")


def _normalized_diagnostic(value: Any) -> str:
    if type(value) is not dict:
        raise _fail("DIAGNOSTIC_INPUT_MISMATCH")
    trimmed = {key: item for key, item in value.items() if key not in _VOLATILE_DIAGNOSTIC_KEYS}
    return json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verify_saved_diagnostic(diagnostic: Any, inspection: dict[str, Any], inspection_bytes: bytes,
                            baseline: dict[str, Any], baseline_bytes: bytes,
                            matrix: dict[str, Any], matrix_bytes: bytes,
                            integration: dict[str, Any], integration_bytes: bytes,
                            *, require_source: str | None = None) -> None:
    """Bind a saved diagnostic to its inspection record by recomputation.

    The claimed ``source_kind`` is reused for the recomputation; it cannot be
    proven offline, so ``require_source`` pins it when the caller knows the
    inspection record's origin (for example the committed historical record).
    Everything except ``observed_at`` must equal ``diagnostics.inspect``.
    """
    _validate_diagnostic(diagnostic, baseline_bytes, matrix_bytes, integration_bytes)
    source = diagnostic["source_kind"]
    if require_source is not None and source != require_source:
        raise _fail("DIAGNOSTIC_INPUT_MISMATCH")
    recomputed = inspect(inspection, inspection_bytes, baseline, baseline_bytes,
                         matrix, matrix_bytes, integration, integration_bytes, source=source)
    if _normalized_diagnostic(diagnostic) != _normalized_diagnostic(recomputed):
        raise _fail("DIAGNOSTIC_INPUT_MISMATCH")


def _identity(baseline: dict[str, Any], diagnostic: dict[str, Any]) -> dict[str, Any]:
    ags, vm = _mapping(baseline.get("ags")), _mapping(baseline.get("vm"))
    if ags.get("revision") != diagnostic.get("ags_revision") or vm.get("revision") != diagnostic.get("vm_revision"):
        raise _fail("DIAGNOSTIC_INPUT_MISMATCH")
    ags_count, ags_manifest = _manifest(ags.get("files"))
    vm_count, vm_manifest = _manifest(vm.get("files"))
    present = ags.get("host_integration_manifest_present")
    host_manifest = "PRESENT" if present is True else "ABSENT" if present is False else "UNKNOWN"
    schema_revision = vm.get("sqlite_schema_revision")
    codes = set(diagnostic["codes"])

    def installed_match(code: str) -> str:
        # Never MATCH: matching the installed artifact requires installed
        # conformance, which this module does not run.
        return "MISMATCH_REPORTED" if code in codes else "NOT_CONFIRMED"

    return {
        # The ags/vm values below are the baseline contract expectation, not
        # values observed on the current installation.
        "identity_source": "baseline_contract",
        "ags": {
            "revision": _revision_value(ags.get("revision")),
            "version_label": _label(ags.get("version_label")),
            "archive_sha256": _sha_or_none(ags.get("archive_sha256")),
            "archive_tree_digest": _sha_or_none(ags.get("archive_tree_digest")),
            "manifest_file_count": ags_count,
            "manifest_sha256": ags_manifest,
            "host_integration_manifest": host_manifest,
            "installed_match": installed_match("AGS_ARTIFACT_MISMATCH"),
        },
        "vm": {
            "revision": _revision_value(vm.get("revision")),
            "distribution": _label(vm.get("distribution")),
            "version_label": _label(vm.get("version_label")),
            "wheel_sha256": _sha_or_none(vm.get("wheel_sha256")),
            "sqlite_schema_revision": schema_revision if type(schema_revision) is int else None,
            "domain_schema_version": _label(vm.get("domain_schema_version")),
            "manifest_file_count": vm_count,
            "manifest_sha256": vm_manifest,
            "installed_match": installed_match("VM_ARTIFACT_MISMATCH"),
        },
        "baseline_sha256": diagnostic["baseline_sha256"],
        "matrix_sha256": diagnostic["matrix_sha256"],
        "integration_sha256": diagnostic["integration_sha256"],
        "inspection_sha256": _sha_or_none(diagnostic.get("inspection_sha256")),
        "matrix_revision": _label(diagnostic.get("matrix_revision")),
    }


def _defects(integration: dict[str, Any], codes: set[str], source: str) -> list[dict[str, Any]]:
    gates = integration.get("unresolved_gates")
    if type(gates) is not list:
        raise _fail("INVALID_INTEGRATION_CONTRACT")
    observed = {REASON_TO_DEFECT[code] for code in codes if code in REASON_TO_DEFECT}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def entry(code: str, tasks: list[str], declared: bool, evidence: str | None,
              change: str | None) -> dict[str, Any]:
        owner, upstream, kind, follow_up = _classify(code)
        is_observed = code in observed
        return {
            "code": code,
            "owner": owner,
            "upstream": upstream,
            "kind": kind,
            "declared_in_contract": declared,
            # Never RESOLVED: closing a defect requires installed conformance
            # at a new artifact identity, which this module does not run.
            "status": "OBSERVED" if is_observed else "DECLARED",
            "observation_source": source if is_observed else None,
            "resolution": "REQUIRES_INSTALLED_CONFORMANCE",
            "follow_up_task": follow_up,
            "linked_tasks": tasks,
            "evidence_ref": evidence,
            "required_change": change,
            "message_key": _message_key(code),
        }

    for gate in gates:
        gate = gate if type(gate) is dict else None
        if gate is None:
            raise _fail("INVALID_INTEGRATION_CONTRACT")
        code = gate.get("code")
        if type(code) is not str or _CODE.fullmatch(code) is None or code in seen:
            raise _fail("INVALID_INTEGRATION_CONTRACT")
        seen.add(code)
        evidence = gate.get("evidence")
        evidence = evidence if type(evidence) is str and _EVIDENCE.fullmatch(evidence) else None
        result.append(entry(code, _tasks(gate.get("blocks", [])), True, evidence,
                            _bounded_text(gate.get("required_change"))))
    for code in sorted(observed - seen):
        result.append(entry(code, [], False, None, None))
    return result


def _routes(diagnostic: dict[str, Any], matrix: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    rows = matrix.get("routes")
    if type(rows) is not list:
        raise _fail("INVALID_SUPPORT_MATRIX")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        if type(row) is not dict or type(row.get("route_id")) is not str or row["route_id"] in by_id:
            raise _fail("INVALID_SUPPORT_MATRIX")
        by_id[row["route_id"]] = row
    diag_routes = diagnostic["routes"]
    if {row["route_id"] for row in diag_routes} != set(by_id) or len(diag_routes) != len(by_id):
        raise _fail("DIAGNOSTIC_INPUT_MISMATCH")
    result = []
    for route in diag_routes:
        row = by_id[route["route_id"]]
        if any(row.get(key) != route[key] for key in ("provider", "auth_kind", "role")):
            raise _fail("DIAGNOSTIC_INPUT_MISMATCH")
        runtime = row.get("runtime")
        if type(runtime) is not str or _RUNTIME.fullmatch(runtime) is None:
            raise _fail("INVALID_SUPPORT_MATRIX")
        codes = sorted(route["codes"])
        code_tasks = sorted({task for code in codes for task in ROUTE_CODE_TASKS.get(code, ())})
        required = route["required"]
        result.append({
            "route_id": route["route_id"],
            "provider": route["provider"],
            "auth_kind": route["auth_kind"],
            "role": route["role"],
            "runtime": runtime,
            "required": required,
            # State is derived only from this route's own codes.
            "state": route["state"],
            "support": ("required" if required else "optional") + "_" + route["state"].lower(),
            "codes": codes,
            "code_tasks": code_tasks,
            "matrix_blocking_tasks": _tasks(row.get("blocking_tasks", [])),
            "dispatchable": False,
            "qualified": False,
        })
    return result, by_id


def _model_effort(routes: list[dict[str, Any]], by_id: dict[str, dict], live_probe: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for route in routes:
        groups.setdefault(route["runtime"], []).append(route)
    result = []
    for runtime, members in groups.items():
        providers = {m["provider"] for m in members}
        auths = {m["auth_kind"] for m in members}
        if len(providers) != 1 or len(auths) != 1:
            raise _fail("INVALID_SUPPORT_MATRIX")
        reported = all(by_id[m["route_id"]].get("probe_status") == "PASS" for m in members)
        # A matrix string is a document claim, never a live observation.
        observation = "REPORTED_REQUIRES_LIVE_EVIDENCE" if reported else "NOT_RUN"
        result.append({
            "runtime": runtime,
            "provider": next(iter(providers)),
            "auth_kind": next(iter(auths)),
            "task": "AIK-082",
            "route_ids": [m["route_id"] for m in members],
            # Per-route state/codes stay separate; one route's stale or
            # unsupported result never changes a sibling's entry here.
            "route_states": [{"route_id": m["route_id"], "state": m["state"], "codes": list(m["codes"])}
                             for m in members],
            # state/codes below are an explicit aggregate of route_states.
            "state_scope": "AGGREGATE_OF_ROUTES",
            "state": "BLOCKED" if any(m["state"] == "BLOCKED" for m in members) else "UNVERIFIED",
            "codes": sorted({code for m in members for code in m["codes"]}),
            "model_observation": observation,
            "effort_observation": observation,
            "live_probe": live_probe,
            "qualified": False,
        })
    return result


def build_defect_report(diagnostic: dict[str, Any], baseline: dict[str, Any], baseline_bytes: bytes,
                        matrix: dict[str, Any], matrix_bytes: bytes,
                        integration: dict[str, Any], integration_bytes: bytes) -> dict[str, Any]:
    """Return a JSON-serializable, GUI-consumable upstream defect list.

    This function validates diagnostic claims and contract digests but does
    not recompute the diagnostic. Call ``verify_saved_diagnostic`` first for a
    diagnostic that was not produced in-process by ``diagnostics.inspect``.
    """
    _validate_diagnostic(diagnostic, baseline_bytes, matrix_bytes, integration_bytes)
    if type(baseline) is not dict or type(matrix) is not dict or type(integration) is not dict:
        raise _fail("INVALID_CONTRACT_FORMAT")
    source = diagnostic["source_kind"]
    codes = set(diagnostic["codes"])
    identity = _identity(baseline, diagnostic)
    defects = _defects(integration, codes, source)
    routes, by_id = _routes(diagnostic, matrix)
    findings = [{"code": code, "category": FINDINGS.get(code, ("evidence", ("AIK-008",)))[0],
                 "linked_tasks": list(FINDINGS.get(code, ("evidence", ("AIK-008",)))[1]),
                 "message_key": _message_key(code)} for code in sorted(codes)]
    a2_codes = sorted(codes & {"A2_NAMESPACE_UNAVAILABLE", "PLUGIN_INCOMPATIBLE"})
    a2_plugin = {
        "task": "AIK-081",
        "state": "BLOCKED" if a2_codes else "UNVERIFIED",
        "codes": a2_codes,
        "ags_revision": identity["ags"]["revision"],
        "ags_version_label": identity["ags"]["version_label"],
        "host_integration_manifest": identity["ags"]["host_integration_manifest"],
        "related_defects": sorted(d["code"] for d in defects if d["owner"] == "AGS"),
        "qualified": False,
    }
    reasons: list[dict[str, Any]] = []
    listed: set[str] = set()
    for defect in defects:
        if defect["status"] == "OBSERVED" and defect["code"] not in listed:
            listed.add(defect["code"])
            reasons.append({"code": defect["code"], "category": defect["kind"],
                            "message_key": defect["message_key"],
                            "linked_tasks": sorted({defect["follow_up_task"], *defect["linked_tasks"]})})
    for finding in findings:
        if finding["code"] not in listed:
            listed.add(finding["code"])
            reasons.append(dict(finding))
    canonical = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compatibility = diagnostic["compatibility"]
    return {
        "format": FORMAT,
        "task_id": "AIK-008",
        "source_kind": source,
        "observed_at": _timestamp(diagnostic.get("observed_at")),
        "diagnostic_sha256": digest(canonical),
        "compatibility": compatibility,
        "compatibility_state": compatibility.lower(),
        "status": "BLOCKED",
        "qualification": "NOT_QUALIFIED",
        "installed_conformance": diagnostic["installed_conformance"],
        "provider_calls": 0,
        "identity": identity,
        "defects": defects,
        "findings": findings,
        "linkage": {
            "a2_plugin": a2_plugin,
            "model_effort": _model_effort(routes, by_id, diagnostic["model_effort_live_probe"]),
        },
        "routes": routes,
        "gui": {
            "ready_for_dispatch": False,
            "primary_code": diagnostic["gui"]["primary_code"],
            "next_step": diagnostic["gui"]["next_step"],
            "reasons": reasons,
        },
    }


def diagnose_defects(inspection: dict[str, Any], inspection_bytes: bytes,
                     baseline: dict[str, Any], baseline_bytes: bytes,
                     matrix: dict[str, Any], matrix_bytes: bytes,
                     integration: dict[str, Any], integration_bytes: bytes,
                     *, source: str = "historical") -> dict[str, Any]:
    """Run the existing aggregator, then derive the defect list."""
    diagnostic = inspect(inspection, inspection_bytes, baseline, baseline_bytes,
                         matrix, matrix_bytes, integration, integration_bytes, source=source)
    return build_defect_report(diagnostic, baseline, baseline_bytes, matrix, matrix_bytes,
                               integration, integration_bytes)


def _failure_report(cause: str) -> dict[str, Any]:
    return {
        "format": FORMAT, "task_id": "AIK-008", "status": "FAIL",
        "code": "INVALID_DEFECT_INPUT", "cause_code": cause,
        "qualification": "NOT_QUALIFIED", "provider_calls": 0,
        "gui": {
            "ready_for_dispatch": False,
            "primary_code": "INVALID_DEFECT_INPUT",
            "next_step": "FIX_DEFECT_INPUT",
            "reasons": [{"code": cause, "category": "input", "message_key": _message_key(cause),
                         "linked_tasks": ["AIK-008"]}],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIK-008 upstream defect list (offline, no provider calls)")
    parser.add_argument("--inspection", type=Path,
                        help="Installed inspection report (default: committed historical record)")
    parser.add_argument("--diagnostic", type=Path,
                        help="Saved aik-upstream-diagnostic-v1 report; recomputed against --inspection")
    parser.add_argument("--baseline", type=Path, default=_CONTRACTS / "baseline.json")
    parser.add_argument("--matrix", type=Path, default=_CONTRACTS / "support-matrix.json")
    parser.add_argument("--integration", type=Path, default=_CONTRACTS / "integration-contract.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-blocked", action="store_true",
                        help="Return zero for a valid blocked report (snapshot review only)")
    args = parser.parse_args(argv)
    if args.output is not None and args.output.resolve().is_relative_to(_REPO):
        parser.error("output must be outside the source checkout")
    try:
        baseline, baseline_bytes = read_json(args.baseline)
        matrix, matrix_bytes = read_json(args.matrix)
        integration, integration_bytes = read_json(args.integration)
        inspection_path = args.inspection or _HISTORICAL_INSPECTION
        inspection, inspection_bytes = read_json(inspection_path)
        if args.diagnostic is not None:
            diagnostic, _ = read_json(args.diagnostic)
            # The committed record is historical; a live claim against it is
            # a forged source. A live claim with another record stays a claim.
            pinned = "historical" if inspection_path.resolve() == _HISTORICAL_INSPECTION.resolve() else None
            verify_saved_diagnostic(diagnostic, inspection, inspection_bytes, baseline, baseline_bytes,
                                    matrix, matrix_bytes, integration, integration_bytes,
                                    require_source=pinned)
        else:
            diagnostic = inspect(inspection, inspection_bytes, baseline, baseline_bytes, matrix,
                                 matrix_bytes, integration, integration_bytes, source="historical")
        report = build_defect_report(diagnostic, baseline, baseline_bytes, matrix, matrix_bytes,
                                     integration, integration_bytes)
        exit_code = 0 if args.allow_blocked else 2
    except (DiagnosticError, ValueError, TypeError, OSError, KeyError, RecursionError) as error:
        # Never echo paths, raw payloads or exception text; only fixed codes.
        if isinstance(error, DiagnosticError):
            cause = _error_code(error)
        elif isinstance(error, OSError):
            cause = "INPUT_UNREADABLE"
        elif isinstance(error, (ValueError, RecursionError)):
            cause = "INPUT_MALFORMED"
        else:
            cause = "UNEXPECTED_INPUT_STRUCTURE"
        report = _failure_report(cause)
        exit_code = 1
    content = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        except OSError:
            print(json.dumps({"status": "FAIL", "code": "OUTPUT_WRITE_FAILED"}))
            return 1
    else:
        print(content, end="")
    return exit_code


__all__ = ["FORMAT", "build_defect_report", "diagnose_defects", "verify_saved_diagnostic", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
