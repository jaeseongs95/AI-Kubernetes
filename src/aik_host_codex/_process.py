"""Bounded supervision of one live host command and its process tree.

This is a package-private helper that uses only the standard library.

POSIX
- Commands start in a new session (``start_new_session=True``). The process
  group ID therefore identifies the command tree.
- On timeout, output overflow or cancellation, the whole group receives
  ``SIGKILL``.
- After a normal exit, members left in the group are killed too, so they
  cannot hold the output pipes open.
- The direct child (the group leader) is reaped only after the group has been
  observed again. Its unreaped zombie keeps the PID, and therefore the PGID,
  from being reused by an unrelated process while the group is signalled.
  Exit is observed with ``waitid(WNOWAIT)``.
- The group is re-observed with Linux ``/proc`` (zombies excluded). Other
  POSIX systems use ``killpg(pgid, 0)``, which also counts the unreaped
  leader, so they report "unconfirmed".
- If the leader was already reaped (``waitid`` unavailable, or another caller
  reaped it), the group is neither signalled nor re-observed, and the result
  is "unconfirmed". The caller trusts the result only when the group is
  confirmed gone.

Windows (NOT_RUN)
- Commands start with ``CREATE_NEW_PROCESS_GROUP`` and are assigned to a
  private Job Object right after start (``track_tree``).
- They are terminated with ``TerminateJobObject``. Termination is confirmed
  only when the job reports no active process. Members left after a normal
  exit are terminated too.
- Without a job (assignment failed), ``taskkill /T /F`` is used and the
  result is always "unconfirmed", because the tree was not observed.

Limits
- A descendant that calls ``setsid`` (or otherwise leaves the group), a
  process created before the job assignment, or one that breaks away from
  the job, is outside this boundary.
- Output is read from pipes, and at most ``limit`` bytes per stream are kept
  in memory. No temporary files are used.
- Cleanup grace: terminating and re-observing the tree may take up to about
  ``4 * GRACE_SECONDS`` beyond the deadline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import os
import signal
import subprocess
import threading
import time
import weakref

GRACE_SECONDS = 5.0
_CHUNK = 65536
_POLL = 0.02
_WAKE = 0.05
_WINDOWS = os.name == "nt"
_HAS_WAITID = all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))
_JOB_BASIC_ACCOUNTING = 1  # JobObjectBasicAccountingInformation

EXITED = "exited"
TIMEOUT = "timeout"
OVERFLOW = "overflow"


@dataclass(frozen=True, slots=True)
class Supervised:
    """Outcome of one supervised command. Never log ``streams``.

    ``tree_confirmed`` is True only when the process tree was observed gone
    and the pipes were drained. Unobservable trees report False.
    """

    state: str
    exit_code: int | None
    streams: tuple[bytes, ...] = field(repr=False)
    complete: bool
    tree_confirmed: bool


def popen_options(windows_flags: int = 0) -> dict:
    """Start the command as the root of its own group/session."""
    if _WINDOWS:
        return {"creationflags": windows_flags | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


@lru_cache(maxsize=1)
def _win32():
    """Windows kernel32 job functions (NOT_RUN)."""
    import ctypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
            ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateJobObject.restype = ctypes.c_int
    kernel32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(Accounting), ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return ctypes, kernel32, Accounting


_WIN_ERRORS = (OSError, AttributeError, ImportError, TypeError, ValueError)


def _close_job(job) -> None:
    try:
        _win32()[1].CloseHandle(job)
    except _WIN_ERRORS:
        pass


def track_tree(process) -> None:
    """Windows: place the started command in a private Job Object (NOT_RUN).

    POSIX needs nothing: the new session's process group is the tree. If the
    assignment fails, the process stays untracked and its termination is
    reported as unconfirmed, never as confirmed.
    """
    if not _WINDOWS or getattr(process, "_aik_job", None) is not None:
        return
    job = None
    try:
        _, kernel32, _ = _win32()
        job = kernel32.CreateJobObjectW(None, None)
        if job and kernel32.AssignProcessToJobObject(job, int(process._handle)):
            weakref.finalize(process, _close_job, job)
            process._aik_job = job
            return
    except _WIN_ERRORS:
        pass
    if job:
        _close_job(job)


def _job_active(job) -> int | None:
    """Number of active processes in the job, or None if unobservable."""
    try:
        ctypes, kernel32, accounting = _win32()
        info = accounting()
        if not kernel32.QueryInformationJobObject(job, _JOB_BASIC_ACCOUNTING, ctypes.byref(info),
                                                  ctypes.sizeof(info), None):
            return None
    except _WIN_ERRORS:
        return None
    return int(info.ActiveProcesses)


def _job_terminate(job) -> None:
    try:
        _win32()[1].TerminateJobObject(job, 1)
    except _WIN_ERRORS:
        pass


def _confirm_job_empty(job) -> bool:
    deadline = time.monotonic() + GRACE_SECONDS
    while True:
        active = _job_active(job)
        if active == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        if active:
            _job_terminate(job)
        time.sleep(_POLL)


class _Reader:
    """Read one pipe, keeping at most ``limit`` bytes, and flag overflow."""

    def __init__(self, stream, limit: int, overflow: threading.Event) -> None:
        self.stream = stream
        self.limit = limit
        self.overflow = overflow
        self.data = bytearray()
        self.complete = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(_CHUNK)
                if not chunk:
                    self.complete = True
                    return
                room = self.limit - len(self.data)
                if len(chunk) > room:
                    self.data.extend(chunk[:room])
                    self.overflow.set()
                    return
                self.data.extend(chunk)
        except (OSError, ValueError):
            return


def _feed(stream, data: bytes) -> None:
    try:
        view = memoryview(data)
        while view:
            written = stream.write(view)
            if not written:
                break
            view = view[written:]
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _peek_exit(process) -> bool | None:
    """POSIX exit observation without reaping; None if that is impossible."""
    if process.returncode is not None or not _HAS_WAITID:
        return None
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except OSError:
        return None


def exited(process) -> bool:
    """Report exit without reaping on POSIX when possible (keeps the PGID valid)."""
    if process.returncode is not None:
        return True
    if not _WINDOWS:
        observed = _peek_exit(process)
        if observed is not None:
            return observed
    return process.poll() is not None


def _signal_group(process, sig) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.send_signal(sig)
        except OSError:
            pass


def _taskkill(pid: int) -> bool:
    """Windows tree termination (NOT_RUN). Uses the system taskkill, not PATH."""
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if not root:
        return False
    executable = os.path.join(root, "System32", "taskkill.exe")
    try:
        completed = subprocess.run(
            [executable, "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, check=False,
            timeout=GRACE_SECONDS * 2, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _group_alive(pgid: int) -> bool | None:
    """True if a non-zombie group member exists, False if none, None if unknown.

    This never reaps: ``waitpid(-pgid)`` would reap the leader and release
    the PGID while it is still being observed.
    """
    if os.path.isfile("/proc/self/stat"):
        try:
            entries = os.listdir("/proc")
        except OSError:
            return None
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    data = handle.read()
            except OSError:
                continue  # exited while scanning
            try:
                values = data[data.rindex(b")") + 2:].split()
                state, group = values[0], int(values[2])
            except (ValueError, IndexError):
                return None
            if group == pgid and state not in (b"Z", b"X", b"x"):
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _confirm_gone(pgid: int) -> bool:
    deadline = time.monotonic() + GRACE_SECONDS
    while True:
        alive = _group_alive(pgid)
        if alive is False:
            return True
        if time.monotonic() >= deadline:
            return False
        if alive:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(_POLL)


def terminate_tree(process, graceful: float = 0.0) -> bool:
    """Terminate the command tree; True only if its termination was confirmed.

    Windows (NOT_RUN): confirmation requires the private Job Object to report
    no active process. Without a job the tree is not observable, so the
    result is False even when ``taskkill`` succeeded.

    POSIX: the leader is reaped only after the group was re-observed. An
    already reaped leader yields False without signalling its old PGID.
    """
    if _WINDOWS:
        job = getattr(process, "_aik_job", None)
        if process.poll() is None:
            if job is not None:
                _job_terminate(job)
            else:
                _taskkill(process.pid)
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        if job is None:
            return False
        return _confirm_job_empty(job)
    if graceful > 0 and not exited(process):
        _signal_group(process, signal.SIGTERM)
        end = time.monotonic() + graceful
        while not exited(process) and time.monotonic() < end:
            time.sleep(_POLL)
    if process.returncode is not None:
        # The leader was reaped; its PID/PGID may already belong to another process.
        return False
    _signal_group(process, signal.SIGKILL)
    end = time.monotonic() + GRACE_SECONDS
    while True:
        leader_exited = _peek_exit(process)
        if leader_exited is None:
            return False
        if leader_exited:
            break
        if time.monotonic() >= end:
            return False
        time.sleep(_POLL)
    confirmed = _confirm_gone(process.pid)
    try:
        process.wait(GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return confirmed


def supervise(process, *, timeout: float, limit: int, stdin: bytes | None = None) -> Supervised:
    """Wait for exit, deadline or output overflow, then end and re-observe the tree."""
    deadline = time.monotonic() + timeout
    overflow = threading.Event()
    readers = []
    writer = None
    state = None
    try:
        track_tree(process)
        readers = [_Reader(stream, limit, overflow) for stream in (process.stdout, process.stderr)
                   if stream is not None]
        if process.stdin is not None:
            writer = threading.Thread(target=_feed, args=(process.stdin, stdin or b""), daemon=True)
            writer.start()
        while state is None:
            if overflow.is_set():
                state = OVERFLOW
            elif exited(process):
                state = EXITED
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    state = TIMEOUT
                else:
                    overflow.wait(min(_WAKE, remaining))
    except BaseException:
        # Cancellation (for example KeyboardInterrupt) also ends the tree.
        terminate_tree(process)
        raise
    confirmed = terminate_tree(process)
    end = time.monotonic() + GRACE_SECONDS
    for reader in readers:
        reader.thread.join(max(0.0, end - time.monotonic()))
    if writer is not None:
        writer.join(max(0.0, end - time.monotonic()))
    drained = not any(reader.thread.is_alive() for reader in readers)
    if writer is not None and writer.is_alive():
        drained = False
    if state == EXITED and overflow.is_set():
        state = OVERFLOW
    for reader in readers:
        if not reader.thread.is_alive():
            try:
                reader.stream.close()
            except OSError:
                pass
    return Supervised(
        state, process.returncode, tuple(bytes(reader.data) for reader in readers),
        all(reader.complete for reader in readers), confirmed and drained,
    )
