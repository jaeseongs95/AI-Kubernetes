"""AIK-017 synthetic canary tests, no live credentials or provider calls."""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from aik_security.redaction import (
    MAX_TEXT_BYTES, OMITTED, REDACTED, OutputRedactor, RedactionError,
)

CANARY = "SYNTHETIC_aB9C8dE7fG6hJ5kL4mN3pQ2r"
SECOND = "sk-ant-api03-SYNTHETIC0123456789abcdef"
ID = "request_1257"


class SecretOutputTests(unittest.TestCase):
    def setUp(self):
        self.redactor = OutputRedactor([CANARY, SECOND])

    def assertClean(self, payload):
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        for value in (CANARY, SECOND, CANARY[8:20],
                      base64.b64encode(CANARY.encode()).decode(), quote(CANARY, safe="")):
            self.assertNotIn(value, text)

    def test_raw_and_partial_key_are_removed(self):
        data = f"value {CANARY}, shortened {CANARY[8:20]} and again {CANARY}"
        out = self.redactor.redact_text(data)
        self.assertNotIn(CANARY, out)
        self.assertNotIn(CANARY[8:20], out)
        self.assertIn(REDACTED, out)

    def test_encoded_known_credentials_are_removed(self):
        raw = CANARY.encode()
        for value in [base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(),
                      raw.hex(), quote(CANARY, safe="")]:
            with self.subTest(value=value[:12]):
                self.assertEqual(self.redactor.redact_text(value), REDACTED)

    def test_provider_shaped_unknown_tokens_do_not_pass_through(self):
        for token in ("sk-test-abcDEF0123456789abcdef", "ghp_123456789012345678901",
                      "github_pat_12345678901234567890", "AIza" + "A" * 21):
            with self.subTest(token=token[:7]):
                self.assertEqual(self.redactor.redact_text(token), REDACTED)

    def test_header_cookie_and_bearer_lines(self):
        fields = [
            "Authorization: Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "Proxy-Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
            "Cookie: sid=secretvalue; other=ok",
            "Set-Cookie: sid=abc",
            "X-Api-Key: long-example-auth-token",
            "Bearer ABCDEFGHIJKLMN"
        ]
        for line in fields:
            with self.subTest(line=line.split(":")[0]):
                self.assertNotIn(line, self.redactor.redact_text(line))
        self.assertIn("[HEADER_REDACTED]", self.redactor.redact_text(fields[0]))

    def test_url_paths_userinfo_query_and_fragments_are_not_emitted(self):
        url = f"https://user:{CANARY}@vendor.test/p/{SECOND}?api_key={CANARY}&access_token=unknown#fragment"
        sanitized = self.redactor.redact_text(f"request to {url} failed")
        self.assertNotIn("vendor.test", sanitized)
        self.assertNotIn("fragment", sanitized)
        self.assertNotIn(CANARY, sanitized)
        self.assertIn("[URL_REDACTED]", sanitized)

    def test_sensitive_assignments_in_plain_logs_are_removed(self):
        sample = "apiKey=SYNTHETIC123 password=samplePass clientSecret=longValue access_token=abcdef"
        out = self.redactor.redact_text(sample)
        for secret in ("SYNTHETIC123", "samplePass", "longValue", "abcdef"):
            self.assertNotIn(secret, out)

    def test_nested_exceptions_and_camelcase_sensitive_fields(self):
        payload = {"state": "FAILED", "error": {
            "details": f"unexpected {CANARY}",
            "context": [
                {"apiKey": "plain-secret-1", "clientSecret": "plain-secret-2",
                 "authorization": "Bearer unknown", "connection_id": "connection-7"},
                {"nested": {"password": CANARY, "privateKey": "plain-secret-3"}},
            ]
        }}
        result = self.redactor.output("ipc_response", payload)
        self.assertClean(result)
        self.assertEqual(result["body"]["error"]["context"][0]["apiKey"], REDACTED)
        self.assertEqual(result["body"]["error"]["context"][0]["clientSecret"], REDACTED)
        self.assertEqual(result["body"]["error"]["context"][1]["nested"]["privateKey"], REDACTED)
        self.assertEqual(result["body"]["error"]["context"][0]["connection_id"], "connection-7")

    def test_each_output_boundary_removes_synthetic_secrets(self):
        diagnostic = {
            "request_id": ID, "task_id": "task_19", "connection_id": "connection_8",
            "traceback": "synthetic " + CANARY,
            "headers": {"Authorization": "Bearer " + SECOND},
            "url": f"https://host.test/api?key={CANARY}",
            "debug": ["tail " + CANARY[8:20], {"refreshToken": "some-other-secret"}],
            "binary": CANARY.encode(),
        }
        for kind in ("ipc_response", "peer", "evidence", "log", "crash_bundle", "export"):
            with self.subTest(kind=kind):
                result = self.redactor.output(kind, diagnostic)
                self.assertClean(result)
                self.assertEqual(result["body"]["request_id"], ID)
                self.assertEqual(result["body"]["task_id"], "task_19")
                self.assertEqual(result["body"]["connection_id"], "connection_8")
                self.assertEqual(result["body"]["binary"], "[BINARY_OMITTED]")

    def test_raw_prompt_is_preserved_for_execution_but_never_used_as_preview(self):
        raw = "원문 유지: " + CANARY + " secret on purpose"
        original = raw
        report = self.redactor.output("prompt_preview", raw)
        self.assertEqual(raw, original)
        self.assertEqual(report["body"]["content"], REDACTED)
        self.assertEqual(report["body"]["bytes"], len(raw.encode("utf-8")))
        self.assertClean(report)

    def test_error_envelope_tracks_ids_without_echoing_exception(self):
        result = self.redactor.sanitized_error(
            "RATE_LIMITED", request_id=ID, task_id="task-52", connection_id="connection-21",
            exception=RuntimeError("sensitive:" + CANARY),
        )
        self.assertEqual(result["error"]["code"], "RATE_LIMITED")
        self.assertEqual(result["request_id"], ID)
        self.assertEqual(result["connection_id"], "connection-21")
        self.assertEqual(result["task_id"], "task-52")
        self.assertClean(result)
        unknown = self.redactor.sanitized_error(
            "FORGED_SECRET_" + CANARY, request_id="https://host.test/" + CANARY,
            task_id=SECOND, connection_id=CANARY,
        )
        self.assertEqual(unknown["error"]["code"], "INTERNAL_ERROR")
        self.assertIsNone(unknown["request_id"])
        self.assertIsNone(unknown["connection_id"])
        self.assertIsNone(unknown["task_id"])
        self.assertClean(unknown)

    def test_secret_registry_refuses_short_and_oversized_values(self):
        for invalid in ("short", "", CANARY * 100, 17, b"not-strings"):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaisesRegex(RedactionError, "INVALID_SECRET_REGISTRY"):
                    OutputRedactor([invalid])
        with self.assertRaisesRegex(RedactionError, "INVALID_SECRET_REGISTRY"):
            OutputRedactor([CANARY] * 17)

    def test_unicode_oversize_is_omitted_wholly(self):
        self.assertEqual(self.redactor.redact_text("가" * (MAX_TEXT_BYTES // 3 + 1)), OMITTED)

    def test_malformed_or_untrusted_python_objects_are_never_stringified(self):
        class Bad:
            def __str__(self):
                raise AssertionError("Secret-bearing object stringification is forbidden")
            def __repr__(self):
                raise AssertionError("Secret-bearing object repr is forbidden")
        out = self.redactor.output("log", {"foreign": Bad(), "exception": Bad()})
        self.assertEqual(out["body"]["foreign"], "[UNSUPPORTED_OMITTED]")
        self.assertEqual(out["body"]["exception"], "[UNSUPPORTED_OMITTED]")

    def test_depth_item_limits_and_nonfinite_values_are_fail_closed(self):
        huge = {"values": list(range(150)), "floating": float("nan"),
                "unexpected": object(), "map": {"long": None}}
        result = self.redactor.output("export", huge)["body"]
        self.assertEqual(len(result["values"]), 129)
        self.assertEqual(result["values"][-1], OMITTED)
        self.assertEqual(result["floating"], OMITTED)
        nested = {"a": 1}
        for _ in range(10):
            nested = {"nested": nested}
        self.assertIn(OMITTED, json.dumps(self.redactor.sanitize(nested)))

    def test_secret_like_field_name_and_malformed_key_is_not_reflected(self):
        payload = {"safe_id": "request-55", "secret_token": CANARY,
                   CANARY + "/unsafe": "malicious", "device_access_token": "clear me"}
        result = self.redactor.sanitize(payload)
        self.assertEqual(result["safe_id"], "request-55")
        self.assertEqual(result["secret_token"], REDACTED)
        self.assertEqual(result["device_access_token"], REDACTED)
        self.assertNotIn(CANARY, json.dumps(result))

    def test_secret_in_key_and_hint_field_are_not_exported(self):
        output = self.redactor.output("export", {
            CANARY: "secret-key-name", "clientSecretHint": "unknown-credential-value",
            "request_id": "request-18",
        })
        self.assertClean(output)
        self.assertNotIn("secret-key-name", json.dumps(output))
        self.assertEqual(output["body"]["clientSecretHint"], REDACTED)
        self.assertEqual(output["body"]["request_id"], "request-18")

    def test_no_generic_unvalidated_kind_or_bad_prompt_value(self):
        with self.assertRaisesRegex(RedactionError, "UNSUPPORTED_OUTPUT_KIND"):
            self.redactor.output("native_shell", {"secret": CANARY})
        with self.assertRaisesRegex(RedactionError, "INVALID_TEXT"):
            self.redactor.output("prompt_preview", {"text": CANARY})


if __name__ == "__main__":
    unittest.main()
