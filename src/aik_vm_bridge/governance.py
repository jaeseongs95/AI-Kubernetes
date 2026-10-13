"""Display-only projection of governance gate results already decided by VM.

AGS and VM are fixed contracts. This module reads two public VM paths:

1. ``run_once()`` results (``blocker_code`` / ``detail``) carried by a
   ``BridgeReceipt`` from ``VmBridge.run_once_hosted``.
2. ``final_report().governance_plugin_tasks`` carried by a ``BridgeReceipt``
   from ``VmBridge.final_report``.

It also lists blocked Task IDs from ``status()`` without adding reasons.

The module only projects facts for display. It makes no AIK gate verdict and
keeps no cache. It has no retry path and no fallback that skips a gate. It
stores no state and imports neither AGS nor VM internals. ``detail`` is
non-authoritative diagnostic text. It is parsed only for display, and the
parse result never changes the gate state. Only ``blocker_code`` decides
the state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Any, Mapping

from .bridge import BridgeReceipt
from .errors import BridgeCode, VmBridgeError
from .surface import Command


class GateState(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    FAILED = "failed"
    CONTRACT_MISMATCH = "contract_mismatch"
    UNRECOGNIZED = "unrecognized"


class GatePhase(StrEnum):
    BEFORE_EXECUTION = "before_execution"
    BEFORE_COMPLETION = "before_completion"
    # The public surface does not report the phase for this result.
    UNREPORTED = "unreported"


GATE_BLOCKER_CODES = MappingProxyType({
    "GOVERNANCE_GATE_PENDING": (GateState.PENDING, GatePhase.UNREPORTED),
    "GOVERNANCE_GATE_BLOCKED": (GateState.BLOCKED, GatePhase.BEFORE_EXECUTION),
    "GOVERNANCE_GATE_FAILED": (GateState.FAILED, GatePhase.BEFORE_COMPLETION),
    "GOVERNANCE_CONTRACT_MISMATCH": (GateState.CONTRACT_MISMATCH, GatePhase.UNREPORTED),
})

KNOWN_REASON_CODES = frozenset({
    "GOVERNANCE_GATE_REQUIRED",
    "GOVERNANCE_ROLE_CONFIGURATION_REQUIRED",
    "GOVERNANCE_A2_PROFILE_REQUIRED",
    "GOVERNANCE_PROVIDER_UNSUPPORTED",
    "GOVERNANCE_USER_CHANGE_OVERLAP",
    "GOVERNANCE_CONTRACT_MISMATCH",
    "GOVERNANCE_TIMEOUT",
    "GOVERNANCE_GATE_ERROR",
})

# Facts that the public VM read surface does not expose. AIK does not read the
# VM SQL history to obtain them and does not request a VM contract change here.
UNREACHABLE_FACTS = (
    "contract_mismatch_phase",
    "operation_no_effect",
    "conformance_check_list",
    "gate_core_operations",
    "governance_blocked_history",
)

RUN_ONCE_PROVENANCE = "vm_run_once"
CLOSURE_PROVENANCE = "local_derived"
LABEL_PROVENANCE = "plugin_manifest_file"

_BLOCKER = re.compile(r"[A-Z][A-Z0-9_]{1,99}\Z")
_REASON = re.compile(r"(GOVERNANCE_[A-Z0-9_]{1,90})(?=\Z|[:\s])")
_DETAIL_PREFIX = "governance gate: "
_CONFORMANCE_PREFIX = "conformance:"
_CONTRACT_MISMATCH = "GOVERNANCE_CONTRACT_MISMATCH"


@dataclass(frozen=True, slots=True)
class GovernanceGateView:
    """A display projection of one VM gate result. It is never a verdict."""

    request_id: str
    project_id: str | None
    task_id: str | None
    attempt_id: str | None
    history_cursor: int | None
    vm_blocker_code: str
    state: GateState
    phase: GatePhase
    reason_code: str | None = None
    check_id: str | None = None
    conformance_check_id: str | None = None
    detail: str | None = None
    provenance: str = RUN_ONCE_PROVENANCE


@dataclass(frozen=True, slots=True)
class PluginTaskObservation:
    """VM's per-Task plugin identity observation from ``final_report``.

    ``plugin_version_label`` is the manifest's self-reported label. It is not
    a provider observation, and it is never used for equality or a verdict.
    """

    task_id: str
    task_ref: str | None
    closure_tree_digest: str | None
    plugin_version_label: str | None
    changed: bool
    changed_phases: tuple[str, ...]
    closure_provenance: str = CLOSURE_PROVENANCE
    label_provenance: str = LABEL_PROVENANCE


def _require_receipt(receipt: object, command: Command) -> Mapping[str, Any]:
    if not isinstance(receipt, BridgeReceipt):
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="receipt")
    if receipt.command != command:
        raise VmBridgeError(BridgeCode.INVALID_REQUEST, subject="command")
    if not isinstance(receipt.payload, Mapping):
        raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="payload")
    return receipt.payload


def _text_or_none(value: object) -> str | None:
    return value if type(value) is str else None


def _ref(receipt: BridgeReceipt, payload: Mapping[str, Any], name: str) -> str | None:
    value = getattr(receipt.refs, name, None) if receipt.refs is not None else None
    if value is None:
        value = payload.get(name)
    return _text_or_none(value)


def _parse_detail(detail: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(reason_code, check_id, conformance_check_id)`` for display only."""
    text = detail[len(_DETAIL_PREFIX):] if detail.startswith(_DETAIL_PREFIX) else detail
    match = _REASON.match(text)
    if match is None:
        return None, None, None
    token = match.group(1)
    reason_code = token if token in KNOWN_REASON_CODES else None
    check_id = conformance_check_id = None
    if token == _CONTRACT_MISMATCH:
        rest = text[match.end():]
        if rest.startswith(": "):
            rest = rest[2:]
            end = rest.find(": ")
            candidate = rest[:end] if end > 0 else ""
            if candidate and not any(char.isspace() for char in candidate):
                check_id = candidate
                if check_id.startswith(_CONFORMANCE_PREFIX) and len(check_id) > len(_CONFORMANCE_PREFIX):
                    conformance_check_id = check_id[len(_CONFORMANCE_PREFIX):]
    return reason_code, check_id, conformance_check_id


