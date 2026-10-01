"""Composition du control plane DEV avec le lancement réel de Jobs.

Point d'assemblage unique : le plan de flotte, les stores d'intention, le
fournisseur de checkpoint frais et les deux lanceurs Kubernetes. La fonction
échoue fermé : un élément manquant, un plan hors contrat ou un environnement
incomplet empêchent la construction plutôt que de produire un exécuteur
partiel qui répondrait « indisponible » à la place d'un vrai lancement.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping

from quadringent.java_catalog import JavaReceiverCatalog
from quadringent.site_config import current as _current_site

from . import fleet as _fleet
from .fleet import FleetError
from .fleet_action_executor import FleetActionExecutor
from .fleet_job_launcher import HistoryJobLauncher, JobTemplate, ReaderJobLauncher
from .fleet_pause_runtime import FleetPauseRuntime
from .fleet_plan import READER_KIND, FleetPlan
from .fleet_progression import RUN_STATE_FILE
from .fleet_providers import ReceiverTailCheckpointProvider
from .fleet_runtime_store import AtomicJsonStateStore
from .k8s_jobs import KubernetesJobsClient


PREPARE_STATE_FILE = "fleet-prepare.json"
HISTORY_STATE_FILE = "fleet-history.json"
PAUSE_STATE_FILE = "fleet-pause.json"

CODE_INCOMPLETE_CONFIGURATION = "incomplete_launch_configuration"
CODE_UNSUPPORTED_PLAN = "unsupported_launch_plan"

# Seul déploiement en production avant le support multi-site : ses quatre
# fichiers `fleet-*.json` existent déjà sur son PVC, sous ces noms nus. La
# stratégie de compatibilité est donc volontairement asymétrique — ce n'est
# pas une migration de fichiers (renommer sous un site qui tourne perdrait
# la garde de reprise mi-vol), mais une exception de nommage : ce site_id
# précis garde les noms historiques pour toujours, tout autre site_id
# obtient un nom préfixé. Une seconde liaison composée sur le même
# `--fleet-state-dir` n'écrase donc jamais l'état de celle-ci.
LEGACY_SITE_ID = "example-corp"


def _state_filename(base: str, site_id: str) -> str:
    """Nom de fichier d'état pour ``site_id`` — isolé, sauf pour le site legacy."""

    if site_id == LEGACY_SITE_ID:
        return base
    return f"{site_id}-{base}"


@dataclass(frozen=True)
class FleetLaunchConfig:
    """Réglages locaux du lancement, fournis par l'exploitant."""

    state_directory: Path
    raw_prefix_root: str
    job_template_path: Path

    def __post_init__(self) -> None:
        if not str(self.raw_prefix_root).strip():
            raise FleetError(CODE_INCOMPLETE_CONFIGURATION, "Préfixe de lancement absent")
        if not Path(self.job_template_path).is_file():
            raise FleetError(CODE_INCOMPLETE_CONFIGURATION, "Modèle de Job illisible")

    def template(self) -> JobTemplate:
        try:
            payload = json.loads(Path(self.job_template_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise FleetError(CODE_INCOMPLETE_CONFIGURATION, "Modèle de Job illisible") from None
        return JobTemplate.parse(payload)


def load_job_template_document(payload: object) -> JobTemplate:
    """Exposé pour les tests et les outils : un document, un modèle validé."""

    return JobTemplate.parse(payload)


def build_fleet_action_executor(
    *,
    plan: FleetPlan,
    config: FleetLaunchConfig,
    client: KubernetesJobsClient,
    environ: Mapping[str, str] | None = None,
    catalog: object | None = None,
    console_reader: object | None = None,
    site_id: str | None = None,
) -> FleetActionExecutor:
    """Assemble l'exécuteur réel ; refuse un plan hors contrat DEV.

    ``site_id`` isole les quatre fichiers d'état sous ``--fleet-state-dir`` :
    par défaut, celui du site courant (:func:`quadringent.site_config.current`)
    — le même que celui déjà résolu par le plan et le reste du module. Deux
    liaisons composées sur le même répertoire n'écrivent jamais le même
    fichier, hors du site legacy ``example-corp`` (voir :data:`LEGACY_SITE_ID`).
    """

    source = os.environ if environ is None else environ
    _assert_dev_plan(plan)
    template = config.template()
    receiver_catalog = catalog if catalog is not None else _java_catalog(plan, source)
    state_directory = Path(config.state_directory)
    resolved_site_id = site_id if site_id is not None else _current_site().site_id
    prepare_store = AtomicJsonStateStore(
        state_directory / _state_filename(PREPARE_STATE_FILE, resolved_site_id)
    )
    history_store = AtomicJsonStateStore(
        state_directory / _state_filename(HISTORY_STATE_FILE, resolved_site_id)
    )
    pause_store = AtomicJsonStateStore(
        state_directory / _state_filename(PAUSE_STATE_FILE, resolved_site_id)
    )
    provider = ReceiverTailCheckpointProvider(receiver_catalog)
    reader_launcher = ReaderJobLauncher(
        client,
        template,
        raw_prefix_root=config.raw_prefix_root,
    )
    history_launcher = HistoryJobLauncher(
        client,
        template,
        run_prefix_root=config.raw_prefix_root,
    )
    pause_runtime = FleetPauseRuntime(prepare_store, history_store, pause_store, client)
    return FleetActionExecutor(
        plan,
        prepare_store,
        history_store,
        provider,
        reader_launcher,
        history_launcher,
        pause_runtime=pause_runtime,
        run_store=AtomicJsonStateStore(
            state_directory / _state_filename(RUN_STATE_FILE, resolved_site_id)
        ),
        console_reader=console_reader,
        jobs=client,
    )


def _assert_dev_plan(plan: object) -> None:
    if type(plan) is not FleetPlan:
        raise FleetError(CODE_UNSUPPORTED_PLAN, "Plan de flotte invalide")
    if plan.environment != _fleet.ENVIRONMENT:
        raise FleetError(CODE_UNSUPPORTED_PLAN, "Plan hors environnement DEV")
    if _fleet.TABLE_COUNT != len(_fleet.MANIFEST):
        raise FleetError(CODE_UNSUPPORTED_PLAN, "Manifeste DEV incomplet")
    groups = plan.journal_groups
    if type(groups) is not tuple or len(groups) != 1:
        raise FleetError(CODE_UNSUPPORTED_PLAN, "Groupes de journaux DEV invalides")
    if groups[0].reader_kind != READER_KIND or groups[0].table_names != _fleet.MANIFEST:
        raise FleetError(CODE_UNSUPPORTED_PLAN, "Lecteur de flotte non conforme")


def _java_catalog(plan: FleetPlan, environ: Mapping[str, str]) -> JavaReceiverCatalog:
    group = plan.journal_groups[0]
    java = (environ.get("AS400_JAVA") or "java").strip()
    classpath = (environ.get("AS400_JAVA_CLASSPATH") or "").strip()
    host = (environ.get("ISERIES_HOST") or "").strip()
    user = (environ.get("ISERIES_USER") or "").strip()
    if not classpath or not host or not user:
        raise FleetError(
            CODE_INCOMPLETE_CONFIGURATION,
            "Identité IBM i ou classe Java absente pour relever le journal",
        )
    return JavaReceiverCatalog(
        java=java,
        classpath=classpath,
        host=host,
        user=user,
        journal_library=group.library,
        journal_name=group.name,
    )
