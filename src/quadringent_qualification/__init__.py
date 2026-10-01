"""Suite de qualification de bout en bout, générique et hors exécution (chantier 7).

Ce paquet ne contacte jamais un IBM i, un cloud ou un Snowflake réel pendant les
tests : la génération de l'oracle, la normalisation canonique et le
rapprochement sont des fonctions pures, testées hors ligne. L'exécution réelle
passe par des adaptateurs (``adapters.py``) branchés par l'orchestrateur
(``orchestrator.py``) à partir d'une configuration YAML/JSON (``config.py``).

Ce paquet n'est volontairement pas inclus dans le wheel d'exécution du produit
(voir ``pyproject.toml``) : c'est un outil de qualification, pas une
dépendance runtime de Quadringent.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
