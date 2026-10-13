"""AIK-014 Codex subscription host-auth bridge for the AIK-011 provider contract.

The bridge only *observes* a login that the user completed in the official
Codex host (CLI). It never reads, copies, stores or logs tokens, session files
or keys, and never reuses a subscription login as an API key. Every host CLI
call goes through an injectable runner. Implementation state is
``implemented``; auth, probe and governed qualification all remain ``NOT_RUN``.
The CLI commands and output formats are unverified assumptions.
"""

from .bridge import (
    DEFAULT_PROFILE, DEFAULT_RUNTIME, GUI_COMMANDS, IMPLEMENTATION_STATE, MAX_LOGIN_TIMEOUT,
    NOT_RUN, PROVIDER, BindingState, CodexHostBridge, HostCapability, HostHandle,
    HostPresence, ProtocolSupport,
)
from .protocol import CodexCliProtocol, LoginMethod, LoginState, ParsedStatus
from .runner import (
    ENV_ALLOWLIST, MAX_OUTPUT_LIMIT, MAX_TIMEOUT, SOURCES, HostNotFound, HostRunner,
    OutputLimitExceeded, RunnerError, RunnerTimeout, RunResult, SubprocessRunner,
    build_environment, validate_invocation,
)

__all__ = [
    "CodexHostBridge", "HostCapability", "HostHandle", "HostPresence", "ProtocolSupport",
    "BindingState", "GUI_COMMANDS", "PROVIDER", "DEFAULT_RUNTIME", "DEFAULT_PROFILE",
    "IMPLEMENTATION_STATE", "NOT_RUN", "MAX_LOGIN_TIMEOUT",
    "CodexCliProtocol", "LoginState", "LoginMethod", "ParsedStatus",
    "HostRunner", "SubprocessRunner", "RunResult", "RunnerError", "HostNotFound",
    "RunnerTimeout", "OutputLimitExceeded", "ENV_ALLOWLIST", "MAX_OUTPUT_LIMIT",
    "MAX_TIMEOUT", "SOURCES", "build_environment", "validate_invocation",
]
