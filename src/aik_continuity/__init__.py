"""AIK-027 director continuity: directives, corrections, checkpoints, handoff and resume.

This is a partial implementation. User directives and later corrections are
stored append-only, in arrival order, together with their scope (which earlier
directive they replace, supplement or withdraw). Model summaries are stored
only as ``model_reported`` auxiliary text. Checkpoints hold only VM/AGS ledger
IDs and revision IDs: no Goal/Task state copy and no second Goal authority.

Resume re-reads the stored references through an injected VM read port
(``status``) and continues with the existing IDs and revisions only when VM
still agrees. On any mismatch it stops and requires user confirmation; it never
creates a new Goal or Attempt itself. Real VM/AGS installations and Windows
behaviour are NOT_RUN.
"""

from .errors import (
    ContinuityConflict, ContinuityError, InvalidContinuityDatabase, InvalidContinuityRecord,
    RevisionRegression,
)
from .models import (
    CORRECTION_SCOPES, DIRECTIVE_KINDS, DIRECTIVE_STATUSES, SUMMARY_AUTHORITY,
    Appended, Checkpoint, ConfigRevision, ContextSummary, Directive, EffectiveDirective,
    HandoffPacket, StoredCheckpoint, StoredConfig, StoredDirective, StoredSummary, TaskRef,
    resolve_directives,
)
from .resume import (
    RESUME_HANDOFFS, RESUME_STATUSES, BridgeStatusPort, ResumeResult, Resumer, VmReadPort,
    verify_checkpoint,
)
from .store import APPLICATION_ID, SCHEMA_VERSION, ContinuityStore, default_continuity_path

__all__ = [
    "ContinuityStore", "default_continuity_path", "APPLICATION_ID", "SCHEMA_VERSION",
    "Directive", "ContextSummary", "ConfigRevision", "TaskRef", "Checkpoint",
    "StoredDirective", "StoredSummary", "StoredConfig", "StoredCheckpoint", "Appended",
    "EffectiveDirective", "HandoffPacket", "resolve_directives",
    "DIRECTIVE_KINDS", "CORRECTION_SCOPES", "DIRECTIVE_STATUSES", "SUMMARY_AUTHORITY",
    "Resumer", "ResumeResult", "VmReadPort", "BridgeStatusPort", "verify_checkpoint",
    "RESUME_STATUSES", "RESUME_HANDOFFS",
    "ContinuityError", "InvalidContinuityRecord", "ContinuityConflict", "RevisionRegression",
    "InvalidContinuityDatabase",
]