def project_run_once(receipt: BridgeReceipt) -> GovernanceGateView | None:
    """Project a run_once receipt to a gate view, or None when it is not a governance block.

    An unknown ``GOVERNANCE_*`` blocker becomes ``unrecognized``. It is never
    treated as passing. ``EXTERNAL_EFFECT_UNKNOWN`` also occurs outside the
    gate, so this function does not claim it as a governance result.
    """
    payload = _require_receipt(receipt, Command.RUN_ONCE)
    if payload.get("action") != "blocked":
        return None
    code = payload.get("blocker_code")
    if type(code) is not str or _BLOCKER.fullmatch(code) is None:
        raise VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="blocker_code")
    mapped = GATE_BLOCKER_CODES.get(code)
    if mapped is None:
        if not code.startswith("GOVERNANCE_"):
            return None
        mapped = (GateState.UNRECOGNIZED, GatePhase.UNREPORTED)
    state, phase = mapped
    detail = _text_or_none(payload.get("detail"))
    reason_code, check_id, conformance_check_id = (None, None, None) if detail is None else _parse_detail(detail)
    cursor = receipt.refs.history_cursor if receipt.refs is not None else None
    if type(cursor) is not int:
        cursor = None
    return GovernanceGateView(
        request_id=receipt.request_id,
        project_id=_ref(receipt, payload, "project_id"),
        task_id=_ref(receipt, payload, "task_id"),
        attempt_id=_ref(receipt, payload, "attempt_id"),
        history_cursor=cursor,
        vm_blocker_code=code,
        state=state,
        phase=phase,
        reason_code=reason_code,
        check_id=check_id,
        conformance_check_id=conformance_check_id,
        detail=detail,
    )


def project_final_report(receipt: BridgeReceipt) -> tuple[PluginTaskObservation, ...]:
    """Project ``governance_plugin_tasks`` in the order VM reported them."""
    payload = _require_receipt(receipt, Command.FINAL_REPORT)
    if "governance_plugin_tasks" not in payload:
        raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, subject="governance_plugin_tasks")
    entries = payload["governance_plugin_tasks"]

    def invalid() -> VmBridgeError:
        return VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="governance_plugin_tasks")

    if not isinstance(entries, (list, tuple)):
        raise invalid()
    observations = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise invalid()
        task_id = entry.get("task_id")
        changed = entry.get("changed")
        phases = entry.get("changed_phases")
        if type(task_id) is not str or not task_id or type(changed) is not bool:
            raise invalid()
        if not isinstance(phases, (list, tuple)) or any(type(item) is not str for item in phases):
            raise invalid()
        optional = {}
        for name in ("task_ref", "closure_tree_digest", "plugin_version_label"):
            value = entry.get(name)
            if value is not None and type(value) is not str:
                raise invalid()
            optional[name] = value
        observations.append(PluginTaskObservation(
            task_id=task_id,
            task_ref=optional["task_ref"],
            closure_tree_digest=optional["closure_tree_digest"],
            plugin_version_label=optional["plugin_version_label"],
            changed=changed,
            changed_phases=tuple(phases),
        ))
    return tuple(observations)


def project_status_blocked_tasks(receipt: BridgeReceipt) -> tuple[str, ...]:
    """Return blocked Task IDs from ``status`` in their original order; no reasons are added."""
    payload = _require_receipt(receipt, Command.STATUS)
    tasks = payload.get("vm_tasks")

    def invalid() -> VmBridgeError:
        return VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="vm_tasks")

    if not isinstance(tasks, (list, tuple)):
        raise invalid()
    blocked = []
    for task in tasks:
        if not isinstance(task, Mapping):
            raise invalid()
        task_id, status = task.get("task_id"), task.get("status")
        if type(task_id) is not str or not task_id or type(status) is not str:
            raise invalid()
        if status == "blocked":
            blocked.append(task_id)
    return tuple(blocked)


__all__ = [
    "GateState", "GatePhase", "GATE_BLOCKER_CODES", "KNOWN_REASON_CODES", "UNREACHABLE_FACTS",
    "GovernanceGateView", "PluginTaskObservation",
    "project_run_once", "project_final_report", "project_status_blocked_tasks",
]
