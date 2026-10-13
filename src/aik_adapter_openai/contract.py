"""Provider-neutral turn, tool, usage and status records (AIK-012).

These records connect an API adapter to the AIK-035 bounded agent tool loop.
They contain no credential field. ``evidence_kind`` keeps fixture results
distinct from network observations; neither is a route qualification.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from aik_provider.contracts import JobState


BILLING_BASIS = "api_key_metered"  # API usage billing, never subscription quota
MAX_TEXT_CHARS = 1_000_000
MAX_ITEMS = 256
MAX_TOOLS = 128
MAX_SCHEMA_BYTES = 65536
ROLES = ("system", "developer", "user", "assistant")

MODEL_AVAILABLE = "available"
MODEL_NOT_FOUND = "not_found"
MODEL_DENIED = "denied"
MODEL_UNAUTHENTICATED = "unauthenticated"
MODEL_UNKNOWN = "unknown"
MODEL_AVAILABILITIES = (MODEL_AVAILABLE, MODEL_NOT_FOUND, MODEL_DENIED, MODEL_UNAUTHENTICATED, MODEL_UNKNOWN)

EVIDENCE_KINDS = ("fixture", "network", "unspecified")

TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
CALL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
RESPONSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_INT = 2**63


def _text(value: object, code: str, *, allow_empty: bool = False) -> None:
    if type(value) is not str or len(value) > MAX_TEXT_CHARS or (not allow_empty and not value):
        raise ValueError(code)


def _count(value: object) -> None:
    if value is not None and (type(value) is not int or not 0 <= value < _MAX_INT):
        raise ValueError("INVALID_USAGE")


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    text: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or self.role not in ROLES:
            raise ValueError("INVALID_ROLE")
        _text(self.text, "INVALID_MESSAGE_TEXT")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A function tool offered to the model; execution stays with AIK-035."""

    name: str
    description: str
    parameters: dict

    def __post_init__(self) -> None:
        if type(self.name) is not str or TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("INVALID_TOOL_NAME")
        if type(self.description) is not str or len(self.description) > 4096:
            raise ValueError("INVALID_TOOL_DESCRIPTION")
        encoded = None
        if type(self.parameters) is dict:
            try:
                encoded = json.dumps(self.parameters, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                encoded = None
        if encoded is None or len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
            raise ValueError("INVALID_TOOL_PARAMETERS")
        # Detach from the caller's mutable dictionary.
        object.__setattr__(self, "parameters", json.loads(encoded))


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider tool request. ``arguments`` is None unless valid JSON object."""

    call_id: str
    name: str
    arguments: dict | None
    arguments_valid: bool

    def __post_init__(self) -> None:
        if type(self.call_id) is not str or CALL_ID.fullmatch(self.call_id) is None:
            raise ValueError("INVALID_CALL_ID")
        if type(self.name) is not str or TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError("INVALID_TOOL_NAME")
        if self.arguments is not None and type(self.arguments) is not dict:
            raise ValueError("INVALID_TOOL_ARGUMENTS")
        if type(self.arguments_valid) is not bool or self.arguments_valid != (self.arguments is not None):
            raise ValueError("INVALID_TOOL_ARGUMENTS")


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    output: str

    def __post_init__(self) -> None:
        if type(self.call_id) is not str or CALL_ID.fullmatch(self.call_id) is None:
            raise ValueError("INVALID_CALL_ID")
        _text(self.output, "INVALID_TOOL_OUTPUT", allow_empty=True)


@dataclass(frozen=True, slots=True)
class TurnInput:
    """One bounded model turn: messages and/or results of earlier tool calls."""

    messages: tuple = ()
    tools: tuple = ()
    tool_results: tuple = ()
    previous_response_id: str | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, kind, limit in (("messages", Message, MAX_ITEMS), ("tools", ToolSpec, MAX_TOOLS),
                                  ("tool_results", ToolResult, MAX_ITEMS)):
            value = getattr(self, name)
            if type(value) is not tuple or len(value) > limit or any(type(item) is not kind for item in value):
                raise ValueError("INVALID_TURN_" + name.upper())
        if not self.messages and not self.tool_results:
            raise ValueError("EMPTY_TURN")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("DUPLICATE_TOOL_NAME")
        if self.previous_response_id is not None and (
            type(self.previous_response_id) is not str or RESPONSE_ID.fullmatch(self.previous_response_id) is None
        ):
            raise ValueError("INVALID_PREVIOUS_RESPONSE_ID")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 2**31
        ):
            raise ValueError("INVALID_MAX_OUTPUT_TOKENS")


@dataclass(frozen=True, slots=True)
class Usage:
    """Only token counts the provider explicitly reported; None means unknown."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    billing_basis: str = BILLING_BASIS

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            _count(getattr(self, name))
        if self.billing_basis != BILLING_BASIS:
            raise ValueError("INVALID_BILLING_BASIS")

    @property
    def reported(self) -> bool:
        return any(value is not None for value in (self.input_tokens, self.output_tokens, self.total_tokens))


@dataclass(frozen=True, slots=True)
class TurnResult:
    """A provider observation in common form, never a VM completion verdict."""

    request_id: str
    provider_job_id: str
    response_id: str
    state: JobState
    provider_status: str | None
    model: str | None
    usage: Usage
    output_text: str | None
    tool_calls: tuple
    unhandled_item_types: tuple
    error_code: str | None
    secret_echo_redacted: bool
    evidence_kind: str
    billing_basis: str = BILLING_BASIS

    def __post_init__(self) -> None:
        if type(self.state) is not JobState:
            raise ValueError("INVALID_JOB_STATE")
        if type(self.usage) is not Usage:
            raise ValueError("INVALID_USAGE")
        if type(self.tool_calls) is not tuple or any(type(call) is not ToolCall for call in self.tool_calls):
            raise ValueError("INVALID_TOOL_CALLS")
        if type(self.unhandled_item_types) is not tuple:
            raise ValueError("INVALID_UNHANDLED_ITEMS")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise ValueError("INVALID_EVIDENCE_KIND")
        if self.billing_basis != BILLING_BASIS:
            raise ValueError("INVALID_BILLING_BASIS")


@dataclass(frozen=True, slots=True)
class ModelCheck:
    model: str
    availability: str
    http_status: int | None

    def __post_init__(self) -> None:
        if self.availability not in MODEL_AVAILABILITIES:
            raise ValueError("INVALID_MODEL_AVAILABILITY")


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    """Implementation disclosure kept separate from route qualification."""

    implementation_state: str = "implemented"
    route_qualification: str = "NOT_RUN"
    live_vendor_round_trip: str = "NOT_RUN"
    endpoint_assumptions: str = "UNVERIFIED"

    def __post_init__(self) -> None:
        if self.implementation_state not in ("implemented", "not_implemented"):
            raise ValueError("INVALID_IMPLEMENTATION_STATE")
        if self.route_qualification != "NOT_RUN" or self.live_vendor_round_trip != "NOT_RUN":
            # Qualification is decided by AIK-022/082/086 evidence, not here.
            raise ValueError("QUALIFICATION_REQUIRES_EXTERNAL_EVIDENCE")
        if self.endpoint_assumptions != "UNVERIFIED":
            raise ValueError("ENDPOINT_ASSUMPTIONS_NOT_VERIFIED")


__all__ = [
    "BILLING_BASIS", "MAX_TEXT_CHARS", "MAX_ITEMS", "ROLES", "EVIDENCE_KINDS",
    "MODEL_AVAILABLE", "MODEL_NOT_FOUND", "MODEL_DENIED", "MODEL_UNAUTHENTICATED", "MODEL_UNKNOWN",
    "Message", "ToolSpec", "ToolCall", "ToolResult", "TurnInput", "Usage", "TurnResult",
    "ModelCheck", "AdapterStatus",
]
