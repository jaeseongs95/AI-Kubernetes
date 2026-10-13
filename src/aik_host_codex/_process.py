"""Bounded supervision of one live host command and its process tree.

This is a package-private helper that uses only the standard library.

POSIX
- Commands start in a new session (``start_new_session=True``). The process
  group ID therefore identifies the command tree.
- On timeout, output overflow or cancellation, the whole group receives
  ``SIGKILL``.
- After a normal exit, members left in the group are killed too, so they
  cannot hold the output pipes open.
- The group is then observed again (Linux ``/proc``; otherwise
  ``killpg(pgid, 0)``). The caller trusts the result only when the group is
  confirmed gone.

Windows (NOT_RUN)
- Commands start with ``CREATE_NEW_PROCESS_GROUP``.
- They are terminated with ``taskkill /T /F`` while the direct child is
  alive.

Limits
- A descendant that calls ``setsid`` (or otherwise leaves the group or tree)
  is outside this boundary.
- Output is read from pipes, and at most ``limit`` bytes per stream are kept
  in memory. No temporary files are used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
import threading
import time

GRACE_SECONDS = 5.0
_CHUNK = 65536
_POLL = 0.02
_WAKE = 0.05

EXITED = "exited"
TIMEOUT = "timeout"
OVERFLOW = "overflow"


@dataclass(frozen=True, slots=True)
class Supervised:
    """Outcome of one supervised command. Never log ``streams``."""

    state: str
    exit_code: int | None
    streams: tuple[bytes, ...] = field(repr=False)
    complete: bool
    tree_confirmed: bool


def popen_options(windows_flags: int = 0) -> dict:
    """Start the command as the root of its own group/session."""
    if os.name == "nt":
        return {"creationflags": windows_flags | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


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


def exited(process) -> bool:
    """Report exit without reaping on POSIX when possible (keeps the PGID valid)."""
    if process.returncode is not None:
        return True
    if os.name != "nt" and all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")):
        try:
            return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except OSError:
            pass
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
    """True if a non-zombie group member exists, False if none, None if unknown."""
    try:
        # Reap members reparented to this process (subreaper or PID 1).
        while os.waitpid(-pgid, os.WNOHANG)[0]:
            pass
    except OSError:
        pass
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
    """Terminate the command tree; True only if its termination was confirmed."""
    if os.name == "nt":
        killed = True
        if process.poll() is None:
            killed = _taskkill(process.pid)
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        return killed
    if graceful > 0 and not exited(process):
        _signal_group(process, signal.SIGTERM)
        end = time.monotonic() + graceful
        while not exited(process) and time.monotonic() < end:
            time.sleep(_POLL)
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return _confirm_gone(process.pid)


def supervise(process, *, timeout: float, limit: int, stdin: bytes | None = None) -> Supervised:
    """Wait for exit, deadline or output overflow, then end and re-observe the tree."""
    deadline = time.monotonic() + timeout
    overflow = threading.Event()
    readers = []
    writer = None
    state = None
    try:
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
