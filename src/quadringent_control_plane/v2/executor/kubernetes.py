"""Client Deployments Kubernetes et exécuteur de pipeline v2 (chantier 4).

``KubernetesDeploymentsClient`` reprend exactement le style de
``k8s_jobs.KubernetesJobsClient`` (même transport injectable, mêmes codes
d'erreur réduits) pour l'étendre aux Deployments — nécessaires pour le
lecteur de capture continue, qui n'est pas un Job.

``KubernetesPipelineExecutor`` implémente ``PipelineExecutorProtocol``
(``services/pipelines.py``) : c'est l'exécuteur réel injecté à la place
d'un faux en production. Il ne décide jamais seul d'une transition
``declared_state`` (c'est le rôle de ``state_machine.py``, déjà validé
avant l'appel) — il traduit l'évènement en objets Kubernetes désirés et les
applique via la réconciliation idempotente de ``reconcile.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import datetime as _dt
import uuid
from typing import Mapping, Protocol, Sequence

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..services.destination_verifier import (
    DECLARED_HISTORY_SCHEMA, DECLARED_MIRROR_SCHEMA,
    DestinationVerifierError, validated_destination_scope,
)
from ..services.state_machine import transition
from .boundary import JournalBoundary, plan_bootstrap
from .evidence import EvidenceReader, InitialCopyEvidence, evidence_key
from .manifests import (
    ENV_BOOTSTRAP_OBSERVED_AT,
    ENV_BOOTSTRAP_RECEIVER,
    ENV_BOOTSTRAP_RECEIVER_LIBRARY,
    ENV_BOOTSTRAP_SEQUENCE,
    InitialCopyDesiredSpec,
    LoaderDesiredSpec,
    LoaderTableSpec,
    ReaderDesiredSpec,
    ReplayDesiredSpec,
    TableBootstrap,
    build_initial_copy_job,
    build_loader_deployment,
    build_reader_deployment,
    build_replay_job,
    destination_secret_ref,
    ibmi_ca_secret_ref,
    ibmi_secret_ref,
    initial_copy_job_name,
    loader_deployment_name,
    with_spec_hash,
)
from .reconcile import ACTION_CREATE, ACTION_UPDATE, reconcile_deployment
from ...fleet_job_launcher import job_is_terminal
from ...k8s_deployments import DeploymentsApiError, DeploymentAlreadyExists, KubernetesDeploymentsClient
from ...k8s_jobs import JobAlreadyExists, JobsApiError, KubernetesJobsClient

JOB_OUTCOME_MISSING = "missing"
JOB_OUTCOME_RUNNING = "running"
JOB_OUTCOME_SUCCEEDED = "succeeded"
JOB_OUTCOME_FAILED = "failed"


def _ca_secret_ref_if_pinned(source_id: str, tls_pinned_pem: str | None) -> str | None:
    """Nom du Secret CA épinglé à référencer dans un manifeste, ou ``None``.

    ``manifests.py::_with_ca_mount`` ne monte rien sans cette valeur —
    jamais un CA par défaut, jamais pour une autorité publique/encore
    ``"unknown"`` (voir ``sources.tls_pinned_pem``, migration
    0013_source_tls_pin)."""

    return ibmi_ca_secret_ref(source_id) if tls_pinned_pem else None


def _loader_scope(manifest: Mapping[str, object]) -> tuple[str | None, str | None, str | None]:
    """Identité effective ; les anciens manifestes ont un schéma unique."""
    try:
        containers = manifest["spec"]["template"]["spec"]["containers"]
        container = next(item for item in containers if item["name"] == "destination-loader")
        env = {item["name"]: item.get("value") for item in container.get("env", ())}
        history = env.get("QUADRINGENT_DESTINATION_SCHEMA")
        return env.get("QUADRINGENT_DESTINATION_DATABASE"), history, env.get("QUADRINGENT_MIRROR_SCHEMA", history)
    except (KeyError, TypeError, StopIteration):
        return None, None, None


class ExecutorError(RuntimeError):
    """Erreur d'exécution réduite à un code sûr — jamais de détail distant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BoundaryReaderProtocol(Protocol):
    """Sonde IBM i injectée : lit la position du journal (étape 1 du protocole).

    Câblée en production vers ``QSYS2.JOURNAL_RECEIVER_INFO`` via
    ``ReadOnlyReceiverCatalog``/``PersistentJavaWorker`` ; un faux lecteur
    déterministe est injecté en tests.
    """

    def read_boundary(self, *, source_id: str, table_id: str) -> JournalBoundary: ...


