"""AIK-093: the ApiToolLoop model port backed by this adapter.

``AnthropicTurnPort.next(request_id, messages)`` turns the loop's vendor-neutral
messages into one Messages API call and the answer back into a ``ModelTurn``.

- Instructions, tools and request options are fixed per port, so every turn of
  a task sends the same prefix and only appends to it (cache friendly).
- Thinking blocks come back as opaque blocks and are sent again unchanged, at
  their original place in the assistant turn.
- The per-turn request id is derived from the loop's request id and the number
  of assistant turns already in the conversation, so a resume asks for the
  same id and the adapter's single-use fence refuses a duplicate call.
- A turn that ended truncated, refused, paused or unknown is raised, never
  passed to the loop as a final answer.
"""
from __future__ import annotations

import json
from typing import Mapping

from aik_dispatch.api_loop import ModelToolCall, ModelTurn, opaque_blocks
from aik_provider.contracts import ConnectionBinding, JobRequest
from aik_provider.turns import CachePolicy

from .messages import (
    Completion, Message, MessagesCall, TextPart, ToolCall, ToolResult, ToolSpec,
)

_MAX_REQUEST_ID = 128


class TurnPortError(Exception):
    """A fixed code; never carries conversation text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AnthropicTurnPort:
    def __init__(self, adapter, binding: ConnectionBinding, *, model: str, route_id: str, vm_attempt_id: str,
                 max_tokens: int, system: str | None = None, tools=(), cache: CachePolicy | None = None,
                 thinking: str | None = None, effort: str | None = None, dispatcher=None):
        if any(type(tool) is not ToolSpec for tool in tools):
            raise TypeError("tools must be ToolSpec values")
        self._adapter = adapter
        self._dispatcher = dispatcher or adapter  # e.g. a GuardedAdapter in the product path
        self._binding = binding
        self._model = model
        self._route_id = route_id
        self._vm_attempt_id = vm_attempt_id
        # Validated once here; the same options go with every turn.
        self._options = dict(max_tokens=max_tokens, system=system, tools=tuple(tools), cache=cache,
                             thinking=thinking, effort=effort)
        MessagesCall((Message("user", (TextPart("probe"),)),), **self._options)

    def next(self, request_id: str, messages) -> ModelTurn:
        turn_number = 1 + sum(1 for message in messages if message.get("role") == "assistant")
        turn_id = f"{request_id}-t{turn_number}"
        if len(turn_id) > _MAX_REQUEST_ID:
            raise TurnPortError("TURN_REQUEST_ID_TOO_LONG")
        call = MessagesCall(to_messages(messages), **self._options)
        self._adapter.prepare(turn_id, call)
        self._dispatcher.start_job(JobRequest(self._binding, turn_id, self._route_id, self._vm_attempt_id,
                                              self._model))
        result = self._adapter.result(turn_id)
        if result is None or result.completion not in (Completion.COMPLETE, Completion.TOOL_USE):
            raise TurnPortError("MODEL_TURN_NOT_CONTINUABLE")
        return ModelTurn(
            stop_reason=result.stop_reason or result.completion.value,
            text=result.text,
            tool_calls=tuple(ModelToolCall(call.call_id, call.name, call.wire_arguments) for call in result.tool_calls),
            opaque=result.opaque_blocks,
            usage=result.usage.breakdown(),
        )


def to_messages(messages) -> tuple:
    """Vendor-neutral loop messages -> Messages API turns (consecutive user parts merged)."""
    turns: list[tuple[str, list]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            parts = [TextPart(_text(message.get("text")))]
            target = "user"
        elif role == "tool":
            parts = [_tool_result(message)]
            target = "user"
        elif role == "assistant":
            parts = _assistant_parts(message)
            target = "assistant"
        else:
            raise TurnPortError("UNSUPPORTED_MESSAGE_ROLE")
        if turns and turns[-1][0] == target and target == "user":
            turns[-1][1].extend(parts)
        else:
            turns.append((target, parts))
    return tuple(Message(role, tuple(parts)) for role, parts in turns)


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise TurnPortError("INVALID_MESSAGE_TEXT")
    return value


def _tool_result(message: Mapping) -> ToolResult:
    payload = message.get("result")
    if not isinstance(payload, Mapping):
        raise TurnPortError("INVALID_TOOL_RESULT")
    content = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ToolResult(message.get("tool_call_id"), content, is_error=payload.get("ok") is False)


def _assistant_parts(message: Mapping) -> list:
    parts: list = []
    text = message.get("text")
    if type(text) is str and text:
        parts.append(TextPart(text))
    for entry in message.get("tool_calls") or ():
        arguments = entry.get("arguments")
        if not isinstance(arguments, Mapping):
            raise TurnPortError("INVALID_TOOL_ARGUMENTS")
        parts.append(ToolCall.create(entry.get("tool_call_id"), entry.get("tool"), dict(arguments),
                                     keep_wire_order=True))
    # Put each opaque block back at its original index among the response blocks.
    for block in sorted(opaque_blocks(message), key=lambda item: item.position):
        parts.insert(min(block.position, len(parts)), block)
    if not parts:
        raise TurnPortError("EMPTY_ASSISTANT_TURN")
    return parts


__all__ = ["AnthropicTurnPort", "TurnPortError", "to_messages"]
