"""AIK-093: the real ApiToolLoop driving the Anthropic adapter through AnthropicTurnPort.

Synthetic transport and test-only vault: no network, no real key. Shows that a
thinking block survives a tool round trip, the request prefix only grows,
cache markers sit at the end of the prefix and usage arrives per turn.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent))

import test_adapter_anthropic as anthropic_tests  # noqa: E402
import test_dispatch as dispatch_tests  # noqa: E402
from test_dispatch import CONTENT, USER  # noqa: E402

from aik_adapter_anthropic import MESSAGES_PATH, ToolSpec  # noqa: E402
from aik_adapter_anthropic.loop_port import AnthropicTurnPort, TurnPortError, to_messages  # noqa: E402
from aik_dispatch import DispatchError, assistant_message  # noqa: E402
from aik_provider import AdapterFailure, CachePolicy, FaultKind, GuardedAdapter, OpaqueBlock  # noqa: E402

THINKING = {"type": "thinking", "thinking": "plan the file", "signature": "c2lnLXN5bnRoZXRpYy0x"}
CREATE = ToolSpec.create("create_file", "Create a file",
                         {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                          "required": ["path", "content"]})


def answer(message_id, content, stop_reason, usage):
    return anthropic_tests.json_response(200, anthropic_tests.message_payload(
        message_id=message_id, content=content, stop_reason=stop_reason, usage=usage))


class AnthropicTurnPortLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = dispatch_tests.ApiToolLoopTests("test_create_file_result_returns_to_the_model_then_end_turn")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.a = anthropic_tests.AnthropicAdapterTests("test_begin_sign_in_is_unsupported_for_api_keys")
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.port = AnthropicTurnPort(
            self.a.adapter, self.a.binding, model=anthropic_tests.MODEL, route_id="route-synthetic",
            vm_attempt_id="vm-attempt-synthetic", max_tokens=256, system="stable instructions", tools=(CREATE,),
            cache=CachePolicy.SHORT, thinking="adaptive",
            dispatcher=GuardedAdapter(self.a.adapter, authorize_dispatch=lambda request: True))

    def bodies(self) -> list:
        return [json.loads(r.body) for r in self.a.transport.requests if r.method == "POST"]

    def test_thinking_tool_round_trip_through_the_real_loop(self) -> None:
        self.a.transport.routes[("POST", MESSAGES_PATH)] = [
            answer("msg_port_1", [THINKING, {"type": "tool_use", "id": "toolu_port_1", "name": "create_file",
                                             "input": {"path": "src/app.txt", "content": CONTENT}}], "tool_use",
                   {"input_tokens": 50, "output_tokens": 30, "cache_creation_input_tokens": 400,
                    "cache_read_input_tokens": 0}),
            answer("msg_port_2", [{"type": "text", "text": "done"}], "end_turn",
                   {"input_tokens": 60, "output_tokens": 5, "cache_creation_input_tokens": 80,
                    "cache_read_input_tokens": 400}),
        ]
        result = self.h.run_loop(self.h.loop(self.port))
        self.assertEqual((result.stop_reason, result.model_calls, result.tool_calls), ("end_turn", 2, 1))
        self.assertEqual((self.h.root / "src" / "app.txt").read_bytes(), CONTENT.encode("utf-8"))
        self.assertEqual(self.h.tool_states("toolu_port_1"), ["intent", "receipt", "returned"])

        first, second = self.bodies()
        # Append-only prefix: the second request starts with the first one's messages.
        self.assertEqual(second["messages"][0]["content"][0]["text"], first["messages"][0]["content"][0]["text"])
        self.assertEqual(second["messages"][1]["content"][0], THINKING)  # unchanged, still first
        self.assertEqual(second["messages"][1]["content"][1]["type"], "tool_use")
        self.assertEqual(second["messages"][2]["content"][0]["type"], "tool_result")
        self.assertIn("cache_control", second["messages"][2]["content"][-1])
        self.assertEqual(second["system"], first["system"])
        self.assertEqual(second["tools"], first["tools"])
        self.assertEqual(second["thinking"], {"type": "adaptive"})

        self.assertEqual([turn.cache_read.value for turn in result.usage], [0, 400])
        self.assertEqual([turn.total_input() for turn in result.usage], [450, 540])
        stored = [m for m in result.messages if m["role"] == "assistant"][0]["opaque"]
        self.assertEqual(json.loads(stored[0]["raw_json"]), THINKING)

    def test_resume_asks_for_the_same_turn_id_and_is_fenced(self) -> None:
        self.a.transport.routes[("POST", MESSAGES_PATH)] = [answer("msg_port_r", [{"type": "text", "text": "hi"}],
                                                                   "end_turn", None)]
        turn = self.port.next("req-api", (USER,))
        self.assertIsNone(turn.usage.total_input())  # nothing reported -> unknown, not 0
        with self.assertRaises(AdapterFailure) as raised:
            self.port.next("req-api", (USER,))  # same conversation -> same per-turn id -> refused
        self.assertIs(raised.exception.fault.kind, FaultKind.DUPLICATE_REQUEST)
        self.assertEqual(len(self.bodies()), 1)

    def test_non_continuable_turns_are_raised(self) -> None:
        self.a.transport.routes[("POST", MESSAGES_PATH)] = [answer("msg_port_t", [{"type": "text", "text": "cut"}],
                                                                   "max_tokens", None)]
        with self.assertRaises(TurnPortError) as raised:
            self.port.next("req-trunc", (USER,))
        self.assertEqual(raised.exception.code, "MODEL_TURN_NOT_CONTINUABLE")

    def test_opaque_blocks_return_to_their_original_position(self) -> None:
        block = OpaqueBlock("anthropic", "thinking", 0, json.dumps(THINKING))
        later = OpaqueBlock("anthropic", "redacted_thinking", 2, json.dumps({"type": "redacted_thinking", "data": "x"}))
        turn = dispatch_tests.create_turn()
        message = assistant_message(type(turn)(turn.stop_reason, "text", turn.tool_calls, (block, later)))
        converted = to_messages((USER, message))[1].parts
        self.assertEqual([type(part).__name__ for part in converted],
                         ["OpaqueBlock", "TextPart", "OpaqueBlock", "ToolCall"])


    def test_loop_rejects_malformed_opaque_blocks_before_any_model_call(self) -> None:
        bad = {"role": "assistant", "text": "x", "tool_calls": [], "opaque": [{"provider": "anthropic"}]}
        model = dispatch_tests.FakeModel()
        with self.assertRaises(DispatchError) as raised:
            self.h.run_loop(self.h.loop(model), messages=(USER, bad, USER))
        self.assertEqual(raised.exception.code, "REPLAY_MISMATCH")
        self.assertEqual(model.calls, [])


if __name__ == "__main__":
    unittest.main()
