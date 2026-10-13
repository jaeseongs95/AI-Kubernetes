"""AIK-020 capability inventory tests with synthetic model names only.

No provider is contacted and no credential value is used. Linux results are
development verification, not Windows support evidence.
"""
from __future__ import annotations

from dataclasses import replace
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
MAX_RAW_BYTES = 1 << 20


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

    def observe_raw(self, connection: str, raw, **overrides):
        # A provider observation must state its source and actual observation time.
        arguments = {"source_ref": "observation-1", "observed_at": self.clock.now}
        arguments.update(overrides)
        return self.inventory.load_list(connection, Provenance.PROVIDER_OBSERVED, raw, **arguments)

    def observe(self, connection: str, *entries):
        return self.observe_raw(connection, listing(*entries))

    def test_raw_order_and_digest_are_preserved(self) -> None:
        raw = listing((BETA, ("medium", "low"), ("shell",)), (ALPHA, ("high",), ()))
        snapshot = self.observe_raw("conn-a", raw)
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
                    self.observe_raw("conn-a", raw)
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
        # The catalog reviewed for the previous binding revision is retained
        # for inspection but no longer supports the new account (F-3).
        retained = self.inventory.validate("conn-b", BETA)
        self.assertFalse(retained.accepted)
        self.assertIsNone(retained.supported_by)
        self.assertEqual(retained.reasons, ("configured_catalog:BINDING_MISMATCH",))
        self.assertIsNotNone(self.inventory.snapshot("conn-b", Provenance.CONFIGURED_CATALOG))
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
        self.observe_raw("conn-a", listing())
        result = self.inventory.validate("conn-a", ALPHA)
        self.assertIsInstance(result, ValidationResult)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reasons, ("provider_observed:MODEL_NOT_LISTED",))

    def test_product_source_does_not_hardcode_vendor_model_names(self) -> None:
        pattern = re.compile(r"(?i)gpt-|claude|gemini|sonnet|opus|haiku|codex-")
        for path in sorted((ROOT / "src" / "aik_capability").glob("*.py")):
            with self.subTest(source=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    # ----- F-1: context limits -------------------------------------------------
    def test_context_limit_is_recorded_and_enforced_fail_closed(self) -> None:
        raw = json.dumps({"models": [
            {"model": ALPHA, "efforts": [], "tools": [], "context_tokens": 200000},
            {"model": BETA, "efforts": [], "tools": []},
        ]})
        snapshot = self.inventory.register_manual("conn-a", raw, catalog_ref="catalog-context-1")
        self.assertEqual(snapshot.find(ALPHA).context_tokens, 200000)
        self.assertIsNone(snapshot.find(BETA).context_tokens)
        enough = self.inventory.validate("conn-a", ALPHA, min_context=100000)
        self.assertTrue(enough.accepted)
        self.assertEqual(enough.min_context, 100000)
        self.assertEqual(self.inventory.revalidate(enough).min_context, 100000)
        too_large = self.inventory.validate("conn-a", ALPHA, min_context=300000)
        self.assertFalse(too_large.accepted)
        self.assertEqual(too_large.reasons, ("configured_catalog:CONTEXT_NOT_SUPPORTED",))
        unknown = self.inventory.validate("conn-a", BETA, min_context=1)
        self.assertFalse(unknown.accepted)
        self.assertEqual(unknown.reasons, ("configured_catalog:CONTEXT_UNKNOWN",))
        self.assertTrue(self.inventory.validate("conn-a", BETA).accepted)
        for value in (True, 0, -1, "1", 1.5):
            with self.subTest(min_context=value):
                with self.assertRaises(CapabilityError) as caught:
                    self.inventory.validate("conn-a", ALPHA, min_context=value)
                self.assertEqual(str(caught.exception), "INVALID_MIN_CONTEXT")

    def test_invalid_context_values_are_rejected_without_state_change(self) -> None:
        before = self.inventory.resource_revision("conn-a")
        for literal, code in (("true", "INVALID_CONTEXT_TOKENS"), ("0", "INVALID_CONTEXT_TOKENS"),
                              ("-1", "INVALID_CONTEXT_TOKENS"), ("1.5", "INVALID_CONTEXT_TOKENS"),
                              ('"200000"', "INVALID_CONTEXT_TOKENS"),
                              (str(2**31), "INVALID_CONTEXT_TOKENS"), ("null", "NULL_VALUE")):
            raw = '{"models":[{"model":"m-1","efforts":[],"tools":[],"context_tokens":%s}]}' % literal
            with self.subTest(context_tokens=literal):
                with self.assertRaises(CapabilityError) as caught:
                    self.inventory.register_manual("conn-a", raw, catalog_ref="catalog-context-2")
                self.assertEqual(str(caught.exception), code)
                self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.CONFIGURED_CATALOG))
                self.assertEqual(self.inventory.resource_revision("conn-a"), before)

    # ----- F-2: observation time and source -------------------------------------
    def test_observed_list_requires_source_and_valid_observation_time(self) -> None:
        raw = listing((ALPHA, (), ()))
        before = self.inventory.resource_revision("conn-a")
        cases = (
            ({"observed_at": self.clock.now}, "OBSERVATION_SOURCE_REQUIRED"),
            ({"source_ref": "observation-1"}, "OBSERVED_AT_REQUIRED"),
            ({"source_ref": "observation-1", "observed_at": datetime(2030, 1, 1)}, "INVALID_OBSERVED_AT"),
            ({"source_ref": "observation-1", "observed_at": "2030-01-01T00:00:00Z"}, "INVALID_OBSERVED_AT"),
            ({"source_ref": "observation-1", "observed_at": self.clock.now + timedelta(seconds=1)},
             "OBSERVED_AT_IN_FUTURE"),
        )
        for arguments, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(CapabilityError) as caught:
                    self.inventory.load_list("conn-a", Provenance.PROVIDER_OBSERVED, raw, **arguments)
                self.assertEqual(str(caught.exception), code)
                self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.PROVIDER_OBSERVED))
                self.assertEqual(self.inventory.resource_revision("conn-a"), before)

    def test_cached_old_observation_is_stale_and_reloading_does_not_refresh_it(self) -> None:
        old = self.clock.now - timedelta(hours=2)
        snapshot = self.observe_raw("conn-a", listing((ALPHA, (), ())), observed_at=old)
        self.assertEqual(snapshot.observed_at, "2029-12-31T22:00:00Z")
        self.assertEqual(snapshot.loaded_at, "2030-01-01T00:00:00Z")
        self.assertEqual(self.inventory.validate("conn-a", ALPHA).reasons,
                         ("provider_observed:INVENTORY_STALE",))
        revision = self.inventory.resource_revision("conn-a")
        self.clock.advance(10)
        self.observe_raw("conn-a", listing((ALPHA, (), ())), observed_at=old)
        self.assertEqual(self.inventory.resource_revision("conn-a"), revision)
        self.assertEqual(self.inventory.validate("conn-a", ALPHA).reasons,
                         ("provider_observed:INVENTORY_STALE",))
        self.observe("conn-a", (ALPHA, (), ()))
        self.assertTrue(self.inventory.validate("conn-a", ALPHA).accepted)

    # ----- F-3: catalog after account change -------------------------------------
    def test_catalog_from_previous_binding_revision_is_rejected_until_reregistered(self) -> None:
        raw = listing((BETA, ("high",), ()))
        self.inventory.register_manual("conn-b", raw, catalog_ref="catalog-account-x")
        self.assertTrue(self.inventory.validate("conn-b", BETA, "high").accepted)
        self.inventory.update_binding(binding("conn-b", revision=2, fingerprint="d"))
        after = self.inventory.validate("conn-b", BETA, "high")
        self.assertFalse(after.accepted)
        self.assertEqual(after.reasons, ("configured_catalog:BINDING_MISMATCH",))
        self.assertEqual(self.inventory.snapshot("conn-b", Provenance.CONFIGURED_CATALOG).connection_revision, 1)
        revision = self.inventory.resource_revision("conn-b")
        self.inventory.register_manual("conn-b", raw, catalog_ref="catalog-account-x")
        self.assertEqual(self.inventory.resource_revision("conn-b"), revision + 1)
        reviewed = self.inventory.validate("conn-b", BETA, "high")
        self.assertTrue(reviewed.accepted)
        self.assertIs(reviewed.supported_by, Provenance.CONFIGURED_CATALOG)

    # ----- F-4: freshness compares evidence --------------------------------------
    def test_freshness_rejects_results_not_matching_current_evidence(self) -> None:
        self.inventory.register_manual("conn-a", listing((ALPHA, (), ())), catalog_ref="catalog-review-3")
        genuine = self.inventory.validate("conn-a", ALPHA)
        self.assertIs(self.inventory.freshness(genuine), Freshness.FRESH)
        rejected = self.inventory.validate("conn-a", BETA)
        self.assertIs(self.inventory.freshness(rejected), Freshness.FRESH)
        forged = (
            replace(genuine, evidence_digest="0" * 64),
            replace(genuine, supported_by=None, evidence_digest=None),
            replace(genuine, supported_by="configured_catalog"),
            replace(genuine, supported_by=Provenance.REQUESTED),
            replace(genuine, accepted=False),
        )
        for number, result in enumerate(forged):
            with self.subTest(forged=number):
                self.assertIs(self.inventory.freshness(result), Freshness.STALE)

    # ----- F-5: rejection and boundary paths ---------------------------------------
    def test_boundary_rejections_have_fixed_codes_and_keep_revision(self) -> None:
        cases = (
            (b"x" * (MAX_RAW_BYTES + 1), "RAW_TOO_LARGE"),
            ("x" * (MAX_RAW_BYTES + 1), "RAW_TOO_LARGE"),
            ('{"models":[{"model":"m-1","efforts":[],"tools":[],"x":Infinity}]}', "INVALID_JSON_CONSTANT"),
            ('{"models":[{"model":"m-1","efforts":[],"tools":[],"x":-Infinity}]}', "INVALID_JSON_CONSTANT"),
            (listing(*((f"m-{number:04d}", (), ()) for number in range(1001))), "INVALID_MODELS"),
            (listing((ALPHA, tuple(f"effort-{number}" for number in range(33)), ())), "INVALID_EFFORTS"),
            (listing((ALPHA, (), tuple(f"tool-{number}" for number in range(65)))), "INVALID_TOOLS"),
            (12345, "INVALID_RAW_TYPE"),
        )
        before = self.inventory.resource_revision("conn-a")
        for raw, code in cases:
            with self.subTest(code=code, kind=type(raw).__name__):
                with self.assertRaises(CapabilityError) as caught:
                    self.observe_raw("conn-a", raw)
                self.assertEqual(str(caught.exception), code)
                self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.PROVIDER_OBSERVED))
                self.assertEqual(self.inventory.resource_revision("conn-a"), before)

    def test_remove_list_increments_revision_and_stales_results(self) -> None:
        self.observe("conn-a", (ALPHA, (), ()))
        result = self.inventory.validate("conn-a", ALPHA)
        revision = self.inventory.resource_revision("conn-a")
        self.inventory.remove_list("conn-a", Provenance.PROVIDER_OBSERVED)
        self.assertEqual(self.inventory.resource_revision("conn-a"), revision + 1)
        self.assertIs(self.inventory.freshness(result), Freshness.STALE)
        self.inventory.remove_list("conn-a", Provenance.PROVIDER_OBSERVED)
        self.assertEqual(self.inventory.resource_revision("conn-a"), revision + 1)
        with self.assertRaises(CapabilityError) as caught:
            self.inventory.remove_list("conn-a", "provider_observed")
        self.assertEqual(str(caught.exception), "INVALID_PROVENANCE")

    def test_binding_update_discards_local_derived_and_keeps_requested(self) -> None:
        self.inventory.load_list("conn-a", Provenance.REQUESTED, listing((ALPHA, (), ())))
        self.inventory.load_list("conn-a", Provenance.LOCAL_DERIVED, listing((ALPHA, (), ())))
        self.inventory.update_binding(binding("conn-a", revision=2, fingerprint="e"))
        self.assertIsNone(self.inventory.snapshot("conn-a", Provenance.LOCAL_DERIVED))
        self.assertIsNotNone(self.inventory.snapshot("conn-a", Provenance.REQUESTED))

    def test_catalog_reference_change_alone_increments_revision(self) -> None:
        raw = listing((ALPHA, (), ()))
        self.inventory.register_manual("conn-a", raw, catalog_ref="catalog-review-a")
        revision = self.inventory.resource_revision("conn-a")
        self.inventory.register_manual("conn-a", raw, catalog_ref="catalog-review-a")
        self.assertEqual(self.inventory.resource_revision("conn-a"), revision)
        self.inventory.register_manual("conn-a", raw, catalog_ref="catalog-review-b")
        self.assertEqual(self.inventory.resource_revision("conn-a"), revision + 1)

    def test_clock_failures_and_invalid_constructor_arguments_are_rejected(self) -> None:
        def broken_clock():
            raise RuntimeError("synthetic clock failure")

        broken = CapabilityInventory(clock=broken_clock)
        broken.register_resource(binding("conn-a"))
        with self.assertRaises(CapabilityError) as caught:
            broken.validate("conn-a", ALPHA)
        self.assertEqual(str(caught.exception), "CLOCK_UNAVAILABLE")
        with self.assertRaises(CapabilityError) as caught:
            broken.load_list("conn-a", Provenance.REQUESTED, listing((ALPHA, (), ())))
        self.assertEqual(str(caught.exception), "CLOCK_UNAVAILABLE")
        self.assertIsNone(broken.snapshot("conn-a", Provenance.REQUESTED))
        self.assertEqual(broken.resource_revision("conn-a"), 1)

        naive = CapabilityInventory(clock=lambda: datetime(2030, 1, 1))
        naive.register_resource(binding("conn-a"))
        with self.assertRaises(CapabilityError) as caught:
            naive.validate("conn-a", ALPHA)
        self.assertEqual(str(caught.exception), "INVALID_CLOCK")

        for ttl in (True, float("inf"), -1, 86401):
            with self.subTest(ttl=ttl):
                with self.assertRaises(CapabilityError) as caught:
                    CapabilityInventory(ttl_seconds=ttl)
                self.assertEqual(str(caught.exception), "INVALID_TTL")
        with self.assertRaises(CapabilityError) as caught:
            CapabilityInventory(clock="not-callable")
        self.assertEqual(str(caught.exception), "INVALID_CALLBACK")

    # ----- F-6: size check before encoding -------------------------------------------
    def test_oversized_text_is_rejected_before_utf8_encoding(self) -> None:
        # A lone surrogate at the end makes encoding fail; reaching the size
        # code proves the length check ran before an encoded copy was made.
        raw = "a" * MAX_RAW_BYTES + "\ud800"
        with self.assertRaises(CapabilityError) as caught:
            self.observe_raw("conn-a", raw)
        self.assertEqual(str(caught.exception), "RAW_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
