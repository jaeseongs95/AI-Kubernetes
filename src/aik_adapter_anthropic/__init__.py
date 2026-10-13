"""AIK-013 Anthropic API (Messages API) adapter for the AIK-011 provider contract.

Every HTTP exchange goes through an injectable transport. The API key is
resolved from the AIK-009 vault by ``credential_ref`` only when a request is
built. Implementation state is ``implemented``, while route qualification
remains ``NOT_RUN``: actual account support is judged only by AIK-022/082/086.
Vendor endpoints, headers and response fields are unverified assumptions.
"""

from .adapter import (
    DEFAULT_API_VERSION, DEFAULT_BASE_URL, DEFAULT_RUNTIME, MESSAGES_PATH, MODELS_PATH,
    PROVIDER, ROUTE_QUALIFICATION, AnthropicAdapter, ModelAvailability,
)
from .messages import (
    SOURCES, Completion, Message, MessagesCall, ModelResult, TextPart, ToolCall,
    ToolResult, ToolSpec, Usage, assistant_message, tool_results_message,
)
from .transport import (
    MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, Transport, TransportNotSent,
    TransportTimeout, UrllibTransport,
)

__all__ = [
    "AnthropicAdapter", "ModelAvailability", "PROVIDER", "DEFAULT_RUNTIME", "DEFAULT_BASE_URL",
    "DEFAULT_API_VERSION", "MESSAGES_PATH", "MODELS_PATH", "ROUTE_QUALIFICATION",
    "Completion", "TextPart", "ToolCall", "ToolResult", "ToolSpec", "Message", "MessagesCall",
    "Usage", "ModelResult", "assistant_message", "tool_results_message", "SOURCES",
    "HttpRequest", "HttpResponse", "Transport", "TransportTimeout", "TransportNotSent",
    "UrllibTransport", "MAX_RESPONSE_BYTES",
]
