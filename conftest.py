"""Rend le paquet importable depuis la racine du dépôt."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for extra in ("tests", "scripts", "src"):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

import site_fixture  # noqa: E402,F401  — déclare le site fictif avant les imports de tests
