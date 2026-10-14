"""Controller records: VM status views, decisions, bounded run policy, reports.

The VM status payload layout parsed here (``goal_state``, ``tasks`` ...) is an
assumed adapter contract, not checked against an installed VM (NOT_RUN).
Anything unrecognized becomes ``unknown`` and never enables an effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math
import re
from typing import Any, Mapping

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .errors import InvalidControllerConfig


GOAL_STATES = (
    "draft", "awaiting_authorization", "active", "goal_test", "goal_verdict_ready",
    "blocked", "paused", "cancelled", "unknown",
)
TASK_STATES = (
    "pending", "ready", "running", "validating", "validation_failed", "blocked", "done",
    "cancelled", "unknown",
)
VERDICT_OUTCOMES = ("passed", "failed")
# outcome -> next user/caller action. Only "completed" means Goal completion.
HANDOFFS = {
    "completed": "review_result",
    "goal_failed": "review_result",
    "report_unverified": "inspect_report",
    "awaiting_authorization": "authorize",
    "blocked": "resolve_blocker",
    "validation_failed": "review_failure",
    "paused": "resume",
    "cancelled": "review_result",
    "authority_denied": "configure_authority",
    "escalation_required": "request_authorization",
    "reconciliation_required": "reconcile",
    "vm_rejected": "inspect_vm",
    "delegation_failed": "check_capacity",
    "no_progress": "resume",
    "deadline_exceeded": "resume",
    "max_ticks": "resume",
    "clock_unavailable": "retry",
}
OUTCOMES = tuple(HANDOFFS)
MAX_TASKS = 4096
MAX_REFS = 1024
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def is_identifier(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def safe_code(value: object, default: str = "UNCLASSIFIED") -> str:
    return value if type(value) is str and _CODE.fullmatch(value) is not None else default


def refs(value: object) -> tuple | None:
    """A bounded tuple of identifiers, () for None, or None when invalid."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REFS:
        return None
    if not all(is_identifier(item) for item in value):
        return None
    return tuple(value)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class TaskView:
    """VM task snapshot. inputs/evidence are VM references copied verbatim."""

    task_id: str
    state: str
    attempt_id: str | None = None
    inputs: tuple = ()
    evidence: tuple = ()
    failure_id: str | None = None
    replan_allowed: bool = False
    blocked_reason: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> "TaskView | None":
        if not isinstance(value, Mapping) or not is_identifier(value.get("task_id")):
            return None
        state = value.get("state")
        if type(state) is not str or state not in TASK_STATES:
            state = "unknown"
        attempt, failure = value.get("attempt_id"), value.get("failure_id")
        for item in (attempt, failure):
            if item is not None and not is_identifier(item):
                return None
        inputs, evidence = refs(value.get("inputs")), refs(value.get("evidence"))
        if inputs is None or evidence is None:
            return None
        reason = value.get("blocked_reason")
        return cls(value["task_id"], state, attempt, inputs, evidence, failure,
                   value.get("replan_allowed") is True, None if reason is None else safe_code(reason))


