"""AIK-037 evidence relay: artifact manifest, content-addressed handoff and digest checks.

The relay collects worker outputs from a canonical workspace root and records
their identity, SHA-256, producing attempt and access scope. It copies the
bytes into a managed content-addressed store. Display derivatives (for example
redacted text) receive their own identity/digest and transform provenance.

This package does not create VM evidence authority, Task completion or AGS
verdicts. Binding predecessor outputs as later Task inputs belongs to AIK-084
and the VM public reference contract. Recency is never inferred from a filename.
"""

from .errors import (
    EvidenceConflict, EvidenceError, EvidenceExportExists, EvidenceIntegrityError,
    EvidencePathRejected, EvidenceUnavailable, InvalidEvidenceDatabase,
    InvalidEvidenceRecord, UnknownArtifact,
)
from .models import (
    ACCESS_SCOPES, KINDS, MAX_ARTIFACT_BYTES, VERIFY_STATUSES, ArtifactRecord,
    ConflictRecord, VerifyResult,
)
from .relay import (
    APPLICATION_ID, REDACTION_TRANSFORM, REDACTION_VERSION, SCHEMA_VERSION,
    EvidenceRelay, redact_text_artifact,
)

__all__ = [
    "EvidenceRelay", "redact_text_artifact", "APPLICATION_ID", "SCHEMA_VERSION",
    "REDACTION_TRANSFORM", "REDACTION_VERSION",
    "ArtifactRecord", "ConflictRecord", "VerifyResult",
    "KINDS", "ACCESS_SCOPES", "VERIFY_STATUSES", "MAX_ARTIFACT_BYTES",
    "EvidenceError", "InvalidEvidenceRecord", "InvalidEvidenceDatabase", "EvidencePathRejected",
    "EvidenceUnavailable", "EvidenceConflict", "EvidenceIntegrityError", "UnknownArtifact",
    "EvidenceExportExists",
]
