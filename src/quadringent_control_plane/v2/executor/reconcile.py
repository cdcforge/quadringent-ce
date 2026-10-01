"""Boucle de réconciliation pure (désiré vs observé), chantier 4.

Ces fonctions ne font aucun appel réseau : elles comparent un manifeste
désiré (construit par ``manifests.py``, avec son empreinte de ``spec``
posée par ``with_spec_hash``) à l'objet Kubernetes observé (tel que rendu
par l'API, ou ``None`` si absent) et rendent la liste d'actions à
appliquer. Idempotence : si l'objet observé porte déjà la même empreinte de
``spec``, aucune action n'est produite — un redémarrage du control plane
ou un appel répété du réconciliateur ne recrée ni ne met à jour un objet
déjà conforme.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .manifests import ANNOTATION_SPEC_HASH, spec_hash

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"

RESOURCE_DEPLOYMENT = "deployment"
RESOURCE_JOB = "job"


@dataclass(frozen=True)
class ReconcileAction:
    kind: str
    resource: str
    namespace: str
    name: str
    manifest: Mapping[str, object] | None = None


def _observed_spec_hash(observed: Mapping[str, object]) -> str | None:
    metadata = observed.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    annotations = metadata.get("annotations")
    if not isinstance(annotations, Mapping):
        return None
    value = annotations.get(ANNOTATION_SPEC_HASH)
    return value if isinstance(value, str) else None


def reconcile_deployment(
    desired: Mapping[str, object] | None,
    observed: Mapping[str, object] | None,
) -> ReconcileAction | None:
    """Un seul Deployment de capture par journal — créé, mis à jour ou retiré.

    ``desired`` porte déjà l'empreinte de ``spec`` (``with_spec_hash``).
    ``desired is None`` signifie « ce journal n'a plus de table live » : le
    Deployment est supprimé (pas seulement mis en pause), pour ne pas
    laisser un lecteur orphelin tourner sans table à capturer.
    """

    if desired is None:
        if observed is None:
            return None
        metadata = observed.get("metadata", {})
        return ReconcileAction(
            ACTION_DELETE,
            RESOURCE_DEPLOYMENT,
            str(metadata.get("namespace")),
            str(metadata.get("name")),
        )
    namespace = str(desired["metadata"]["namespace"])
    name = str(desired["metadata"]["name"])
    if observed is None:
        return ReconcileAction(ACTION_CREATE, RESOURCE_DEPLOYMENT, namespace, name, desired)
    desired_hash = desired["metadata"]["annotations"][ANNOTATION_SPEC_HASH]
    if _observed_spec_hash(observed) == desired_hash:
        return None
    return ReconcileAction(ACTION_UPDATE, RESOURCE_DEPLOYMENT, namespace, name, desired)


def reconcile_jobs(
    desired: Sequence[Mapping[str, object]],
    observed_by_name: Mapping[str, Mapping[str, object]],
) -> list[ReconcileAction]:
    """Jobs de copie initiale/rejeu : création seule, jamais de mise à jour.

    Un Job est immuable une fois créé (Kubernetes l'interdit pour la
    ``spec`` du pod) : un nom déjà observé n'est jamais réappliqué, que son
    contenu corresponde ou non — c'est le même principe que
    ``fleet_job_launcher.py`` (« un Job déjà présent avec une intention ou
    une position différente est refusé, jamais réutilisé silencieusement »).
    Un nom différent est nécessaire pour relancer (nouveau ``run_id``), ce
    que ``manifests.initial_copy_job_name``/``replay_job_name`` garantissent
    déjà en dérivant le nom de l'identifiant de run.
    """

    actions: list[ReconcileAction] = []
    for manifest in desired:
        name = str(manifest["metadata"]["name"])
        if name in observed_by_name:
            continue
        namespace = str(manifest["metadata"]["namespace"])
        actions.append(ReconcileAction(ACTION_CREATE, RESOURCE_JOB, namespace, name, manifest))
    return actions


def spec_matches(manifest: Mapping[str, object], observed: Mapping[str, object]) -> bool:
    """Vrai si ``observed`` porte l'empreinte de la ``spec`` de ``manifest``."""

    return _observed_spec_hash(observed) == spec_hash(manifest["spec"])
