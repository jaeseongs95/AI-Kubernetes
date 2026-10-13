"""Source and PyInstaller entrypoint. Only packaged local and product modules are imported."""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # Source runs reach the product packages (aik_vault, ...) used by the AIK-022 probe;
    # the frozen engine bundles them through build-sidecar.ps1 --paths src.
    _PRODUCT_SOURCE = Path(__file__).resolve().parents[3] / "src"
    if _PRODUCT_SOURCE.is_dir():
        sys.path.insert(1, str(_PRODUCT_SOURCE))

from aik_spike.server import main

if __name__ == "__main__":
    raise SystemExit(main())
