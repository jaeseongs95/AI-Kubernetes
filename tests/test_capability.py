"""AIK-020 capability inventory tests with synthetic model names only.

No provider is contacted and no credential value is used. Linux results are
development verification, not Windows support evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_capability import (  # noqa: E402
    CapabilityError, CapabilityInventory, ModelCapability, Provenance,
    UnknownResource, ValidationResult,
)
from aik_pool import Freshness  # noqa: E402
from aik_provider import AuthKind, ConnectionBinding  # noqa: E402


ALPHA = "synthetic-model-alpha"
BETA = "synthetic-model-beta"
GAMMA = "synthetic-model-gamma"


def binding(connection: str = "conn-a", revision: int = 1, fingerprint: str = "a") -> ConnectionBinding:
    return ConnectionBinding("synthetic", "synthetic-host", connection, revision, AuthKind.API_KEY,
                             f"secret://local/{connection}-ref", fingerprint * 64)


def listing(*entries) -> str:
    return json.dumps({"models": [
        {"model": model, "efforts": list(efforts), "tools": list(tools)}
        for model, efforts, tools in entries
    ]})


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.inventory = CapabilityInventory(ttl_seconds=60, clock=self.clock)
        self.inventory.register_resource(binding("conn-a"))
        self.inventory.register_resource(binding("conn-b", fingerprint="b"))

    def observe(self, connection: str, *entries):
        return self.inventory.load_list(connection, Provenance.PROVIDER_OBSERVED, listing(*entries))

    def test_raw_order_and_digest_are_preserved(self) -> None:
        raw = listing((BETA, ("medium", "low"), ("shell",)), (ALPHA, ("high",), ()))
        snapshot = self.inventory.load_list("conn-a", Provenance.PROVIDER_OBSERVED, raw)
        self.assertEqual(snapshot.digest, hashlib.sha256(raw.encode("utf-8")).hexdigest())
        self.assertEqual(snapshot.models, (BETA, ALPHA))
        self.assertEqual(snapshot.entries[0].efforts, ("medium", "low"))
        self.assertTrue(all(entry.provenance is Provenance.PROVIDER_OBSERVED for entry in snapshot.entries))
        self.assertTrue(all(entry.connection_id == "conn-a" for entry in snapshot.entries))

    def test_invalid_raw_lists_are_rejected_before_typed_conversion(self) -> None:
        cases = (
            (listing((ALPHA, (), ()), (ALPHA, (), ())), "DUPLICATE_MODEL"),
            (listing(("Synthetic-Model-Alpha", (), ()), (ALPHA, (), ())), "DUPLICATE_MODEL"),
            (listing((ALPHA, ("high", "high"), ())), "DUPLICATE_EFFORT"),
            (listing((ALPHA, ("high", "HIGH"), ())), "DUPLICATE_EFFORT"),
            (listing((ALPHA, (), ("shell", "shell"))), "DUPLICATE_TOOL"),
            ('{"models":[{"model":null,"efforts":[],"tools":[]}]}', "NULL_VALUE"),
            ('{"models":[{"model":"m-1","efforts":[null],"tools":[]}]}', "NULL_VALUE"),
            ('{"models":null}', "NULL_VALUE"),
            (listing(("", (), ())), "EMPTY_VALUE"),
            (listing(("   ", (), ())), "EMPTY_VALUE"),
            (listing((ALPHA, ("",), ())), "EMPTY_VALUE"),
            ('{"models":[],"models":[]}', "DUPLICATE_KEY"),
            ('{"models":[{"model":"m-1","efforts":[],"tools":[],"x":NaN}]}', "INVALID_JSON_CONSTANT"),
            ('{"models":[{"model":"m-1","efforts":[],"tools":[],"extra":"v"}]}', "INVALID_ENTRY"),
            ('{"models":[{"model":"m-1","efforts":[]}]}', "INVALID_ENTRY"),
            ("[]", "INVALID_DOCUMENT"),
            ('{"models":[{"model":7,"efforts":[],"tools":[]}]}', "INVALID_MODEL"),
            (listing(("has space", (), ())), "INVALID_MODEL"),
            ("not json", "INVALID_JSON"),
            (b"\xff\xfe", "INVALID_ENCODING"),
        )
        before = self.inventory.resource_revision("conn-a")
        for raw, code in cases:
            with self.subTest(code=code, raw=raw):
                with self.assertRaises(CapabilityError) as caught:
                    self.inventory.load_list("conn-a", Provenance.PROVIDER_OBSERVED, raw)
                self.assertEqual(str(caught.exception), code)
                self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.PROVIDER_OBSERVED))
                self.assertEqual(self.inventory.resource_revision("conn-a"), before)

    def test_error_messages_do_not_echo_raw_content(self) -> None:
        marker = "SYNTHETIC-MARKER-NOT-ECHOED"
        with self.assertRaises(CapabilityError) as caught:
            self.observe("conn-a", (marker, (), ()), (marker, (), ()))
        self.assertNotIn(marker, str(caught.exception))

    def test_same_model_name_on_two_resources_has_separate_capability(self) -> None:
        self.observe("conn-a", (ALPHA, ("high", "low"), ("shell",)))
        self.observe("conn-b", (ALPHA, ("low",), ()))
        accepted = self.inventory.validate("conn-a", ALPHA, "high", ("shell",))
        self.assertTrue(accepted.accepted)
        self.assertIs(accepted.supported_by, Provenance.PROVIDER_OBSERVED)
        rejected = self.inventory.validate("conn-b", ALPHA, "high", ("shell",))
        self.assertFalse(rejected.accepted)
        self.assertIn("provider_observed:EFFORT_NOT_SUPPORTED", rejected.reasons)
        self.assertIn("provider_observed:TOOL_NOT_SUPPORTED:shell", rejected.reasons)

    def test_requested_and_local_derived_values_are_never_promoted(self) -> None:
        self.inventory.load_list("conn-a", Provenance.REQUESTED, listing((ALPHA, ("high",), ())))
        self.inventory.load_list("conn-a", Provenance.LOCAL_DERIVED, listing((ALPHA, ("high",), ())))
        result = self.inventory.validate("conn-a", ALPHA, "high")
        self.assertFalse(result.accepted)
        self.assertIs(result.request_provenance, Provenance.REQUESTED)
        self.assertIsNone(result.supported_by)
        self.assertEqual(result.reasons, ("NO_SUPPORT_EVIDENCE", "requested:NOT_SUPPORT_EVIDENCE",
                                          "local_derived:NOT_SUPPORT_EVIDENCE"))
        self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.PROVIDER_OBSERVED))

    def test_unlisted_model_and_unsupported_combinations_are_rejected_with_reasons(self) -> None:
        self.observe("conn-a", (ALPHA, ("low",), ("search",)))
        missing = self.inventory.validate("conn-a", BETA)
        self.assertEqual(missing.reasons, ("provider_observed:MODEL_NOT_LISTED",))
        effort = self.inventory.validate("conn-a", ALPHA, "high")
        self.assertEqual(effort.reasons, ("provider_observed:EFFORT_NOT_SUPPORTED",))
        tool = self.inventory.validate("conn-a", ALPHA, "low", ("search", "shell"))
        self.assertEqual(tool.reasons, ("provider_observed:TOOL_NOT_SUPPORTED:shell",))
        self.assertTrue(self.inventory.validate("conn-a", ALPHA).accepted)

    def test_model_list_change_turns_previous_results_stale(self) -> None:
        self.observe("conn-a", (ALPHA, ("low",), ()))
        result = self.inventory.validate("conn-a", ALPHA, "low")
        self.assertIs(self.inventory.freshness(result), Freshness.FRESH)
        self.observe("conn-a", (ALPHA, ("low",), ()), (BETA, (), ()))
        self.assertIs(self.inventory.freshness(result), Freshness.STALE)
        current = self.inventory.revalidate(result)
        self.assertTrue(current.accepted)
        self.observe("conn-a", (BETA, (), ()))
        self.assertIs(self.inventory.freshness(current), Freshness.STALE)
        removed = self.inventory.revalidate(current)
        self.assertFalse(removed.accepted)
        self.assertEqual(removed.reasons, ("provider_observed:MODEL_NOT_LISTED",))

    def test_identical_reload_keeps_result_fresh_and_other_resource_unaffected(self) -> None:
        self.observe("conn-a", (ALPHA, (), ()))
        self.observe("conn-b", (ALPHA, (), ()))
        result_b = self.inventory.validate("conn-b", ALPHA)
        result_a = self.inventory.validate("conn-a", ALPHA)
        self.observe("conn-a", (ALPHA, (), ()))
        self.assertIs(self.inventory.freshness(result_a), Freshness.FRESH)
        self.observe("conn-a", (GAMMA, (), ()))
        self.assertIs(self.inventory.freshness(result_a), Freshness.STALE)
        self.assertIs(self.inventory.freshness(result_b), Freshness.FRESH)

    def test_observed_list_ttl_and_clock_regression_are_stale(self) -> None:
        self.observe("conn-a", (ALPHA, (), ()))
        result = self.inventory.validate("conn-a", ALPHA)
        self.clock.advance(61)
        self.assertIs(self.inventory.freshness(result), Freshness.STALE)
        expired = self.inventory.validate("conn-a", ALPHA)
        self.assertEqual(expired.reasons, ("provider_observed:INVENTORY_STALE",))
        self.observe("conn-a", (ALPHA, (), ()))
        self.assertTrue(self.inventory.validate("conn-a", ALPHA).accepted)
        self.clock.advance(-5)
        self.assertEqual(self.inventory.validate("conn-a", ALPHA).reasons,
                         ("provider_observed:INVENTORY_STALE",))

    def test_manual_registration_requires_reference_and_observed_list_takes_precedence(self) -> None:
        raw = listing((ALPHA, ("high",), ("shell",)))
        with self.assertRaises(CapabilityError) as caught:
            self.inventory.load_list("conn-a", Provenance.CONFIGURED_CATALOG, raw)
        self.assertEqual(str(caught.exception), "CATALOG_REFERENCE_REQUIRED")
        snapshot = self.inventory.register_manual("conn-a", raw, catalog_ref="catalog-review-1")
        self.assertEqual(snapshot.source_ref, "catalog-review-1")
        manual = self.inventory.validate("conn-a", ALPHA, "high", ("shell",))
        self.assertTrue(manual.accepted)
        self.assertIs(manual.supported_by, Provenance.CONFIGURED_CATALOG)
        self.assertIs(self.inventory.freshness(manual), Freshness.FRESH)
        # A provider observation lacking the tool overrides the manual claim.
        self.observe("conn-a", (ALPHA, ("high",), ()))
        overridden = self.inventory.validate("conn-a", ALPHA, "high", ("shell",))
        self.assertFalse(overridden.accepted)
        self.assertEqual(overridden.reasons, ("provider_observed:TOOL_NOT_SUPPORTED:shell",))
        self.assertIs(self.inventory.freshness(manual), Freshness.STALE)

    def test_binding_update_discards_account_scoped_lists_and_stales_results(self) -> None:
        self.observe("conn-a", (ALPHA, (), ()))
        self.inventory.register_manual("conn-b", listing((BETA, (), ())), catalog_ref="catalog-review-2")
        self.observe("conn-b", (BETA, (), ()))
        result = self.inventory.validate("conn-a", ALPHA)
        self.inventory.update_binding(binding("conn-a", revision=2, fingerprint="c"))
        self.assertIs(self.inventory.freshness(result), Freshness.STALE)
        self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.PROVIDER_OBSERVED))
        self.assertEqual(self.inventory.validate("conn-a", ALPHA).reasons, ("NO_SUPPORT_EVIDENCE",))
        self.inventory.update_binding(binding("conn-b", revision=2, fingerprint="d"))
        retained = self.inventory.validate("conn-b", BETA)
        self.assertIs(retained.supported_by, Provenance.CONFIGURED_CATALOG)
        with self.assertRaises(CapabilityError):
            self.inventory.update_binding(binding("conn-a", revision=2))
        other_kind = ConnectionBinding("synthetic", "synthetic-host", "conn-a", 3,
                                       AuthKind.NATIVE_SUBSCRIPTION, "host-session://local/conn-a",
                                       "a" * 64)
        with self.assertRaises(CapabilityError):
            self.inventory.update_binding(other_kind)

    def test_input_validation_and_unknown_resources(self) -> None:
        with self.assertRaises(UnknownResource):
            self.inventory.validate("conn-missing", ALPHA)
        with self.assertRaises(UnknownResource):
            self.inventory.load_list("conn-missing", Provenance.REQUESTED, listing())
        with self.assertRaises(CapabilityError):
            self.inventory.validate("conn-a", ALPHA, tools=["shell"])
        with self.assertRaises(CapabilityError):
            self.inventory.validate("conn-a", ALPHA, tools=("shell", "shell"))
        with self.assertRaises(CapabilityError):
            self.inventory.validate("conn-a", "bad model")
        with self.assertRaises(CapabilityError):
            self.inventory.load_list("conn-a", "provider_observed", listing())
        with self.assertRaises(CapabilityError):
            self.inventory.register_resource(binding("conn-a"))
        with self.assertRaises(CapabilityError):
            ModelCapability("conn-a", ALPHA, (), (), "requested")
        with self.assertRaises(TypeError):
            ModelCapability("conn-a", ALPHA, (), ())  # provenance has no default
        with self.assertRaises(CapabilityError):
            CapabilityInventory(ttl_seconds=0)

    def test_empty_model_list_is_observed_but_supports_nothing(self) -> None:
        self.inventory.load_list("conn-a", Provenance.PROVIDER_OBSERVED, listing())
        result = self.inventory.validate("conn-a", ALPHA)
        self.assertIsInstance(result, ValidationResult)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("provider_observed:MODEL_NOT_LISTED",))

    def test_product_source_does_not_hardcode_vendor_model_names(self) -> None:
        pattern = re.compile(r"(?i)gpt-|claude|gemini|sonnet|opus|haiku|codex-")
        for path in sorted((ROOT / "src" / "aik_capability").glob("*.py")):
            with self.subTest(source=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
