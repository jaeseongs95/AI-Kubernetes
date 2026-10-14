"""Source entrypoint of the desktop engine for ``python -I`` runs (AIK-045).

``-I`` ignores PYTHONPATH and the user site, so the product sources are added
from this checkout explicitly. A frozen engine bundles the packages instead.

AIK-046/047: this file is also the composition root. Common packages never
import vendor packages (PRN-VENDOR), so the vendor connectors are built here
and handed to the engine. The per-user vault is opened only when a connection
command first needs it; starting the engine or reading status never touches it.
"""

import sys
import threading
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from aik_desktop_engine import main  # noqa: E402
from aik_vault import VaultBackend, open_user_vault  # noqa: E402


class LazyUserVault(VaultBackend):
    """Opens the platform vault on first use; a failure is raised again on every call."""

    def __init__(self):
        self._lock = threading.Lock()
        self._vault = None

    def _open(self) -> VaultBackend:
        with self._lock:
            if self._vault is None:
                self._vault = open_user_vault()
            return self._vault

    def put(self, secret):
        return self._open().put(secret)

    def replace(self, credential_ref, secret):
        return self._open().replace(credential_ref, secret)

    def delete(self, credential_ref):
        return self._open().delete(credential_ref)

    def resolve(self, credential_ref):
        return self._open().resolve(credential_ref)


def build_connectors(vault: VaultBackend) -> tuple:
    """Every vendor connector that can be built here; one that cannot is left out of the catalog."""
    from aik_adapter_anthropic import connector as anthropic
    from aik_adapter_openai import connector as openai
    from aik_host_claude import connector as claude
    from aik_host_codex import connector as codex

    built = []
    for factory, arguments in ((anthropic.create_connector, {"vault": vault}),
                               (openai.create_connector, {"vault": vault}),
                               (claude.create_connector, {}),
                               (codex.create_connector, {})):
        try:
            built.append(factory(**arguments))
        except Exception:  # noqa: BLE001 - a missing piece removes one vendor path, not the engine
            continue
    return tuple(built)


if __name__ == "__main__":
    vault = LazyUserVault()
    raise SystemExit(main(connectors=build_connectors(vault), vault_factory=lambda: vault))
