"""AIK-043 non-authoritative read model joining VM, AGS and AIK events.

This package is a rebuildable projection that the GUI reads. It is NOT an
authoritative ledger. VM remains the authority for Goal, Task, Attempt, retry,
verification and GoalVerdict. AGS owns its workflow, peer and session records.
AIK owns its runtime, dispatch, lifecycle and evidence records. Every view here
is a display copy of those sources, linked by ID, revision and cursor. Nothing
in this package decides completion, readiness or authorization.

No collector for real VM History or AGS PEER events exists here. Callers turn
source records into ``SourceEvent`` values, either with the pure adapters or
directly, and pass them to ``ReadModel.apply``.
"""

from .adapters import from_bridge_receipt, from_lease, from_peer_message
from .errors import InvalidEvent, InvalidReadModelDatabase, ReadModelError, ScopeMismatch
from .models import (
    EXPANDING_KEYS, PEER_STATES, REF_KEYS, SOURCES, ApplyReport, AttemptView, ConflictRecord,
    CursorStatus, DeliveryState, PeerView, SourceEvent, SourceState,
)
from .store import APPLICATION_ID, SCHEMA_VERSION, ReadModel

__all__ = [
    "ReadModel", "APPLICATION_ID", "SCHEMA_VERSION",
    "SourceEvent", "ApplyReport", "AttemptView", "PeerView", "SourceState", "CursorStatus",
    "DeliveryState", "ConflictRecord", "SOURCES", "REF_KEYS", "EXPANDING_KEYS", "PEER_STATES",
    "from_bridge_receipt", "from_lease", "from_peer_message",
    "ReadModelError", "InvalidEvent", "ScopeMismatch", "InvalidReadModelDatabase",
]
