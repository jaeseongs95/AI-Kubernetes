"""Independent formal checks for the proposed AIK task execution binding.

Usage: python schema-validation.py --schema binding.schema.json --output report.json
       [--before original.schema.json]

Requires the separately installed jsonschema==4.26.0 package. This is standalone
evidence tooling, not a new dependency of AIK's standard-library backlog tests.
Fixtures below are hand-written from the contract, not generated from schema
properties, required lists, previous PASS records, or a model response.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import shlex
import sys

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for


BASE = {
    "vm_instance_id": "vm-instance-independent-probe",
    "vm_project_id": "project-independent-probe",
    "vm_goal_revision_id": "goal-revision-independent-probe",
    "vm_plan_revision_id": "plan-revision-independent-probe",
    "vm_task_id": "task-independent-probe",
    "vm_attempt_id": "attempt-independent-probe",
    "vm_execution_spec_digest": "sha256:" + "12" * 32,
    "vm_runtime_intent_id": "runtime-intent-independent-probe",
    "vm_provider_call_id": "provider-call-independent-probe",
    "cluster_id": "cluster-independent-probe",
    "route_id": "openai-api_key-developer-v1",
    "route_revision": "1",
    "connection_id": "connection-independent-probe",
    "connection_revision": "1",
    "provider": "openai",
    "auth_kind": "api_key",
    "provider_account_fingerprint": "account-fingerprint-independent-probe",
    "pool_id": "pool-independent-probe",
    "placement_id": "placement-independent-probe",
    "lease_id": "lease-independent-probe",
    "lease_fencing_token": 1,
    "support_matrix_digest": "sha256:" + "34" * 32,
    "input_manifest_digest": "sha256:" + "56" * 32,
    "workspace_root_ref": "workspace-root-independent-probe",
}


def fixtures() -> list[dict]:
    cases = [{"id": "valid_api_binding", "expected_valid": True, "instance": deepcopy(BASE)}]
    native = deepcopy(BASE)
    native.update({"provider": "anthropic", "auth_kind": "native_subscription",
                   "route_id": "anthropic-native_subscription-developer-v1",
                   "connection_id": "native-connection-independent-probe", "lease_fencing_token": 42})
    cases.append({"id": "valid_native_binding", "expected_valid": True, "instance": native})

    def altered(label: str, field: str, value: object) -> None:
        sample = deepcopy(BASE)
        sample[field] = value
        cases.append({"id": label, "expected_valid": False, "instance": sample})

    # Independent positive fixture enumerates the contract fields.  Each must be
    # present; do not infer this expectation from schema["required"].
    for field in BASE:
        sample = deepcopy(BASE)
        del sample[field]
        cases.append({"id": "missing_" + field, "expected_valid": False, "instance": sample})

    for field in ("route_id", "provider_account_fingerprint", "vm_task_id", "vm_attempt_id"):
        altered("empty_" + field, field, "")
    for field, value in (("route_id", 7), ("provider_account_fingerprint", None),
                         ("vm_task_id", []), ("vm_attempt_id", {})):
        altered("wrong_type_" + field, field, value)
    for label, value in (("zero", 0), ("negative", -1), ("boolean", True),
                         ("fraction", 1.5), ("string", "1")):
        altered("fencing_" + label, "lease_fencing_token", value)

    # Canonical digest is exactly sha256: followed by 64 lowercase hexadecimal
    # characters. A trailing line terminator is not part of a digest identity.
    for field in ("vm_execution_spec_digest", "support_matrix_digest", "input_manifest_digest"):
        for label, value in (("trailing_newline", "sha256:" + "a" * 64 + "\n"),
                             ("short", "sha256:" + "a" * 63),
                             ("non_hex", "sha256:" + "g" * 64),
                             ("wrong_prefix", "sha512:" + "a" * 64)):
            altered(field + "_" + label, field, value)

    # Test sentinels only, never real credentials. Unknown secret fields must not
    # become part of an otherwise valid cross-reference binding.
    for field in ("secret", "api_key", "access_token", "refresh_token", "password", "authorization"):
        altered("reject_unknown_" + field, field, "SYNTHETIC-NOT-A-CREDENTIAL")
    cases.extend([
        {"id": "root_array", "expected_valid": False, "instance": [deepcopy(BASE)]},
        {"id": "root_null", "expected_valid": False, "instance": None},
    ])
    return cases


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check_schema(path: Path, cases: list[dict]) -> dict:
    raw = path.read_bytes()
    schema = json.loads(raw)
    report = {
        "schema_path": str(path.resolve()), "schema_sha256": digest(raw),
        "schema_uri": schema.get("$schema"), "schema_id": schema.get("$id"),
        "validator_class": "jsonschema.Draft202012Validator",
        "declared_validator_matches": validator_for(schema) is Draft202012Validator,
    }
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        report.update({"status": "FAIL", "metaschema_status": "FAIL", "error": error.message,
                       "case_count": 0, "passed": 0, "failed": 0, "failed_case_ids": [], "cases": []})
        return report
    report["metaschema_status"] = "PASS"
    validator = Draft202012Validator(schema)
    results = []
    for case in cases:
        errors = list(validator.iter_errors(case["instance"]))
        valid = not errors
        results.append({
            "id": case["id"], "expected_valid": case["expected_valid"], "observed_valid": valid,
            "status": "PASS" if valid == case["expected_valid"] else "FAIL",
            "errors": [{"keyword": error.validator, "instance_path": list(error.absolute_path),
                        "schema_path": list(error.absolute_schema_path), "message": error.message}
                       for error in errors],
        })
    report.update({"status": "PASS" if report["declared_validator_matches"] and all(r["status"] == "PASS" for r in results) else "FAIL",
                   "case_count": len(results), "passed": sum(r["status"] == "PASS" for r in results),
                   "failed": sum(r["status"] == "FAIL" for r in results),
                   "failed_case_ids": [r["id"] for r in results if r["status"] == "FAIL"], "cases": results})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    args = parser.parse_args()
    source_paths = {args.schema.resolve(), Path(__file__).resolve()}
    if args.before:
        source_paths.add(args.before.resolve())
    if args.output.resolve() in source_paths:
        parser.error("output must not overwrite a schema or this script")
    cases = fixtures()
    packages = {name: importlib.metadata.version(name) for name in (
        "jsonschema", "jsonschema-specifications", "referencing", "attrs", "rpds-py", "typing-extensions")}
    report = {
        "format": "aik-binding-schema-validation-v1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "AIK-001 standalone formal contract validation; no product runtime qualification",
        "validator": {"class": "jsonschema.Draft202012Validator", "packages": packages,
                      "metaschema_source": "validator-bundled Draft 2020-12 metaschema and referenced specifications",
                      "python": platform.python_version(), "platform": platform.platform()},
        "references": ["https://python-jsonschema.readthedocs.io/en/stable/validate/",
                       "https://json-schema.org/draft/2020-12/schema"],
        "reproduction": {
            "install_argv": ["python", "-m", "pip", "install"] + [f"{name}=={version}" for name, version in packages.items()],
            "validation_argv": [sys.executable, str(Path(__file__).resolve()), "--schema", str(args.schema.resolve()),
                                "--output", str(args.output.resolve())] + (["--before", str(args.before.resolve())] if args.before else []),
            "script_sha256": digest(Path(__file__).read_bytes()),
            "fixture_construction": "Hand-written synthetic API/native positive bindings; independent required-field, type, digest, fencing and unknown-secret negative mutations. No fixtures are generated from schema required/properties or previous result claims.",
        },
        "fixtures": cases,
        "limits": ["Schema validity is not owner authorization, existence of VM Task/Attempt, account ownership or an unexpired lease.",
                   "A digest string matching its shape does not establish that it matches actual source bytes.",
                   "No actual credentials, provider calls, AGS/VM effects, installer or GUI were used in this check."],
    }
    if args.before:
        report["before"] = check_schema(args.before, cases)
    report["after"] = check_schema(args.schema, cases)
    if args.before:
        original = json.loads(args.before.read_text(encoding="utf-8"))
        updated = json.loads(args.schema.read_text(encoding="utf-8"))
        report["property_changes"] = {
            name: {"before": value, "after": updated.get("properties", {}).get(name)}
            for name, value in original.get("properties", {}).items()
            if value != updated.get("properties", {}).get(name)
        }
    report["status"] = report["after"]["status"]
    report["reproduction"]["command"] = shlex.join(report["reproduction"]["validation_argv"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"],
                      "before": {k: report["before"][k] for k in ("schema_sha256", "status", "passed", "failed", "failed_case_ids")} if "before" in report else None,
                      "after": {k: report["after"][k] for k in ("schema_sha256", "status", "passed", "failed", "failed_case_ids")},
                      "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
