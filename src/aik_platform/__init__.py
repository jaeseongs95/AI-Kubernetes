"""OS port layer (AIK-091, DEC14).

Common AIK code does not inspect the operating system. It calls the small
concern-specific ports here; this package is the only place that decides
between the Windows and POSIX adapters. Adding an operating system means
adding an adapter here, not editing callers.

Modules (standard library only):

- ``fs``: durable file and directory flushes, link/reparse-point inspection.
- ``locks``: non-blocking exclusive byte-range/file locks on a descriptor.
- ``paths``: per-user product state locations.
- ``process``: process-group launch options, tree termination and supervision.
- ``session``: whether the interactive session is locked.
"""

from __future__ import annotations

from ._os import OS_NAME, family, is_windows
from .errors import PlatformError, PlatformUnavailable
from .fs import is_link_or_reparse_point, sync_directory, sync_file
from .locks import lock_ops

__all__ = [
    "OS_NAME", "PlatformError", "PlatformUnavailable", "family", "is_link_or_reparse_point",
    "is_windows", "lock_ops", "sync_directory", "sync_file",
]
