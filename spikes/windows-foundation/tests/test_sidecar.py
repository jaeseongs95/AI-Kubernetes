import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
sys.path.insert(0, str(PYTHON_ROOT))
from aik_spike.server import MAX_FRAME, SAMPLE, ProtocolError, dispatch, parse_frame

NONCE = "a" * 64


def request(command="health"):
    return {"protocol_version": 1, "request_id": str(uuid.uuid4()), "command": command,
            "workspace_id": "foundation-spike", "payload": {}, "capability": NONCE}


class ProtocolTests(unittest.TestCase):
    def test_rejects_scope_version_command_and_payload(self):
        cases = [
            ("capability", "b" * 64, "UNAUTHORIZED"),
            ("capability", "한글-secret", "UNAUTHORIZED"),
            ("protocol_version", 2, "INCOMPATIBLE_PROTOCOL"),
            ("protocol_version", True, "INCOMPATIBLE_PROTOCOL"),
            ("workspace_id", "other", "UNKNOWN_WORKSPACE"),
            ("command", "shell", "UNSUPPORTED_COMMAND"),
            ("payload", {"command": "touch /tmp/injected"}, "INVALID_REQUEST"),
            ("request_id", "../../secret", "INVALID_REQUEST"),
        ]
        for key, value, code in cases:
            with self.subTest(key=key, value=value):
                value_request = request()
                value_request[key] = value
                response = dispatch(value_request, NONCE)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], code)
                self.assertNotIn("injected", json.dumps(response))
                self.assertNotIn(NONCE, json.dumps(response))

    def test_rejects_extra_fields(self):
        value = request()
        value["secret"] = "never-echo-this"
        result = dispatch(value, NONCE)
        self.assertEqual(result["error"]["code"], "INVALID_REQUEST")
        self.assertNotIn("never-echo-this", json.dumps(result))

    def test_frame_rejects_duplicate_keys_and_oversize(self):
        for raw in [b'{"x":1,"x":2}', b'[]', b'\xff', b" " * (MAX_FRAME + 1), b'{"x":' + b"1" * 5000 + b"}"]:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ProtocolError):
                    parse_frame(raw)

    def test_dispatch_does_not_mutate_input(self):
        value = request()
        before = copy.deepcopy(value)
        self.assertTrue(dispatch(value, NONCE)["ok"])
        self.assertEqual(value, before)

    def test_real_worker_writes_verifies_and_removes_temporary_file(self):
        response = dispatch(request("worker_probe"), NONCE)
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertNotEqual(result["pid"], os.getpid())
        self.assertEqual(result["parent_pid"], os.getpid())
        self.assertEqual(result["artifact_sha256"], hashlib.sha256(SAMPLE).hexdigest())
        self.assertEqual(result["artifact_bytes"], len(SAMPLE))
        self.assertTrue(result["temporary_artifact_removed"])

    def test_real_pipe_bootstrap_health_and_worker(self):
        values = [
            {"kind": "bootstrap", "protocol_version": 1, "nonce": NONCE},
            request(),
            request("worker_probe"),
        ]
        result = subprocess.run(
            [sys.executable, "-I", str(PYTHON_ROOT / "sidecar_entry.py")],
            input="".join(json.dumps(value) + "\n" for value in values),
            capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["kind"], "ready")
        self.assertEqual(lines[1]["result"]["pid"], lines[0]["pid"])
        self.assertEqual(lines[2]["result"]["parent_pid"], lines[0]["pid"])
        self.assertNotIn(NONCE, result.stdout)

    def test_invalid_bootstrap_exits_without_effect(self):
        result = subprocess.run(
            [sys.executable, "-I", str(PYTHON_ROOT / "sidecar_entry.py")],
            input='{"kind":"bootstrap","protocol_version":1,"nonce":"wrong"}\n',
            capture_output=True, text=True, timeout=5
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    @unittest.skipIf(sys.platform == "win32", "Non-Windows refusal only")
    def test_non_windows_vault_is_not_run(self):
        response = dispatch(request("vault_probe"), NONCE)
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["status"], "NOT_RUN")
        self.assertEqual(response["result"]["code"], "WINDOWS_REQUIRED")


if __name__ == "__main__":
    unittest.main()
