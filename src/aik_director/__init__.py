"""AIK-024 director bootstrap, start, stop and health (partial implementation).

The director is started on user-selected resources without any worker
placement. Before start, the package checks the director profile right before
execution (aik_profiles), the runner connection/login probe and an optional
task-scoped credential grant (aik_broker). Single ownership and lifecycle come
from aik_supervisor, and wake signals come from aik_inbox.

Actual model or provider execution is behind the injected DirectorRunner
interface. This package never calls a model itself, never reads secrets and
never wakes the runner for an idle inbox.
"""

from .director import Director, DirectorRunner
from .errors import DirectorError, InvalidDirectorConfig, InvalidRunnerResult
from .models import (
    CHECK_STATUSES, LIFECYCLE_STATES, PROBE_STATUSES, RUNNER_HEALTH, SETUP_ACTIONS, START_STATUSES,
    CheckResult, CredentialScope, DirectorBinding, DirectorConfig, DirectorEvent, Observation,
    ProbeResult, RunnerSession, SetupIssue, StartRequest, StartResult, StopResult, classify,
)

__all__ = [
    "Director", "DirectorRunner", "DirectorConfig", "CredentialScope",
    "DirectorBinding", "ProbeResult", "RunnerSession", "StartRequest", "Observation",
    "SetupIssue", "CheckResult", "StartResult", "StopResult", "DirectorEvent", "classify",
    "LIFECYCLE_STATES", "CHECK_STATUSES", "START_STATUSES", "PROBE_STATUSES", "RUNNER_HEALTH",
    "SETUP_ACTIONS",
    "DirectorError", "InvalidDirectorConfig", "InvalidRunnerResult",
]
