"""AIK-017 output-boundary regressions; only synthetic secrets are used."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aik_security.redaction import OutputRedactor, RedactionError, REDACTED, OMITTED, MAX_OUTPUT_BYTES


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class RedactionBoundaryTests(unittest.TestCase):
    # The first eight cases are reproduced on the pinned pre-fix source.
    def test_01_overlapping_registered_values_leave_no_secret_tail(self):
        for secrets in (["ABCDEFGHIJKL", "KLMNOPQRSTUV"], ["KLMNOPQRSTUV", "ABCDEFGHIJKL"]):
            self.assertEqual(OutputRedactor(secrets).redact_text("before ABCDEFGHIJKLMNOPQRSTUV after"),
                             "before " + REDACTED + " after")

    def test_02_known_value_is_not_broken_by_an_earlier_pattern(self):
        value = "AA Bearer CCCC ZZ"
        self.assertEqual(OutputRedactor([value]).redact_text(value), REDACTED)

    def test_03_overlapping_partial_matches_cover_their_entire_union(self):
        secret = "abcdefghijklmnopqrstuvwxyz012345"
        self.assertEqual(OutputRedactor([secret]).redact_text(secret[2:-2]), REDACTED)

    def test_04_json_diagnostic_masks_the_complete_quoted_value(self):
        value = 'dummy value with "quoted" and a newline\ninside'
        text = json.dumps({"api_key": value, "request_id": "req-1"})
        result = OutputRedactor().redact_text(text)
        for fragment in ("dummy value", "quoted", "inside"):
            self.assertNotIn(fragment, result)
        self.assertIn("req-1", result)

    def test_05_acronym_sensitive_fields_are_not_treated_as_safe(self):
        result = OutputRedactor().sanitize({"clientAPIKey": "dummy-value", "userIDToken": "dummy-token",
                                            "request_id": "req-1"})
        self.assertEqual(result["clientAPIKey"], REDACTED)
        self.assertEqual(result["userIDToken"], REDACTED)
        self.assertEqual(result["request_id"], "req-1")

    def test_06_invalid_unicode_is_omitted_not_propagated(self):
        redactor = OutputRedactor()
        self.assertEqual(redactor.redact_text("canary-\ud800"), OMITTED)
        self.assertEqual(redactor.sanitize({"message": "canary-\ud800"}), {"message": OMITTED})
        wire(redactor.output("log", {"message": "canary-\ud800"}))

    def test_07_registry_rejects_unicode_atomically_with_a_fixed_code(self):
        redactor = OutputRedactor(["existing-canary-123"])
        before = tuple(redactor._secrets)
        with self.assertRaisesRegex(RedactionError, "^INVALID_SECRET_REGISTRY$"):
            redactor.add_secret("invalid-canary-\ud800")
        self.assertEqual(tuple(redactor._secrets), before)
        self.assertEqual(redactor.redact_text("existing-canary-123"), REDACTED)

    def test_08_size_limit_includes_the_outer_output_envelope(self):
        result = OutputRedactor().output("export", "x" * (MAX_OUTPUT_BYTES - 2))
        self.assertLessEqual(len(wire(result)), MAX_OUTPUT_BYTES)
        self.assertEqual(result["body"], OMITTED)

    def test_duplicate_registration_at_capacity_is_idempotent(self):
        secrets = [f"synthetic-{index:02d}-credential" for index in range(16)]
        redactor = OutputRedactor(secrets)
        redactor.add_secret(secrets[0])
        self.assertEqual(len(redactor._secrets), 16)
        with self.assertRaisesRegex(RedactionError, "^INVALID_SECRET_REGISTRY$"):
            redactor.add_secret("one-more-credential")
        self.assertEqual(redactor.redact_text(secrets[-1]), REDACTED)

    def test_mixed_case_percent_encoding_and_unpadded_base64(self):
        secret = "AbcdEf12/gh?Jk+L"
        escaped = quote(secret, safe="")
        mixed = re.sub(r"%[0-9A-F]{2}", lambda m: m[0].lower(), escaped)
        encoded = base64.b64encode(secret.encode()).decode().rstrip("=")
        for text in (escaped, mixed, encoded):
            self.assertEqual(OutputRedactor([secret]).redact_text(text), REDACTED)

    def test_quoted_assignments_handle_escapes_empty_and_unterminated_values(self):
        for text, fragments in (
            ('password="two words" next=ok', ["two words"]),
            ("clientSecret='two words' next=ok", ["two words"]),
            ('{"Authorization":"Basic dXNlcjpkdW1teQ=="}', ["dXNlcjpkdW1teQ=="]),
            ('api_key="unterminated dummy', ["unterminated dummy"]),
            ('{"access_token":""}', ['"access_token"']),
        ):
            with self.subTest(sample=text[:12]):
                result = OutputRedactor().redact_text(text)
                for fragment in fragments:
                    self.assertNotIn(fragment, result)

    def test_reserved_omission_metadata_cannot_be_overwritten_by_input(self):
        result = OutputRedactor().sanitize({"unsafe/key": "discard", "_unsafe_field_omitted": False})
        self.assertIs(result["_unsafe_field_omitted"], True)

    def test_total_node_and_text_budgets_omit_an_oversized_tree(self):
        redactor = OutputRedactor()
        self.assertEqual(redactor.sanitize([[0] * 120 for _ in range(10)]), OMITTED)
        self.assertEqual(redactor.sanitize(["x" * 40000, "y" * 40000]), OMITTED)
        self.assertLessEqual(len(wire(redactor.output("log", [[0] * 120 for _ in range(10)]))), MAX_OUTPUT_BYTES)

    def test_many_overlapping_matches_are_bounded_and_omitted(self):
        self.assertEqual(OutputRedactor(["a" * 12]).redact_text("a" * 65536), OMITTED)

    def test_invalid_or_oversized_prompt_preview_never_hashes_or_echoes_input(self):
        for value in ("dummy-\ud800", "x" * 65537):
            result = OutputRedactor().output("prompt_preview", value)
            self.assertEqual(result["body"], OMITTED)
            wire(result)
        with self.assertRaisesRegex(RedactionError, "^INVALID_TEXT$"):
            OutputRedactor().output("prompt_preview", [])

    def test_valid_unicode_preview_keeps_original_fingerprint(self):
        original = "원문 보존 🔒 synthetic-credential"
        result = OutputRedactor().output("prompt_preview", original)
        self.assertEqual(result["body"], {"content": REDACTED, "bytes": len(original.encode()),
                                           "sha256": hashlib.sha256(original.encode()).hexdigest()})
        self.assertEqual(original, "원문 보존 🔒 synthetic-credential")

    def test_all_output_kinds_preserve_safe_trace_ids_and_do_not_mutate_inputs(self):
        secrets = ["ABCDEFGHIJKL", "KLMNOPQRSTUV"]
        source = {"connection_id": "conn-1", "task_id": "task-1", "request_id": "req-1",
                  "detail": "ABCDEFGHIJKLMNOPQRSTUV", "nested": [{"userAPIKey": "unknown-canary"}]}
        before = copy.deepcopy(source)
        for kind in ("ipc_response", "peer", "evidence", "log", "crash_bundle", "export"):
            result = OutputRedactor(secrets).output(kind, source)
            self.assertEqual(result["body"]["detail"], REDACTED)
            self.assertEqual(result["body"]["nested"][0]["userAPIKey"], REDACTED)
            for key in ("connection_id", "task_id", "request_id"):
                self.assertEqual(result["body"][key], source[key])
            self.assertLessEqual(len(wire(result)), MAX_OUTPUT_BYTES)
        self.assertEqual(source, before)

    def test_cycles_and_unsupported_objects_do_not_execute_serializers(self):
        class Hostile:
            def __str__(self):
                raise AssertionError("must not stringify")
            def __repr__(self):
                raise AssertionError("must not repr")
            def __iter__(self):
                raise AssertionError("must not iterate")
        cycle = []; cycle.append(cycle)
        result = OutputRedactor().sanitize({"cycle": cycle, "object": Hostile()})
        self.assertIn(OMITTED, json.dumps(result))
        self.assertEqual(result["object"], "[UNSUPPORTED_OMITTED]")

    def test_matching_does_not_reinterpret_generated_mask_markers(self):
        # A registered literal must not be searched inside a marker generated by another rule.
        result = OutputRedactor(["URL_REDACTED"]).redact_text("https://example.invalid/path")
        self.assertEqual(result, "[URL_REDACTED]")

    def test_ordinary_output_is_not_withheld_and_under_limit_stays_intact(self):
        value = {"state": "READY", "request_id": "r-22", "values": [True, None, 1, 2.5], "한글": "ignored-key"}
        result = OutputRedactor().sanitize(value)
        self.assertEqual(result["values"], [True, None, 1, 2.5])
        self.assertEqual(result["request_id"], "r-22")
        self.assertEqual(OutputRedactor().output("log", "x" * 1000)["body"], "x" * 1000)

    def test_generated_overlap_cases_have_no_unmasked_prefix_or_suffix(self):
        text = "abcdefghijklmnopqrstuvwxyz0123456789"
        for offset in range(1, 12):
            left, right = text[:16], text[offset:offset + 16]
            with self.subTest(offset=offset):
                self.assertEqual(OutputRedactor([left, right]).redact_text(text[:offset + 16]), REDACTED)


if __name__ == "__main__":
    unittest.main()
