"""Lanceurs de Jobs Kubernetes pour la flotte DEV.

Le contrôleur ne connaît pas la mécanique du lecteur : il applique un modèle de
Job fourni par l'exploitant (rendu par la chart) aux éléments qui dépendent de
l'intention — nom déterministe, étiquettes, annotations de preuve et variables
de position. Un modèle ne peut jamais porter de secret en clair, un
`hostNetwork` ni un conteneur privilégié.

Le reçu rendu décrit un lancement, pas un effet : le runtime vérifie ensuite
l'état durable. Un Job déjà présent avec une intention ou une position
différente est refusé, jamais réutilisé silencieusement.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, Sequence

from quadringent.site_config import SiteConfig, current as _current_site
from .fleet import FleetError, JournalCheckpoint
from .fleet_history_runtime import HistoryLaunchRequest, HistoryReceipt
from .fleet_prepare_runtime import ReaderLaunchRequest, ReaderReceipt, RECEIPT_RUNNING
from .k8s_jobs import JobAlreadyExists, JobsApiError, KubernetesJobsClient


TEMPLATE_FORMAT_VERSION = "quadringent-job-template-v1"
READER_JOB_PREFIX = "quadringent-reader"
HISTORY_JOB_PREFIX = "quadringent-history"
RUN_ID_LENGTH = 12

LABEL_INTENT = "quadringent.io/intent-id"
LABEL_KIND = "quadringent.io/request-kind"
LABEL_ENVIRONMENT = "quadringent.io/environment"
ANNOTATION_CHECKPOINT = "quadringent.io/checkpoint"
ANNOTATION_MANIFEST = "quadringent.io/manifest-sha256"
ANNOTATION_PREPARE_INTENT = "quadringent.io/prepare-intent-id"
ANNOTATION_LANES = "quadringent.io/lanes-sha256"
ANNOTATION_READER = "quadringent.io/reader-id"

ENV_BOOTSTRAP_RECEIVER = "AS400_BOOTSTRAP_RECEIVER"
ENV_BOOTSTRAP_SEQUENCE = "AS400_BOOTSTRAP_SEQUENCE"
ENV_RAW_PREFIX = "AS400_RAW_PREFIX"
ENV_STREAM_KEY = "AS400_STREAM_KEY"
ENV_INTENT = "AS400_FLEET_INTENT_ID"
ENV_FLEET_TABLES = "AS400_FLEET_TABLES"
ENV_FLEET_TABLE_ROOT = "AS400_FLEET_TABLE_ROOT"
ENV_CONSOLE_SNAPSHOT_KEY = "AS400_CONSOLE_SNAPSHOT_S3_KEY"

KIND_READER = "reader"
KIND_HISTORY = "history"
# Environnement d'étiquette des Jobs — résolu à l'accès via ``__getattr__``.
ENVIRONMENT: str


def _site() -> SiteConfig:
    return _current_site()


def __getattr__(name: str) -> object:
    if name == "ENVIRONMENT":
        return _site().environment
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_TEMPLATE_KEYS = (
    "format_version",
    "container",
    "pod",
    "backoff_limit",
    "active_deadline_seconds",
    "ttl_seconds_after_finished",
    "reserve_run",
)
_SECRET_TOKENS = ("password", "passwd", "secret", "token", "private_key", "credential", "api_key")


class LauncherError(FleetError):
    """Refus de lancement, exprimé comme erreur de flotte sûre."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class JobTemplate:
    """Modèle de Job validé, réutilisé pour chaque lancement."""

    container: str
    pod: dict[str, object]
    backoff_limit: int
    active_deadline_seconds: int | None
    ttl_seconds_after_finished: int | None
    reserve_run: bool

    @classmethod
    def parse(cls, payload: object) -> "JobTemplate":
        if not isinstance(payload, Mapping):
            raise LauncherError("invalid_job_template", "Modèle de Job invalide")
        unknown = set(payload) - set(_TEMPLATE_KEYS)
        if unknown or payload.get("format_version") != TEMPLATE_FORMAT_VERSION:
            raise LauncherError("invalid_job_template", "Modèle de Job invalide")
        container = payload.get("container")
        pod = payload.get("pod")
        if not isinstance(container, str) or not container.strip():
            raise LauncherError("invalid_job_template", "Conteneur du modèle absent")
        if not isinstance(pod, Mapping):
            raise LauncherError("invalid_job_template", "Pod du modèle absent")
        backoff = payload.get("backoff_limit", 0)
        deadline = payload.get("active_deadline_seconds")
        ttl = payload.get("ttl_seconds_after_finished")
        reserve_run = payload.get("reserve_run", False)
        if backoff != 0:
            raise LauncherError("invalid_job_template", "Le modèle doit interdire les reprises Kubernetes")
        if not isinstance(reserve_run, bool):
            raise LauncherError("invalid_job_template", "Modèle de Job invalide")
        for value in (deadline, ttl):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise LauncherError("invalid_job_template", "Bornes du modèle invalides")
        _assert_pod_is_safe(pod, container)
        _find_container(pod, container)
        return cls(
            container=container,
            pod=dict(pod),
            backoff_limit=0,
            active_deadline_seconds=deadline,
            ttl_seconds_after_finished=ttl,
            reserve_run=reserve_run,
        )

    def to_manifest(
        self,
        *,
        name: str,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
        env: Mapping[str, str],
        args: Sequence[str],
    ) -> dict[str, object]:
        """Assemble le Job, en remplaçant les variables portées par l'intention."""

        pod = json.loads(json.dumps(self.pod))
        container = _find_container(pod, self.container)
        existing = container.setdefault("env", [])
        if not isinstance(existing, list):
            raise LauncherError("invalid_job_template", "Environnement du conteneur invalide")
        _replace_env(existing, env)
        if args:
            base = container.setdefault("args", [])
            if not isinstance(base, list) or not all(isinstance(item, str) for item in base):
                raise LauncherError("invalid_job_template", "Arguments du conteneur invalides")
            base.extend(args)
        spec: dict[str, object] = {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": dict(labels), "annotations": dict(annotations)},
                "spec": pod,
            },
        }
        if self.active_deadline_seconds is not None:
            spec["activeDeadlineSeconds"] = self.active_deadline_seconds
        if self.ttl_seconds_after_finished is not None:
            spec["ttlSecondsAfterFinished"] = self.ttl_seconds_after_finished
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": dict(labels), "annotations": dict(annotations)},
            "spec": spec,
        }


