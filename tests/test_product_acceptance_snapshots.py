"""AIK-083 file-evidence regressions; synthetic local files, no model calls."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import product_acceptance as acceptance
import acceptance_io as file_io

GOOD = {
    "rows": [
        {"order_id": "A-001", "net_krw": 10000, "vat_krw": 1000, "gross_krw": 11000},
        {"order_id": "B-002", "net_krw": 20005, "vat_krw": 2001, "gross_krw": 22006},
        {"order_id": "C-003", "net_krw": 11505, "vat_krw": 1151, "gross_krw": 12656},
    ],
    "totals": {"net_krw": 41510, "vat_krw": 4152, "gross_krw": 45662},
}


class AcceptanceSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aik083-snapshot-")
        self.addCleanup(temporary.cleanup)
        # The oracle resolves workspace paths. Match that identity on Windows
        # too, where TEMP may contain an 8.3 alias such as RUNNER~1.
        self.workspace = Path(temporary.name).resolve() / "한글 공백 프로젝트"
        shutil.copytree(ROOT / "contracts/product-acceptance/fixture/project", self.workspace)
        self.artifact = self.workspace / "settlement.json"
        self.write_artifact(GOOD)

    def write_artifact(self, value):
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.artifact.write_bytes(raw)
        return raw

    # Run these eight cases against the exact pinned old implementation too.
    def test_01_json_object_key_order_does_not_change_the_verdict(self):
        self.write_artifact({"totals": GOOD["totals"], "rows": GOOD["rows"]})
        self.assertEqual(acceptance.check_oracle(self.workspace)["status"], "PASS")

    def test_02_receipt_does_not_hash_a_later_unvalidated_path_read(self):
        good_bytes = self.artifact.read_bytes()
        original = acceptance.load_json
        def replacing_loader(path):
            result = original(path)
            if path == self.artifact:
                path.write_bytes(b'{"tampered":true}')
            return result
        with patch.object(acceptance, "load_json", side_effect=replacing_loader):
            receipt = acceptance.check_oracle(self.workspace)
        self.assertEqual(receipt["artifact_sha256"], hashlib.sha256(good_bytes).hexdigest())

    def test_03_fixture_hash_check_cannot_be_followed_by_unchecked_input_reread(self):
        malicious = copy.deepcopy(GOOD)
        malicious["rows"][0].update(net_krw=9000, vat_krw=900, gross_krw=9900)
        malicious["totals"].update(net_krw=40510, vat_krw=4052, gross_krw=44562)
        self.write_artifact(malicious)
        original = acceptance.expected_settlement
        def replacing_calculation(folder):
            path = folder / "orders.csv"
            path.write_bytes(path.read_bytes().replace(b"10000", b"9000"))
            return original(folder)
        with patch.object(acceptance, "expected_settlement", side_effect=replacing_calculation):
            with self.assertRaises(acceptance.ContractError):
                acceptance.check_oracle(self.workspace)

    def test_04_oversized_file_is_rejected_before_unbounded_read_bytes(self):
        huge = self.workspace / "huge.json"
        with huge.open("wb") as stream:
            stream.truncate(acceptance.MAX_JSON_BYTES + 1)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            with self.assertRaisesRegex(acceptance.ContractError, "OVERSIZED_JSON"):
                acceptance.load_json(huge)

    def test_05_finite_number_overflow_is_rejected(self):
        self.artifact.write_bytes(b'{"number":1e309}')
        with self.assertRaises(acceptance.ContractError):
            acceptance.load_json(self.artifact)

    def test_06_excessive_json_nesting_is_rejected(self):
        self.artifact.write_bytes(b'{"value":' + b'[' * 200 + b'0' + b']' * 200 + b'}')
        with self.assertRaises(acceptance.ContractError):
            acceptance.load_json(self.artifact)

    def test_07_escaped_lone_surrogates_are_rejected(self):
        self.artifact.write_bytes(b'{"message":"\\ud800"}')
        with self.assertRaises(acceptance.ContractError):
            acceptance.load_json(self.artifact)

    def test_08_nonregular_input_returns_a_fixed_contract_error(self):
        with self.assertRaises(acceptance.ContractError):
            acceptance.load_json(self.workspace)

    def test_receipt_size_and_digest_match_exact_original_serialization(self):
        raw = self.write_artifact({"totals": GOOD["totals"], "rows": GOOD["rows"]})
        receipt = acceptance.check_oracle(self.workspace)
        self.assertEqual(receipt["artifact_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt["artifact_bytes"], len(raw))
        self.assertEqual(receipt["governed_agent_e2e"], "NOT_RUN")

    def test_each_workspace_file_is_opened_only_once(self):
        original = file_io.os.open
        calls = []
        def counting(path, flags, *args, **kwargs):
            calls.append(Path(path))
            return original(path, flags, *args, **kwargs)
        with patch.object(file_io.os, "open", side_effect=counting):
            self.assertEqual(acceptance.check_oracle(self.workspace)["status"], "PASS")
        for name in ("orders.csv", "policy.json", "settlement.json"):
            self.assertEqual(calls.count(self.workspace / name), 1)

    def test_artifact_change_after_parsing_is_detected(self):
        original, raw = acceptance.parse_json, self.artifact.read_bytes()
        def editing(data, **kwargs):
            result = original(data, **kwargs)
            if data == raw:
                self.artifact.write_bytes(b'{"changed":true}')
            return result
        with patch.object(acceptance, "parse_json", side_effect=editing):
            with self.assertRaisesRegex(acceptance.ContractError, "FILE_CHANGED_DURING_CHECK"):
                acceptance.check_oracle(self.workspace)

    def test_input_change_during_calculation_is_detected(self):
        original = acceptance._expected_settlement_from_bytes
        def editing(raw, policy):
            result = original(raw, policy)
            path = self.workspace / "orders.csv"
            path.write_bytes(b"changed input")
            return result
        with patch.object(acceptance, "_expected_settlement_from_bytes", side_effect=editing):
            with self.assertRaisesRegex(acceptance.ContractError, "FILE_CHANGED_DURING_CHECK"):
                acceptance.check_oracle(self.workspace)

    def test_replace_between_lstat_and_open_is_detected_before_read(self):
        original = file_io.os.open
        def replacing(path, flags, *args, **kwargs):
            replacement = self.workspace / "replacement.json"
            replacement.write_bytes(b'{"replacement":true}')
            os.replace(replacement, path)
            return original(path, flags, *args, **kwargs)
        with patch.object(file_io.os, "open", side_effect=replacing):
            with self.assertRaisesRegex(acceptance.ContractError, "FILE_CHANGED_DURING_CHECK"):
                file_io.read_snapshot(self.artifact)

    def test_mutation_during_descriptor_read_is_detected(self):
        original, changed = file_io.os.read, False
        def changing(fd, count):
            nonlocal changed
            raw = original(fd, count)
            if not changed:
                changed = True
                self.artifact.write_bytes(b'{"changed":true}')
            return raw
        with patch.object(file_io.os, "read", side_effect=changing):
            with self.assertRaises(acceptance.ContractError):
                file_io.read_snapshot(self.artifact)
        self.assertTrue(changed)

    def test_snapshot_detects_later_deletion(self):
        captured = file_io.read_snapshot(self.artifact)
        self.artifact.unlink()
        with self.assertRaisesRegex(acceptance.ContractError, "FILE_CHANGED_DURING_CHECK"):
            captured.require_unchanged()
        self.assertIsInstance(captured.data, bytes)

    def test_symlink_and_windows_reparse_shapes_rejected_before_open(self):
        info = self.artifact.stat()
        class LinkInfo:
            st_mode = stat.S_IFLNK | 0o777
            st_file_attributes = 0
        class ReparseInfo:
            st_mode = info.st_mode
            st_file_attributes = 0x400
        for observed in (LinkInfo(), ReparseInfo()):
            with patch.object(Path, "lstat", return_value=observed), patch.object(file_io.os, "open") as opened:
                with self.assertRaisesRegex(acceptance.ContractError, "FILE_NOT_REGULAR"):
                    file_io.read_snapshot(self.artifact)
                opened.assert_not_called()

    def test_nested_duplicate_keys_and_invalid_numbers_remain_rejected(self):
        for raw in (b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-1e309}'):
            with self.subTest(raw=raw):
                self.artifact.write_bytes(raw)
                with self.assertRaises(acceptance.ContractError):
                    acceptance.load_json(self.artifact)

    def test_strict_money_schema_row_order_and_extra_fields_are_preserved(self):
        candidates = []
        for value in (True, 1000.0, "1000"):
            data = copy.deepcopy(GOOD); data["rows"][0]["vat_krw"] = value; candidates.append(data)
        data = copy.deepcopy(GOOD); data["rows"].reverse(); candidates.append(data)
        data = copy.deepcopy(GOOD); data["secret"] = "synthetic"; candidates.append(data)
        data = copy.deepcopy(GOOD); data["totals"]["net_krw"] += 1; candidates.append(data)
        for value in candidates:
            self.write_artifact(value)
            with self.assertRaises(acceptance.ContractError):
                acceptance.check_oracle(self.workspace)

    def test_invalid_encoding_and_nonobject_json_return_fixed_errors(self):
        for raw in (b'\xff', b'[]', b'null', b'{"x":'):
            self.artifact.write_bytes(raw)
            with self.assertRaises(acceptance.ContractError) as error:
                acceptance.load_json(self.artifact)
            self.assertNotIn("settlement", str(error.exception))

    def test_custom_size_limit_is_enforced_without_full_allocation(self):
        with self.assertRaisesRegex(acceptance.ContractError, "OVERSIZED_JSON"):
            file_io.read_snapshot(self.artifact, maximum=1)
        for limit in (0, True, -1, 1048577):
            with self.assertRaisesRegex(acceptance.ContractError, "INVALID_READ_LIMIT"):
                file_io.read_snapshot(self.artifact, maximum=limit)

    def test_descriptors_close_on_success_and_read_failure(self):
        original_close = file_io.os.close
        with patch.object(file_io.os, "close", wraps=original_close) as closed:
            file_io.read_snapshot(self.artifact)
            self.assertEqual(closed.call_count, 1)
        with patch.object(file_io.os, "read", side_effect=OSError("synthetic-private-detail")), \
                patch.object(file_io.os, "close", wraps=original_close) as closed:
            with self.assertRaisesRegex(acceptance.ContractError, "^FILE_READ_FAILED$"):
                file_io.read_snapshot(self.artifact)
            self.assertEqual(closed.call_count, 1)

    def test_json_node_budget_is_enforced(self):
        raw = json.dumps({"values": [0] * 10001}).encode()
        with self.assertRaisesRegex(acceptance.ContractError, "JSON_COMPLEXITY_LIMIT"):
            file_io.parse_json(raw)
        self.assertEqual(file_io.parse_json(b'{"values":[0,1,2]}'), {"values": [0, 1, 2]})

    def test_cli_unicode_workspace_reports_bytes_and_safe_invalid_json(self):
        command = [sys.executable, str(ROOT / "tools/product_acceptance.py"), "oracle", "--workspace", str(self.workspace)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["artifact_bytes"], self.artifact.stat().st_size)
        self.artifact.write_bytes(b'{"secret":"synthetic-private-detail","n":1e309}')
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "FAIL")
        self.assertNotIn("synthetic-private-detail", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_success_does_not_modify_inputs_or_artifacts(self):
        files = [self.workspace / name for name in ("orders.csv", "policy.json", "settlement.py", "settlement.json")]
        before = [path.read_bytes() for path in files]
        acceptance.check_oracle(self.workspace)
        self.assertEqual([path.read_bytes() for path in files], before)


if __name__ == "__main__":
    unittest.main()