class SecretsProvisionerProtocol(Protocol):
    """Sous-ensemble de ``secrets_provisioner.SecretsProvisioner`` requis ici."""

    def provision_source_secret(self, source_id: str) -> str: ...

    def provision_destination_secret(self, destination_id: str) -> str: ...

    def provision_source_ca_secret(self, source_id: str) -> str | None: ...


@dataclass(frozen=True)
class ExecutorConfig:
    namespace: str
    reader_image: str
    copy_image: str
    replay_image: str
    raw_prefix_root: str
    storage_backend: str
    # ServiceAccount à identité cloud (S3/GCS/DynamoDB) du site — jamais
    # celui du control plane, qui n'a pas ces droits (voir
    # ``chart/templates/control-plane.yaml``, ``serviceAccount.name`` de la
    # chart, distinct de ``controlPlane.serviceAccount.name``).
    service_account_name: str
    reader_timeout_seconds: int = 300
    reader_poll_seconds: float = 5.0
    loader_poll_seconds: float = 10.0
    loader_flush_each_batch: bool = False
    loader_history_mode: str = "streaming"
    active_deadline_seconds: int = 3600
    # Chargeur de destination (historique Snowpipe/SQL + MERGE miroir,
    # docs/decisions/2026-09-23-miroir-snowflake.md). ``loader_image`` vide
    # désactive la réconciliation du Deployment chargeur (compatibilité
    # ascendante : un site qui ne déclare pas cette image reste sur le
    # chemin COPY/MERGE existant, piloté hors de cet exécuteur). Les anciens
    # champs de scope restent acceptés ; le chargeur v2 utilise exclusivement
    # la destination persistée, comme le script SQL et /verify.
    loader_image: str = ""
    destination_database: str = "QUADRINGENT"
    destination_schema: str = "CURATED"
    # Repris tels quels par quadringent.storage_backend.StorageBackend.
    # from_environment côté chargeur — vides tant que loader_image ne l'est
    # pas non plus (le constructeur de LoaderDesiredSpec les exige alors).
    raw_bucket: str = ""
    checkpoint_location: str = ""

    def __post_init__(self) -> None:
        if not 0 < self.reader_poll_seconds <= 60 or not 0 < self.loader_poll_seconds <= 60:
            raise ValueError("les intervalles de lecture et de chargement doivent être entre 0 et 60 s")
        if self.loader_history_mode not in {"streaming", "sql"}:
            raise ValueError("loader_history_mode doit être streaming ou sql")


@dataclass(frozen=True)
class _PipelineContext:
    pipeline_id: str
    declared_state: str
    table_id: str
    schema_name: str
    table_name: str
    journal_library: str | None
    journal_name: str | None
    source_id: str
    source_time_zone: str
    destination_id: str
    ibmi_host: str
    ibmi_user: str
    # CA épinglé de la source (``sources.tls_pinned_pem``, migration
    # 0013_source_tls_pin) — ``None`` pour une autorité publique ou encore
    # ``"unknown"`` (jamais montée tant que l'opérateur ne l'a pas épinglée).
    tls_pinned_pem: str | None = None


