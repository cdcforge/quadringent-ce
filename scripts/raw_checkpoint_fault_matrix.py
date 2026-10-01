#!/usr/bin/env python3
"""Vérifie hors réseau la persistance du brut avant le checkpoint."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from quadringent.fault_matrix import run_fault_matrix
from site_fixture import build_test_site


if __name__ == "__main__":
    print(json.dumps(run_fault_matrix(site=build_test_site()), sort_keys=True))