class ReaderJobLauncher:
    """Implémente le contrat `ReaderLauncher` avec un Job Kubernetes."""

    def __init__(
        self,
        client: KubernetesJobsClient,
        template: JobTemplate,
        *,
        raw_prefix_root: str,
    ) -> None:
        if not isinstance(client, KubernetesJobsClient):
            raise LauncherError("invalid_launcher", "Client Jobs invalide")
        if not isinstance(template, JobTemplate):
            raise LauncherError("invalid_job_template", "Modèle de Job invalide")
        if not raw_prefix_root.strip() or raw_prefix_root.startswith("/") or ".." in raw_prefix_root:
            raise LauncherError("invalid_launcher", "Préfixe brut invalide")
        self._client = client
        self._template = template
        self._raw_prefix_root = raw_prefix_root.rstrip("/")

    def launch(self, request: ReaderLaunchRequest) -> ReaderReceipt:
        if type(request) is not ReaderLaunchRequest:
            raise LauncherError("invalid_request", "Demande de lecteur invalide")
        run_id = _run_id(request.intent_id)
        name = f"{READER_JOB_PREFIX}-{run_id}"
        annotations = {
            ANNOTATION_CHECKPOINT: _checkpoint_label(request.checkpoint),
            ANNOTATION_MANIFEST: _manifest_digest(request.manifest),
            ANNOTATION_READER: name,
        }
        labels = {
            LABEL_INTENT: request.intent_id,
            LABEL_KIND: KIND_READER,
            LABEL_ENVIRONMENT: _site().environment,
        }
        env = {
            ENV_BOOTSTRAP_RECEIVER: request.checkpoint.receiver,
            ENV_BOOTSTRAP_SEQUENCE: str(request.checkpoint.sequence),
            # Deux espaces distincts, comme dans le dépôt : les tables vivent
            # sous `<racine>/<table>/journal`, la copie de la fenêtre lue vit
            # sous `<racine>/fleet/runs/<run>` et n'écrase aucun préfixe de table.
            ENV_RAW_PREFIX: f"{self._raw_prefix_root}/fleet/runs/{run_id}",
            ENV_INTENT: request.intent_id,
            # Le manifeste de flotte voyage avec l'intention : le runtime de
            # capture reste indépendant du paquet control-plane.
            ENV_FLEET_TABLES: ",".join(request.manifest),
            ENV_FLEET_TABLE_ROOT: self._raw_prefix_root,
            # Console dédiée à la flotte : le réglage global du site pointe un
            # flux mono-table et deux écrivains sur la même clé se corrompraient.
            ENV_CONSOLE_SNAPSHOT_KEY: f"{self._raw_prefix_root}/fleet/console-snapshot.json",
        }
        args = ["--reserve-run-id", run_id] if self._template.reserve_run else []
        manifest = self._template.to_manifest(
            name=name, labels=labels, annotations=annotations, env=env, args=args
        )
        # Le contrôleur TTL retire seulement le Job terminal. Le même nom
        # reste l'autorité d'idempotence tant qu'un lecteur peut être actif.
        manifest["spec"].setdefault("ttlSecondsAfterFinished", 60)
        self._apply(
            name,
            manifest,
            intent=request.intent_id,
            expected=annotations,
            request_kind=KIND_READER,
        )
        return ReaderReceipt(
            status=RECEIPT_RUNNING,
            intent_id=request.intent_id,
            checkpoint=request.checkpoint,
            manifest=request.manifest,
            reader_id=name,
        )

    def _apply(
        self,
        name: str,
        manifest: Mapping[str, object],
        *,
        intent: str,
        expected: Mapping[str, str],
        request_kind: str,
    ) -> None:
        try:
            self._client.create_job(manifest)
            return
        except JobAlreadyExists:
            pass
        except JobsApiError as error:
            # Création au résultat ambigu : on ne relance jamais à l'aveugle.
            if error.code != "jobs_api_unavailable":
                raise LauncherError("launch_failed", "Lancement du Job refusé") from None
            existing = self._read(name)
            if existing is None:
                raise LauncherError("launch_failed", "Lancement du Job incertain") from None
            _assert_same_intent(
                existing, intent=intent, expected=expected, request_kind=request_kind
            )
            return
        existing = self._read(name)
        _assert_same_intent(existing, intent=intent, expected=expected, request_kind=request_kind)

    def _read(self, name: str) -> dict[str, object] | None:
        try:
            return self._client.read_job(name)
        except JobsApiError:
            raise LauncherError("launch_failed", "Relecture du Job impossible") from None


