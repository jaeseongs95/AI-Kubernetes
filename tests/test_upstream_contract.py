"""설치물 검사기의 오류·경계 검사. 실제 upstream 실행 근거와 분리한다."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inspect_upstream", ROOT / "tools/inspect_upstream.py")
assert spec and spec.loader
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


class ArtifactBoundaryTests(unittest.TestCase):
    def test_mutated_and_missing_bytes_do_not_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "entry.mjs").write_bytes(b"original\n")
            expected = [{"path": "entry.mjs", "sha256": inspector.sha256(b"original\n")}]
            self.assertEqual(inspector.verify_files(root, expected)["status"], "PASS")
            (root / "entry.mjs").write_bytes(b"changed\n")
            self.assertEqual(inspector.verify_files(root, expected)["files"][0]["status"], "MISMATCH")
            (root / "entry.mjs").unlink()
            self.assertEqual(inspector.verify_files(root, expected)["files"][0]["status"], "MISSING")

    def test_invalid_paths_cannot_read_outside_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            for path in ("../private", "/private", "C:/private", "..\\private", "./entry", "a//b", "", 7):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    inspector.verify_files(Path(temporary), [{"path": path, "sha256": "0" * 64}])

    def test_duplicate_and_empty_manifests_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            item = {"path": "entry", "sha256": "0" * 64}
            for manifest in ([], [item, item], [{"path": "entry", "sha256": "unknown"}]):
                with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                    inspector.verify_files(Path(temporary), manifest)

    def test_symlink_is_rejected_even_when_target_bytes_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            outside = Path(temporary) / "outside"
            outside.write_bytes(b"secret")
            try:
                (root / "link").symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable on this host")
            with self.assertRaises(ValueError):
                inspector.verify_files(root, [{"path": "link", "sha256": inspector.sha256(b"secret")}])

    def test_duplicate_json_keys_cannot_rebind_contract(self):
        with self.assertRaises(ValueError):
            inspector.strict_json('{"revision": "pinned", "revision": "other"}')

    def test_child_receives_no_provider_or_user_state_configuration(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-only-sentinel", "ANTHROPIC_API_KEY": "test-only-sentinel",
            "FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT": "/user/plugin",
            "AGENT_GOVERNANCE_DB_PATH": "/user/workflows.sqlite3",
            "SKILL_REGISTRY_PATH": "/user/skills.json", "PYTHONPATH": "/user/modules",
        }):
            state = Path("/explicit/probe/state")
            env = inspector.isolated_environment(state)
            for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT", "SKILL_REGISTRY_PATH", "PYTHONPATH"):
                self.assertNotIn(name, env)
            self.assertEqual(env["AGENT_GOVERNANCE_DB_PATH"], str(state / "workflows.sqlite3"))


class ReleasedContractTests(unittest.TestCase):
    def test_each_required_route_has_explicit_qualification_evidence_and_role_capabilities(self):
        matrix = inspector.strict_json((ROOT / "contracts/upstream/support-matrix.json").read_text(encoding="utf-8"))
        required = [row for row in matrix["routes"] if row["required"]]
        self.assertEqual(len({row["route_id"] for row in required}), len(required))
        expected = {(provider, auth, role) for provider in ("openai", "anthropic")
                    for auth in ("api_key", "native_subscription")
                    for role in ("director", "developer", "reviewer")}
        self.assertEqual({(row["provider"], row["auth_kind"], row["role"]) for row in required}, expected)
        for row in required:
            with self.subTest(route_id=row["route_id"]):
                self.assertTrue(row["evidence_ref"])
                self.assertTrue(row["observed_at"])
                if row["qualification_status"] not in {"PASS", "qualified"}:
                    self.assertFalse(row["operationally_advertised"])
                if row["role"] == "developer":
                    self.assertEqual(set(row["required_tool_capabilities"]), {"read", "create", "update", "bounded_verify"})
                if row["role"] == "reviewer":
                    self.assertNotIn("create", row["required_tool_capabilities"])


if __name__ == "__main__":
    unittest.main()
