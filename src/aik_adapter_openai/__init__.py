"""AIK-012 OpenAI API-key adapter on top of the AIK-011 provider contract.

The adapter turns an official API-key connection into the common resource,
usage and job-observation contract. It sends nothing by itself. HTTP goes
through an explicitly injected transport, and credentials are resolved from
``aik_vault`` by opaque reference only when a request is built.

Implementation state and route qualification are separate. This package never
reports a qualified route, and a fixture transport is not a live observation.
"""

from .adapter import PROVIDER, RUNTIME, OpenAIApiAdapter, slot_fingerprint
from .contract import (
    BILLING_BASIS, EVIDENCE_KINDS, MODEL_AVAILABLE, MODEL_DENIED, MODEL_NOT_FOUND,
    MODEL_UNAUTHENTICATED, MODEL_UNKNOWN, AdapterStatus, Message, ModelCheck,
    ToolCall, ToolResult, ToolSpec, TurnInput, TurnResult, Usage,
)
from .transport import (
    DEFAULT_BASE_URL, MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, Transport,
    UrllibTransport,
)

__all__ = [
    "OpenAIApiAdapter", "PROVIDER", "RUNTIME", "slot_fingerprint",
    "AdapterStatus", "Message", "ModelCheck", "ToolCall", "ToolResult", "ToolSpec",
    "TurnInput", "TurnResult", "Usage", "BILLING_BASIS", "EVIDENCE_KINDS",
    "MODEL_AVAILABLE", "MODEL_DENIED", "MODEL_NOT_FOUND", "MODEL_UNAUTHENTICATED",
    "MODEL_UNKNOWN", "HttpRequest", "HttpResponse", "Transport", "UrllibTransport",
    "DEFAULT_BASE_URL", "MAX_RESPONSE_BYTES",
]
