"""AIK-022 synthetic auth/vault failure-path probe on the product modules.

The fake-vault run exercises every case on any OS. The real run uses the
product Windows user DPAPI vault and only runs on Windows; elsewhere the
probe must report NOT_RUN without touching storage.
"""

import json
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(1, str(ROOT / "src"))

from aik_spike.auth_vault import BACKEND, CASES, EXPECTED, probe  # noqa: E402
from aik_spike.server import dispatch  # noqa: E402
from aik_vault import FakeVaultBackend  # noqa: E402

NONCE = "a" * 64


def request(command):
    return {"protocol_version": 1, "request_id": str(uuid.uuid4()), "command": command,
            "workspace_id": "foundation-spike", "payload": {}, "capability": NONCE}


def fake_factory(directory, timeout):
    return FakeVaultBackend(directory, key=bytes(range(32)), timeout=timeout)


class AuthVaultProbeTests(unittest.TestCase):
    def test_every_case_hits_its_expected_code_with_the_fake_vault(self):
        result = probe(vault_factory=fake_factory)
        self.assertEqual(set(result), {"status", "backend", "code", "cases"})
        self.assertEqual(result["backend"], BACKEND)
        self.assertEqual(tuple(result["cases"]), CASES)
        self.assertEqual(result["cases"], EXPECTED)
        self.assertEqual((result["status"], result["code"]), ("PASS", "AUTH_VAULT_FAILURE_PATHS_OK"))
        # Cross-user DPAPI rejection is declared NOT_RUN, never claimed.
        self.assertEqual(result["cases"]["other_user_profile"], "NOT_RUN_SINGLE_USER_SESSION")

    def test_output_holds_only_fixed_codes(self):
        serialized = json.dumps(probe(vault_factory=fake_factory))
        self.assertNotIn("aik-synthetic-not-a-key-", serialized)
        self.assertNotIn("secret://", serialized)

    def test_internal_failure_is_a_fail_with_a_fixed_code(self):
        def broken(directory, timeout):
            raise OSError("synthetic storage failure")

        result = probe(vault_factory=broken)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["code"], "AUTH_VAULT_OSERROR")
        self.assertEqual(set(result["cases"].values()), {"NOT_REACHED"})

    @unittest.skipIf(sys.platform == "win32", "Non-Windows refusal only")
    def test_non_windows_real_probe_is_not_run(self):
        self.assertEqual(probe(), {"status": "NOT_RUN", "backend": BACKEND, "code": "WINDOWS_REQUIRED", "cases": {}})
        response = dispatch(request("auth_vault_probe"), NONCE)
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["status"], "NOT_RUN")

    @unittest.skipUnless(sys.platform == "win32", "Real Windows user DPAPI is NOT_RUN outside Windows.")
    def test_real_windows_dpapi_failure_paths(self):
        response = dispatch(request("auth_vault_probe"), NONCE)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["cases"], EXPECTED)
        self.assertEqual(response["result"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
