"""Source and PyInstaller entrypoint. Only packaged local modules are imported."""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from aik_spike.server import main

if __name__ == "__main__":
    raise SystemExit(main())
