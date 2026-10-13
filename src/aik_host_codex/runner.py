"""Injectable host CLI runner for the AIK-014 Codex bridge.

Tests inject a synthetic runner. ``SubprocessRunner`` is the standard library
live runner; its use with a real Codex host is NOT_RUN.

Every ``run`` invocation has three limits:
- an absolute timeout
- an output byte limit, enforced while the command runs
- an environment allowlist

Timeout, output overflow and cancellation terminate the command's whole
process tree. The captured output is returned only to the bridge parser and
is never kept.

Cleanup grace: after the deadline, terminating and re-observing the tree may
add up to about ``4 * _process.GRACE_SECONDS`` (20 seconds) of wall-clock
time. ``MAX_TIMEOUT`` bounds the command, not the whole call.

``spawn`` (interactive login) is the exception: it checks the environment
allowlist but has no deadline and no output capture of its own. The caller
must call the handle's ``terminate()``; a discarded handle leaves the process
running. ``terminate()`` does nothing when the direct child already exited,
so helpers it left behind are not terminated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
import shutil
import subprocess
from typing import Protocol, runtime_checkable

from . import _process

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
    """The absolute deadline passed.

    Raised as this exact class, the process tree was terminated and confirmed
    gone. The subclass ``ProcessTreeUnconfirmed`` (also raised after a normal
    exit) means that termination could not be confirmed, so ``except
    RunnerTimeout`` must not assume that the tree has ended.
    """

    code = "HOST_TIMEOUT"


class ProcessTreeUnconfirmed(RunnerTimeout):
    """Termination of the command's process tree could not be confirmed.

    The outcome is unknown and is never reported as a success.
    """

    code = "HOST_TERMINATION_UNKNOWN"


class OutputLimitExceeded(RunnerError):
    """The process produced more output than allowed; its tree was terminated."""

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
    """A spawned interactive login process; its output is discarded.

    ``terminate()`` ends the whole process tree if the direct child is still
    running. ``termination_confirmed`` records the re-observed result:
    - ``True``: the tree was terminated and confirmed gone.
    - ``False``: termination could not be confirmed.
    - ``None``: no termination was attempted.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self.termination_confirmed: bool | None = None

    def __repr__(self) -> str:
        return "HostProcessHandle()"

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        if _process.exited(self._process):
            self._process.poll()
            return
        try:
            self.termination_confirmed = _process.terminate_tree(self._process, graceful=5.0)
        except (OSError, subprocess.SubprocessError):
            self.termination_confirmed = False


class SubprocessRunner:
    """Live runner (standard library). Real Codex host use is NOT_RUN."""

    source = "live"

    def __repr__(self) -> str:
        return "SubprocessRunner()"

    @staticmethod
    def _resolve(argv: tuple[str, ...], env: dict[str, str]) -> tuple[str, ...]:
        """Resolve only through the allowlisted PATH, never the parent's PATH or cwd.

        A bare name must resolve to a file whose directory is one of the
        absolute entries of ``env["PATH"]``. Windows ``shutil.which`` may try
        the current directory first, which this rule rejects. An explicit name
        must be absolute.
        """
        search = env.get("PATH")
        if not search:
            raise HostNotFound()
        explicit = bool(os.path.dirname(argv[0]))
        if explicit and not os.path.isabs(argv[0]):
            raise HostNotFound()
        located = shutil.which(argv[0], path=search)
        if located is None:
            raise HostNotFound()
        if not explicit:
            allowed = {
                os.path.normcase(os.path.normpath(entry))
                for entry in search.split(os.pathsep) if entry and os.path.isabs(entry)
            }
            if os.path.normcase(os.path.dirname(os.path.abspath(located))) not in allowed:
                raise HostNotFound()
        return (located, *argv[1:])

    def run(self, argv, *, env, timeout, max_output) -> RunResult:
        if timeout is None or max_output is None:
            raise ValueError("LIMITS_REQUIRED")
        argv, env = validate_invocation(argv, env, timeout, max_output)
        command = self._resolve(argv, env)
        problem = None
        process = None
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, close_fds=True, shell=False, bufsize=0, **_process.popen_options(_CREATION_FLAGS),
            )
        except FileNotFoundError:
            problem = HostNotFound
        except (OSError, subprocess.SubprocessError):
            problem = RunnerError
        if problem is not None:
            raise problem()
        outcome = _process.supervise(process, timeout=float(timeout), limit=max_output)
        if not outcome.tree_confirmed or outcome.exit_code is None:
            raise ProcessTreeUnconfirmed()
        if outcome.state == _process.OVERFLOW:
            raise OutputLimitExceeded()
        if outcome.state == _process.TIMEOUT:
            raise RunnerTimeout()
        if not outcome.complete:
            raise RunnerError()
        return RunResult(outcome.exit_code, outcome.streams[0])

    def spawn(self, argv, *, env) -> _ProcessHandle:
        argv, env = validate_invocation(argv, env)
        command = self._resolve(argv, env)
        problem = None
        process = None
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, close_fds=True, shell=False, **_process.popen_options(_CREATION_FLAGS),
            )
        except FileNotFoundError:
            problem = HostNotFound
        except (OSError, subprocess.SubprocessError):
            problem = RunnerError
        if problem is not None:
            raise problem()
        _process.track_tree(process)
        return _ProcessHandle(process)


__all__ = [
    "RunResult", "RunnerError", "HostNotFound", "RunnerTimeout", "ProcessTreeUnconfirmed",
    "OutputLimitExceeded", "HostRunner", "SubprocessRunner", "ENV_ALLOWLIST", "MAX_TIMEOUT",
    "MAX_OUTPUT_LIMIT", "SOURCES", "build_environment", "validate_invocation",
]
