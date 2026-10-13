"""Deterministic AIK-083 patch from the pinned pre-fix source; no network.

Used only to prepare tested blobs on the task branch. The final commit includes
its output tools/product_acceptance.py and does not run this at application start.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ORIGINAL = "766a91bf50596377b5a72bcb301d6396a1b78b34"


def blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def patched(raw: bytes) -> bytes:
    if blob(raw) != ORIGINAL:
        raise RuntimeError("Patch source identity mismatch")
    source = raw.decode("utf-8")
    old = "class ContractError(ValueError):\n    pass"
    assert source.count(old) == 1
    source = source.replace(old, "from acceptance_io import ContractError, parse_json, read_snapshot")
    start, end = source.index("def load_json("), source.index("def blob_sha(")
    source = source[:start] + '''def load_json(path: Path) -> dict:
    snapshot = read_snapshot(path, maximum=MAX_JSON_BYTES)
    result = parse_json(snapshot.data, maximum=MAX_JSON_BYTES)
    snapshot.require_unchanged()
    return result


''' + source[end:]
    start, end = source.index("def expected_settlement("), source.index("def check_oracle(")
    function = source[start:end]
    before = '''def expected_settlement(folder: Path) -> dict:
    inputs = folder / "orders.csv"
    policy = load_json(folder / "policy.json")'''
    after = '''def _expected_settlement_from_bytes(inputs: bytes, policy: dict) -> dict:
    # Only already captured, hash-checked bytes enter the independent calculation.'''
    assert function.count(before) == 1
    function = function.replace(before, after)
    before = '    with inputs.open("r", newline="", encoding="utf-8") as stream:'
    after = '    with io.StringIO(inputs.decode("utf-8"), newline="") as stream:'
    assert function.count(before) == 1
    function = function.replace(before, after)
    function += '''def expected_settlement(folder: Path) -> dict:
    inputs = read_snapshot(folder / "orders.csv")
    policy = read_snapshot(folder / "policy.json")
    result = _expected_settlement_from_bytes(inputs.data, parse_json(policy.data))
    inputs.require_unchanged()
    policy.require_unchanged()
    return result


'''
    source = source[:start] + function + source[end:]
    start, end = source.index("def check_oracle("), source.index("def release_preflight(")
    oracle = '''def check_oracle(workspace: Path, root: Path = ROOT) -> dict:
    contract = validate_contract(root)
    fixture_spec = contract["oracle"]["fixture_git_blobs"]
    workspace = workspace.resolve(strict=True)
    require(workspace.is_dir(), "WORKSPACE_MISSING")
    inputs = {}
    for name in ("orders.csv", "policy.json"):
        candidate = workspace / name
        require(candidate.is_file() and not candidate.is_symlink() and
                candidate.resolve().parent == workspace, "FIXTURE_INPUT_UNSAFE")
        snapshot = read_snapshot(candidate)
        require(blob_sha(snapshot.data) == fixture_spec[name], "FIXTURE_INPUT_CHANGED")
        inputs[name] = snapshot
    expected = _expected_settlement_from_bytes(
        inputs["orders.csv"].data, parse_json(inputs["policy.json"].data))
    actual_path = workspace / "settlement.json"
    require(actual_path.is_file() and not actual_path.is_symlink() and
            actual_path.resolve().parent == workspace, "ARTIFACT_MISSING")
    artifact = read_snapshot(actual_path)
    actual = parse_json(artifact.data)
    # Object member order is not significant. Row order and exact integer types
    # remain contractual: bool/float amounts, duplicate/extra fields are rejected.
    require(type(actual.get("rows")) is list and type(actual.get("totals")) is dict and
            set(actual) == {"rows", "totals"}, "ARTIFACT_SCHEMA")
    require(len(actual["rows"]) == len(expected["rows"]), "ARTIFACT_ROWS")
    for row, match in zip(actual["rows"], expected["rows"]):
        require(type(row) is dict and set(row) == set(match) and
                all(type(row[k]) is type(match[k]) and row[k] == match[k] for k in match),
                "ARTIFACT_ROW_MISMATCH")
    require(set(actual["totals"]) == set(expected["totals"]) and
            all(type(actual["totals"][k]) is int and actual["totals"][k] == v
                for k, v in expected["totals"].items()), "ARTIFACT_TOTAL_MISMATCH")
    # Do not reread any path to compute the receipt. Detect ordinary file edits
    # during validation and bind the digest to exactly the parsed artifact bytes.
    for snapshot in (*inputs.values(), artifact):
        snapshot.require_unchanged()
    return {"status": "PASS", "artifact_sha256": hashlib.sha256(artifact.data).hexdigest(),
            "artifact_bytes": len(artifact.data),
            "net_krw": expected["totals"]["net_krw"],
            "vat_krw": expected["totals"]["vat_krw"],
            "gross_krw": expected["totals"]["gross_krw"],
            "independent_file_oracle": True, "governed_agent_e2e": "NOT_RUN"}


'''
    source = source[:start] + oracle + source[end:]
    compile(source, "tools/product_acceptance.py", "exec")
    return source.encode("utf-8")


def apply():
    path = ROOT / "tools/product_acceptance.py"
    path.write_bytes(patched(path.read_bytes()))


if __name__ == "__main__":
    apply()
