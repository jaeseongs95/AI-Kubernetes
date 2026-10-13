"""AIK-015 Claude Code subscription host authentication bridge.

The bridge only *observes* a login that the user completed in the official
Claude Code host (CLI). It never reads, copies, stores or logs tokens,
session files or keys. It never reuses subscription authentication as an API
key. Every external command goes through an injectable runner with an
absolute timeout, an output-size limit and an environment allowlist.

The implementation state is ``implemented``. Route qualification is still
``NOT_RUN``. Host command names and output formats are unverified
assumptions. Live support is judged only by AIK-022/082/086.
"""

from .bridge import (
    DEFAULT_EXEC_TIMEOUT, HANDLE_NAMESPACE, LIVE_EVIDENCE_TASKS, PROVIDER, ROUTE_QUALIFICATION,
    RUNTIME, ClaudeHostBridge, ConnectionCapability, HostCall, HostResult,
)
from .protocol import (
    MIN_HOST_VERSION, MODEL_PLACEHOLDER, AuthMethod, HostCommands, HostState, HostStatus,
    account_fingerprint,
)
from .runner import (
    ENV_ALLOWLIST, MAX_TIMEOUT_SECONDS, SOURCES, CommandResult, CommandRunner, CommandSpec,
    RunnerNotStarted, RunnerTimeout, SubprocessRunner, build_environment,
)

__all__ = [
    "ClaudeHostBridge", "ConnectionCapability", "HostCall", "HostResult", "PROVIDER", "RUNTIME",
    "HANDLE_NAMESPACE", "LIVE_EVIDENCE_TASKS", "ROUTE_QUALIFICATION", "DEFAULT_EXEC_TIMEOUT",
    "AuthMethod", "HostCommands", "HostState", "HostStatus", "MIN_HOST_VERSION",
    "MODEL_PLACEHOLDER", "account_fingerprint",
    "CommandResult", "CommandRunner", "CommandSpec", "RunnerNotStarted", "RunnerTimeout",
    "SubprocessRunner", "build_environment", "ENV_ALLOWLIST", "MAX_TIMEOUT_SECONDS", "SOURCES",
]
