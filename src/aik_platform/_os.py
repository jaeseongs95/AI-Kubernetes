"""The single operating-system decision of the AIK code base."""

from __future__ import annotations

import os
import sys

OS_NAME = os.name


def is_windows(os_name: str | None = None) -> bool:
    return (OS_NAME if os_name is None else os_name) == "nt"


def family() -> str:
    """``windows``, ``linux``, ``macos`` or another ``sys.platform`` value."""
    if is_windows():
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform
