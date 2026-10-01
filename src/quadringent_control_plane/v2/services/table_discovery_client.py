"""Client de découverte de tables réel — implémente ``TableDiscoveryClientProtocol``.

Adapte ``quadringent.java_worker.PersistentJavaWorker`` (processus persistant
géré, ``discover`` renvoie une sortie texte ligne à ligne) au contrat attendu
par ``services/tables.py::TablesService.refresh`` (``tuple[DiscoveredTable, ...]``),
en réutilisant ``quadringent.table_discovery.parse_discover_output`` — la
même fonction de parsing que le CLI/worker historique, une seule vérité
entre les deux chemins.

Ce module ne parle jamais lui-même au processus Java : il délègue à un
objet dont il n'exige que la méthode ``discover`` (duck typing), pour rester
testable sans sous-processus réel.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from quadringent.table_discovery import DiscoveredTable, parse_discover_output


class DiscoverCapableWorker(Protocol):
    """Sous-ensemble de ``PersistentJavaWorker`` utilisé par cet adaptateur."""

    def discover(
        self, *, libraries: Sequence[str] | None = None, limit: int = 500, search: str | None = None
    ) -> str: ...


class PersistentJavaWorkerTableDiscoveryClient:
    """Adapte un worker Java persistant au contrat ``TableDiscoveryClientProtocol``."""

    def __init__(self, worker: DiscoverCapableWorker) -> None:
        self._worker = worker

    def discover(
        self, *, libraries: tuple[str, ...] | None, limit: int, search: str | None
    ) -> tuple[DiscoveredTable, ...]:
        raw_output = self._worker.discover(libraries=libraries, limit=limit, search=search)
        return parse_discover_output(raw_output)