class HistoryJobLauncher:
    """Implémente le contrat `HistoryLauncher` avec un Job Kubernetes."""

    def __init__(
        self,
        client: KubernetesJobsClient,
        template: JobTemplate,
        *,
        run_prefix_root: str,
    ) -> None:
        if not isinstance(client, KubernetesJobsClient):
            raise LauncherError("invalid_launcher", "Client Jobs invalide")
        if not isinstance(template, JobTemplate):
            raise LauncherError("invalid_job_template", "Modèle de Job invalide")
        if not run_prefix_root.strip() or run_prefix_root.startswith("/") or ".." in run_prefix_root:
            raise LauncherError("invalid_launcher", "Préfixe de run invalide")
        self._client = client
        self._template = template
        self._run_prefix_root = run_prefix_root.rstrip("/")

    def launch(self, request: HistoryLaunchRequest) -> HistoryReceipt:
        if type(request) is not HistoryLaunchRequest:
            raise LauncherError("invalid_request", "Demande historique invalide")
        name = f"{HISTORY_JOB_PREFIX}-{_run_id(request.intent_id)}"
        annotations = {
            ANNOTATION_CHECKPOINT: _checkpoint_label(request.checkpoint),
            ANNOTATION_MANIFEST: _manifest_digest(request.manifest),
            ANNOTATION_PREPARE_INTENT: request.prepare_intent_id,
            ANNOTATION_LANES: _lanes_digest(request.lanes),
            ANNOTATION_READER: request.reader_id,
        }
        labels = {
            LABEL_INTENT: request.intent_id,
            LABEL_KIND: KIND_HISTORY,
            LABEL_ENVIRONMENT: _site().environment,
        }
        env = {
            ENV_BOOTSTRAP_RECEIVER: request.checkpoint.receiver,
            ENV_BOOTSTRAP_SEQUENCE: str(request.checkpoint.sequence),
            # Curseur dédié : l'historique ne peut pas partager le CAS du
            # lecteur de flotte, sinon leurs transitions s'excluent.
            ENV_STREAM_KEY: f"{self._run_prefix_root}/fleet/history",
            ENV_RAW_PREFIX: f"{self._run_prefix_root}/fleet/history",
            ENV_INTENT: request.intent_id,
            ENV_CONSOLE_SNAPSHOT_KEY: f"{self._run_prefix_root}/fleet/history/console-snapshot.json",
        }
        manifest = self._template.to_manifest(
            name=name, labels=labels, annotations=annotations, env=env, args=[]
        )
        try:
            self._client.create_job(manifest)
        except JobAlreadyExists:
            existing = self._read(name)
            _assert_same_intent(
                existing,
                intent=request.intent_id,
                expected=annotations,
                request_kind=KIND_HISTORY,
            )
        except JobsApiError as error:
            if error.code != "jobs_api_unavailable":
                raise LauncherError("launch_failed", "Lancement du Job refusé") from None
            existing = self._read(name)
            if existing is None:
                raise LauncherError("launch_failed", "Lancement du Job incertain") from None
            _assert_same_intent(
                existing,
                intent=request.intent_id,
                expected=annotations,
                request_kind=KIND_HISTORY,
            )
        return HistoryReceipt(
            status=RECEIPT_RUNNING,
            intent_id=request.intent_id,
            prepare_intent_id=request.prepare_intent_id,
            reader_id=request.reader_id,
            checkpoint=request.checkpoint,
            manifest=request.manifest,
            lanes=request.lanes,
            max_concurrency=request.max_concurrency,
            orchestrator_id=name,
        )

    def _read(self, name: str) -> dict[str, object] | None:
        try:
            return self._client.read_job(name)
        except JobsApiError:
            raise LauncherError("launch_failed", "Relecture du Job impossible") from None


