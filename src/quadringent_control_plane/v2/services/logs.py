"""Journaux de pipeline v2 (``GET /v2/pipelines/{id}/logs``, contrat §2.4).

Comme ``PipelineExecutorProtocol``/``TableDiscoveryClientProtocol``, la
source de journaux est injectable — jamais de client Kubernetes câblé ici.
Une implémentation réelle lira les logs du pod du lecteur (mêmes patterns
que le client Kubernetes déjà utilisé côté runtime de flotte : préfixe de
conteneur, ``kubectl logs``/API Kubernetes) ou des événements enregistrés
(``events`` — table v2) ; sans source injectée, ``NullLogSource`` renvoie
une liste vide (échec fermé, jamais un journal inventé).

Règle de rédaction (documentée ici, testée dans
``tests/test_v2_pipeline_logs.py``) : avant de renvoyer un message au
client, ce module masque systématiquement
  1. les paires ``clé=valeur``/``clé: valeur`` dont la clé ressemble à un
     secret (``password``, ``secret``, ``token``, ``api_key``,
     ``authorization``, ``credential``) — remplacées par
     ``<clé>=[SECRET_MASQUE]`` ;
  2. les blocs ``{...}`` contenant au moins deux paires ``"clé": valeur``
     (heuristique : un objet JSON à plusieurs champs ressemble à une ligne
     de donnée source sérialisée, jamais à un simple identifiant) —
     remplacés en bloc par ``{"redacted": "donnee_de_ligne_masquee"}``.
Cette rédaction s'applique à **toute** source (Kubernetes ou événements
enregistrés) : elle vit dans ce service, pas dans les fournisseurs, pour ne
jamais dépendre de la discipline d'un client injecté.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

_SECRET_KEY_VALUE = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|authorization|credential)s?"
    r"\s*[:=]\s*\"?([^\s,;\"]+)\"?"
)
_JSON_LIKE_BLOCK = re.compile(r"\{[^{}]*\}")


def redact(message: str) -> str:
    """Masque secrets et blocs ressemblant à de la donnée de ligne — voir
    l'en-tête du module pour la règle exacte."""

    redacted = _SECRET_KEY_VALUE.sub(lambda match: f"{match.group(1)}=[SECRET_MASQUE]", message)

    def _mask_row_like_block(match: re.Match[str]) -> str:
        block = match.group(0)
        if block.count(":") >= 2:
            return '{"redacted": "donnee_de_ligne_masquee"}'
        return block

    return _JSON_LIKE_BLOCK.sub(_mask_row_like_block, redacted)


@dataclass(frozen=True)
class RawLogEntry:
    """Entrée brute renvoyée par une source injectée, avant rédaction."""

    at: str
    level: str
    message: str
    incident_id: str | None = None


@dataclass(frozen=True)
class LogEntry:
    at: str
    level: str
    message: str
    incident_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "level": self.level,
            "message": self.message,
            "incident_id": self.incident_id,
        }


KNOWN_LEVELS = frozenset({"info", "warning", "error"})


class LogSourceProtocol(Protocol):
    """Contrat minimal d'une source de journaux — jamais Kubernetes ici."""

    def fetch(self, pipeline_id: str, *, since: str | None) -> tuple[RawLogEntry, ...]: ...


class NullLogSource:
    """Source par défaut : échoue fermé, aucun journal disponible."""

    def fetch(self, pipeline_id: str, *, since: str | None) -> tuple[RawLogEntry, ...]:
        return ()


class LogsService:
    """Filtre (niveau, depuis, corrélation d'incident) + redacte, quelle que
    soit la source injectée."""

    def __init__(self, source: LogSourceProtocol | None = None) -> None:
        self._source = source if source is not None else NullLogSource()

    def fetch(
        self,
        pipeline_id: str,
        *,
        since: str | None = None,
        level: str | None = None,
        incident: bool = False,
    ) -> tuple[LogEntry, ...]:
        raw_entries = self._source.fetch(pipeline_id, since=since)
        entries = []
        for raw in raw_entries:
            if level is not None and raw.level != level:
                continue
            if incident and raw.incident_id is None:
                continue
            entries.append(
                LogEntry(
                    at=raw.at,
                    level=raw.level,
                    message=redact(raw.message),
                    incident_id=raw.incident_id,
                )
            )
        return tuple(entries)
