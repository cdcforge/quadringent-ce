#!/usr/bin/env python3
"""Compatibilité du lanceur historique ; installer le paquet avant utilisation."""
from pathlib import Path
import sys

# Évite que ce fichier masque le paquet installé du même nom.
_DIRECTORY = str(Path(__file__).resolve().parent)
if _DIRECTORY in sys.path:
    sys.path.remove(_DIRECTORY)

from quadringent_control_plane.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
