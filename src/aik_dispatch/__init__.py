"""AIK-035 dispatch skeleton: bind a VM-reserved Attempt to an AIK lease and provider connection.

VM remains the authority for Task, Attempt, retry and completion. AGS gates
are enforced by VM; this package neither skips nor replaces them. The package
dispatches an Attempt that VM has already reserved, through an injected
provider adapter, and records every step append-only in its own journal.

Two paths exist:

- ``NativeDispatcher``: dispatch intent on the lease, then one provider job
  start, then an acknowledgement and an observed start, each a separate state.
- ``ApiToolLoop``: a bounded model/tool loop. Each tool call is recorded as
  intent before the effect and as receipt after it, and the result is marked
  returned before the next model call.

The package contains no real provider/API client. Live behaviour is NOT_RUN.
"""

from .api_loop import (
    STOP_REASONS, TOOL_NAMES, ApiToolLoop, LoopResult, ModelToolCall, ModelTurn,
    assistant_message, opaque_blocks, tool_message,
)
from .errors import DISPATCH_ERROR_CODES, DispatchError, DispatchOutcomeUnknown, DispatchRejected
from .identity import DISPATCH_PATHS, DispatchIdentity
from .journal import (
    APPLICATION_ID, DISPATCH_STATES, TOOL_STATES, DispatchEvent, DispatchJournal, DispatchRecord, ToolEvent,
)
from .native import STARTED_JOB_STATES, NativeDispatcher

__all__ = [
    "DispatchIdentity", "DISPATCH_PATHS",
    "DispatchJournal", "DispatchRecord", "DispatchEvent", "ToolEvent",
    "APPLICATION_ID", "DISPATCH_STATES", "TOOL_STATES",
    "NativeDispatcher", "STARTED_JOB_STATES",
    "ApiToolLoop", "ModelTurn", "ModelToolCall", "LoopResult", "STOP_REASONS", "TOOL_NAMES",
    "assistant_message", "tool_message", "opaque_blocks",
    "DispatchError", "DispatchOutcomeUnknown", "DispatchRejected", "DISPATCH_ERROR_CODES",
]
