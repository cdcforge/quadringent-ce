"""Source de journaux Kubernetes réelle (``LogSourceProtocol``, chantier
« observabilité v2 », suite).

Sélectionne les pods par le label posé par l'exécuteur
(``executor/manifests.py::LABEL_PIPELINE`` = ``quadringent.io/pipeline-id``)
— exactement les Jobs de copie initiale et de rejeu, qui portent ce label
sur leur pod (voir ``build_initial_copy_job``/``build_replay_job``). **Hors
périmètre** : le Deployment lecteur (un par source+journal, partagé entre
plusieurs pipelines — voir ``build_reader_deployment``, dont le pod n'est
labellisé que par ``source-id``/``journal-key``, jamais par
``pipeline-id``) n'a pas de sélection non ambiguë par pipeline avec le
modèle de labels actuel ; l'inclure exigerait de deviner quelles lignes lui
appartiennent, ce que ce module refuse de faire. Décision documentée, pas
un oubli — un futur chantier peut ajouter la résolution
source_id/journal_key + un filtrage par identifiant de table dans le texte
des lignes.

Chaque ligne k8s est demandée avec ``timestamps=true`` (horodatage RFC3339
posé par le serveur Kubernetes lui-même, jamais recalculé ici). Le niveau
(``info``/``warning``/``error``) est une heuristique textuelle documentée
(comme ``redact()`` dans ``services/logs.py``) : présence du mot
``error``/``ERROR`` -> ``error`` ; ``warn``/``WARNING`` -> ``warning`` ;
sinon ``info``. ``incident_id`` est extrait d'un motif ``incident_id=...``
explicite dans la ligne, jamais deviné en son absence.
"""

from __future__ import annotations

import re

from ...k8s_pods import KubernetesPodsClient
from .logs import RawLogEntry

LABEL_PIPELINE = "quadringent.io/pipeline-id"

_TIMESTAMPED_LINE = re.compile(r"^(?P<at>\S+)\s(?P<message>.*)$")
_INCIDENT_PATTERN = re.compile(r"incident_id[=:]\s*([A-Za-z0-9_-]+)")
_ERROR_PATTERN = re.compile(r"error", re.IGNORECASE)
_WARNING_PATTERN = re.compile(r"warn", re.IGNORECASE)

_MAX_ENTRIES_PER_POD = 5000


class KubernetesLogSource:
    """``LogSourceProtocol`` réel — lecture bornée des journaux des pods du
    Job de pipeline (copie initiale/rejeu), sélectionnés par label."""

    def __init__(
        self,
        pods_client: KubernetesPodsClient,
        *,
        tail_lines: int = 200,
        max_pods: int = 5,
    ) -> None:
        self._pods_client = pods_client
        self._tail_lines = tail_lines
        self._max_pods = max_pods

    def fetch(self, pipeline_id: str, *, since: str | None) -> tuple[RawLogEntry, ...]:
        selector = f"{LABEL_PIPELINE}={pipeline_id}"
        pod_names = self._pods_client.list_pod_names(label_selector=selector, limit=self._max_pods)
        entries: list[RawLogEntry] = []
        for pod_name in pod_names:
            raw_text = self._pods_client.read_pod_log(pod_name, since_time=since, tail_lines=self._tail_lines)
            if raw_text is None:
                continue
            for line in raw_text.splitlines()[:_MAX_ENTRIES_PER_POD]:
                entry = _parse_line(line)
                if entry is not None:
                    entries.append(entry)
        entries.sort(key=lambda entry: entry.at)
        return tuple(entries)


def _parse_line(line: str) -> RawLogEntry | None:
    if not line.strip():
        return None
    match = _TIMESTAMPED_LINE.match(line)
    if match is None:
        return None
    at = match.group("at")
    message = match.group("message")
    incident_match = _INCIDENT_PATTERN.search(message)
    incident_id = incident_match.group(1) if incident_match else None
    if _ERROR_PATTERN.search(message):
        level = "error"
    elif _WARNING_PATTERN.search(message):
        level = "warning"
    else:
        level = "info"
    return RawLogEntry(at=at, level=level, message=message, incident_id=incident_id)
