"""Le magasin de liaisons est-il réellement branché au serveur ?

Les routes ``/v1/connections`` ne sont montées que si ``serve()`` reçoit un
``connections_store``. Sans ce branchement, elles répondent 404 exactement
comme une route inconnue : la fonctionnalité existe dans le code, passe ses
propres tests, et reste invisible en production.

Ce fichier verrouille le raccordement lui-même, pas le magasin.

Le script de lancement est chargé dans un sous-processus : il porte le même
nom que le package ``quadringent_control_plane`` et retouche ``sys.path`` à
l'import, ce qui suffirait à dérégler la découverte des autres tests s'il
était exécuté dans ce processus-ci.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _ROOT / "src" / "quadringent_control_plane" / "cli.py"
_PYTHONPATH = ":".join(
    str(_ROOT / part) for part in ("src", "src/quadringent", "scripts", "tests")
)

_PROBE = """
import argparse, importlib.util, json, sys
spec = importlib.util.spec_from_file_location("launcher", {script!r})
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
store = launcher._build_connections_store(argparse.Namespace(fleet_state_dir={state_dir!r}))
print(json.dumps({{"built": store is not None}}))
"""


def _build_store_in_subprocess(state_dir: object) -> bool:
    """Construit le magasin via le script réel et dit s'il existe."""

    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(script=str(_SCRIPT_PATH), state_dir=state_dir)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": _PYTHONPATH, "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"sonde en échec : {result.stderr.strip()}")
    return bool(__import__("json").loads(result.stdout.strip())["built"])


class ConnectionsWiringTests(unittest.TestCase):
    def test_un_repertoire_d_etat_product_un_magasin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(_build_store_in_subprocess(directory))

    def test_un_demarrage_neuf_cree_le_repertoire_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nouveau" / "etat"
            self.assertTrue(_build_store_in_subprocess(str(path)))
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_sans_repertoire_d_etat_aucune_liaison_n_est_acceptee(self) -> None:
        # Accepter une création que rien ne conserverait serait pire qu'un 404 :
        # l'opérateur croirait sa liaison enregistrée.
        for value in ("", "   "):
            self.assertFalse(_build_store_in_subprocess(value))

    def test_serve_recoit_le_magasin(self) -> None:
        """``serve()`` doit être appelé avec ``connections_store``.

        Vérifié sur le source : démarrer ``main()`` exigerait S3
        et des sondes que ce test n'a pas à reconstituer pour prouver un
        passage d'argument.
        """

        import ast
        tree = ast.parse(_SCRIPT_PATH.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "serve"]
        self.assertEqual(len(calls), 1)
        self.assertIn("connections_store", {keyword.arg for keyword in calls[0].keywords})

    def test_le_magasin_vise_le_repertoire_d_etat_de_la_flotte(self) -> None:
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        builder = source[source.index("def _build_connections_store(") :]
        builder = builder[: builder.index("\n\n\n")]
        self.assertIn("fleet_state_dir", builder)
        self.assertIn("connections_state_path", builder)


if __name__ == "__main__":
    unittest.main()