def _assert_same_intent(
    existing: Mapping[str, object] | None,
    *,
    intent: str,
    expected: Mapping[str, str],
    request_kind: str,
) -> None:
    """Un Job existant n'est réutilisé que s'il porte exactement cette intention."""

    if existing is None:
        raise LauncherError("launch_failed", "Job créé introuvable")
    metadata = existing.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, Mapping) else None
    labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
    if not isinstance(annotations, Mapping) or not isinstance(labels, Mapping):
        raise LauncherError("needs_recovery", "Job existant sans intention lisible")
    if labels.get(LABEL_KIND) != request_kind or labels.get(LABEL_ENVIRONMENT) != _site().environment:
        raise LauncherError("needs_recovery", "Job existant hors périmètre")
    if labels.get(LABEL_INTENT) != intent:
        raise LauncherError("needs_recovery", "Job existant pour une autre intention")
    for key, value in expected.items():
        if annotations.get(key) != value:
            raise LauncherError("needs_recovery", "Job existant pour une autre position")
    if job_is_terminal(existing):
        raise LauncherError("job_terminal", "Job terminé ; nettoyage TTL attendu avant reprise")


def job_is_terminal(job: Mapping[str, object]) -> bool:
    """Une condition terminale ou un compteur terminal interdit le faux RUNNING."""
    status = job.get("status")
    if not isinstance(status, Mapping):
        return False
    conditions = status.get("conditions", [])
    if isinstance(conditions, list) and any(
        isinstance(condition, Mapping) and condition.get("type") in {"Complete", "Failed"}
        and condition.get("status") == "True" for condition in conditions
    ):
        return True
    return not status.get("active") and any(
        type(status.get(key)) is int and status[key] > 0 for key in ("succeeded", "failed")
    )


