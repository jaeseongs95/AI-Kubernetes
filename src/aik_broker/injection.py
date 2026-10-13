"""Child-process injection without command-line or persistent-file exposure.

The secret reaches a child only through stdin (default) or one environment
variable of a private environment copy. Arguments and the base environment are
checked for the secret and its supported encodings (aik_security.redaction).
Child output is redacted before it leaves this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
import signal
import subprocess

from aik_security.redaction import OMITTED, REDACTED, OutputRedactor, RedactionError

from .errors import InjectionError


_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
MAX_ARGS = 256
MAX_ARG_CHARS = 32768
MAX_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_CHARS = 65536
_EMPTY = OutputRedactor()


@dataclass(frozen=True, slots=True)
class ChildResult:
    """Exit status and redacted output of a completed child process."""

    returncode: int
    stdout: str
    stderr: str


class _SecretDetector:
    """Never log, repr, pickle or store this object; it holds the plaintext."""

    __slots__ = ("_secret", "_redactor")

    def __init__(self, secret: str):
        self._secret = secret
        try:
            self._redactor = OutputRedactor([secret])
        except RedactionError:
            # aik_security accepts 12..256 characters; shorter or longer
            # secrets fall back to exact-substring handling only.
            self._redactor = None

    def __repr__(self) -> str:
        return "_SecretDetector()"

    def __reduce_ex__(self, protocol):
        raise TypeError("A secret detector cannot be serialized.")

    def present(self, text: str) -> bool:
        if self._secret in text:
            return True
        if self._redactor is None:
            return False
        return self._redactor.redact_text(text) != _EMPTY.redact_text(text)

    def redact(self, text: str) -> str:
        if self._redactor is None:
            result = text.replace(self._secret, REDACTED)
        else:
            result = self._redactor.redact_text(text)
        if self._secret in result or len(result) > MAX_OUTPUT_CHARS:
            return OMITTED
        return result


def secret_present(text: str, secret: str) -> bool:
    """True when the plaintext or a supported encoding of it occurs in text."""
    if type(text) is not str or type(secret) is not str or not secret:
        return False
    return _SecretDetector(secret).present(text)


def validate_command(argv, *, delivery: str = "stdin", env_name: str | None = None,
                     base_env=None, timeout: float = 30.0, cwd=None) -> None:
    """Validate the command shape before a grant use is consumed."""
    valid = (
        type(argv) in (list, tuple) and 1 <= len(argv) <= MAX_ARGS
        and all(type(arg) is str and arg and len(arg) <= MAX_ARG_CHARS and "\0" not in arg for arg in argv)
        and delivery in ("stdin", "env")
        and (env_name is None if delivery == "stdin" else type(env_name) is str and _ENV_NAME.fullmatch(env_name) is not None)
        and (base_env is None or (type(base_env) is dict and all(
            type(key) is str and type(value) is str for key, value in base_env.items())))
        and type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= MAX_TIMEOUT_SECONDS
        and (cwd is None or isinstance(cwd, (str, os.PathLike)))
    )
    if not valid:
        raise InjectionError("INVALID_REQUEST")


def _terminate(process: subprocess.Popen) -> None:
    # POSIX: the child leads its own session, so its process group includes
    # descendants that did not create a new session. The child has not been
    # reaped yet, so its PID/PGID cannot have been reused.
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.communicate(timeout=5)
    except Exception:
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def run_injected(argv, secret: str, *, delivery: str = "stdin", env_name: str | None = None,
                 base_env=None, cwd=None, timeout: float = 30.0) -> ChildResult:
    """Run a child with the secret on stdin or in one private environment variable.

    shell=False, close_fds=True. No file is written. On timeout the child (and
    on POSIX its process group) is killed and CHILD_TIMEOUT is raised.
    """
    validate_command(argv, delivery=delivery, env_name=env_name, base_env=base_env, timeout=timeout, cwd=cwd)
    if type(secret) is not str or not secret:
        raise InjectionError("INVALID_REQUEST")
    detector = _SecretDetector(secret)
    if any(detector.present(arg) for arg in argv):
        raise InjectionError("ARGV_CONTAINS_SECRET")
    env = dict(os.environ if base_env is None else base_env)
    if any(detector.present(value) for value in env.values()):
        env.clear()
        raise InjectionError("ENV_CONTAINS_SECRET")
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env, "cwd": cwd,
              "shell": False, "close_fds": True,
              "stdin": subprocess.PIPE if delivery == "stdin" else subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    data = secret.encode("utf-8") if delivery == "stdin" else None
    if delivery == "env":
        env[env_name] = secret
    process = None
    outcome = None
    code = None
    try:
        try:
            process = subprocess.Popen(list(argv), **kwargs)
        except (OSError, ValueError, subprocess.SubprocessError):
            code = "CHILD_START_FAILED"
        if process is not None:
            try:
                stdout, stderr = process.communicate(data, timeout=float(timeout))
                outcome = (process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                code = "CHILD_TIMEOUT"
                _terminate(process)
            except BaseException:
                _terminate(process)
                raise
    finally:
        data = None
        env.clear()  # drop the parent's private copy; os.environ is never modified
    if code is not None:
        raise InjectionError(code)
    returncode, stdout, stderr = outcome
    return ChildResult(
        returncode,
        detector.redact(stdout.decode("utf-8", errors="replace")),
        detector.redact(stderr.decode("utf-8", errors="replace")),
    )
