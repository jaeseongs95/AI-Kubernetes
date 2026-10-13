"""Injected ports. Fakes implement these in tests; real adapters are NOT_RUN."""

from __future__ import annotations

from typing import Protocol

from aik_vm_bridge import Command, RunOnceRequest

from .models import TaskView


# Commands that change VM state and therefore require an authority check.
# AUTHORIZE is excluded: user approval is never issued by the director.
EFFECTFUL_COMMANDS = frozenset({
    Command.PREPARE, Command.REVISE, Command.RUN_ONCE, Command.REPLAN, Command.PAUSE, Command.CANCEL,
})


class DelegationPort(Protocol):
    """Builds the bound run request (binding/placement/lease) for a VM-ready task.

    Returns None when no capacity is available. ``release`` is optional and is
    called when the request was certainly not sent to VM.
    """

    def prepare_run(self, project_id: str, task: TaskView, request_id: str) -> RunOnceRequest | None: ...


class CommandAuthority(Protocol):
    """Raises aik_authority.AuthorityDenied / EffectOutcomeUnknown; returns on permission."""

    def check(self, command: Command, *, project_id: str, request_id: str, task_id: str | None = None) -> None: ...


class WakeSource(Protocol):
    """aik_director.Director.poll(): wake the runner only for leasable inbox work."""

    def poll(self): ...
