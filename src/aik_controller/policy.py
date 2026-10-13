"""Pure per-tick decision: VM state in, next controller action out.

The function never decides readiness, ordering, retries or success itself.
It only picks which public VM command to send next for the state VM reports.
"""

from __future__ import annotations

from .models import Action, Decision, Memory, RunPolicy, VmView


def decide(view: VmView | None, memory: Memory, policy: RunPolicy) -> Decision:
    if view is None:
        return Decision(Action.OBSERVE, "STATUS_UNAVAILABLE")
    if type(view) is not VmView or type(memory) is not Memory or type(policy) is not RunPolicy:
        raise TypeError("decide requires a VmView, Memory and RunPolicy.")
    goal = view.goal_state
    if goal == "cancelled":
        return Decision(Action.STOP, "VM_CANCELLED", outcome="cancelled")
    if goal == "paused":
        return Decision(Action.STOP, "VM_PAUSED", outcome="paused")
    if goal == "goal_verdict_ready":
        if view.goal_verdict_id is None:
            return Decision(Action.OBSERVE, "VERDICT_REFERENCE_MISSING")
        return Decision(Action.FINAL_REPORT, "GOAL_VERDICT_READY")
    # An unclear effect is observed first; nothing new is launched meanwhile.
    if memory.unresolved:
        return Decision(Action.OBSERVE, "EFFECT_OUTCOME_UNKNOWN")
    if goal == "awaiting_authorization":
        return Decision(Action.STOP, "USER_AUTHORIZATION_REQUIRED", outcome="awaiting_authorization")
    if goal == "blocked":
        return Decision(Action.STOP, view.blocked_reason or "VM_BLOCKED", outcome="blocked")
    if goal == "unknown" or any(task.state == "unknown" for task in view.tasks):
        return Decision(Action.OBSERVE, "VM_STATE_UNKNOWN")
    if goal == "draft":
        return Decision(Action.OBSERVE, "PLAN_NOT_READY")

    failed = [task for task in view.tasks if task.state == "validation_failed"]
    for task in failed:
        if task.failure_id is None or not task.replan_allowed:
            return Decision(Action.STOP, "VALIDATION_FAILED_NO_REWORK", task=task, outcome="validation_failed")
        if task.failure_id in memory.replanned:
            continue
        if memory.replans >= policy.max_replans:
            return Decision(Action.STOP, "REPLAN_LIMIT", task=task, outcome="validation_failed")
        return Decision(Action.REPLAN, "VALIDATION_FAILED", task=task)

    for task in view.tasks:
        if task.state == "ready" and task.attempt_id is not None and task.task_id not in memory.unresolved:
            return Decision(Action.RUN_ONCE, "TASK_READY", task=task)
    if any(task.state in ("running", "validating") for task in view.tasks):
        return Decision(Action.OBSERVE, "WORK_IN_PROGRESS")
    if goal == "goal_test":
        return Decision(Action.OBSERVE, "GOAL_TEST_IN_PROGRESS")
    blocked = [task for task in view.tasks if task.state == "blocked"]
    if blocked:
        return Decision(Action.STOP, blocked[0].blocked_reason or "TASK_BLOCKED", task=blocked[0], outcome="blocked")
    if failed:
        return Decision(Action.OBSERVE, "REWORK_REQUESTED")
    return Decision(Action.OBSERVE, "AWAITING_VM")
