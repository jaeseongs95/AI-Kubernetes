"""Injectable command boundary for the AIK-015 Claude Code host bridge.

Tests inject a synthetic runner. ``SubprocessRunner`` is the standard-library
live runner. Its real use against the Claude Code host is NOT_RUN.

Every command carries the following:
- an absolute timeout
- a stdout size limit
- an explicit environment

The environment is built from an allowlist. Any variable that looks like a
credential is dropped, including ``ANTHROPIC_API_KEY`` and every
``*TOKEN*`` variable. This keeps the subscription path from silently
falling back to an API key. Prompt bytes travel on stdin, never in argv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import shutil
import subprocess
import tempfile
from typing import Protocol, runtime_checkable

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
    """Bounded command output. ``truncated`` means stdout exceeded its limit."""

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


def _bounded(stream, limit: int) -> tuple[bytes, bool]:
    stream.seek(0)
    data = stream.read(limit + 1)
    return data[:limit], len(data) > limit


class SubprocessRunner:
    """Live runner (standard library). Real Claude Code use is NOT_RUN.

    ``run`` spools output to private temporary files and reads at most the
    limit back. On timeout, ``subprocess.run`` kills the direct child process.
    Grandchildren of a host CLI are not guaranteed to be terminated.
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
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            extra = {"input": spec.stdin} if spec.stdin is not None else {"stdin": subprocess.DEVNULL}
            try:
                completed = subprocess.run(
                    argv, stdout=out, stderr=err, env=env, timeout=spec.timeout,
                    shell=False, check=False, creationflags=flags, **extra,
                )
            except subprocess.TimeoutExpired:
                raise RunnerTimeout() from None
            except OSError:
                raise RunnerNotStarted() from None
            stdout, truncated = _bounded(out, spec.max_output_bytes)
            stderr, _ = _bounded(err, spec.max_output_bytes)
        return CommandResult(completed.returncode, stdout, stderr, truncated)

    def launch(self, spec: CommandSpec) -> None:
        """Start the official interactive login without capturing its I/O."""
        if type(spec) is not CommandSpec:
            raise TypeError("CommandSpec required")
        argv, env = self._resolve(spec)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(argv, env=env, shell=False, close_fds=True, creationflags=flags)
        except OSError:
            raise RunnerNotStarted() from None
        self._launched = [p for p in self._launched if p.poll() is None][-15:] + [process]


__all__ = [
    "SOURCES", "MAX_TIMEOUT_SECONDS", "MAX_OUTPUT_LIMIT", "MAX_STDIN_BYTES", "ENV_ALLOWLIST",
    "RunnerTimeout", "RunnerNotStarted", "build_environment", "CommandSpec", "CommandResult",
    "CommandRunner", "SubprocessRunner",
]
