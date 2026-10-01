"""Disposition de stockage brut, unique source de vérité.

Constat du premier pipeline réel sur GKE (24 septembre 2026, `QDC_ORDERS`) :
les trois composants qui lisent/écrivent sous ``AS400_RAW_PREFIX`` employaient
chacun sa propre convention de chemin, jamais la même :

- le lecteur (``scripts/as400_continuous_capture.py``), en mode une seule
  table, écrivait ses lots **à la racine** du préfixe (aucun segment par
  table) ;
- le chargeur (``scripts/quadringent_destination_loader.py``) cherchait ses
  lots sous ``<racine>/<SCHÉMA>/<TABLE>`` (convention « v1 », jamais produite
  par le lecteur v2) ;
- la copie initiale (``scripts/as400_snapshot_publish.py``) publiait son
  instantané sous ``<racine>/snapshot/<table>/...`` (segment ``snapshot``
  *avant* la table).

Résultat : le chargeur ne trouvait jamais rien, et même corrigé, n'aurait
jamais chargé l'instantané (segment ``snapshot`` en tête, jamais cherché là).

Ce module fixe une disposition unique, **la table en premier segment**, déjà
celle documentée (mais pas encore appliquée partout) par
``quadringent.fleet_capture.table_object_prefix`` :

```
<AS400_RAW_PREFIX>/<table>/journal/...     # lots + reçus du lecteur (par table)
<AS400_RAW_PREFIX>/<table>/snapshot/...    # instantané de la copie initiale
<AS400_RAW_PREFIX>/<table_id>/evidence/... # preuve de copie (clé par table_id,
                                            # voir quadringent_control_plane.
                                            # v2.executor.evidence.evidence_key —
                                            # source de vérité distincte : la
                                            # preuve est un objet du control
                                            # plane, jamais importé par l'image
                                            # de capture, qui ne porte pas ce
                                            # paquet)
```

``journal``/``snapshot`` sont routés par **nom** de table (minuscule, la
convention déjà en place côté flotte) : c'est ce que le lecteur connaît sans
consulter la base (``ISERIES_TABLE``). ``evidence`` reste routée par
``table_id`` (clé stable du control plane) : c'est la convention déjà en
place et déjà testée (``evidence.py::evidence_key``), non modifiée ici.

Utilisé par :

- ``quadringent.fleet_capture.table_object_prefix`` (lots + reçus du lecteur,
  mode flotte *et* mode une seule table — voir
  ``v2/executor/manifests.py::build_reader_deployment``) ;
- ``scripts/quadringent_destination_loader.py::raw_prefix_for_table`` (le
  chargeur lit exactement là où le lecteur a écrit) ;
- ``scripts/as400_snapshot_publish.py::snapshot_object_key`` (copie
  initiale) ;
- ``v2/executor/manifests.py`` (construction des manifestes des trois
  composants, seul endroit qui connaît à la fois la racine du site et le nom
  de chaque table).
"""

from __future__ import annotations

import re

_TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")

JOURNAL_SEGMENT = "journal"
SNAPSHOT_SEGMENT = "snapshot"


def _clean_root(raw_prefix_root: str) -> str:
    root = raw_prefix_root.strip().strip("/")
    if ".." in root.split("/"):
        raise ValueError("raw prefix root is unsafe")
    return root


def _clean_table(table_name: str) -> str:
    table = table_name.strip().lower()
    if not _TABLE_NAME.fullmatch(table):
        raise ValueError("table name is unsafe")
    return table


def table_root(raw_prefix_root: str, table_name: str) -> str:
    """Préfixe racine d'une table : ``<racine>/<table>`` (minuscule)."""

    root = _clean_root(raw_prefix_root)
    table = _clean_table(table_name)
    return f"{root}/{table}" if root else table


def journal_prefix(raw_prefix_root: str, table_name: str) -> str:
    """Préfixe des lots/reçus du lecteur pour cette table.

    Même formule que ``quadringent.fleet_capture.table_object_prefix`` (mode
    flotte) : ce module en devient l'implémentation partagée, appelée aussi
    en mode une seule table.
    """

    return f"{table_root(raw_prefix_root, table_name)}/{JOURNAL_SEGMENT}"


def snapshot_prefix(raw_prefix_root: str, table_name: str) -> str:
    """Préfixe de l'instantané de copie initiale pour cette table."""

    return f"{table_root(raw_prefix_root, table_name)}/{SNAPSHOT_SEGMENT}"


def snapshot_object_key(raw_prefix_root: str, table_name: str, filename: str) -> str:
    """Clé complète d'un objet d'instantané (payload ou manifeste)."""

    if not filename or "/" in filename or filename in {".", ".."}:
        raise ValueError("snapshot filename is unsafe")
    return f"{snapshot_prefix(raw_prefix_root, table_name)}/{filename}"
