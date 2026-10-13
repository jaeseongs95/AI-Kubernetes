"""Fixed tool list, effect classification and the trusted verification policy.

Verification commands are defined only here, by the trusted backend. A worker
selects a ``command_id``; it never supplies argv, cwd, environment, timeout or
output limits, and it cannot edit pinned oracle files through these tools.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import ntpath
import os
from types import MappingProxyType

from aik_authority import EffectKind, ToolDeclaration
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier
from aik_vm_bridge.binding import DIGEST_PATTERN
from aik_workspace import InvalidInputPath, normalize_input_path

from .errors import DevToolCode as D, InvalidDevToolPolicy


TOOL_READ_FILE = "dev.read_file"
TOOL_CREATE_FILE = "dev.create_file"
TOOL_UPDATE_FILE = "dev.update_file"
TOOL_RUN_VERIFICATION = "dev.run_verification"

EFFECT_KINDS = MappingProxyType({
    TOOL_READ_FILE: EffectKind.READ_ONLY,
    # Create/update are idempotent at the level of the target bytes: a resend
    # observes the file digest and never writes a second time.
    TOOL_CREATE_FILE: EffectKind.IDEMPOTENT,
    TOOL_UPDATE_FILE: EffectKind.IDEMPOTENT,
    # Running a command can have arbitrary side effects; never re-run it.
    TOOL_RUN_VERIFICATION: EffectKind.NON_IDEMPOTENT,
})


@dataclass(frozen=True, slots=True)
class EffectContract:
    tool_id: str
    effect_kind: EffectKind
    resend: str
    interrupted: str
    cancel: str


EFFECT_CONTRACTS = MappingProxyType({
    TOOL_READ_FILE: EffectContract(TOOL_READ_FILE, EffectKind.READ_ONLY, "stored_result",
                                   "reauthorize_and_reread", "no_effect"),
    TOOL_CREATE_FILE: EffectContract(TOOL_CREATE_FILE, EffectKind.IDEMPOTENT, "stored_result",
                                     "observe_file_digest", "before_effect_only"),
    TOOL_UPDATE_FILE: EffectContract(TOOL_UPDATE_FILE, EffectKind.IDEMPOTENT, "stored_result",
                                     "observe_file_digest", "before_effect_only"),
    TOOL_RUN_VERIFICATION: EffectContract(TOOL_RUN_VERIFICATION, EffectKind.NON_IDEMPOTENT, "stored_result",
                                          "unknown_never_rerun", "kill_process_group"),
})

# Shells and script launchers are not supported capabilities: commands run with
# shell=False from an absolute executable and fixed argv.
SHELL_EXECUTABLES = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash", "busybox", "env",
    "cmd", "powershell", "pwsh", "wsl", "bash.exe", "start",
})
_SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf"})
UNSUPPORTED_CAPABILITIES = (
    "shell", "arbitrary_command", "worker_supplied_arguments", "environment_override", "cwd_override",
    "delete", "rename", "network_isolation", "os_sandbox",
)
MAX_ARGV = 64
MAX_ARGUMENT_CHARS = 32768
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 3600.0
MAX_FILE_BYTES = 64 * 1024 * 1024


def tool_declarations() -> tuple:
    """aik_authority declarations for building a gateway ToolPolicy."""
    return tuple(ToolDeclaration(tool_id, kind) for tool_id, kind in EFFECT_KINDS.items())


def _policy_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise InvalidDevToolPolicy(subject=field) from None


def _policy_path(value: object, field: str) -> str:
    try:
        if normalize_input_path(value) != value:
            raise InvalidInputPath("normalized path differs")
    except InvalidInputPath:
        raise InvalidDevToolPolicy(subject=field) from None
    return value


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """One fixed verification command; cwd is always the workspace root."""

    command_id: str
    argv: tuple
    timeout_seconds: float = 60.0
    max_output_bytes: int = 65536
    oracle_files: tuple = ()

    def __post_init__(self) -> None:
        _policy_id(self.command_id, "command_id")
        if type(self.argv) is not tuple or not 1 <= len(self.argv) <= MAX_ARGV:
            raise InvalidDevToolPolicy(subject="argv")
        for argument in self.argv:
            if type(argument) is not str or len(argument) > MAX_ARGUMENT_CHARS or "\x00" in argument:
                raise InvalidDevToolPolicy(subject="argv")
        executable = self.argv[0]
        if not executable or not os.path.isabs(executable):
            # No PATH lookup: the executable identity is fixed by the policy.
            raise InvalidDevToolPolicy(subject="argv")
        name = ntpath.basename(executable).casefold()
        stem, extension = os.path.splitext(name)
        if name in SHELL_EXECUTABLES or stem in SHELL_EXECUTABLES or extension in _SCRIPT_EXTENSIONS:
            raise InvalidDevToolPolicy(D.UNSUPPORTED_CAPABILITY, subject="argv")
        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise InvalidDevToolPolicy(subject="timeout_seconds")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= MAX_OUTPUT_BYTES:
            raise InvalidDevToolPolicy(subject="max_output_bytes")
        if type(self.oracle_files) is not tuple:
            raise InvalidDevToolPolicy(subject="oracle_files")
        seen = set()
        for entry in self.oracle_files:
            if type(entry) is not tuple or len(entry) != 2:
                raise InvalidDevToolPolicy(subject="oracle_files")
            path, digest = entry
            _policy_path(path, "oracle_files")
            if type(digest) is not str or DIGEST_PATTERN.fullmatch(digest) is None:
                raise InvalidDevToolPolicy(subject="oracle_files")
            if path.casefold() in seen:
                raise InvalidDevToolPolicy(subject="oracle_files")
            seen.add(path.casefold())

    def definition_digest(self) -> str:
        payload = {
            "format": "aik.devtools.verification.v1", "command_id": self.command_id, "argv": list(self.argv),
            "timeout_seconds": float(self.timeout_seconds), "max_output_bytes": self.max_output_bytes,
            "oracle_files": [list(entry) for entry in self.oracle_files],
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class DevToolPolicy:
    """Trusted per-workspace limits; ``analysis_only_paths`` are readable but never writable."""

    workspace_id: str
    verifications: tuple = ()
    analysis_only_paths: tuple = ()
    max_file_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        _policy_id(self.workspace_id, "workspace_id")
        if type(self.verifications) is not tuple or len(self.verifications) > 256:
            raise InvalidDevToolPolicy(subject="verifications")
        ids = set()
        for command in self.verifications:
            if type(command) is not VerificationCommand or command.command_id in ids:
                raise InvalidDevToolPolicy(subject="verifications")
            ids.add(command.command_id)
        if type(self.analysis_only_paths) is not tuple or len(self.analysis_only_paths) > 256:
            raise InvalidDevToolPolicy(subject="analysis_only_paths")
        for path in self.analysis_only_paths:
            _policy_path(path, "analysis_only_paths")
        if type(self.max_file_bytes) is not int or not 1 <= self.max_file_bytes <= MAX_FILE_BYTES:
            raise InvalidDevToolPolicy(subject="max_file_bytes")

    def verification(self, command_id: object) -> VerificationCommand | None:
        for command in self.verifications:
            if command.command_id == command_id:
                return command
        return None

    def protected_paths(self) -> tuple:
        oracles = (path for command in self.verifications for path, _ in command.oracle_files)
        return tuple(dict.fromkeys((*self.analysis_only_paths, *oracles)))

    def is_protected(self, relative: str) -> bool:
        # Case-folded on every OS so a case variant cannot bypass the guard.
        folded = relative.casefold()
        return any(folded == path.casefold() or folded.startswith(path.casefold() + "/")
                   for path in self.protected_paths())


def rebuild_policy(policy: object) -> DevToolPolicy:
    """Reconstruct, so object.__setattr__ forgeries are validated again."""
    if type(policy) is not DevToolPolicy or type(policy.verifications) is not tuple:
        raise InvalidDevToolPolicy(subject="policy")
    commands = []
    for command in policy.verifications:
        if type(command) is not VerificationCommand:
            raise InvalidDevToolPolicy(subject="verifications")
        commands.append(VerificationCommand(command.command_id, command.argv, command.timeout_seconds,
                                            command.max_output_bytes, command.oracle_files))
    return DevToolPolicy(policy.workspace_id, tuple(commands), policy.analysis_only_paths, policy.max_file_bytes)
