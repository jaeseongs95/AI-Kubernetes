"""Source entrypoint of the desktop engine for ``python -I`` runs (AIK-045).

``-I`` ignores PYTHONPATH and the user site, so the product sources are added
from this checkout explicitly. A frozen engine bundles the packages instead.
"""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from aik_desktop_engine import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
