"""AIK-093 vendor-neutral cache, opaque block and usage types (no provider, no network)."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_provider import (  # noqa: E402
    AccountIsolation, AdapterCapabilities, AuthKind, CacheMode, CachePolicy, Measured, OpaqueBlock,
    PromptCacheCapability, Provenance, Support, UsageBreakdown,
)

VENDOR_WORDS = ("cache_control", "ephemeral", "thinking", "anthropic", "openai", "cached_tokens", "ttl")


class TurnTypesTests(unittest.TestCase):
    def test_measured_distinguishes_unknown_from_zero(self):
        self.assertFalse(Measured().known)
        self.assertEqual(Measured.observed(0), Measured(0, Provenance.PROVIDER_OBSERVED))
        self.assertEqual(Measured.observed(None), Measured())
        for bad in ((None, Provenance.LOCAL_DERIVED), (5, None), (-1, Provenance.PROVIDER_OBSERVED), (True, None)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Measured(*bad)

    def test_breakdown_totals_only_when_every_input_component_is_known(self):
        full = UsageBreakdown(Measured.observed(10), Measured.observed(1), Measured.observed(2), Measured(),
                              Measured.observed(100), Measured.observed(7), Measured.observed(3))
        self.assertEqual((full.cache_write(), full.total_input()), (3, 113))
        self.assertIsNone(UsageBreakdown(input_uncached=Measured.observed(10)).total_input())
        self.assertIsNone(UsageBreakdown().cache_write())
        with self.assertRaises(ValueError):
            UsageBreakdown(output=Measured.observed(2), reasoning=Measured.observed(3))
        document = full.to_document()
        self.assertEqual(document["reasoning"], {"value": 3, "provenance": "provider_observed"})
        self.assertEqual(document["cache_write_unspecified"], {"value": None, "provenance": None})

    def test_opaque_block_is_kept_verbatim_and_bound_to_its_provider(self):
        raw = json.dumps({"type": "x", "signature": "abc", "z": 1, "a": 2})
        block = OpaqueBlock("vendor-a", "x", 3, raw)
        self.assertEqual(block.raw_json, raw)
        self.assertEqual(OpaqueBlock.from_document(block.to_document()), block)
        self.assertIs(block.for_provider("vendor-a"), block)
        with self.assertRaisesRegex(ValueError, "OPAQUE_BLOCK_PROVIDER_MISMATCH"):
            block.for_provider("vendor-b")
        for bad in (("Vendor", "x", 0, raw), ("v", "x", -1, raw), ("v", "x", 0, "[1]"), ("v", "x", 0, "{")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                OpaqueBlock(*bad)

    def test_cache_capability_and_adapter_disclosure(self):
        explicit = PromptCacheCapability(CacheMode.EXPLICIT, frozenset({CachePolicy.SHORT}), 4)
        self.assertTrue(explicit.supports(CachePolicy.NONE) and explicit.supports(CachePolicy.SHORT))
        self.assertFalse(explicit.supports(CachePolicy.LONG))
        with self.assertRaises(ValueError):
            PromptCacheCapability(CacheMode.NONE, frozenset({CachePolicy.SHORT}))
        positional = AdapterCapabilities("vendor-a", "api", AuthKind.NATIVE_SUBSCRIPTION,
                                         AccountIsolation.ISOLATED, *(Support.SUPPORTED,) * 6)
        self.assertIsNone(positional.prompt_cache)  # older positional callers keep working
        with self.assertRaises(ValueError):
            AdapterCapabilities("vendor-a", "api", AuthKind.NATIVE_SUBSCRIPTION,
                                AccountIsolation.ISOLATED, *(Support.SUPPORTED,) * 6, prompt_cache="explicit")

    def test_common_port_has_no_vendor_syntax(self):
        source = (ROOT / "src" / "aik_provider" / "turns.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        body = code.split('"""', 2)[2]  # skip the module docstring, which names the vendor words it excludes
        for word in VENDOR_WORDS:
            self.assertNotIn(word, body.lower(), word)


if __name__ == "__main__":
    unittest.main()
