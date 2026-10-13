"""Injectable command boundary for the AIK-015 Claude Code host bridge.

Tests inject a synthetic runner. ``SubprocessRunner`` is the standard-library
live runner. Its real use against the Claude Code host is NOT_RUN.

Every command carries three limits.

Absolute timeout
- ``run`` terminates the command's whole process tree when the timeout
  expires.
- ``launch`` does the same if the direct child is still running at the
  deadline.

Output limit
- The limit is enforced while the command runs.
- When the limit is exceeded, the process tree is terminated.

Explicit environment
- The environment is built from an allowlist.
- Any variable that looks like a credential is dropped, including
  ``ANTHROPIC_API_KEY`` and every ``*TOKEN*`` variable. This keeps the
  subscription path from silently falling back to an API key.

Prompt bytes travel on stdin, never in argv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import shutil
import subprocess
import threading
from typing import Protocol, runtime_checkable

from . import _process

SOURCES = ("synthetic_fixture", "live")
MAX_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_LIMIT = 16 * 1024 * 1024
MAX_STDIN_BYTES = 8 * 1024 * 1024
ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
    "CLAUDE_CONFIG_DIR",
})
# Applied even when a caller extends the allowlist.
_DENIED_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH")


class RunnerTimeout(Exception):
    """The absolute deadline passed; the command may already have acted."""

    def __init__(self) -> None:
        super().__init__("RUNNER_TIMEOUT")


class RunnerTerminationUnknown(RunnerTimeout):
    """Termination of the command's process tree could not be confirmed.

    This is a subclass of ``RunnerTimeout``, so callers treat it as an
    unknown outcome. It is never a success.
    """

    def __init__(self) -> None:
        Exception.__init__(self, "RUNNER_TERMINATION_UNKNOWN")


class RunnerNotStarted(Exception):
    """The host executable could not be found or started (host absent)."""

    def __init__(self) -> None:
        super().__init__("RUNNER_NOT_STARTED")


def build_environment(source, allowlist=ENV_ALLOWLIST) -> tuple[tuple[str, str], ...]:
    """Copy only allowlisted, non-credential variables; never inherit the rest."""
    if not callable(getattr(source, "items", None)):
        raise TypeError("An environment mapping is required.")
    allowed = set()
    for name in allowlist:
        if type(name) is not str:
            raise TypeError("Environment allowlist entries must be strings.")
        allowed.add(name.upper())
    result: dict[str, str] = {}
    for name, value in source.items():
        if type(name) is not str or type(value) is not str or not name:
            continue
        upper = name.upper()
        if upper not in allowed or upper.startswith("ANTHROPIC_"):
            continue
        if any(part in upper for part in _DENIED_PARTS):
            continue
        if "=" in name or "\0" in name or "\0" in value:
            continue
        result[name] = value
    return tuple(sorted(result.items()))


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One host command. Never log, pickle or persist ``env``/``stdin``."""

    argv: tuple[str, ...]
    timeout: float
    max_output_bytes: int
    env: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    stdin: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.argv) is not tuple or not 1 <= len(self.argv) <= 64:
            raise ValueError("INVALID_COMMAND")
        for argument in self.argv:
            if type(argument) is not str or not argument or len(argument) > 4096 or "\0" in argument:
                raise ValueError("INVALID_COMMAND")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= MAX_TIMEOUT_SECONDS):
            raise ValueError("INVALID_TIMEOUT")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= MAX_OUTPUT_LIMIT:
            raise ValueError("INVALID_OUTPUT_LIMIT")
        if type(self.env) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not str
            for item in self.env
        ):
            raise ValueError("INVALID_ENVIRONMENT")
        if self.stdin is not None and (type(self.stdin) is not bytes or len(self.stdin) > MAX_STDIN_BYTES):
            raise ValueError("INVALID_STDIN")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded command output.

    ``truncated`` means that stdout or stderr exceeded the limit. When the
    live runner reports it, the command's process tree was terminated at that
    point, and ``exit_code`` reflects that termination.
    """

    exit_code: int
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    truncated: bool = False

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("INVALID_EXIT_CODE")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise ValueError("INVALID_OUTPUT")
        if type(self.truncated) is not bool:
            raise ValueError("INVALID_OUTPUT")


@runtime_checkable
class CommandRunner(Protocol):
    """``source`` is ``synthetic_fixture`` (tests) or ``live``; never qualification."""

    source: str

    def run(self, spec: CommandSpec) -> CommandResult: ...

    def launch(self, spec: CommandSpec) -> None: ...


def _terminate_after_deadline(process) -> bool:
    """Kill a launched process tree whose direct child outlived its deadline."""
    try:
        if _process.exited(process):
            return True
        return _process.terminate_tree(process)
    except (OSError, subprocess.SubprocessError):
        return False


class SubprocessRunner:
    """Live runner (standard library). Real Claude Code use is NOT_RUN.

    ``run``
    - Reads stdout and stderr from pipes, keeping at most the limit of each.
      Nothing is spooled to disk.
    - Terminates the command's process tree when it times out or exceeds the
      output limit.
    - After a normal exit, kills members left in the POSIX process group.
    - Re-observes the tree. If its termination cannot be confirmed,
      ``RunnerTerminationUnknown`` is raised instead of returning a result.

    ``launch``
    - Arms a daemon timer.
    - When ``spec.timeout`` expires, the timer terminates the tree if the
      direct child is still running.

    The Windows paths (``taskkill /T /F``) are NOT_RUN.
    """

    source = "live"

    def __init__(self) -> None:
        self._launched: list = []

    def __repr__(self) -> str:
        return "SubprocessRunner()"

    @staticmethod
    def _resolve(spec: CommandSpec) -> tuple[list[str], dict[str, str]]:
        env = dict(spec.env)
        search = next((value for name, value in env.items() if name.upper() == "PATH"), os.defpath)
        executable = shutil.which(spec.argv[0], path=search)
        if executable is None:
            raise RunnerNotStarted()
        return [executable, *spec.argv[1:]], env

    def run(self, spec: CommandSpec) -> CommandResult:
        if type(spec) is not CommandSpec:
            raise TypeError("CommandSpec required")
        argv, env = self._resolve(spec)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE if spec.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=False,
                close_fds=True, bufsize=0, **_process.popen_options(flags),
            )
        except (OSError, subprocess.SubprocessError):
            raise RunnerNotStarted() from None
        outcome = _process.supervise(process, timeout=spec.timeout, limit=spec.max_output_bytes,
                                     stdin=spec.stdin)
        if not outcome.tree_confirmed or outcome.exit_code is None:
            raise RunnerTerminationUnknown()
        if outcome.state == _process.TIMEOUT:
            raise RunnerTimeout()
        stdout, stderr = outcome.streams
        truncated = outcome.state == _process.OVERFLOW or not outcome.complete
        return CommandResult(outcome.exit_code, stdout, stderr, truncated)

    def launch(self, spec: CommandSpec) -> None:
        """Start the official interactive login without capturing its I/O.

        When ``spec.timeout`` expires and the direct child is still running,
        its process tree is terminated.
        """
        if type(spec) is not CommandSpec:
            raise TypeError("CommandSpec required")
        argv, env = self._resolve(spec)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(argv, env=env, shell=False, close_fds=True,
                                       **_process.popen_options(flags))
        except (OSError, subprocess.SubprocessError):
            raise RunnerNotStarted() from None
        timer = threading.Timer(spec.timeout, _terminate_after_deadline, (process,))
        timer.daemon = True
        timer.start()
        self._launched = [p for p in self._launched if p.poll() is None][-15:] + [process]


__all__ = [
    "SOURCES", "MAX_TIMEOUT_SECONDS", "MAX_OUTPUT_LIMIT", "MAX_STDIN_BYTES", "ENV_ALLOWLIST",
    "RunnerTimeout", "RunnerTerminationUnknown", "RunnerNotStarted", "build_environment",
    "CommandSpec", "CommandResult", "CommandRunner", "SubprocessRunner",
]
