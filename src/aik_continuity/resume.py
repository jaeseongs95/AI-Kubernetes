"""Resume from stored references after re-reading VM; never create a Goal here.

The resumer only calls the read command ``status`` of an injected VM port.
It has no code path to prepare/revise/run_once/replan or any other effectful
VM command. Matching references -> continue with the existing IDs/revisions;
mismatching (including directives stored after the checkpoint) -> stop for
user confirmation; unreadable -> stop and inspect VM.
The VM status payload layout used here is an assumed adapter contract (NOT_RUN).
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
from typing import Mapping, Protocol

from aik_controller.models import VmView, refs
from aik_runtime.models import MAX_INTEGER

from .errors import InvalidContinuityRecord
from .models import StoredCheckpoint, check_id, is_id
from .store import ContinuityStore


RESUME_HANDOFFS = {
    "continue": "continue_existing",
    "confirmation_required": "confirm_with_user",
    "unverifiable": "inspect_vm",
    "unavailable": "retry",
    "no_checkpoint": "start_goal",
}
RESUME_STATUSES = tuple(RESUME_HANDOFFS)
_FINISHED_TASK_STATES = ("done", "cancelled")
_DIRECTIVES_AFTER_CHECKPOINT = ("DIRECTIVES_AFTER_CHECKPOINT", None)


class VmReadPort(Protocol):
    """Read-only VM status for one project; returns the status payload mapping."""

    def status(self, project_id: str) -> Mapping: ...


class BridgeStatusPort:
    """Adapter over aik_vm_bridge VmBridge.status (read-only). NOT_RUN against a real VM."""

    def __init__(self, bridge):
        if not callable(getattr(bridge, "status", None)):
            raise InvalidContinuityRecord("The bridge must provide the VM status command.")
        self._bridge = bridge
        self._run = "cont-" + secrets.token_hex(6)
        self._sequence = 0
        self._lock = threading.Lock()

    def status(self, project_id: str):
        from aik_vm_bridge import ProjectQuery

        with self._lock:
            self._sequence += 1
            request_id = f"{self._run}-{self._sequence:06d}"
        receipt = self._bridge.status(ProjectQuery(request_id, project_id))
        return getattr(receipt, "payload", None)


@dataclass(frozen=True, slots=True)
class ResumeResult:
    """mismatches: (code, subject_id|None) pairs. VM values are read, not stored.

    vm_task_attempts holds (task_id, attempt_id) as VM reported them for the
    checkpoint's tasks; vm_authorization_revision_id is VM's current
    authorization revision when it was readable.
    """

    status: str
    project_id: str
    checkpoint: StoredCheckpoint | None
    mismatches: tuple = ()
    vm_goal_state: str | None = None
    open_task_ids: tuple = ()
    vm_evidence_refs: tuple = ()
    vm_task_attempts: tuple = ()
    vm_authorization_revision_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in RESUME_HANDOFFS:
            raise InvalidContinuityRecord("Unknown resume status.")
        if (self.status == "continue") != (self.checkpoint is not None and not self.mismatches):
            raise InvalidContinuityRecord("Only verified references may continue.")

    @property
    def handoff(self) -> str:
        return RESUME_HANDOFFS[self.status]

    @property
    def continuation(self) -> dict | None:
        """Existing IDs/revisions to continue with; None unless VM agreed.

        Task attempts are VM's observed values: a task without an attempt at
        checkpoint time may have gained one, which must not be recreated.
        """
        if self.status != "continue":
            return None
        checkpoint = self.checkpoint.checkpoint
        observed = dict(self.vm_task_attempts)
        return {
            "project_id": checkpoint.project_id,
            "checkpoint_revision": self.checkpoint.revision,
            "goal_id": checkpoint.goal_id,
            "goal_revision_id": checkpoint.goal_revision_id,
            "plan_revision_id": checkpoint.plan_revision_id,
            "authorization_revision_id": checkpoint.authorization_revision_id,
            "tasks": tuple(
                (task.task_id, observed[task.task_id] if task.task_id in observed else task.attempt_id)
                for task in checkpoint.tasks
            ),
        }


def _pending_directives(checkpoint, latest_directive_seq: object) -> tuple:
    if latest_directive_seq is None:
        return ()
    if type(latest_directive_seq) is not int or not 0 <= latest_directive_seq <= MAX_INTEGER:
        raise InvalidContinuityRecord("The latest directive position must be a nonnegative bounded integer.")
    return (_DIRECTIVES_AFTER_CHECKPOINT,) if latest_directive_seq > checkpoint.directive_seq else ()


def verify_checkpoint(stored: StoredCheckpoint, payload: object, *,
                      latest_directive_seq: int | None = None) -> ResumeResult:
    """Pure comparison of stored references with one VM status payload.

    When latest_directive_seq (the project's highest stored directive seq) is
    given and exceeds the checkpoint's directive_seq, the result cannot
    continue: a later directive or correction has not been reconciled.
    """
    if type(stored) is not StoredCheckpoint:
        raise InvalidContinuityRecord("A stored checkpoint is required.")
    checkpoint = stored.checkpoint
    project = checkpoint.project_id
    pending = _pending_directives(checkpoint, latest_directive_seq)
    if not isinstance(payload, Mapping):
        return ResumeResult("unverifiable", project, stored, (("VM_STATUS_INVALID", None),) + pending)
    view = VmView.from_payload(payload)
    goal_id = payload.get("goal_id")
    goal_revision = payload.get("goal_revision_id")
    authorization = payload.get("authorization_revision_id")
    reported_project = payload.get("project_id")
    goal_evidence = refs(payload.get("evidence"))
    vm_authorization = authorization if is_id(authorization) else None
    unreadable = []
    if view.goal_state == "unknown":
        unreadable.append(("VM_STATE_UNKNOWN", None))
    if not is_id(goal_id):
        unreadable.append(("GOAL_REFERENCE_UNREADABLE", None))
    if not is_id(goal_revision):
        unreadable.append(("GOAL_REVISION_UNREADABLE", None))
    if view.plan_revision_id is None:
        unreadable.append(("PLAN_REVISION_UNREADABLE", None))
    if checkpoint.authorization_revision_id is not None and not is_id(authorization):
        unreadable.append(("AUTHORIZATION_UNREADABLE", None))
    if reported_project is not None and not is_id(reported_project):
        unreadable.append(("PROJECT_REFERENCE_UNREADABLE", None))
    if goal_evidence is None:
        unreadable.append(("EVIDENCE_UNREADABLE", None))
    if unreadable:
        return ResumeResult("unverifiable", project, stored, tuple(unreadable) + pending, view.goal_state,
                            vm_authorization_revision_id=vm_authorization)

    mismatches = list(pending)
    if reported_project is not None and reported_project != project:
        mismatches.append(("PROJECT_MISMATCH", None))
    if goal_id != checkpoint.goal_id:
        mismatches.append(("GOAL_MISMATCH", None))
    elif goal_revision != checkpoint.goal_revision_id:
        mismatches.append(("GOAL_REVISION_CHANGED", None))
    if view.plan_revision_id != checkpoint.plan_revision_id:
        mismatches.append(("PLAN_REVISION_CHANGED", None))
    if checkpoint.authorization_revision_id is not None and authorization != checkpoint.authorization_revision_id:
        mismatches.append(("AUTHORIZATION_CHANGED", None))
    if view.goal_state == "cancelled":
        mismatches.append(("GOAL_CANCELLED", None))
    tasks = {task.task_id: task for task in view.tasks}
    for reference in checkpoint.tasks:
        task = tasks.get(reference.task_id)
        if task is None:
            mismatches.append(("TASK_MISSING", reference.task_id))
            continue
        # A recorded attempt must still be VM's attempt. A task that had no
        # attempt at checkpoint time may have gained one (VM progress).
        if reference.attempt_id is not None and task.attempt_id != reference.attempt_id:
            mismatches.append(("ATTEMPT_CHANGED", reference.task_id))
        if any(item not in task.evidence for item in reference.evidence):
            mismatches.append(("EVIDENCE_MISSING", reference.task_id))
    vm_evidence = list(goal_evidence)
    for task in view.tasks:
        for item in task.evidence:
            if item not in vm_evidence:
                vm_evidence.append(item)
    for item in checkpoint.evidence_refs:
        if item not in vm_evidence:
            mismatches.append(("EVIDENCE_MISSING", item))
    open_ids = tuple(task.task_id for task in view.tasks if task.state not in _FINISHED_TASK_STATES)
    attempts = tuple((reference.task_id, tasks[reference.task_id].attempt_id)
                     for reference in checkpoint.tasks if reference.task_id in tasks)
    status = "confirmation_required" if mismatches else "continue"
    return ResumeResult(status, project, stored, tuple(mismatches), view.goal_state, open_ids,
                        tuple(vm_evidence), attempts, vm_authorization)


class Resumer:
    """Re-read stored references through VM status; never create Goal/Attempt."""

    def __init__(self, store: ContinuityStore, vm_port):
        if not isinstance(store, ContinuityStore):
            raise InvalidContinuityRecord("A ContinuityStore is required.")
        if not callable(getattr(vm_port, "status", None)):
            raise InvalidContinuityRecord("The VM port must provide status(project_id).")
        self._store = store
        self._port = vm_port

    def resume(self, project_id: str) -> ResumeResult:
        check_id(project_id, "project_id")
        # Checkpoint and directive position come from one store transaction.
        stored, latest_seq = self._store.resume_snapshot(project_id)
        if stored is None:
            # Starting a Goal stays an explicit caller/user decision.
            return ResumeResult("no_checkpoint", project_id, None)
        pending = _pending_directives(stored.checkpoint, latest_seq)
        try:
            payload = self._port.status(project_id)
        except Exception:
            return ResumeResult("unavailable", project_id, stored,
                                (("VM_STATUS_UNAVAILABLE", None),) + pending)
        return verify_checkpoint(stored, payload, latest_directive_seq=latest_seq)
