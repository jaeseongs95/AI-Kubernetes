"""Select the per-user product vault backend for this platform (DEC09).

Windows: DPAPI-protected files (``DpapiVaultBackend``). Linux: the desktop
keyring through the Secret Service (``SecretServiceVaultBackend``). Any other
platform, or a backend that is not usable, raises ``VaultUnavailable``; there
is no plaintext or test-backend fallback.
"""

from __future__ import annotations

from pathlib import Path

from aik_platform import family

from .backend import VaultBackend
from .dpapi import DpapiVaultBackend
from .errors import VaultUnavailable
from .secret_service import SecretServiceVaultBackend, namespace_for


def open_user_vault(directory: Path | str | None = None, *, timeout: float = 5.0) -> VaultBackend:
    """Open the per-user product vault; ``directory`` selects a separate vault."""
    platform = family()
    if platform == "windows":
        return DpapiVaultBackend(directory, timeout=timeout)
    if platform == "linux":
        return SecretServiceVaultBackend(namespace_for(directory), timeout=timeout)
    raise VaultUnavailable()
