"""AIK-039 restart, unknown-outcome and retry reconciliation.

After an app, sidecar or network failure, the reconciler compares the stored
leases, provider jobs and in-flight tool effects with VM attempt state. A
provider job is observed again on its stored route and account before any
decision. Every decision is appended to a separate journal. Earlier evidence,
receipts and decisions are never overwritten.

This package never starts a provider job, never re-executes a tool effect and
never creates a retry. VM's unknown/retry contract stays the authority. No
exactly-once delivery or execution is claimed.
"""

from .adapters import READ_ONLY_TOOLS, UNKNOWN_CODE, ToolEffectAdapter
from .journal import APPLICATION_ID, SCHEMA_VERSION, RecoveryJournal, decision_fingerprint
from .models import (
    ACTIONS, IDEMPOTENCY_CLASSES, OUTCOMES, SUBJECT_KINDS, VM_STATES, EffectRef,
    RecoveryDecision, RecoveryError, RecoveryReport,
)
from .reconciler import LEASE_SCAN_STATES, RecoveryReconciler

__all__ = [
    "RecoveryReconciler", "RecoveryJournal", "ToolEffectAdapter", "READ_ONLY_TOOLS", "UNKNOWN_CODE", "decision_fingerprint", "APPLICATION_ID", "SCHEMA_VERSION",
    "EffectRef", "RecoveryDecision", "RecoveryReport", "RecoveryError",
    "ACTIONS", "OUTCOMES", "SUBJECT_KINDS", "VM_STATES", "IDEMPOTENCY_CLASSES", "LEASE_SCAN_STATES",
]
