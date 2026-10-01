"""scripts/generate_chart_values_doc.py doit produire une table Markdown
stable et à jour depuis chart/values.schema.json."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "generate_chart_values_doc.py"
OUTPUT = REPO_ROOT / "docs" / "product" / "chart-values.md"


class GenerateChartValuesDocTests(unittest.TestCase):
    def test_committed_doc_is_up_to_date(self) -> None:
        """Le fichier versionné doit correspondre exactement à ce que
        régénère le script à partir du schéma actuel : évite une doc
        périmée après une modification de values.schema.json."""

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_doc_lists_every_top_level_key(self) -> None:
        import json

        schema = json.loads((REPO_ROOT / "chart" / "values.schema.json").read_text(encoding="utf-8"))
        content = OUTPUT.read_text(encoding="utf-8")
        for key in schema["properties"]:
            self.assertIn(f"`{key}`", content, f"clé racine {key} absente de la doc générée")

    def test_generated_doc_marks_required_root_keys(self) -> None:
        content = OUTPUT.read_text(encoding="utf-8")
        self.assertIn("| `image` | `object` | oui |", content)
        self.assertIn("| `nameOverride` | `string` | non |", content)


if __name__ == "__main__":
    unittest.main()
