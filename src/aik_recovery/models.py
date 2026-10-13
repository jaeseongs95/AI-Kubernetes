"""AIK-039 recovery records. Fields hold IDs, states and codes, never secrets or payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier


VM_STATES = ("active", "terminal", "unknown", "absent")
IDEMPOTENCY_CLASSES = ("read_only", "idempotent", "non_idempotent")
SUBJECT_KINDS = ("lease", "effect", "scan")
OUTCOMES = ("recorded", "planned", "applied", "failed")
ACTIONS = (
    "release_provider_terminal", "keep_running", "observe_failed_keep_unknown",
    "release_undispatched", "await_vm", "handoff_vm_unknown",
    "resend_result", "record_late_receipt", "no_action_result_delivered", "scan_failed",
)


class RecoveryError(ValueError):
    """Fixed code-only failure; the message never contains supplied values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise RecoveryError("INVALID_" + field.upper()) from None


def _check_flag(value: object, field: str) -> None:
    if type(value) is not bool:
        raise RecoveryError("INVALID_" + field.upper())


@dataclass(frozen=True, slots=True)
class EffectRef:
    """One in-flight tool call/effect as reported by the effect journal adapter.

    has_receipt: an executor receipt was journaled for the effect.
    model_result_returned: the receipt's result was already returned to the model.
    cancelled: the attempt was cancelled before this record (late effect/receipt).
    """

    vm_attempt_id: str
    tool_call_id: str
    operation: str
    has_receipt: bool
    model_result_returned: bool
    idempotency: str
    cancelled: bool = False
    receipt_ref: str | None = None

    def __post_init__(self) -> None:
        check_id(self.vm_attempt_id, "vm_attempt_id")
        check_id(self.tool_call_id, "tool_call_id")
        check_id(self.operation, "operation")
        _check_flag(self.has_receipt, "has_receipt")
        _check_flag(self.model_result_returned, "model_result_returned")
        _check_flag(self.cancelled, "cancelled")
        if type(self.idempotency) is not str or self.idempotency not in IDEMPOTENCY_CLASSES:
            raise RecoveryError("INVALID_IDEMPOTENCY")
        if self.receipt_ref is not None:
            check_id(self.receipt_ref, "receipt_ref")
        if self.model_result_returned and not self.has_receipt:
            raise RecoveryError("INCONSISTENT_EFFECT_REF")

    @property
    def subject_id(self) -> str:
        # Identifiers cannot contain '/', so this key is unambiguous.
        return f"{self.vm_attempt_id}/{self.tool_call_id}/{self.operation}"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One appended journal row. observed holds the state seen when deciding."""

    seq: int
    decision_id: str
    subject_kind: str
    subject_id: str
    action: str
    outcome: str
    reasons: tuple[str, ...]
    observed: Mapping
    evidence_refs: tuple[str, ...]
    decided_at: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """decisions: rows appended in this run.

    unchanged: subjects whose identical decision (or applied action) was
    already journaled, so nothing was repeated. failures: subjects whose
    action or scan failed; they stay in their fail-closed state.
    """

    decisions: tuple[RecoveryDecision, ...]
    unchanged: tuple[str, ...]
    failures: tuple[str, ...]
