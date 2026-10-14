"""Vendor-neutral model-turn contract produced by the AIK-013 adapter.

These types carry the user's RAW conversation and the model's RAW output for
the agent tool loop (AIK-035). They never carry credentials. Validation
errors use fixed codes and never include message content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
import re

from aik_provider.turns import CachePolicy, Measured, OpaqueBlock, UsageBreakdown

SOURCES = ("synthetic_fixture", "live")
MAX_TEXT_CHARS = 1_000_000
MAX_MESSAGES = 1000
MAX_PARTS = 256
MAX_TOOLS = 128
MAX_OUTPUT_TOKENS = 1_000_000
THINKING_MODES = ("adaptive",)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PROVIDER_NAME = "anthropic"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CALL_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_INT = 2**63 - 1


def _text_ok(value: object, *, allow_empty: bool = False) -> bool:
    if type(value) is not str or len(value) > MAX_TEXT_CHARS or (not value and not allow_empty):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _reject_constant(_value: str):
    raise ValueError("NON_FINITE_JSON")


def canonical_json_object(value: object) -> str | None:
    if type(value) is not dict:
        return None
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None
    return text if _text_ok(text) else None


def _parse_object(text: object) -> dict | None:
    if not _text_ok(text):
        return None
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return value if type(value) is dict else None


class Completion(StrEnum):
    """How a model turn ended. Only COMPLETE is a final answer."""

    COMPLETE = "complete"
    TOOL_USE = "tool_use"
    TRUNCATED = "truncated"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str

    def __post_init__(self) -> None:
        if not _text_ok(self.text):
            raise ValueError("INVALID_TEXT_PART")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model request to run a tool; this adapter never executes tools.

    ``arguments_json`` is canonical (sorted keys) and is what digests use.
    ``wire_arguments_json`` keeps the provider's own key order so the turn can
    be sent back exactly as the model wrote it (AIK-093); it is not compared.
    """

    call_id: str
    name: str
    arguments_json: str
    wire_arguments_json: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if type(self.call_id) is not str or _CALL_ID.fullmatch(self.call_id) is None:
            raise ValueError("INVALID_TOOL_CALL_ID")
        if type(self.name) is not str or _TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("INVALID_TOOL_NAME")
        arguments = _parse_object(self.arguments_json)
        if arguments is None:
            raise ValueError("INVALID_TOOL_ARGUMENTS")
        if self.wire_arguments_json is not None:
            wire = _parse_object(self.wire_arguments_json)
            if wire is None or canonical_json_object(wire) != canonical_json_object(arguments):
                raise ValueError("INVALID_TOOL_ARGUMENTS")

    @classmethod
    def create(cls, call_id: str, name: str, arguments: dict, *, keep_wire_order: bool = False) -> "ToolCall":
        text = canonical_json_object(arguments)
        if text is None:
            raise ValueError("INVALID_TOOL_ARGUMENTS")
        wire = None
        if keep_wire_order:
            wire = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return cls(call_id, name, text, wire)

    @property
    def wire_arguments(self) -> dict:
        """Arguments in the order to send back: the provider's order when known."""
        return json.loads(self.wire_arguments_json if self.wire_arguments_json is not None else self.arguments_json)

    @property
    def arguments(self) -> dict:
        return json.loads(self.arguments_json)


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if type(self.call_id) is not str or _CALL_ID.fullmatch(self.call_id) is None:
            raise ValueError("INVALID_TOOL_CALL_ID")
        if not _text_ok(self.content, allow_empty=True):
            raise ValueError("INVALID_TOOL_RESULT")
        if type(self.is_error) is not bool:
            raise ValueError("INVALID_TOOL_RESULT")


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    parts: tuple

    def __post_init__(self) -> None:
        if type(self.role) is not str or self.role not in ("user", "assistant"):
            raise ValueError("INVALID_MESSAGE_ROLE")
        parts = self.parts
        if type(parts) is list:
            parts = tuple(parts)
            object.__setattr__(self, "parts", parts)
        if type(parts) is not tuple or not 1 <= len(parts) <= MAX_PARTS:
            raise ValueError("INVALID_MESSAGE_PARTS")
        for part in parts:
            if type(part) is TextPart:
                continue
            if type(part) in (ToolCall, OpaqueBlock) and self.role == "assistant":
                continue
            if type(part) is ToolResult and self.role == "user":
                continue
            raise ValueError("INVALID_MESSAGE_PART")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema_json: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or _TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("INVALID_TOOL_NAME")
        if not _text_ok(self.description):
            raise ValueError("INVALID_TOOL_DESCRIPTION")
        if _parse_object(self.input_schema_json) is None:
            raise ValueError("INVALID_TOOL_SCHEMA")

    @classmethod
    def create(cls, name: str, description: str, input_schema: dict) -> "ToolSpec":
        text = canonical_json_object(input_schema)
        if text is None:
            raise ValueError("INVALID_TOOL_SCHEMA")
        return cls(name, description, text)

    @property
    def input_schema(self) -> dict:
        return json.loads(self.input_schema_json)


@dataclass(frozen=True, slots=True)
class MessagesCall:
    """The conversation for one model turn, prepared before dispatch."""

    messages: tuple
    max_tokens: int
    system: str | None = None
    tools: tuple = ()
    cache: CachePolicy | None = None   # AIK-093: None keeps the request body unchanged
    thinking: str | None = None        # "adaptive" or None (provider default)
    effort: str | None = None          # one of EFFORTS or None (provider default)

    def __post_init__(self) -> None:
        for name in ("messages", "tools"):
            value = getattr(self, name)
            if type(value) is list:
                object.__setattr__(self, name, tuple(value))
        if type(self.messages) is not tuple or not 1 <= len(self.messages) <= MAX_MESSAGES:
            raise ValueError("INVALID_MESSAGES")
        if any(type(message) is not Message for message in self.messages):
            raise ValueError("INVALID_MESSAGES")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError("INVALID_MAX_TOKENS")
        if self.system is not None and not _text_ok(self.system):
            raise ValueError("INVALID_SYSTEM_PROMPT")
        if type(self.tools) is not tuple or len(self.tools) > MAX_TOOLS:
            raise ValueError("INVALID_TOOLS")
        if any(type(tool) is not ToolSpec for tool in self.tools):
            raise ValueError("INVALID_TOOLS")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("DUPLICATE_TOOL_NAME")
        if self.cache is not None and not isinstance(self.cache, CachePolicy):
            raise ValueError("INVALID_CACHE_POLICY")
        if self.cache is CachePolicy.PROVIDER_DEFAULT:
            raise ValueError("UNSUPPORTED_CACHE_POLICY")  # explicit caching only (capability)
        if self.thinking is not None and self.thinking not in THINKING_MODES:
            raise ValueError("INVALID_THINKING")
        if self.effort is not None and self.effort not in EFFORTS:
            raise ValueError("INVALID_EFFORT")


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-reported counters only; absent or malformed values stay None."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_5m_input_tokens: int | None = None   # cache_creation.ephemeral_5m_input_tokens
    cache_creation_1h_input_tokens: int | None = None   # cache_creation.ephemeral_1h_input_tokens

    def __post_init__(self) -> None:
        for name in _USAGE_NAMES:
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= _MAX_INT):
                raise ValueError("INVALID_USAGE")

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in _USAGE_NAMES}

    def breakdown(self) -> UsageBreakdown:
        """The vendor-neutral split (AIK-093). Thinking tokens are not reported separately."""
        short, long_ = self.cache_creation_5m_input_tokens, self.cache_creation_1h_input_tokens
        by_ttl = short is not None and long_ is not None
        return UsageBreakdown(
            input_uncached=Measured.observed(self.input_tokens),
            cache_write_short=Measured.observed(short) if by_ttl else Measured(),
            cache_write_long=Measured.observed(long_) if by_ttl else Measured(),
            cache_write_unspecified=Measured() if by_ttl else Measured.observed(self.cache_creation_input_tokens),
            cache_read=Measured.observed(self.cache_read_input_tokens),
            output=Measured.observed(self.output_tokens),
        )


_USAGE_NAMES = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                "cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens")


@dataclass(frozen=True, slots=True)
class ModelResult:
    """One observed model turn. Not a VM completion verdict nor qualification.

    ``source == "synthetic_fixture"`` results must never be recorded as
    measurements. ``model`` is the provider-reported value or None.
    """

    request_id: str
    provider_job_id: str
    model: str | None
    stop_reason: str | None
    completion: Completion
    parts: tuple
    usage: Usage
    source: str

    def __post_init__(self) -> None:
        for name in ("request_id", "provider_job_id"):
            value = getattr(self, name)
            if type(value) is not str or _ID.fullmatch(value) is None:
                raise ValueError("INVALID_RESULT_ID")
        if self.model is not None and type(self.model) is not str:
            raise ValueError("INVALID_RESULT_MODEL")
        if self.stop_reason is not None and type(self.stop_reason) is not str:
            raise ValueError("INVALID_STOP_REASON")
        if type(self.completion) is not Completion:
            raise ValueError("INVALID_COMPLETION")
        if type(self.parts) is not tuple or any(type(p) not in (TextPart, ToolCall, OpaqueBlock) for p in self.parts):
            raise ValueError("INVALID_RESULT_PARTS")
        if type(self.usage) is not Usage:
            raise ValueError("INVALID_USAGE")
        if self.source not in SOURCES:
            raise ValueError("INVALID_RESULT_SOURCE")

    @property
    def tool_calls(self) -> tuple:
        return tuple(part for part in self.parts if type(part) is ToolCall)

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts if type(part) is TextPart)

    @property
    def opaque_blocks(self) -> tuple:
        return tuple(part for part in self.parts if type(part) is OpaqueBlock)

    @property
    def is_final(self) -> bool:
        return self.completion is Completion.COMPLETE

    @property
    def measured(self) -> bool:
        """A live transport observation; still not route qualification."""
        return self.source == "live"


def assistant_message(result: ModelResult) -> Message:
    """Append a continuable model turn to the conversation for the next call."""
    if type(result) is not ModelResult:
        raise ValueError("INVALID_RESULT")
    if result.completion not in (Completion.COMPLETE, Completion.TOOL_USE, Completion.INCOMPLETE):
        raise ValueError("RESULT_NOT_CONTINUABLE")
    if not result.parts:
        raise ValueError("EMPTY_ASSISTANT_TURN")
    return Message("assistant", result.parts)


def tool_results_message(results) -> Message:
    """Wrap tool results executed elsewhere (AIK-035) as the next user turn."""
    return Message("user", tuple(results))


__all__ = [
    "Completion", "TextPart", "ToolCall", "ToolResult", "Message", "ToolSpec", "MessagesCall",
    "Usage", "ModelResult", "assistant_message", "tool_results_message", "SOURCES",
    "canonical_json_object", "THINKING_MODES", "EFFORTS",
]