class KubernetesPipelineExecutor:
    """Implémente ``PipelineExecutorProtocol`` — le seul point d'entrée K8s."""

    def __init__(
        self,
        engine: Engine,
        *,
        jobs_client: KubernetesJobsClient,
        deployments_client: KubernetesDeploymentsClient,
        boundary_reader: BoundaryReaderProtocol,
        evidence_reader: EvidenceReader,
        config: ExecutorConfig,
        secrets_provisioner: SecretsProvisionerProtocol | None = None,
        run_id_factory=lambda: str(uuid.uuid4()),
        now=lambda: _dt.datetime.now(_dt.timezone.utc),
    ) -> None:
        self._engine = engine
        self._jobs = jobs_client
        self._deployments = deployments_client
        self._boundary_reader = boundary_reader
        self._evidence_reader = evidence_reader
        self._config = config
        # Optionnel : sans provisionneur injecté, les Secrets référencés
        # (``envFrom.secretRef``) doivent déjà exister — provisionnés par la
        # chart ou par un appel explicite ailleurs. Avec un provisionneur,
        # ils sont maintenus à jour (upsert idempotent) avant chaque Job/
        # Deployment qui les référence — jamais de valeur journalisée ici,
        # cf. ``secrets_provisioner.py``.
        self._secrets_provisioner = secrets_provisioner
        self._run_id_factory = run_id_factory
        self._now = now

    # -- PipelineExecutorProtocol -------------------------------------------------

    def execute(self, *, pipeline_id: str, event: str) -> None:
        """Traduit l'évènement déjà validé par la machine à états (tâche 5).

        ``PipelinesService.apply_action`` calcule l'état suivant *avant*
        d'appeler l'exécuteur, puis ne persiste ``declared_state`` en base
        qu'*après* — l'exécuteur ne peut donc pas relire l'état suivant
        depuis la base pendant son propre appel. Il le recalcule ici avec
        la même fonction pure (``state_machine.transition``), à partir de
        l'état encore courant chargé par ``_load_context`` : c'est
        strictement la même transition, déjà validée en amont, jamais une
        nouvelle décision. ``replay`` ne change jamais l'état déclaré (cf.
        ``services/pipelines.py``) : aucune transition à recalculer.
        """

        context = self._load_context(pipeline_id)
        if event == "replay":
            return  # traité par `replay_range`, jamais par la machine à états
        target_state = transition(context.declared_state, event)
        override = {pipeline_id: target_state}
        if event in ("start", "restart_initial_copy"):
            self._start(context, override=override)
        else:
            self._reconcile_reader_for_source(context.source_id, override=override)
            self._reconcile_loader_for_destination(context.destination_id, override=override)

    # -- Actions de haut niveau ----------------------------------------------------

    def replay_range(self, pipeline_id: str, *, from_sequence: int, to_sequence: int) -> Mapping[str, object]:
        context = self._load_context(pipeline_id)
        if context.journal_library is None or context.journal_name is None:
            raise ExecutorError("capability_unavailable", "aucun journal connu pour cette table")
        boundary = self._boundary_reader.read_boundary(source_id=context.source_id, table_id=context.table_id)
        manifest = with_spec_hash(
            build_replay_job(
                ReplayDesiredSpec(
                    pipeline_id=context.pipeline_id,
                    table_id=context.table_id,
                    schema_name=context.schema_name,
                    table_name=context.table_name,
                    source_id=context.source_id,
                    image=self._config.replay_image,
                    namespace=self._config.namespace,
                    storage_backend=self._config.storage_backend,
                    source_time_zone=context.source_time_zone,
                    raw_prefix=self._config.raw_prefix_root,
                    receiver_name=boundary.receiver_name,
                    from_sequence=from_sequence,
                    to_sequence=to_sequence,
                    destination_secret_ref=destination_secret_ref(context.destination_id),
                    ibmi_secret_ref=ibmi_secret_ref(context.source_id),
                    service_account_name=self._config.service_account_name,
                    active_deadline_seconds=self._config.active_deadline_seconds,
                    ca_secret_ref=_ca_secret_ref_if_pinned(context.source_id, context.tls_pinned_pem),
                )
            )
        )
        self._create_job_idempotent(manifest)
        return manifest

    def copying_evidence(self, pipeline_id: str, run_id: str) -> InitialCopyEvidence | None:
        context = self._load_context(pipeline_id)
        key = evidence_key(self._config.raw_prefix_root, context.table_id, run_id)
        return self._evidence_reader.read(key)

    # -- Interne ---------------------------------------------------------------

    def _load_context(self, pipeline_id: str) -> _PipelineContext:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        v2_schema.pipelines.c.id,
                        v2_schema.pipelines.c.declared_state,
                        v2_schema.pipelines.c.destination_id,
                        v2_schema.tables.c.id.label("table_id"),
                        v2_schema.tables.c.schema_name,
                        v2_schema.tables.c.table_name,
                        v2_schema.tables.c.journal_library,
                        v2_schema.tables.c.journal_name,
                        v2_schema.tables.c.source_id,
                        v2_schema.sources.c.detected_timezone,
                        v2_schema.sources.c.tls_pinned_pem,
                        v2_schema.sources.c.ibmi_host,
                        v2_schema.sources.c.ibmi_user,
                    )
                    .select_from(
                        v2_schema.pipelines.join(
                            v2_schema.tables, v2_schema.tables.c.id == v2_schema.pipelines.c.table_id
                        ).join(v2_schema.sources, v2_schema.sources.c.id == v2_schema.tables.c.source_id)
                    )
                    .where(v2_schema.pipelines.c.id == pipeline_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ExecutorError("not_found", "pipeline introuvable pour l'exécuteur")
        source_time_zone = row["detected_timezone"]
        if not source_time_zone:
            raise ExecutorError(
                "capability_unavailable",
                "AS400_SOURCE_TIME_ZONE inconnu : relancer le test de la source (règle de capture requise)",
            )
        return _PipelineContext(
            pipeline_id=row["id"],
            declared_state=row["declared_state"],
            table_id=row["table_id"],
            schema_name=row["schema_name"],
            table_name=row["table_name"],
            journal_library=row["journal_library"],
            journal_name=row["journal_name"],
            source_id=row["source_id"],
            source_time_zone=source_time_zone,
            destination_id=row["destination_id"],
            ibmi_host=row["ibmi_host"],
            ibmi_user=row["ibmi_user"],
            tls_pinned_pem=row["tls_pinned_pem"],
        )

    def _start(self, context: _PipelineContext, *, override: Mapping[str, str]) -> None:
        self._ensure_secrets_provisioned(source_id=context.source_id, destination_id=context.destination_id)
        boundary = plan_bootstrap(
            self._boundary_reader.read_boundary(source_id=context.source_id, table_id=context.table_id)
        )
        run_id = self._run_id_factory()
        key = evidence_key(self._config.raw_prefix_root, context.table_id, run_id)
        manifest = with_spec_hash(
            build_initial_copy_job(
                InitialCopyDesiredSpec(
                    pipeline_id=context.pipeline_id,
                    table_id=context.table_id,
                    schema_name=context.schema_name,
                    table_name=context.table_name,
                    source_id=context.source_id,
                    image=self._config.copy_image,
                    namespace=self._config.namespace,
                    storage_backend=self._config.storage_backend,
                    source_time_zone=context.source_time_zone,
                    raw_prefix=self._config.raw_prefix_root,
                    boundary=boundary,
                    run_id=run_id,
                    evidence_key=key,
                    destination_secret_ref=destination_secret_ref(context.destination_id),
                    ibmi_secret_ref=ibmi_secret_ref(context.source_id),
                    service_account_name=self._config.service_account_name,
                    ibmi_host=context.ibmi_host,
                    ibmi_user=context.ibmi_user,
                    raw_bucket=self._config.raw_bucket,
                    active_deadline_seconds=self._config.active_deadline_seconds,
                    ca_secret_ref=_ca_secret_ref_if_pinned(context.source_id, context.tls_pinned_pem),
                )
            )
        )
        self._create_job_idempotent(manifest)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == context.pipeline_id)
                .values(active_run_id=run_id, attention_reason=None)
            )
        self._reconcile_reader_for_source(context.source_id, override=override)
        self._reconcile_loader_for_destination(context.destination_id, override=override)

    def copy_job_outcome(self, pipeline_id: str, run_id: str) -> str:
        """Statut du Job de copie initiale pour ce ``run_id`` (tâche 2, boucle de fond).

        Réutilise ``fleet_job_launcher.job_is_terminal`` (déjà éprouvé côté
        flotte v1) plutôt que de réinventer la lecture des conditions d'un
        Job — même définition de « terminé » partout dans le produit.
        """

        name = initial_copy_job_name(self._load_context(pipeline_id).table_id, run_id)
        try:
            job = self._jobs.read_job(name)
        except JobsApiError as error:
            raise ExecutorError("executor_unavailable", "lecture du Job de copie impossible") from error
        if job is None:
            return JOB_OUTCOME_MISSING
        if not job_is_terminal(job):
            return JOB_OUTCOME_RUNNING
        status = job.get("status", {})
        conditions = status.get("conditions", []) if isinstance(status, Mapping) else []
        failed_condition = any(
            isinstance(condition, Mapping)
            and condition.get("type") == "Failed"
            and condition.get("status") == "True"
            for condition in conditions
        )
        failed_counter = isinstance(status, Mapping) and isinstance(status.get("failed"), int) and status.get("failed", 0) > 0
        succeeded_counter = isinstance(status, Mapping) and isinstance(status.get("succeeded"), int) and status.get("succeeded", 0) > 0
        if failed_condition or (failed_counter and not succeeded_counter):
            return JOB_OUTCOME_FAILED
        return JOB_OUTCOME_SUCCEEDED

    def _create_job_idempotent(self, manifest: Mapping[str, object]) -> None:
        try:
            self._jobs.create_job(manifest)
        except JobAlreadyExists:
            return
        except JobsApiError as error:
            raise ExecutorError("executor_unavailable", "création du Job refusée") from error

    def _ensure_secrets_provisioned(self, *, source_id: str, destination_id: str) -> None:
        """Maintient à jour les Secrets référencés, avant tout Job/Deployment qui les cite.

        Sans provisionneur injecté (``self._secrets_provisioner is None``),
        ne fait rien : les Secrets doivent déjà exister (provisionnés par la
        chart ou par un appel explicite ailleurs) — jamais un échec bloquant
        pour un déploiement qui gère ses Secrets autrement.
        """

        if self._secrets_provisioner is None:
            return
        try:
            self._secrets_provisioner.provision_source_secret(source_id)
            self._secrets_provisioner.provision_destination_secret(destination_id)
            # PEM épinglé de la source (``sources.tls_pinned_pem``) — sans
            # épinglage, ``provision_source_ca_secret`` ne fait rien (jamais
            # de Secret créé pour une autorité publique/encore inconnue).
            self._secrets_provisioner.provision_source_ca_secret(source_id)
        except Exception as error:  # noqa: BLE001 — jamais de détail de secret dans l'erreur
            raise ExecutorError(
                "executor_unavailable", "provisionnement des Secrets Kubernetes impossible"
            ) from error

    # -- Réconciliation de dérive (chantier 2026-09-24, tâche 3) ----------------
    #
    # Constat : les Deployments lecteur/chargeur déjà en place n'étaient
    # réappliqués qu'aux transitions de ``declared_state`` pilotées par
    # ``execute`` (pause, resume, start...) — jamais après une simple mise à
    # jour du produit (nouvelle image, manifests changés) pendant qu'un
    # pipeline reste ``live``/``copying``/``paused`` sans transition. Ces deux
    # méthodes publiques exposent la même réconciliation idempotente
    # (``_reconcile_reader_for_source``/``_reconcile_loader_for_destination``,
    # comparaison d'empreinte via ``reconcile_deployment`` — aucune écriture
    # si l'empreinte est identique) à ``ReconciliationLoop``, sans
    # ``override`` : elles ne lisent que l'état déjà persisté.

    def reconcile_source(self, source_id: str) -> None:
        self._reconcile_reader_for_source(source_id)

    def reconcile_destination(self, destination_id: str) -> None:
        self._reconcile_loader_for_destination(destination_id)

    def _reconcile_reader_for_source(
        self, source_id: str, *, override: Mapping[str, str] | None = None
    ) -> None:
        """Recompose et applique le Deployment de lecteur pour chaque journal.

        Regroupe toutes les tables ``copying``/``live`` (pas ``paused``,
        ``attention`` ni ``stopped``) de la source par journal, construit le
        manifeste désiré et le compare à l'observé — idempotent, sûr à
        rappeler à chaque action ou au redémarrage du control plane (sans
        ``override``, il ne lit que l'état déjà persisté).

        ``override`` porte l'état *suivant* d'un pipeline dont la
        transition n'est pas encore persistée (cf. ``execute`` : l'exécuteur
        est appelé avant l'écriture en base) — sans lui, une pause ne
        retirerait la table du lecteur qu'au prochain appel.
        """

        desired_by_journal = self._desired_reader_manifests(source_id, override=override or {})
        observed_journals = self._known_journals(source_id)
        for journal in observed_journals | set(desired_by_journal):
            desired = desired_by_journal.get(journal)
            if desired is not None:
                desired = with_spec_hash(desired)
            name = desired["metadata"]["name"] if desired is not None else self._reader_name(source_id, journal)
            try:
                observed = self._deployments.read_deployment(name)
            except DeploymentsApiError as error:
                raise ExecutorError("executor_unavailable", "lecture du Deployment impossible") from error
            action = reconcile_deployment(desired, observed)
            if action is None:
                continue
            try:
                if action.kind == ACTION_CREATE:
                    self._deployments.create_deployment(action.manifest)
                elif action.kind == ACTION_UPDATE:
                    self._deployments.replace_deployment(action.name, action.manifest)
                else:  # ACTION_DELETE — plus de table live sur ce journal
                    self._deployments.delete_deployment(action.name)
            except DeploymentAlreadyExists:
                pass
            except DeploymentsApiError as error:
                raise ExecutorError("executor_unavailable", "application du Deployment refusée") from error

    def _reader_name(self, source_id: str, journal: tuple[str, str]) -> str:
        from .manifests import reader_deployment_name

        return reader_deployment_name(source_id, journal[0], journal[1])

    def _known_journals(self, source_id: str) -> set[tuple[str, str]]:
        """Tous les journaux jamais vus pour cette source (pas seulement les tables live).

        Nécessaire pour retrouver — et supprimer — un Deployment de lecteur
        dont plus aucune table n'est ``copying``/``live`` : sans lister les
        objets Kubernetes existants (le client volontairement minimal
        n'expose pas de sélecteur d'étiquettes, cf. ``k8s_jobs.py``), c'est
        la seule façon de retrouver son nom déterministe pour vérifier s'il
        doit être retiré.
        """

        with self._engine.connect() as connection:
            rows = connection.execute(
                select(v2_schema.tables.c.journal_library, v2_schema.tables.c.journal_name)
                .where(v2_schema.tables.c.source_id == source_id)
                .distinct()
            ).all()
        return {(row[0], row[1]) for row in rows if row[0] and row[1]}

    def _desired_reader_manifests(
        self, source_id: str, *, override: Mapping[str, str]
    ) -> dict[tuple[str, str], Mapping[str, object]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        v2_schema.tables.c.id,
                        v2_schema.tables.c.schema_name,
                        v2_schema.tables.c.table_name,
                        v2_schema.tables.c.journal_library,
                        v2_schema.tables.c.journal_name,
                        v2_schema.pipelines.c.id.label("pipeline_id"),
                        v2_schema.pipelines.c.declared_state,
                        v2_schema.pipelines.c.active_run_id,
                        v2_schema.pipelines.c.destination_id,
                        v2_schema.sources.c.detected_timezone,
                        v2_schema.sources.c.tls_pinned_pem,
                        v2_schema.sources.c.ibmi_host,
                        v2_schema.sources.c.ibmi_user,
                    )
                    .select_from(
                        v2_schema.tables.join(
                            v2_schema.pipelines, v2_schema.pipelines.c.table_id == v2_schema.tables.c.id
                        ).join(v2_schema.sources, v2_schema.sources.c.id == v2_schema.tables.c.source_id)
                    )
                    .where(v2_schema.tables.c.source_id == source_id)
                )
                .mappings()
                .all()
            )
        by_journal: dict[tuple[str, str], list[Mapping[str, object]]] = {}
        for row in rows:
            state = override.get(row["pipeline_id"], row["declared_state"])
            if state not in ("copying", "live"):
                continue
            if not row["journal_library"] or not row["journal_name"]:
                continue
            key = (row["journal_library"], row["journal_name"])
            by_journal.setdefault(key, []).append(row)
        manifests: dict[tuple[str, str], Mapping[str, object]] = {}
        for (journal_library, journal_name), table_rows in by_journal.items():
            tables = tuple(
                TableBootstrap(
                    table_id=row["id"],
                    schema_name=row["schema_name"],
                    table_name=row["table_name"],
                    boundary=self._reader_boundary(source_id, row),
                )
                for row in table_rows
            )
            source_time_zone = table_rows[0]["detected_timezone"] or "UTC"
            destination_id = table_rows[0]["destination_id"]
            self._ensure_secrets_provisioned(source_id=source_id, destination_id=destination_id)
            manifests[(journal_library, journal_name)] = build_reader_deployment(
                ReaderDesiredSpec(
                    source_id=source_id,
                    journal_library=journal_library,
                    journal_name=journal_name,
                    image=self._config.reader_image,
                    namespace=self._config.namespace,
                    storage_backend=self._config.storage_backend,
                    source_time_zone=source_time_zone,
                    raw_prefix=self._config.raw_prefix_root,
                    reader_timeout_seconds=self._config.reader_timeout_seconds,
                    tables=tables,
                    destination_secret_ref=destination_secret_ref(destination_id),
                    ibmi_secret_ref=ibmi_secret_ref(source_id),
                    service_account_name=self._config.service_account_name,
                    ibmi_host=table_rows[0]["ibmi_host"],
                    ibmi_user=table_rows[0]["ibmi_user"],
                    raw_bucket=self._config.raw_bucket,
                    checkpoint_location=self._config.checkpoint_location,
                    extra_env={"AS400_POLL_SECONDS": str(self._config.reader_poll_seconds)},
                    ca_secret_ref=_ca_secret_ref_if_pinned(source_id, table_rows[0]["tls_pinned_pem"]),
                )
            )
        return manifests

    def _reader_boundary(self, source_id: str, row: Mapping[str, object]) -> JournalBoundary:
        """Réutilise la bascule de la copie ; un journal qui avance ne la déplace pas.

        La preuve GCS reste après l'expiration du Job. Pendant une copie en
        cours, le Job porte déjà la frontière lue avant son lancement.
        """
        run_id = row["active_run_id"]
        if not run_id:
            # Compatibilité avec les anciens pipelines sans copie enregistrée.
            return self._boundary_reader.read_boundary(source_id=source_id, table_id=str(row["id"]))
        try:
            job = self._jobs.read_job(initial_copy_job_name(str(row["id"]), str(run_id)))
        except JobsApiError as error:
            raise ExecutorError("executor_unavailable", "lecture du Job de copie impossible") from error
        if job is not None:
            try:
                environment = job["spec"]["template"]["spec"]["containers"][0]["env"]
                values = {item["name"]: item["value"] for item in environment}
                return JournalBoundary.from_dict({
                    "receiver_library": values[ENV_BOOTSTRAP_RECEIVER_LIBRARY],
                    "receiver_name": values[ENV_BOOTSTRAP_RECEIVER],
                    "last_sequence": values[ENV_BOOTSTRAP_SEQUENCE],
                    "observed_at": values[ENV_BOOTSTRAP_OBSERVED_AT],
                })
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise ExecutorError("capability_unavailable", "frontière du Job de copie illisible") from error
        key = evidence_key(self._config.raw_prefix_root, str(row["id"]), str(run_id))
        evidence = self._evidence_reader.read(key)
        if evidence is None:
            raise ExecutorError("capability_unavailable", "frontière de copie introuvable pour le lecteur")
        if evidence.table_id != row["id"] or evidence.pipeline_id != row["pipeline_id"] or evidence.run_id != run_id:
            raise ExecutorError("capability_unavailable", "preuve de copie incohérente pour le lecteur")
        return evidence.boundary

    # -- Chargeur de destination (historique Snowpipe Streaming + MERGE miroir) --

    def _reconcile_loader_for_destination(
        self, destination_id: str, *, override: Mapping[str, str] | None = None
    ) -> None:
        """Recompose et applique le Deployment chargeur pour une destination.

        Même schéma que ``_reconcile_reader_for_source`` : un Deployment
        désiré par destination couvrant toutes ses tables ``copying``/
        ``live``, comparé à l'observé, idempotent. Sans image de chargeur
        configurée (``ExecutorConfig.loader_image`` vide), ne fait rien :
        un site qui ne la déclare pas reste sur le chemin COPY/MERGE
        existant, piloté hors de cet exécuteur.
        """

        if not self._config.loader_image:
            return
        desired = self._desired_loader_manifest(destination_id, override=override or {})
        name = loader_deployment_name(destination_id)
        if desired is not None:
            desired = with_spec_hash(desired)
        try:
            observed = self._deployments.read_deployment(name)
        except DeploymentsApiError as error:
            raise ExecutorError("executor_unavailable", "lecture du Deployment de chargeur impossible") from error
        if desired is not None and observed is not None and _loader_scope(desired) != _loader_scope(observed):
            # Un checkpoint acquitté dans l'ancien scope ne prouve aucune
            # ligne dans le nouveau. Arrêter l'ancien chargeur avant de
            # refuser la reprise, sans modifier sa destination.
            if observed["spec"].get("replicas", 1) != 0:
                stopped = deepcopy(observed)
                stopped["spec"]["replicas"] = 0
                try:
                    self._deployments.replace_deployment(name, stopped)
                except DeploymentsApiError as error:
                    raise ExecutorError("executor_unavailable", "arrêt du chargeur avant migration impossible") from error
            raise ExecutorError(
                "capability_unavailable",
                "périmètre Snowflake modifié : migration des données ou nouvelle copie initiale requise avant reprise",
            )
        action = reconcile_deployment(desired, observed)
        if action is None:
            return
        try:
            if action.kind == ACTION_CREATE:
                self._deployments.create_deployment(action.manifest)
            elif action.kind == ACTION_UPDATE:
                self._deployments.replace_deployment(action.name, action.manifest)
            else:  # ACTION_DELETE — plus de table live/copying sur cette destination
                self._deployments.delete_deployment(action.name)
        except DeploymentAlreadyExists:
            pass
        except DeploymentsApiError as error:
            raise ExecutorError("executor_unavailable", "application du Deployment de chargeur refusée") from error

    def _desired_loader_manifest(
        self, destination_id: str, *, override: Mapping[str, str]
    ) -> Mapping[str, object] | None:
        with self._engine.connect() as connection:
            destination = connection.execute(
                select(v2_schema.destinations.c.destination_database, v2_schema.destinations.c.destination_schema)
                .where(v2_schema.destinations.c.id == destination_id)
            ).mappings().one_or_none()
            rows = (
                connection.execute(
                    select(
                        v2_schema.tables.c.id,
                        v2_schema.tables.c.schema_name,
                        v2_schema.tables.c.table_name,
                        v2_schema.tables.c.key_columns,
                        v2_schema.tables.c.discovered_columns,
                        v2_schema.pipelines.c.id.label("pipeline_id"),
                        v2_schema.pipelines.c.declared_state,
                        v2_schema.pipelines.c.active_run_id,
                    )
                    .select_from(
                        v2_schema.tables.join(
                            v2_schema.pipelines, v2_schema.pipelines.c.table_id == v2_schema.tables.c.id
                        )
                    )
                    .where(v2_schema.pipelines.c.destination_id == destination_id)
                )
                .mappings()
                .all()
            )
        table_rows = []
        for row in rows:
            state = override.get(row["pipeline_id"], row["declared_state"])
            if state not in ("copying", "live"):
                continue
            table_rows.append(row)
        if not table_rows:
            return None
        if destination is None:
            raise ExecutorError("capability_unavailable", "destination Snowflake absente")
        try:
            database, output_schema = validated_destination_scope(
                destination["destination_database"], destination["destination_schema"]
            )
        except DestinationVerifierError as error:
            raise ExecutorError("capability_unavailable", "périmètre Snowflake enregistré invalide") from error
        history_schema = output_schema if output_schema is not None else DECLARED_HISTORY_SCHEMA
        mirror_schema = output_schema if output_schema is not None else DECLARED_MIRROR_SCHEMA
        missing_columns = [row["table_name"] for row in table_rows if not row["discovered_columns"]]
        if missing_columns:
            raise ExecutorError(
                "capability_unavailable",
                "colonnes non déclarées pour "
                + ", ".join(sorted(missing_columns))
                + " — voir PUT /v2/tables/{id}/discovered-columns",
            )
        self._ensure_destination_secret_provisioned(destination_id)
        tables = tuple(
            LoaderTableSpec(
                table_id=row["id"],
                schema_name=row["schema_name"],
                table_name=row["table_name"],
                key_columns=tuple(row["key_columns"].split(",")) if row["key_columns"] else (),
                columns=tuple(row["discovered_columns"]),
                # ``active_run_id`` n'est jamais effacé à la promotion
                # ``copying -> live`` (voir ``_promote_to_live``) : il reste
                # le run de la dernière copie initiale connue pour cette
                # table, donc la bonne clé de preuve tant qu'aucune nouvelle
                # copie n'a été relancée.
                evidence_key=(
                    evidence_key(self._config.raw_prefix_root, row["id"], row["active_run_id"])
                    if row["active_run_id"]
                    else None
                ),
            )
            for row in table_rows
        )
        return build_loader_deployment(
            LoaderDesiredSpec(
                destination_id=destination_id,
                image=self._config.loader_image,
                namespace=self._config.namespace,
                storage_backend=self._config.storage_backend,
                raw_bucket=self._config.raw_bucket,
                raw_prefix=self._config.raw_prefix_root,
                checkpoint_location=self._config.checkpoint_location,
                destination_database=database,
                destination_schema=history_schema,
                mirror_schema=mirror_schema,
                tables=tables,
                destination_secret_ref=destination_secret_ref(destination_id),
                service_account_name=self._config.service_account_name,
                extra_env={
                    "QUADRINGENT_LOADER_POLL_SECONDS": str(self._config.loader_poll_seconds),
                    "QUADRINGENT_STREAMING_FLUSH_EACH_BATCH": (
                        "true" if self._config.loader_flush_each_batch else "false"
                    ),
                    "QUADRINGENT_HISTORY_MODE": self._config.loader_history_mode,
                },
            )
        )

    def _ensure_destination_secret_provisioned(self, destination_id: str) -> None:
        """Maintient à jour le Secret Snowflake référencé par le chargeur.

        Contrairement à ``_ensure_secrets_provisioned`` (lecteur/copie), le
        chargeur de destination ne référence jamais de Secret IBM i — il ne
        parle qu'à Snowflake. Sans provisionneur injecté, ne fait rien (le
        Secret doit déjà exister) — même politique que le reste de
        l'exécuteur.
        """

        if self._secrets_provisioner is None:
            return
        try:
            self._secrets_provisioner.provision_destination_secret(destination_id)
        except Exception as error:  # noqa: BLE001 — jamais de détail de secret dans l'erreur
            raise ExecutorError(
                "executor_unavailable", "provisionnement du Secret Snowflake impossible"
            ) from error