@dataclass(frozen=True, slots=True)
class VmView:
    """Controller-side reading of one VM status receipt (VM stays the authority)."""

    goal_state: str
    tasks: tuple = ()
    goal_verdict_id: str | None = None
    blocked_reason: str | None = None
    history_cursor: int | None = None
    plan_revision_id: str | None = None

    @property
    def fingerprint(self) -> tuple:
        return (
            self.goal_state, self.goal_verdict_id, self.history_cursor, self.plan_revision_id,
            tuple((task.task_id, task.attempt_id, task.state, task.failure_id) for task in self.tasks),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "VmView":
        unknown = cls("unknown")
        if not isinstance(payload, Mapping):
            return unknown
        goal = payload.get("goal_state")
        if type(goal) is not str or goal not in GOAL_STATES:
            goal = "unknown"
        raw = payload.get("tasks", ())
        if raw is None:
            raw = ()
        if not isinstance(raw, (list, tuple)) or len(raw) > MAX_TASKS:
            return unknown
        tasks, seen = [], set()
        for item in raw:
            task = TaskView.from_mapping(item)
            if task is None or task.task_id in seen:
                return unknown
            seen.add(task.task_id)
            tasks.append(task)
        verdict, plan = payload.get("goal_verdict_id"), payload.get("plan_revision_id")
        for item in (verdict, plan):
            if item is not None and not is_identifier(item):
                return unknown
        cursor = payload.get("history_cursor")
        if type(cursor) is not int or cursor < 0:
            cursor = None
        reason = payload.get("blocked_reason")
        return cls(goal, tuple(tasks), verdict, None if reason is None else safe_code(reason), cursor, plan)


class Action(StrEnum):
    RUN_ONCE = "run_once"
    OBSERVE = "observe"
    REPLAN = "replan"
    FINAL_REPORT = "final_report"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    task: TaskView | None = None
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class Memory:
    """What the controller already requested; not a DAG and not a retry ledger."""

    unresolved: frozenset = frozenset()
    replanned: frozenset = frozenset()
    replans: int = 0


@dataclass(frozen=True, slots=True)
class RunPolicy:
    """Bounds of one run(); reaching a bound returns control, it is not a Goal verdict.

    The replan count restarts with each run(); a failure already sent to VM is
    never requested again by the same controller instance.
    """

    max_ticks: int = 100
    max_no_progress: int = 10
    max_replans: int = 3
    poll_interval_seconds: float = 1.0
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        for name, low, high in (("max_ticks", 1, 10000), ("max_no_progress", 1, 1000), ("max_replans", 0, 100)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise InvalidControllerConfig(f"Invalid run policy field: {name}.")
        interval = self.poll_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) \
                or not 0 <= interval <= 3600:
            raise InvalidControllerConfig("Invalid run policy field: poll_interval_seconds.")
        if self.deadline is not None and not _aware(self.deadline):
            raise InvalidControllerConfig("The run deadline must be a timezone-aware datetime.")


@dataclass(frozen=True, slots=True)
class ActionRecord:
    tick: int
    action: str
    request_id: str | None
    result: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class GoalClassificationNote:
    """Outcome of the optional classifier step for one prepare/revise request.

    The classification is advisory and never changes the goal text, the VM
    command or the skill choice. ``classification_id`` and ``status`` are set
    when the classifier returned a record; ``error_code`` when it raised.
    """

    request_id: str
    command: str
    classification_id: str | None
    status: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class RunReport:
    """goal_complete is True only for a VM GoalVerdict 'passed' with evidence."""

    outcome: str
    goal_complete: bool
    project_id: str
    reason: str
    handoff: str
    ticks: int
    vm_goal_state: str | None
    goal_verdict_id: str | None
    verdict_outcome: str | None
    evidence_refs: tuple
    deliverable_refs: tuple
    task_states: tuple
    model_reported: bool
    unresolved_task_ids: tuple
    vm_code: str | None
    actions: tuple

    def __post_init__(self) -> None:
        if self.outcome not in HANDOFFS or self.handoff != HANDOFFS[self.outcome]:
            raise InvalidControllerConfig("Unknown controller outcome.")
        verified = (self.outcome == "completed" and self.verdict_outcome == "passed"
                    and self.goal_verdict_id is not None and bool(self.evidence_refs))
        if self.goal_complete is not verified:
            raise InvalidControllerConfig("Goal completion requires a VM verdict with evidence.")


def interpret_final_report(payload: object, expected_verdict_id: str | None) -> tuple:
    """(outcome, verdict_outcome, evidence, deliverables, reason) from a VM final report.

    Only a matching GoalVerdict with a recognized outcome, nonempty evidence and
    well-formed deliverable references is accepted; everything else is
    report_unverified (not completion).
    """
    if not isinstance(payload, Mapping):
        return "report_unverified", None, (), (), "REPORT_INVALID"
    evidence = refs(payload.get("evidence"))
    deliverables = refs(payload.get("deliverables"))
    listed = deliverables or ()
    verdict: Any = payload.get("goal_verdict")
    if not isinstance(verdict, Mapping):
        return "report_unverified", None, evidence or (), listed, "GOAL_VERDICT_MISSING"
    if expected_verdict_id is None or verdict.get("verdict_id") != expected_verdict_id:
        return "report_unverified", None, evidence or (), listed, "GOAL_VERDICT_MISMATCH"
    outcome = verdict.get("outcome")
    if type(outcome) is not str or outcome not in VERDICT_OUTCOMES:
        return "report_unverified", None, evidence or (), listed, "GOAL_VERDICT_UNRECOGNIZED"
    if not evidence:
        return "report_unverified", None, (), listed, "EVIDENCE_MISSING"
    if deliverables is None:
        # A malformed deliverable list must not silently become "no deliverables".
        return "report_unverified", None, evidence, (), "DELIVERABLES_INVALID"
    return ("completed" if outcome == "passed" else "goal_failed"), outcome, evidence, deliverables, "VM_GOAL_VERDICT"
