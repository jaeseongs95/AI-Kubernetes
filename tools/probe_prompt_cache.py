"""Live observation for AIK-093: prompt cache write -> read, and a thinking tool round trip.

Uses the AIK-013 adapter and AnthropicTurnPort through an egress gateway that
injects the credential (same rule as probe_anthropic_messages.py: the adapter's
placeholder key header is dropped and no secret is read, stored or printed).
No redirect, no retry. The report holds counts and block kinds only, never
prompt text, thinking text or signatures.

    python -I tools/probe_prompt_cache.py --base-url https://GATEWAY \
        --gateway-injects-credential --model MODEL --output report.json

A pass is an observation of this path on this day. It is not route
qualification (AIK-086) and says nothing about another provider.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(Path(__file__).resolve().parent))

from probe_anthropic_messages import PLACEHOLDER, GatewayTransport, MemoryVault  # noqa: E402

from aik_adapter_anthropic import (  # noqa: E402
    AnthropicAdapter, Message, MessagesCall, TextPart, ToolSpec, UrllibTransport,
)
from aik_adapter_anthropic.loop_port import AnthropicTurnPort  # noqa: E402
from aik_dispatch import assistant_message  # noqa: E402
from aik_provider import CachePolicy  # noqa: E402
from aik_provider.contracts import JobRequest  # noqa: E402

# A synthetic, stable instruction block long enough for any model's minimum cacheable prefix.
PREFIX = "\n".join(
    f"Rule {number:04d}: when the synthetic catalog lists item {number}, its code is "
    f"C{number * 7919 % 100000:05d} and its shelf is S{number % 97:02d}."
    for number in range(1, 401)
)
LOOKUP = ToolSpec.create("lookup_code", "Look up the shelf code of a synthetic catalog item.",
                         {"type": "object", "properties": {"item": {"type": "integer"}}, "required": ["item"]})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _usage(result) -> dict:
    split = result.usage.breakdown()
    return {"reported": result.usage.as_dict(), "cache_write": split.cache_write(),
            "cache_read": split.cache_read.value, "total_input": split.total_input()}


def _failure(error: Exception) -> dict:
    fault = getattr(error, "fault", None)
    return {"error": type(error).__name__, "fault": str(getattr(fault, "kind", "") or ""),
            "native": str(getattr(fault, "native_code", "") or ""), "status": getattr(fault, "http_status", None),
            "code": str(getattr(error, "code", "") or "")}


def cache_probe(adapter, binding, model: str) -> dict:
    calls = []
    for number in (1, 2):
        request_id = f"cache-probe-{number}"
        call = MessagesCall((Message("user", (TextPart("Reply with the single word: ready"),)),), max_tokens=16,
                            system=PREFIX, cache=CachePolicy.SHORT)
        adapter.prepare(request_id, call)
        receipt = adapter.start_job(JobRequest(binding, request_id, "route-probe", "vm-attempt-probe", model))
        result = adapter.result(request_id)
        calls.append({"request": number, "state": receipt.state.value, "completion": result.completion.value,
                      "observed_model": result.model, **_usage(result)})
    write_seen = (calls[0]["cache_write"] or 0) > 0
    read_seen = (calls[1]["cache_read"] or 0) > 0
    return {"status": "observed" if write_seen and read_seen else "not_observed",
            "write_then_read": [write_seen, read_seen], "calls": calls}


def thinking_probe(adapter, binding, model: str) -> dict:
    port = AnthropicTurnPort(adapter, binding, model=model, route_id="route-probe", vm_attempt_id="vm-attempt-probe",
                             max_tokens=4096, system=PREFIX, tools=(LOOKUP,), cache=CachePolicy.SHORT,
                             thinking="adaptive", effort="high")
    user = {"role": "user", "text": ("Item 137's shelf code is not in your rules on purpose. Call lookup_code for "
                                     "item 137 first, then answer with the code and the shelf from the rules.")}
    first = port.next("thinking-probe", (user,))
    turns = [{"turn": 1, "stop_reason": first.stop_reason, "tool_calls": len(first.tool_calls),
              "opaque_kinds": [block.kind for block in first.opaque],
              "opaque_positions": [block.position for block in first.opaque],
              "cache_read": first.usage.cache_read.value, "cache_write": first.usage.cache_write()}]
    if not first.tool_calls:
        return {"status": "tool_not_called", "turns": turns}
    messages = [user, assistant_message(first)]
    for call in first.tool_calls:
        messages.append({"role": "tool", "tool_call_id": call.tool_call_id,
                         "result": {"ok": True, "item": 137, "code": "C08403"}})
    second = port.next("thinking-probe", tuple(messages))
    turns.append({"turn": 2, "stop_reason": second.stop_reason, "tool_calls": len(second.tool_calls),
                  "opaque_kinds": [block.kind for block in second.opaque],
                  "cache_read": second.usage.cache_read.value, "cache_write": second.usage.cache_write()})
    sent_back = bool(first.opaque)
    return {"status": ("round_trip_with_thinking" if sent_back else "round_trip_without_thinking_block"),
            "thinking_blocks_sent_back": sent_back, "turns": turns}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--gateway-injects-credential", action="store_true", required=True,
                        help="Required: this tool never takes a real API key.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    transport = GatewayTransport(UrllibTransport())
    adapter = AnthropicAdapter(MemoryVault(), transport, base_url=args.base_url, timeout=300)
    binding = adapter.register_key(PLACEHOLDER, connection_id="connection-probe")
    report = {"format": "aik-prompt-cache-probe-v1", "task": "AIK-093", "started_at": _now(),
              "requested_model": args.model,
              "credential": "injected by egress gateway; adapter x-api-key placeholder removed; no secret handled",
              "prefix_chars": len(PREFIX)}
    for name, probe in (("cache", cache_probe), ("thinking", thinking_probe)):
        try:
            report[name] = probe(adapter, binding, args.model)
        except Exception as error:  # noqa: BLE001 - reported as a fixed shape, never the message
            report[name] = {"status": "FAIL", **_failure(error)}
    report["exchanges"] = transport.sent
    report["finished_at"] = _now()
    report["not_verified"] = ["route qualification (AIK-086)", "other providers", "1h retention (LONG policy)",
                              "Windows host"]
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    ok = report["cache"].get("status") == "observed" and report["thinking"].get("status", "").startswith("round_trip")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
