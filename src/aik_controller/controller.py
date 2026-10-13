"""Bounded director loop over VM public commands.

One tick: wake poll -> VM status -> pure decide() -> one VM command. The loop
stops at a STOP decision, a VM final report, max ticks, an absolute deadline or
too many ticks without VM-visible progress. Unclear effects are never resent;
they are observed until VM shows their state. Guards are in memory only.

run() holds the controller lock for its whole duration: pause()/cancel() from
another thread wait until run() returns. Users pause or cancel via GUI -> VM;
the loop stops on the next tick when VM reports paused/cancelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import threading
import time

from aik_authority import AuthorityError, EffectOutcomeUnknown
from aik_vm_bridge import (
    CancelRequest, Command, EffectOutcome, FinalReportRequest, GoalRequest, JobPhase, ObserveRequest,
    PauseRequest, ProjectQuery, ReplanRequest, RunOnceRequest, VmBridgeError,
)

from .errors import InvalidControllerConfig
from .models import (
    HANDOFFS, Action, ActionRecord, Decision, Memory, RunPolicy, RunReport, TaskView, VmView,
    interpret_final_report, is_identifier, safe_code,
)
from .policy import decide


_BRIDGE_METHODS = ("prepare", "revise", "run_once", "observe", "pause", "cancel", "status", "replan", "final_report")
_CONTINUE = "continue"
_WAIT = "wait"
_UNSET = object()


@dataclass(frozen=True, slots=True)
class _Stop:
    outcome: str
    reason: str
    vm_code: str | None = None


def _bridge_code(error: Exception) -> str:
    if isinstance(error, VmBridgeError):
        return safe_code(error.vm_code, error.code.value) if error.vm_code else error.code.value
    return "UNCLASSIFIED"


class Controller:
    """Director control loop for one VM project; it owns no task authority."""

    def __init__(self, bridge, *, project_id: str, delegation, authority, wake=None,
                 policy: RunPolicy | None = None, clock=None, sleep=None, run_id: str | None = None):
        if any(not callable(getattr(bridge, name, None)) for name in _BRIDGE_METHODS):
            raise InvalidControllerConfig("The bridge does not provide the VM public commands.")
        if not callable(getattr(delegation, "prepare_run", None)):
            raise InvalidControllerConfig("A delegation port with prepare_run is required.")
        if not callable(getattr(authority, "check", None)):
            raise InvalidControllerConfig("A command authority with check is required.")
        if wake is not None and not callable(getattr(wake, "poll", None)):
            raise InvalidControllerConfig("The wake source must provide poll().")
        if policy is None:
            policy = RunPolicy()
        if type(policy) is not RunPolicy:
            raise InvalidControllerConfig("The policy must be a RunPolicy.")
        if (clock is not None and not callable(clock)) or (sleep is not None and not callable(sleep)):
            raise InvalidControllerConfig("clock and sleep must be callables.")
        if not is_identifier(project_id):
            raise InvalidControllerConfig("Invalid VM project identifier.")
        run_id = "ctl-" + secrets.token_hex(6) if run_id is None else run_id
        if not is_identifier(run_id) or len(run_id) > 96:
            raise InvalidControllerConfig("Invalid run identifier.")
        self.project_id = project_id
        self.policy = policy
        self.run_id = run_id
        self._bridge = bridge
        self._delegation = delegation
        self._authority = authority
        self._wake = wake
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._sleep = sleep if sleep is not None else time.sleep
        self._lock = threading.RLock()
        self._sequence = 0
        self._unresolved: dict[str, str | None] = {}
        # task_id -> (failure_id, plan_revision_id) of a replan whose effect is unclear.
        self._unresolved_replans: dict[str, tuple] = {}
        self._replanned: set[str] = set()
        self._replans = 0
        self._model_reported = False
        self.wake_failures = 0

    # ------------------------------------------------------------ helpers
    def _next_id(self) -> str:
        self._sequence += 1
        return f"{self.run_id}-{self._sequence:06d}"

    def _now(self) -> datetime | None:
        try:
            value = self._clock()
        except Exception:
            return None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            return None
        return value

    def _unresolved_ids(self) -> frozenset:
        return frozenset(self._unresolved) | frozenset(self._unresolved_replans)

    def _memory(self) -> Memory:
        return Memory(self._unresolved_ids(), frozenset(self._replanned), self._replans)

    def _poll_wake(self, tick: int, trace: list) -> None:
        if self._wake is None:
            return
        try:
            self._wake.poll()
        except Exception:
            self.wake_failures += 1
            trace.append(ActionRecord(tick, "wake", None, "WAKE_FAILED"))

    def _pause(self, deadline: datetime | None) -> None:
        interval = self.policy.poll_interval_seconds
        if interval <= 0:
            return
        if deadline is not None:
            now = self._now()
            if now is None:
                return
            remaining = (deadline - now).total_seconds()
            if remaining <= 0:
                return
            interval = min(interval, remaining)
        try:
            self._sleep(interval)
        except Exception:
            pass

    def _release(self, request) -> None:
        release = getattr(self._delegation, "release", None)
        if callable(release):
            try:
                release(request)
            except Exception:
                pass

    def _report(self, outcome: str, reason: str, view: VmView | None, ticks: int, trace: list, *,
                verdict_outcome=None, evidence=(), deliverables=(), vm_code=None, goal_complete=False) -> RunReport:
        return RunReport(
            outcome=outcome, goal_complete=goal_complete, project_id=self.project_id, reason=safe_code(reason),
            handoff=HANDOFFS[outcome], ticks=ticks,
            vm_goal_state=None if view is None else view.goal_state,
            goal_verdict_id=None if view is None else view.goal_verdict_id,
            verdict_outcome=verdict_outcome, evidence_refs=tuple(evidence), deliverable_refs=tuple(deliverables),
            task_states=() if view is None else tuple((task.task_id, task.state) for task in view.tasks),
            model_reported=self._model_reported, unresolved_task_ids=tuple(sorted(self._unresolved_ids())),
            vm_code=None if vm_code is None else safe_code(vm_code), actions=tuple(trace),
        )

    def _effect(self, command: Command, request, *, task_id: str | None = None):
        """Authority check, then the VM command. Errors propagate to the caller."""
        self._authority.check(command, project_id=self.project_id, request_id=request.request_id, task_id=task_id)
        return getattr(self._bridge, command.value)(request)

    # ---------------------------------------------------- caller commands
    def start_goal(self, goal_text: str, candidate_count: int | None = None):
        """Send the natural-language goal to VM prepare (VM creates Goal/Plan/Task)."""
        with self._lock:
            request = GoalRequest(self._next_id(), self.project_id, goal_text, candidate_count)
            return self._effect(Command.PREPARE, request)

    def revise_goal(self, goal_text: str, candidate_count: int | None = None):
        with self._lock:
            request = GoalRequest(self._next_id(), self.project_id, goal_text, candidate_count)
            return self._effect(Command.REVISE, request)

    def pause(self, reason: str):
        """Blocks while run() is active; use the GUI -> VM path to pause a running loop."""
        with self._lock:
            return self._effect(Command.PAUSE, PauseRequest(self._next_id(), self.project_id, reason))

    def cancel(self, reason: str):
        """Blocks while run() is active; use the GUI -> VM path to cancel a running loop."""
        with self._lock:
            return self._effect(Command.CANCEL, CancelRequest(self._next_id(), self.project_id, reason))

    def note_model_reported(self) -> None:
        """The director model ended its response. This is never Goal completion."""
        with self._lock:
            self._model_reported = True

    @property
    def unresolved_task_ids(self) -> tuple:
        with self._lock:
            return tuple(sorted(self._unresolved_ids()))

    # --------------------------------------------------------------- loop
    def run(self, *, deadline: datetime | None = None) -> RunReport:
        with self._lock:
            policy = self.policy
            if deadline is None:
                deadline = policy.deadline
            elif not isinstance(deadline, datetime) or deadline.utcoffset() is None:
                raise InvalidControllerConfig("The run deadline must be a timezone-aware datetime.")
            # max_replans bounds one run(); a failure already sent stays in _replanned.
            self._replans = 0
            trace: list = []
            view: VmView | None = None
            last = _UNSET
            no_progress = 0
            ticks = 0
            for tick in range(1, policy.max_ticks + 1):
                if deadline is not None:
                    now = self._now()
                    if now is None:
                        return self._report("clock_unavailable", "CLOCK_UNAVAILABLE", view, ticks, trace)
                    if now >= deadline:
                        return self._report("deadline_exceeded", "DEADLINE_EXCEEDED", view, ticks, trace)
                ticks = tick
                self._poll_wake(tick, trace)
                view = self._status(tick, trace)
                self._resolve(view)
                decision = decide(view, self._memory(), policy)
                if decision.action is Action.STOP:
                    trace.append(ActionRecord(tick, "stop", None, safe_code(decision.reason),
                                              None if decision.task is None else decision.task.task_id))
                    return self._report(decision.outcome, decision.reason, view, ticks, trace)
                result = self._execute(decision, tick, view, trace)
                if isinstance(result, RunReport):
                    return result
                if isinstance(result, _Stop):
                    return self._report(result.outcome, result.reason, view, ticks, trace, vm_code=result.vm_code)
                fingerprint = None if view is None else view.fingerprint
                if fingerprint is not None and fingerprint != last:
                    no_progress = 0
                else:
                    no_progress += 1
                last = fingerprint
                if no_progress >= policy.max_no_progress:
                    unresolved = self._unresolved or self._unresolved_replans
                    outcome = "reconciliation_required" if unresolved else "no_progress"
                    return self._report(outcome, "NO_PROGRESS_LIMIT", view, ticks, trace)
                if result == _WAIT:
                    self._pause(deadline)
            return self._report("max_ticks", "MAX_TICKS", view, ticks, trace)

    def _status(self, tick: int, trace: list) -> VmView | None:
        request_id = self._next_id()
        try:
            receipt = self._bridge.status(ProjectQuery(request_id, self.project_id))
            view = VmView.from_payload(receipt.payload)
        except VmBridgeError as error:
            trace.append(ActionRecord(tick, "status", request_id, _bridge_code(error)))
            return None
        except Exception:
            trace.append(ActionRecord(tick, "status", request_id, "STATUS_FAILED"))
            return None
        trace.append(ActionRecord(tick, "status", request_id, view.goal_state))
        return view

    def _resolve(self, view: VmView | None) -> None:
        """Clear an unclear effect only when VM shows a definite later state."""
        if view is None or view.goal_state == "unknown":
            return
        tasks = {task.task_id: task for task in view.tasks}
        for task_id, attempt in list(self._unresolved.items()):
            task = tasks.get(task_id)
            if task is None:
                continue
            if task.attempt_id != attempt or task.state not in ("ready", "unknown"):
                del self._unresolved[task_id]
        for task_id, (failure_id, plan) in list(self._unresolved_replans.items()):
            task = tasks.get(task_id)
            if task is not None and task.state == "unknown":
                continue
            changed_plan = view.plan_revision_id is not None and view.plan_revision_id != plan
            changed_task = task is not None and (task.state != "validation_failed" or task.failure_id != failure_id)
            if changed_plan or changed_task:
                del self._unresolved_replans[task_id]

    def _execute(self, decision: Decision, tick: int, view: VmView, trace: list):
        if decision.action is Action.OBSERVE:
            return self._observe(decision, tick, trace)
        if decision.action is Action.RUN_ONCE:
            return self._run_task(decision.task, tick, trace)
        if decision.action is Action.REPLAN:
            return self._replan(decision.task, tick, trace, view)
        if decision.action is Action.FINAL_REPORT:
            return self._final_report(view, tick, trace)
        raise InvalidControllerConfig("Unknown controller action.")

    def _observe(self, decision: Decision, tick: int, trace: list):
        request_id = self._next_id()
        try:
            self._bridge.observe(ObserveRequest(request_id, self.project_id))
            result = safe_code(decision.reason)
        except VmBridgeError as error:
            result = _bridge_code(error)
        except Exception:
            result = "OBSERVE_FAILED"
        trace.append(ActionRecord(tick, "observe", request_id, result))
        return _WAIT

    def _run_task(self, task: TaskView, tick: int, trace: list):
        request_id = self._next_id()
        record = lambda result: trace.append(ActionRecord(tick, "run_once", request_id, result, task.task_id))
        try:
            request = self._delegation.prepare_run(self.project_id, task, request_id)
        except Exception:
            record("DELEGATION_FAILED")
            return _Stop("delegation_failed", "DELEGATION_FAILED")
        if request is None:
            record("NO_CAPACITY")
            return _WAIT
        if type(request) is not RunOnceRequest or request.request_id != request_id \
                or request.binding.vm_project_id != self.project_id \
                or request.binding.vm_task_id != task.task_id or request.binding.vm_attempt_id != task.attempt_id:
            if type(request) is RunOnceRequest:
                # Certainly not sent to VM: return its placement/lease reservation.
                self._release(request)
            record("DELEGATION_MISMATCH")
            return _Stop("delegation_failed", "DELEGATION_MISMATCH")
        try:
            self._authority.check(Command.RUN_ONCE, project_id=self.project_id, request_id=request_id,
                                  task_id=task.task_id)
        except EffectOutcomeUnknown as error:
            self._unresolved[task.task_id] = task.attempt_id
            record(error.code.value)
            return _CONTINUE
        except AuthorityError as error:
            self._release(request)
            record(error.code.value)
            return _Stop("escalation_required" if error.escalation else "authority_denied", error.code.value)
        except Exception:
            self._release(request)
            record("AUTHORITY_UNAVAILABLE")
            return _Stop("authority_denied", "AUTHORITY_UNAVAILABLE")
        try:
            receipt = self._bridge.run_once(request)
        except VmBridgeError as error:
            code = _bridge_code(error)
            record(code)
            if error.outcome is EffectOutcome.UNKNOWN:
                self._unresolved[task.task_id] = task.attempt_id
                return _CONTINUE
            self._release(request)
            return _Stop("vm_rejected", code, error.vm_code)
        except Exception:
            self._unresolved[task.task_id] = task.attempt_id
            record("OUTCOME_UNKNOWN")
            return _CONTINUE
        job = getattr(receipt, "job", None)
        if job is None or job.phase is JobPhase.UNKNOWN:
            self._unresolved[task.task_id] = task.attempt_id
            record("JOB_STATE_UNKNOWN")
        else:
            record("RUN_REQUESTED")
        return _CONTINUE

    def _mark_replan(self, task: TaskView, view: VmView | None, *, unresolved: bool) -> None:
        self._replanned.add(task.failure_id)
        self._replans += 1
        if unresolved:
            plan = None if view is None else view.plan_revision_id
            self._unresolved_replans[task.task_id] = (task.failure_id, plan)

    def _replan(self, task: TaskView, tick: int, trace: list, view: VmView | None = None):
        request_id = self._next_id()
        record = lambda result: trace.append(ActionRecord(tick, "replan", request_id, result, task.task_id))
        rationale = (f"Independent validation failed for VM task {task.task_id} "
                     f"(failure {task.failure_id}); requesting VM rework.")
        request = ReplanRequest(request_id, self.project_id, rationale)
        # Authority first: a definite denial leaves no mark, so a later run() can request it.
        try:
            self._authority.check(Command.REPLAN, project_id=self.project_id, request_id=request_id,
                                  task_id=task.task_id)
        except EffectOutcomeUnknown as error:
            # The intent may be recorded; never resend for this failure, observe first.
            self._mark_replan(task, view, unresolved=True)
            record(error.code.value)
            return _CONTINUE
        except AuthorityError as error:
            record(error.code.value)
            return _Stop("escalation_required" if error.escalation else "authority_denied", error.code.value)
        except Exception:
            record("AUTHORITY_UNAVAILABLE")
            return _Stop("authority_denied", "AUTHORITY_UNAVAILABLE")
        # Mark before sending: an unclear replan is never resent for the same failure.
        self._mark_replan(task, view, unresolved=False)
        try:
            self._bridge.replan(request)
        except VmBridgeError as error:
            code = _bridge_code(error)
            record(code)
            if error.outcome is EffectOutcome.UNKNOWN:
                self._mark_replan_unresolved(task, view)
                return _CONTINUE
            # VM definitely did not apply it: undo the mark so it can be requested again.
            self._replanned.discard(task.failure_id)
            self._replans = max(0, self._replans - 1)
            return _Stop("vm_rejected", code, error.vm_code)
        except Exception:
            self._mark_replan_unresolved(task, view)
            record("OUTCOME_UNKNOWN")
            return _CONTINUE
        record("REPLAN_REQUESTED")
        return _CONTINUE

    def _mark_replan_unresolved(self, task: TaskView, view: VmView | None) -> None:
        plan = None if view is None else view.plan_revision_id
        self._unresolved_replans[task.task_id] = (task.failure_id, plan)

    def _final_report(self, view: VmView, tick: int, trace: list):
        request_id = self._next_id()
        try:
            receipt = self._bridge.final_report(FinalReportRequest(request_id, self.project_id, view.goal_verdict_id))
        except VmBridgeError as error:
            trace.append(ActionRecord(tick, "final_report", request_id, _bridge_code(error)))
            return _WAIT
        except Exception:
            trace.append(ActionRecord(tick, "final_report", request_id, "REPORT_FAILED"))
            return _WAIT
        outcome, verdict, evidence, deliverables, reason = interpret_final_report(receipt.payload, view.goal_verdict_id)
        trace.append(ActionRecord(tick, "final_report", request_id, reason))
        return self._report(outcome, reason, view, tick, trace, verdict_outcome=verdict, evidence=evidence,
                            deliverables=deliverables, goal_complete=outcome == "completed")