def _run_id(intent_id: object) -> str:
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise LauncherError("invalid_request", "Intention de lancement invalide")
    if len(intent_id) > 63 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        for character in intent_id
    ):
        raise LauncherError("invalid_request", "Intention de lancement invalide")
    digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()
    return digest[:RUN_ID_LENGTH]


def _checkpoint_label(checkpoint: JournalCheckpoint) -> str:
    return f"{checkpoint.receiver}:{checkpoint.sequence}"


def _manifest_digest(manifest: Sequence[str]) -> str:
    return hashlib.sha256(",".join(manifest).encode("utf-8")).hexdigest()


def _lanes_digest(lanes: Sequence[object]) -> str:
    payload = json.dumps([lane.to_dict() for lane in lanes], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _find_container(pod: Mapping[str, object], name: str) -> dict[str, object]:
    containers = pod.get("containers")
    if not isinstance(containers, list):
        raise LauncherError("invalid_job_template", "Conteneurs du modèle absents")
    for container in containers:
        if isinstance(container, dict) and container.get("name") == name:
            return container
    raise LauncherError("invalid_job_template", "Conteneur cible introuvable")


def _replace_env(entries: list[object], values: Mapping[str, str]) -> None:
    remaining = dict(values)
    for entry in entries:
        if not isinstance(entry, dict):
            raise LauncherError("invalid_job_template", "Environnement du conteneur invalide")
        name = entry.get("name")
        if name in remaining:
            entry.clear()
            entry.update({"name": name, "value": remaining.pop(name)})
    for name, value in remaining.items():
        entries.append({"name": name, "value": value})


def _assert_pod_is_safe(pod: Mapping[str, object], container_name: str) -> None:
    """Refuse un modèle qui élargirait les droits ou porterait un secret en clair."""

    if pod.get("hostNetwork") is True:
        raise LauncherError("invalid_job_template", "Le modèle ne peut pas partager le réseau de l'hôte")
    if pod.get("restartPolicy") != "Never":
        raise LauncherError("invalid_job_template", "Le modèle doit rester sans redémarrage")
    containers = pod.get("containers")
    if not isinstance(containers, list) or not containers:
        raise LauncherError("invalid_job_template", "Conteneurs du modèle absents")
    for container in containers:
        if not isinstance(container, Mapping):
            raise LauncherError("invalid_job_template", "Conteneur du modèle invalide")
        security = container.get("securityContext")
        if isinstance(security, Mapping) and security.get("privileged") is True:
            raise LauncherError("invalid_job_template", "Conteneur privilégié interdit")
        if container.get("name") == container_name:
            _assert_env_has_no_plaintext_secret(container)


def _assert_env_has_no_plaintext_secret(container: Mapping[str, object]) -> None:
    for source in ("env", "envFrom"):
        value = container.get(source)
        if value is None or not isinstance(value, list):
            continue
        for entry in value:
            if not isinstance(entry, Mapping):
                raise LauncherError("invalid_job_template", "Environnement du conteneur invalide")
            name = str(entry.get("name", "")).lower()
            if any(token in name for token in _SECRET_TOKENS) and "value" in entry:
                raise LauncherError(
                    "invalid_job_template",
                    "Un secret doit venir d'une référence, jamais d'une valeur en clair",
                )
