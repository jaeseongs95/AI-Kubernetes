"""AIK-026 director control loop: Goal -> VM command -> delegation -> collection -> report.

This package does not own a DAG, retry or success verdict. Goal, Plan and Task
changes and task readiness stay in VM and go through the public commands of
``aik_vm_bridge``. Tool and command permissions come from ``aik_authority``,
and inbox wake uses the existing ``aik_director.Director.poll`` API.

Only VM final_report, GoalVerdict and evidence can produce Goal completion. A
finished model response (model_reported) is never treated as completion.

This is a partial implementation verified with a fake VM port and fake ports.
Real model, VM installation, Windows and E2E runs are NOT_RUN.
"""

from .authority import GatewayCommandAuthority
from .controller import Controller
from .errors import ControllerError, InvalidControllerConfig
from .models import (
    GOAL_STATES, HANDOFFS, OUTCOMES, TASK_STATES, VERDICT_OUTCOMES, Action, ActionRecord, Decision,
    Memory, RunPolicy, RunReport, TaskView, VmView, interpret_final_report,
)
from .policy import decide
from .ports import EFFECTFUL_COMMANDS, CommandAuthority, DelegationPort, WakeSource

__all__ = [
    "Controller", "decide", "GatewayCommandAuthority",
    "CommandAuthority", "DelegationPort", "WakeSource", "EFFECTFUL_COMMANDS",
    "Action", "ActionRecord", "Decision", "Memory", "RunPolicy", "RunReport", "TaskView", "VmView",
    "interpret_final_report", "GOAL_STATES", "TASK_STATES", "OUTCOMES", "HANDOFFS", "VERDICT_OUTCOMES",
    "ControllerError", "InvalidControllerConfig",
]
