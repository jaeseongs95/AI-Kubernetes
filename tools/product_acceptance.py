#!/usr/bin/env python3
"""AIK-083 candidate acceptance contract and independent file oracle.

This checker is a fail-closed contract/evidence preflight, NOT a substitute for
live VM/AGS, Windows qualification, or an independent human release audit.
No provider, credential, task, or model is invoked.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/product-acceptance/v1.json"
MAX_JSON_BYTES = 1024 * 1024
PHASE_IDS = {
    "M4": [f"P{x:02d}" for x in range(1, 11)],
    "M5": [f"P{x:02d}" for x in range(1, 12)],
    "M6": [f"P{x:02d}" for x in range(1, 13)],
}
GATES = {"M4": "AIK-064", "M5": "AIK-089", "M6": "AIK-090"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def unique_pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def load_json(path: Path) -> dict:
    raw = path.read_bytes()
    require(len(raw) <= MAX_JSON_BYTES, "OVERSIZED_JSON")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ContractError("NONFINITE_JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("INVALID_JSON") from exc
    require(type(parsed) is dict, "JSON_OBJECT_REQUIRED")
    return parsed


def blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def safe_member(root: Path, name: str) -> Path:
    require(type(name) is str and name and not name.startswith(("/", "\\")) and
            "\\" not in name and ".." not in name.split("/") and
            re.fullmatch(r"[a-zA-Z0-9_./-]+", name) is not None,
            "UNSAFE_CONTRACT_PATH")
    candidate = root / name
    require(candidate.is_file() and not candidate.is_symlink(), "MISSING_OR_SYMLINK_FILE")
    require(candidate.resolve().is_relative_to(root.resolve()), "UNSAFE_CONTRACT_PATH")
    return candidate


def validate_contract(root: Path = ROOT) -> dict:
    contract = load_json(root / "contracts/product-acceptance/v1.json")
    require(contract.get("format") == "aik-product-acceptance-v1" and
            type(contract.get("version")) is int and contract["version"] == 1 and
            contract.get("task_id") == "AIK-083", "CONTRACT_VERSION")
    require(contract.get("decision_status") in ("proposed", "confirmed") and
            contract.get("authority") in ("candidate_not_qualified", "qualified"),
            "DECISION_STATUS")
    matrix_spec = contract.get("matrix")
    require(type(matrix_spec) is dict and matrix_spec.get("path") == "contracts/upstream/support-matrix.json" and
            matrix_spec.get("decision_id") == "DEC12" and matrix_spec.get("git_blob_sha") and
            HEX40.fullmatch(matrix_spec["git_blob_sha"]), "MATRIX_BINDING")
    matrix_file = safe_member(root, matrix_spec["path"])
    require(blob_sha(matrix_file.read_bytes()) == matrix_spec["git_blob_sha"], "MATRIX_BYTES_MISMATCH")
    matrix = load_json(matrix_file)
    require(matrix.get("format") == "aik-support-matrix-v1" and
            str(matrix.get("revision")) == str(matrix_spec.get("revision")) and
            matrix.get("decision_status") == matrix_spec.get("decision_status"),
            "MATRIX_VERSION_MISMATCH")
    routes = matrix.get("routes")
    require(type(routes) is list and len(routes) >= 2 and
            len({row.get("route_id") for row in routes if type(row) is dict}) == len(routes),
            "MATRIX_ROUTES")
    required = [row for row in routes if row.get("required") is True]
    require({row.get("provider") for row in required} >= {"openai", "anthropic"} and
            {"director", "developer", "reviewer"} <= {row.get("role") for row in required},
            "REQUIRED_ROUTE_COVERAGE")

    journeys = contract.get("journeys")
    require(type(journeys) is list and len(journeys) == 12, "JOURNEY_COUNT")
    ids = [row.get("id") for row in journeys if type(row) is dict]
    require(len(ids) == 12 and ids == PHASE_IDS["M6"], "JOURNEY_IDS_ORDER")
    phases = contract.get("phases")
    require(type(phases) is dict and set(phases) == set(GATES), "PHASE_IDS")
    task_report = (root / "docs/development-backlog.ko.md").read_text(encoding="utf-8")
    known_tasks = set(re.findall(r"^### (AIK-\d{3})$", task_report, flags=re.MULTILINE))
    for phase, ids_needed in PHASE_IDS.items():
        value = phases[phase]
        require(type(value) is dict and value.get("gate") == GATES[phase] and
                value.get("journeys") == ids_needed and value.get("platform") == "Windows 11 x64",
                "PHASE_GATE_OR_COVERAGE")
        previous = None if phase == "M4" else GATES["M4" if phase == "M5" else "M5"]
        require(value.get("required_previous_gate") == previous, "PHASE_ORDER")
    for row in journeys:
        id_, scope = row["id"], row.get("scope")
        require(scope in GATES and row.get("gate") == GATES[scope], "JOURNEY_GATE")
        for field in ("title", "input", "observable_success", "blocking_failure"):
            require(type(row.get(field)) is str and len(row[field].strip()) >= 20,
                    "JOURNEY_DEFINITION_INCOMPLETE")
        owners = row.get("owners")
        require(type(owners) is dict and set(owners) == {"implementation", "gui", "verification"},
                "JOURNEY_OWNERS")
        for category in ("implementation", "gui", "verification"):
            links = owners[category]
            require(type(links) is list and links and
                    all(type(task) is str and task in known_tasks for task in links),
                    "JOURNEY_UNKNOWN_TASK")
        require(GATES[scope] in owners["verification"], "JOURNEY_GATE_OWNER")
        require(id_ in phases[scope]["journeys"], "JOURNEY_SCOPE")

    platform = contract.get("fixed_measurement_candidate")
    require(type(platform) is dict and platform.get("status") == "proposed_before_product_measurement" and
            platform.get("measurement_partition") == "local_gui_control_excludes_external_provider_latency",
            "MEASUREMENT_PROVENANCE")
    workload = platform.get("workloads", {})
    require(workload.get("startup_runs_each", 0) >= 20 and workload.get("idle_minutes_each", 0) >= 10 and
            workload.get("visible_sessions", 0) >= 20 and workload.get("active_workers", 0) >= 4 and
            workload.get("ui_interactions", 0) >= 100 and workload.get("soak_hours", 0) >= 8,
            "MEASUREMENT_WORKLOAD")
    thresholds = platform.get("thresholds", {})
    required_metrics = ("cold_start_p95_ms_max", "warm_start_p95_ms_max",
                        "control_input_p95_ms_max", "control_input_p99_ms_max",
                        "idle_unrequested_model_calls_max", "soak_memory_growth_mib_max",
                        "orphaned_owned_workers_max", "stale_or_unknown_effect_auto_retries_max")
    require(set(thresholds) == set(required_metrics) and
            all(type(thresholds[k]) is int and thresholds[k] >= 0 for k in required_metrics) and
            thresholds["idle_unrequested_model_calls_max"] == 0 and
            thresholds["orphaned_owned_workers_max"] == 0 and
            thresholds["stale_or_unknown_effect_auto_retries_max"] == 0,
            "MEASUREMENT_THRESHOLDS")

    oracle = contract.get("oracle")
    require(type(oracle) is dict and oracle.get("initial_expected") == "FAIL" and
            oracle.get("required_post_rework") == "PASS" and
            oracle.get("artifact_name") == "settlement.json" and
            type(oracle.get("raw_goal")) is str and "orders.csv" in oracle["raw_goal"] and
            "settlement.py" in oracle["raw_goal"] and
            "prebuilt_plan" in oracle.get("forbidden_bootstrap", []),
            "ORACLE_DEFINITION")
    fixture = oracle.get("fixture_git_blobs")
    require(type(fixture) is dict and set(fixture) == {"orders.csv", "policy.json", "settlement.py"},
            "ORACLE_FIXTURE_FILES")
    for name, sha in fixture.items():
        require(type(sha) is str and HEX40.fullmatch(sha) is not None, "ORACLE_FIXTURE_HASH")
        raw = safe_member(root, oracle["workspace_template"] + "/" + name).read_bytes()
        require(blob_sha(raw) == sha, "ORACLE_FIXTURE_CHANGED")

    policy = contract.get("release_policy")
    require(type(policy) is dict and policy.get("mandatory_status") == "PASS" and
            {"FAIL", "NOT_RUN", "BLOCKED", "SKIP", "UNKNOWN"} <= set(policy.get("forbidden_statuses", [])) and
            policy.get("critical_or_high_defects_max") == 0 and
            all(policy.get(key) is True for key in
                ("require_same_build_and_installer_digest", "require_matrix_bound_live_evidence",
                 "require_independent_oracle", "no_cross_phase_evidence_reuse",
                 "selected_optional_capabilities_must_be_in_gate_dependencies")),
            "RELEASE_POLICY")
    return contract


def expected_settlement(folder: Path) -> dict:
    inputs = folder / "orders.csv"
    policy = load_json(folder / "policy.json")
    require(policy.get("rounding") == "ROUND_HALF_UP_PER_LINE" and
            type(policy.get("vat_rate_bps")) is int and policy["vat_rate_bps"] == 1000 and
            policy.get("output_file") == "settlement.json", "FIXTURE_POLICY")
    with inputs.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        require(reader.fieldnames == ["order_id", "subtotal_krw", "discount_krw"], "FIXTURE_COLUMNS")
        rows = list(reader)
    require(len(rows) == 3 and len({r["order_id"] for r in rows}) == len(rows), "FIXTURE_ROWS")
    expected_rows = []
    for row in rows:
        require(re.fullmatch(r"[A-Z]-[0-9]{3}", row["order_id"]) is not None, "FIXTURE_ID")
        require(all(re.fullmatch(r"[0-9]+", row[k]) for k in ("subtotal_krw", "discount_krw")),
                "FIXTURE_MONEY")
        subtotal, discount = int(row["subtotal_krw"]), int(row["discount_krw"])
        require(subtotal >= discount, "FIXTURE_DISCOUNT")
        net = subtotal - discount
        vat = int((Decimal(net) * Decimal(policy["vat_rate_bps"]) / Decimal(10000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
        expected_rows.append({"order_id": row["order_id"], "net_krw": net,
                              "vat_krw": vat, "gross_krw": net + vat})
    total = {k: sum(row[k] for row in expected_rows) for k in ("net_krw", "vat_krw", "gross_krw")}
    return {"rows": expected_rows, "totals": total}


def check_oracle(workspace: Path, root: Path = ROOT) -> dict:
    contract = validate_contract(root)
    fixture_spec = contract["oracle"]["fixture_git_blobs"]
    workspace = workspace.resolve(strict=True)
    require(workspace.is_dir(), "WORKSPACE_MISSING")
    for name in ("orders.csv", "policy.json"):
        candidate = workspace / name
        require(candidate.is_file() and not candidate.is_symlink() and
                candidate.resolve().parent == workspace, "FIXTURE_INPUT_UNSAFE")
        require(blob_sha(candidate.read_bytes()) == fixture_spec[name], "FIXTURE_INPUT_CHANGED")
    expected = expected_settlement(workspace)
    actual_path = workspace / "settlement.json"
    require(actual_path.is_file() and not actual_path.is_symlink() and
            actual_path.resolve().parent == workspace, "ARTIFACT_MISSING")
    actual = load_json(actual_path)
    # Numeric JSON booleans are ints in Python: value equality alone would accept them.
    require(type(actual.get("rows")) is list and type(actual.get("totals")) is dict and
            list(actual) == ["rows", "totals"], "ARTIFACT_SCHEMA")
    require(len(actual["rows"]) == len(expected["rows"]), "ARTIFACT_ROWS")
    for row, match in zip(actual["rows"], expected["rows"]):
        require(type(row) is dict and set(row) == set(match) and
                all(type(row[k]) is type(match[k]) and row[k] == match[k] for k in match),
                "ARTIFACT_ROW_MISMATCH")
    require(set(actual["totals"]) == set(expected["totals"]) and
            all(type(actual["totals"][k]) is int and actual["totals"][k] == v
                for k, v in expected["totals"].items()), "ARTIFACT_TOTAL_MISMATCH")
    return {"status": "PASS", "artifact_sha256": hashlib.sha256(actual_path.read_bytes()).hexdigest(),
            "net_krw": expected["totals"]["net_krw"],
            "vat_krw": expected["totals"]["vat_krw"],
            "gross_krw": expected["totals"]["gross_krw"],
            "independent_file_oracle": True, "governed_agent_e2e": "NOT_RUN"}


def release_preflight(report: dict, contract: dict) -> dict:
    reasons = []
    def need(ok, code):
        if not ok:
            reasons.append(code)
    scope = report.get("scope")
    need(scope in PHASE_IDS, "UNKNOWN_SCOPE")
    need(contract["decision_status"] == "confirmed" and
         contract["matrix"]["decision_status"] == "confirmed", "UNAPPROVED_SCOPE_OR_MATRIX")
    need(contract["authority"] == "qualified", "CONTRACT_NOT_QUALIFIED")
    need(report.get("contract_blob_sha") == blob_sha(
        (ROOT / "contracts/product-acceptance/v1.json").read_bytes()), "CONTRACT_IDENTITY_MISMATCH")
    need(report.get("matrix_blob_sha") == contract["matrix"]["git_blob_sha"], "MATRIX_IDENTITY_MISMATCH")
    commit, installer = report.get("build_commit"), report.get("installer_sha256")
    need(type(commit) is str and HEX40.fullmatch(commit) is not None, "BUILD_IDENTITY")
    need(type(installer) is str and HEX64.fullmatch(installer) is not None, "INSTALLER_IDENTITY")
    host = report.get("host_qualification", {})
    need(type(host) is dict and host.get("status") == "PASS" and
         type(host.get("os")) is str and host["os"].startswith("Windows 11") and
         host.get("standard_user") is True and host.get("development_tools_absent") is True,
         "CLEAN_CLIENT_NOT_PROVEN")
    need(report.get("critical_or_high_defects") == 0, "CRITICAL_DEFECT")
    provenance = report.get("provenance", {})
    need(type(provenance) is dict and provenance.get("independent_review") is True and
         type(provenance.get("review_evidence_ref")) is str and
         bool(provenance["review_evidence_ref"].strip()), "INDEPENDENT_REVIEW_MISSING")
    if scope in PHASE_IDS:
        need(report.get("gate") == GATES[scope], "GATE_MISMATCH")
        rows = report.get("journeys", {})
        need(type(rows) is dict and set(rows) == set(PHASE_IDS[scope]), "JOURNEY_EVIDENCE_COVERAGE")
        if type(rows) is dict:
            for jid in PHASE_IDS[scope]:
                row = rows.get(jid, {})
                need(type(row) is dict and row.get("status") == "PASS" and
                     row.get("build_commit") == commit and row.get("installer_sha256") == installer and
                     row.get("matrix_blob_sha") == contract["matrix"]["git_blob_sha"] and
                     type(row.get("evidence_ref")) is str and bool(row["evidence_ref"].strip()) and
                     row.get("independent_oracle") is True, "JOURNEY_" + jid + "_NOT_QUALIFIED")
        if scope != "M4":
            need(report.get("previous_gate_status") == "PASS" and
                 report.get("previous_gate_on_same_build") is True, "PREVIOUS_GATE_NOT_PROVEN")
    metric = report.get("metrics", {})
    threshold = contract["fixed_measurement_candidate"]["thresholds"]
    need(type(metric) is dict and
         all(type(metric.get(key.replace("_max", ""))) in (int, float) and
             not isinstance(metric.get(key.replace("_max", "")), bool) and
             0 <= metric[key.replace("_max", "")] <= maximum
             for key, maximum in threshold.items()), "PERFORMANCE_NOT_QUALIFIED")
    need(report.get("threshold_approval_evidence_ref"), "THRESHOLD_APPROVAL_MISSING")
    need(report.get("live_required_routes_qualified") is True, "REQUIRED_ROUTES_UNQUALIFIED")
    need(report.get("selected_optional_dependencies_qualified") is True, "OPTIONAL_SCOPE_UNQUALIFIED")
    return {"status": "BLOCKED" if reasons else "READY_FOR_INDEPENDENT_RELEASE_REVIEW",
            "reasons": reasons, "scope": scope, "release_approved": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    oracle = sub.add_parser("oracle")
    oracle.add_argument("--workspace", required=True, type=Path)
    release = sub.add_parser("release-preflight")
    release.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        contract = validate_contract()
        if args.command == "validate":
            result = {"status": "VALID_DRAFT", "task_id": "AIK-083", "journeys": 12,
                      "decision_status": contract["decision_status"], "release_approved": False}
        elif args.command == "oracle":
            result = check_oracle(args.workspace)
        else:
            result = release_preflight(load_json(args.report), contract)
    except (ContractError, OSError, KeyError, TypeError, ValueError) as exc:
        result = {"status": "FAIL", "code": str(exc) if isinstance(exc, ContractError) else "CHECKER_ERROR"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in ("VALID_DRAFT", "PASS") else 2 if result["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
