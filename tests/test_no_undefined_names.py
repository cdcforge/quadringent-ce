"""Static guard against undefined names in the mounted modules.

A missing `import time` in as400_continuous_capture.py survived unittest
(2026-08-27) because the wiring tests only grepped the source for strings and
py_compile does not resolve names. The affected line only runs inside a live
IBM i job, so the failure would have appeared in production.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["src/quadringent", "src/quadringent_control_plane", "scripts"]


def _pyflakes_available() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", "--version"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


class UndefinedNameTests(unittest.TestCase):
    @unittest.skipUnless(_pyflakes_available(), "pyflakes not installed")
    def test_no_undefined_names_anywhere(self) -> None:
        for target in TARGETS:
            self.assertTrue((ROOT / target).is_dir(), f"static analysis target missing: {target}")
        files = [
            str(path)
            for target in TARGETS
            for path in (ROOT / target).glob("*.py")
        ]
        self.assertTrue(files, "static analysis must inspect real files")
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", *files],
            capture_output=True, text=True, check=False,
        )
        offending = [
            line for line in result.stdout.splitlines()
            if "undefined name" in line
        ]
        self.assertEqual(offending, [], "\n".join(offending))

    def test_every_module_imports_cleanly(self) -> None:
        """Import each module so a missing top-level dependency shows up."""

        names = []
        for package in ("quadringent", "quadringent_control_plane"):
            modules = list((ROOT / "src" / package).glob("*.py"))
            self.assertTrue(modules, f"no modules found for {package}")
            for path in modules:
                if path.stem == "__init__":
                    continue
                names.append(f"{package}.{path.stem}")
        # CLI tests put scripts/ first on sys.path; its quadringent_control_plane.py
        # then shadows the package. Check the real source tree in a clean
        # interpreter, independently of test collection order and sys.modules.
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import importlib,sys; sys.path.insert(0,sys.argv[1]); "
             "[importlib.import_module(name) for name in sys.argv[2:]]",
             str(ROOT / "src"), *names],
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
