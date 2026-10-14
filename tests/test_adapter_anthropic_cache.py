"""AIK-093 Anthropic adapter: prompt cache markers, thinking blocks, wire order and usage split.

Synthetic transport and test-only vault only; responses are protocol fixtures,
not measurements. The live observation is tools/probe_prompt_cache.py.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent))

import test_adapter_anthropic as base  # noqa: E402
from aik_adapter_anthropic import (  # noqa: E402
    MESSAGES_PATH, Completion, Message, MessagesCall, TextPart, ToolCall, ToolResult, ToolSpec, Usage,
    assistant_message, tool_results_message,
)
from aik_provider import CacheMode, CachePolicy, JobState, OpaqueBlock, Provenance  # noqa: E402

THINKING = {"type": "thinking", "thinking": "synthetic reasoning", "signature": "c2lnLXN5bnRoZXRpYw=="}
REDACTED = {"type": "redacted_thinking", "data": "ZW5jcnlwdGVkLXN5bnRoZXRpYw=="}
TOOL = ToolSpec.create("read_file", "Read a synthetic file",
                       {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


class AnthropicCacheAndThinkingTests(base.AnthropicAdapterTests):
    """Reuses the adapter fixture; load_tests below runs only the cases defined here."""

    def posted(self, index: int = -1) -> dict:
        return json.loads([r for r in self.transport.requests if r.method == "POST"][index].body)

    def turn(self, request_id, payload, call):
        self.transport.routes[("POST", MESSAGES_PATH)] = base.json_response(200, payload)
        self.adapter.prepare(request_id, call)
        return self.guard.start_job(self.job(request_id))

    def test_default_request_body_is_unchanged(self) -> None:
        self.turn("req-plain", base.message_payload(), base.simple_call())
        self.assertEqual(set(self.posted()), {"model", "max_tokens", "messages"})

    def test_cache_policy_marks_system_tools_and_last_message(self) -> None:
        user = Message("user", (TextPart("first"), TextPart("second")))
        for policy, control in ((CachePolicy.SHORT, {"type": "ephemeral"}),
                                (CachePolicy.LONG, {"type": "ephemeral", "ttl": "1h"})):
            with self.subTest(policy=policy):
                call = MessagesCall((user,), max_tokens=64, system="stable instructions", tools=(TOOL,), cache=policy)
                self.turn(f"req-cache-{policy.value}", base.message_payload(message_id=f"msg_{policy.value}"), call)
                body = self.posted()
                self.assertEqual(body["system"], [{"type": "text", "text": "stable instructions", "cache_control": control}])
                self.assertEqual(body["tools"][-1]["cache_control"], control)
                self.assertNotIn("cache_control", body["messages"][0]["content"][0])
                self.assertEqual(body["messages"][0]["content"][-1]["cache_control"], control)
        with self.assertRaises(ValueError):
            MessagesCall((user,), max_tokens=64, cache=CachePolicy.PROVIDER_DEFAULT)

    def test_thinking_and_effort_are_requested_only_when_set(self) -> None:
        call = MessagesCall((Message("user", (TextPart("q"),)),), max_tokens=64, thinking="adaptive", effort="high")
        self.turn("req-think", base.message_payload(), call)
        body = self.posted()
        self.assertEqual((body["thinking"], body["output_config"]), ({"type": "adaptive"}, {"effort": "high"}))
        for bad in ({"thinking": "enabled"}, {"effort": "extreme"}):
            with self.assertRaises(ValueError):
                MessagesCall((Message("user", (TextPart("q"),)),), max_tokens=64, **bad)

    def test_thinking_blocks_succeed_and_go_back_unchanged_in_place(self) -> None:
        first = base.message_payload(message_id="msg_think_1", stop_reason="tool_use", content=[
            THINKING, {"type": "text", "text": "checking"}, REDACTED,
            {"type": "tool_use", "id": "toolu_synthetic_1", "name": "read_file",
             "input": {"path": "a.txt", "encoding": "utf-8"}},
        ])
        user = Message("user", (TextPart("synthetic task"),))
        job = self.turn("req-think-1", first, MessagesCall((user,), max_tokens=64, tools=(TOOL,), thinking="adaptive",
                                                            cache=CachePolicy.SHORT))
        result = self.adapter.result("req-think-1")
        self.assertIs(job.receipt.state, JobState.SUCCEEDED)
        self.assertIs(result.completion, Completion.TOOL_USE)
        self.assertEqual([(b.kind, b.position) for b in result.opaque_blocks], [("thinking", 0), ("redacted_thinking", 2)])
        self.assertEqual(json.loads(result.opaque_blocks[0].raw_json), THINKING)
        # Digest-stable canonical arguments, provider order kept separately for the wire.
        self.assertEqual(result.tool_calls[0], ToolCall.create("toolu_synthetic_1", "read_file",
                                                               {"encoding": "utf-8", "path": "a.txt"}))
        self.assertEqual(list(result.tool_calls[0].wire_arguments), ["path", "encoding"])

        follow_up = MessagesCall((user, assistant_message(result),
                                  tool_results_message([ToolResult("toolu_synthetic_1", "contents")])),
                                 max_tokens=64, tools=(TOOL,), thinking="adaptive", cache=CachePolicy.SHORT)
        self.turn("req-think-2", base.message_payload(message_id="msg_think_2"), follow_up)
        sent = self.posted()["messages"][1]["content"]
        self.assertEqual(sent[0], THINKING)
        self.assertEqual(sent[1], {"type": "text", "text": "checking"})
        self.assertEqual(sent[2], REDACTED)
        self.assertEqual(list(sent[3]["input"]), ["path", "encoding"])
        self.assertTrue(all("cache_control" not in block for block in sent))
        self.assertIn("cache_control", self.posted()["messages"][2]["content"][-1])

    def test_other_unknown_blocks_still_fail_the_turn(self) -> None:
        job = self.turn("req-server", base.message_payload(content=[
            THINKING, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}]),
            base.simple_call())
        self.assertIs(job.receipt.state, JobState.FAILED)
        self.assertIs(self.adapter.result("req-server").completion, Completion.UNKNOWN)

    def test_opaque_blocks_are_not_sent_to_another_provider(self) -> None:
        foreign = OpaqueBlock("other-vendor", "reasoning", 0, json.dumps({"type": "reasoning"}))
        call = MessagesCall((Message("user", (TextPart("q"),)), Message("assistant", (foreign, TextPart("a"))),
                             Message("user", (TextPart("next"),))), max_tokens=64)
        with self.assertRaises(base.AdapterFailure) as raised:
            self.adapter.prepare("req-foreign", call)
        self.assertIs(raised.exception.fault.kind, base.FaultKind.INVALID_CONTRACT)
        self.assertEqual(raised.exception.fault.native_code, "opaque_provider_mismatch")
        self.assertEqual(self.transport.count("POST", MESSAGES_PATH), 0)

    def test_usage_split_by_cache_retention(self) -> None:
        usage = {"input_tokens": 20, "output_tokens": 9, "cache_creation_input_tokens": 300,
                 "cache_read_input_tokens": 4000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}}
        self.turn("req-usage", base.message_payload(usage=usage), base.simple_call())
        result = self.adapter.result("req-usage")
        self.assertEqual(result.usage, Usage(20, 9, 300, 4000, 100, 200))
        split = result.usage.breakdown()
        self.assertEqual((split.input_uncached.value, split.cache_write_short.value, split.cache_write_long.value,
                          split.cache_read.value, split.output.value), (20, 100, 200, 4000, 9))
        self.assertFalse(split.cache_write_unspecified.known)
        self.assertFalse(split.reasoning.known)  # thinking tokens are not reported separately
        self.assertIs(split.cache_read.provenance, Provenance.PROVIDER_OBSERVED)
        self.assertEqual((split.cache_write(), split.total_input()), (300, 4320))

    def test_usage_without_retention_split_keeps_an_unspecified_write(self) -> None:
        split = Usage(10, 2, 30, 0).breakdown()
        self.assertEqual((split.cache_write_unspecified.value, split.total_input()), (30, 40))
        self.assertFalse(split.cache_write_short.known)
        self.assertIsNone(Usage().breakdown().total_input())

    def test_capability_discloses_explicit_cache(self) -> None:
        cache = self.adapter.describe(self.binding).prompt_cache
        self.assertIs(cache.mode, CacheMode.EXPLICIT)
        self.assertTrue(cache.supports(CachePolicy.LONG))
        self.assertFalse(cache.supports(CachePolicy.PROVIDER_DEFAULT))


def load_tests(loader, tests, pattern):
    # Only the AIK-093 cases; the inherited adapter cases run in test_adapter_anthropic.
    names = [name for name in loader.getTestCaseNames(AnthropicCacheAndThinkingTests)
             if name in AnthropicCacheAndThinkingTests.__dict__]
    return unittest.TestSuite(AnthropicCacheAndThinkingTests(name) for name in names)


if __name__ == "__main__":
    unittest.main()
