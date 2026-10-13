"""Injectable host CLI runner for the AIK-014 Codex bridge.

Tests inject a synthetic runner. ``SubprocessRunner`` is the standard library
live runner; its use with a real Codex host is NOT_RUN. Every invocation has
an absolute timeout, an output byte limit and an environment allowlist. The
captured output is returned only to the bridge parser and is never kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import shutil
import subprocess
import threading
import time
from typing import Protocol, runtime_checkable

SOURCES = ("synthetic_fixture", "live")
MAX_TIMEOUT = 120.0
MAX_OUTPUT_LIMIT = 1024 * 1024
_MAX_ARGS = 32
_MAX_ARG_CHARS = 4096
_MAX_ENV_VALUE = 32767
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# Only non-secret process plumbing and the host's own config location are
# forwarded. OPENAI_API_KEY and every other variable are always dropped, so a
# subscription login can never silently become an API-key execution route.
ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "LC_CTYPE",
    "CODEX_HOME", "XDG_CONFIG_HOME", "NO_COLOR",
})


class RunnerError(Exception):
    """Fixed-code runner failure; never carries process output."""

    code = "HOST_RUNNER_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class HostNotFound(RunnerError):
    """The host executable could not be located; nothing was started."""

    code = "HOST_NOT_FOUND"


class RunnerTimeout(RunnerError):
    """The absolute deadline passed; the process was terminated."""

    code = "HOST_TIMEOUT"


class OutputLimitExceeded(RunnerError):
    """The process produced more output than allowed; it was terminated."""

    code = "HOST_OUTPUT_LIMIT"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Exit code plus bounded combined output. Never log or persist ``output``."""

    exit_code: int
    output: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.exit_code) is not int:
            raise ValueError("INVALID_EXIT_CODE")
        if type(self.output) is not bytes:
            raise ValueError("INVALID_OUTPUT")


def build_environment(source=None) -> dict[str, str]:
    """Return only allowlisted, non-secret variables plus a fixed NO_COLOR=1."""
    items = os.environ if source is None else source
    result: dict[str, str] = {}
    for name, value in items.items():
        if (type(name) is str and name in ENV_ALLOWLIST and name != "NO_COLOR"
                and type(value) is str and "\0" not in value and len(value) <= _MAX_ENV_VALUE):
            result[name] = value
    result["NO_COLOR"] = "1"
    return result


def validate_invocation(argv, env, timeout=None, max_output=None) -> tuple[tuple[str, ...], dict[str, str]]:
    """Reject non-allowlisted environment, unbounded deadlines and limits."""
    if type(argv) is not tuple or not 1 <= len(argv) <= _MAX_ARGS:
        raise ValueError("INVALID_ARGV")
    for item in argv:
        if type(item) is not str or not item or len(item) > _MAX_ARG_CHARS or "\0" in item:
            raise ValueError("INVALID_ARGV")
    if type(env) is not dict:
        raise ValueError("INVALID_ENVIRONMENT")
    for name, value in env.items():
        if (type(name) is not str or name not in ENV_ALLOWLIST or type(value) is not str
                or "\0" in value or len(value) > _MAX_ENV_VALUE):
            raise ValueError("ENVIRONMENT_NOT_ALLOWLISTED")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT
    ):
        raise ValueError("INVALID_TIMEOUT")
    if max_output is not None and (type(max_output) is not int or not 1 <= max_output <= MAX_OUTPUT_LIMIT):
        raise ValueError("INVALID_OUTPUT_LIMIT")
    return argv, dict(env)


@runtime_checkable
class HostRunner(Protocol):
    """``source`` is ``synthetic_fixture`` (tests) or ``live``; never qualification."""

    source: str

    def run(self, argv: tuple[str, ...], *, env: dict[str, str], timeout: float,
            max_output: int) -> RunResult: ...

    def spawn(self, argv: tuple[str, ...], *, env: dict[str, str]): ...


class _ProcessHandle:
    """A spawned interactive login process; its output is discarded."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def __repr__(self) -> str:
        return "HostProcessHandle()"

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                self._process.kill()


class SubprocessRunner:
    """Live runner (standard library). Real Codex host use is NOT_RUN."""

    source = "live"

    def __repr__(self) -> str:
        return "SubprocessRunner()"

    @staticmethod
    def _resolve(argv: tuple[str, ...], env: dict[str, str]) -> tuple[str, ...]:
        located = shutil.which(argv[0], path=env.get("PATH"))
        if located is None:
            raise HostNotFound()
        return (located, *argv[1:])

    def run(self, argv, *, env, timeout, max_output) -> RunResult:
        if timeout is None or max_output is None:
            raise ValueError("LIMITS_REQUIRED")
        argv, env = validate_invocation(argv, env, timeout, max_output)
        command = self._resolve(argv, env)
        deadline = time.monotonic() + float(timeout)
        problem = None
        process = None
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, close_fds=True, shell=False, creationflags=_CREATION_FLAGS,
            )
        except FileNotFoundError:
            problem = HostNotFound
        except OSError:
            problem = RunnerError
        if problem is not None:
            raise problem()
        buffer = bytearray()
        overflow = threading.Event()

        def read() -> None:
            try:
                while True:
                    chunk = process.stdout.read1(4096)
                    if not chunk:
                        return
                    if len(buffer) + len(chunk) > max_output:
                        overflow.set()
                        return
                    buffer.extend(chunk)
            except (OSError, ValueError):
                return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(max(0.0, deadline - time.monotonic()))
        outcome = None
        exit_code = None
        try:
            if overflow.is_set():
                outcome = OutputLimitExceeded
            elif reader.is_alive():
                outcome = RunnerTimeout
            else:
                try:
                    exit_code = process.wait(max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    outcome = RunnerTimeout
        finally:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            try:
                process.stdout.close()
            except OSError:
                pass
        if outcome is not None:
            raise outcome()
        return RunResult(exit_code, bytes(buffer))

    def spawn(self, argv, *, env) -> _ProcessHandle:
        argv, env = validate_invocation(argv, env)
        command = self._resolve(argv, env)
        problem = None
        process = None
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, close_fds=True, shell=False, creationflags=_CREATION_FLAGS,
            )
        except FileNotFoundError:
            problem = HostNotFound
        except OSError:
            problem = RunnerError
        if problem is not None:
            raise problem()
        return _ProcessHandle(process)


__all__ = [
    "RunResult", "RunnerError", "HostNotFound", "RunnerTimeout", "OutputLimitExceeded",
    "HostRunner", "SubprocessRunner", "ENV_ALLOWLIST", "MAX_TIMEOUT", "MAX_OUTPUT_LIMIT",
    "SOURCES", "build_environment", "validate_invocation",
]
