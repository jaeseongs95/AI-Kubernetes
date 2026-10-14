"""Exclusive, non-blocking OS lock on an open descriptor.

``lock_ops(descriptor)`` returns ``(acquire, release)``. ``acquire`` raises
``OSError`` with errno EACCES, EAGAIN or EDEADLK while another holder exists;
callers own retry, timeouts and the lockfile's lifetime. Windows locks byte 0
(``msvcrt``), which is allowed beyond EOF; POSIX uses ``flock``.
"""

from __future__ import annotations

import os

from ._os import is_windows


def lock_ops(descriptor: int, os_name: str | None = None):
    if is_windows(os_name):
        import msvcrt

        def acquire():
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

        def release():
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def acquire():
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    return acquire, release
