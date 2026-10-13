"""Separate completion-related states of one VM Attempt with their provenance.

VM is the only completion authority. Nothing produced by AIK (provider
success, dispatch acknowledgement, worker claim, independent check PASS) can
make ``vm_verdict_recorded`` true. A recorded GoalVerdict may itself be a
failure; its content is read from VM, not decided here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from aik_dispatch import DISPATCH_STATES
from aik_vm_bridge.bridge import BridgeReceipt
from aik_vm_bridge.governance import GovernanceGateView, project_run_once
from aik_vm_bridge.surface import Command

from .errors import ValidationError
from .models import CheckRecord, check_id, check_text


ACK_STATES = ("sent_ack", "started")
FIELD_PROVENANCE = MappingProxyType({
    "provider_state": "provider_observed",
    "dispatch_ack": "local_derived",
    "worker_claim": "model_reported",
    "independent_check": "local_derived",
    "vm_validation_result_id": "vm",
    "gate": "vm_run_once",
    "goal_verdict_id": "vm",
    "rework_reasons": "vm",
})
_REWORK_FIELDS = ("failure_class", "suggested_repair_action", "blocker_code")


@dataclass(frozen=True, slots=True)
class CompletionView:
    vm_attempt_id: str
    provider_state: str | None
    dispatch_ack: bool
    worker_claim: str | None
    independent_check: CheckRecord | None
    vm_validation_result_id: str | None
    gate: GovernanceGateView | None
    goal_verdict_id: str | None
    rework_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        check_id(self.vm_attempt_id, "vm_attempt_id")
        if type(self.dispatch_ack) is not bool:
            raise ValidationError("INVALID_DISPATCH_ACK")
        if self.independent_check is not None and type(self.independent_check) is not CheckRecord:
            raise ValidationError("INVALID_CHECK_RECORD")
        if self.gate is not None and type(self.gate) is not GovernanceGateView:
            raise ValidationError("INVALID_GATE")
        if type(self.rework_reasons) is not tuple or any(type(item) is not str for item in self.rework_reasons):
            raise ValidationError("INVALID_REWORK_REASONS")

    @property
    def completion_authority(self) -> str:
        return "vm"

    @property
    def vm_verdict_recorded(self) -> bool:
        """True only when VM's final report names a GoalVerdict; AIK values never set this.

        The verdict may be a failure. Whether the Goal passed is VM's verdict content.
        """
        return self.goal_verdict_id is not None

    @property
    def provenance(self) -> MappingProxyType:
        return FIELD_PROVENANCE

    @property
    def check_verdict(self) -> str | None:
        return None if self.independent_check is None else self.independent_check.verdict


def _bind(observed: object, vm_attempt_id: str) -> None:
    if type(observed) is not str or observed != vm_attempt_id:
        raise ValidationError("ATTEMPT_MISMATCH")


def _vm_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    check_text(value, field, limit=1024)
    return value


def _receipt_payload(receipt: object, command: Command, code: str) -> Mapping:
    """Only an aik_vm_bridge BridgeReceipt of the expected command is read."""
    if type(receipt) is not BridgeReceipt or receipt.command != command or not isinstance(receipt.payload, Mapping):
        raise ValidationError(code)
    return receipt.payload


def _receipt_attempt(receipt: BridgeReceipt, payload: Mapping) -> object:
    value = receipt.refs.attempt_id if receipt.refs is not None else None
    return payload.get("attempt_id") if value is None else value


def _receipt_project(receipt: BridgeReceipt, payload: Mapping) -> object:
    value = receipt.refs.project_id if receipt.refs is not None else None
    if value is None:
        value = receipt.project_id
    return payload.get("project_id") if value is None else value


def _bind_project(receipt: BridgeReceipt, payload: Mapping, vm_project_id: str | None) -> None:
    observed = _receipt_project(receipt, payload)
    if vm_project_id is not None and observed is not None and observed != vm_project_id:
        raise ValidationError("PROJECT_MISMATCH")


def _provider_state(state: object) -> str:
    if isinstance(state, Enum):
        state = state.value if isinstance(state.value, str) else state.name
    if type(state) is not str:
        raise ValidationError("INVALID_OBSERVATION")
    check_text(state, "provider_state", limit=128)
    return state


def build_completion_view(vm_attempt_id: str, *, vm_project_id: str | None = None, observation=None,
                          dispatch_record=None, worker_claim=None, check=None, run_once_receipt=None,
                          attempt_receipt=None, final_report_receipt=None) -> CompletionView:
    """Combine owner outputs without mixing them.

    Attempt-level inputs must name the same Attempt. VM's final report is
    project-level: it is bound by ``vm_project_id``, which is then required.
    """
    check_id(vm_attempt_id, "vm_attempt_id")
    if vm_project_id is not None:
        check_id(vm_project_id, "vm_project_id")

    provider_state = None
    if observation is not None:
        _bind(getattr(observation, "vm_attempt_id", None), vm_attempt_id)
        provider_state = _provider_state(getattr(observation, "job_state", None))

    dispatch_ack = False
    if dispatch_record is not None:
        _bind(getattr(getattr(dispatch_record, "identity", None), "vm_attempt_id", None), vm_attempt_id)
        state = getattr(dispatch_record, "state", None)
        if state is not None and state not in DISPATCH_STATES:
            raise ValidationError("INVALID_DISPATCH_RECORD")
        dispatch_ack = state in ACK_STATES

    if worker_claim is not None:
        # Model-reported text; displayed as is and never promoted to any state.
        check_text(worker_claim, "worker_claim", limit=256)

    if check is not None:
        if type(check) is not CheckRecord:
            raise ValidationError("INVALID_CHECK_RECORD")
        _bind(check.vm_attempt_id, vm_attempt_id)

    reasons: list[str] = []
    validation_ids: list[str] = []
    gate = None
    if run_once_receipt is not None:
        payload = _receipt_payload(run_once_receipt, Command.RUN_ONCE, "UNREADABLE_RUN_ONCE_RECEIPT")
        _bind(_receipt_attempt(run_once_receipt, payload), vm_attempt_id)
        _bind_project(run_once_receipt, payload, vm_project_id)
        for name in _REWORK_FIELDS:
            value = _vm_text(payload.get(name), name)
            if value is not None:
                reasons.append(value)
        value = _vm_text(payload.get("validation_result_id"), "validation_result_id")
        if value is not None:
            validation_ids.append(value)
        try:
            gate = project_run_once(run_once_receipt)
        except Exception:
            raise ValidationError("GATE_PROJECTION_FAILED") from None
        if gate is not None:
            if type(gate) is not GovernanceGateView:
                raise ValidationError("GATE_PROJECTION_FAILED")
            if gate.attempt_id is not None and gate.attempt_id != vm_attempt_id:
                raise ValidationError("ATTEMPT_MISMATCH")
            if gate.reason_code is not None:
                reasons.append(_vm_text(gate.reason_code, "reason_code"))

    if attempt_receipt is not None:
        payload = _receipt_payload(attempt_receipt, Command.ATTEMPT_DETAIL, "UNREADABLE_ATTEMPT_RECEIPT")
        _bind(_receipt_attempt(attempt_receipt, payload), vm_attempt_id)
        _bind_project(attempt_receipt, payload, vm_project_id)
        value = _vm_text(payload.get("validation_result_id"), "validation_result_id")
        if value is not None:
            validation_ids.append(value)
    if len(set(validation_ids)) > 1:
        raise ValidationError("VALIDATION_RESULT_CONFLICT")

    goal_verdict_id = None
    if final_report_receipt is not None:
        if vm_project_id is None:
            raise ValidationError("PROJECT_REQUIRED")
        payload = _receipt_payload(final_report_receipt, Command.FINAL_REPORT, "UNREADABLE_FINAL_REPORT_RECEIPT")
        if _receipt_project(final_report_receipt, payload) != vm_project_id:
            raise ValidationError("PROJECT_MISMATCH")
        goal_verdict_id = _vm_text(payload.get("goal_verdict_id"), "goal_verdict_id")

    return CompletionView(
        vm_attempt_id=vm_attempt_id,
        provider_state=provider_state,
        dispatch_ack=dispatch_ack,
        worker_claim=worker_claim,
        independent_check=check,
        vm_validation_result_id=validation_ids[0] if validation_ids else None,
        gate=gate,
        goal_verdict_id=goal_verdict_id,
        rework_reasons=tuple(dict.fromkeys(reasons)),
    )
