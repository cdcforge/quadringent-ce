"""Sonde de source et découverte de tables réelles, via Jobs Kubernetes éphémères
(chantier « prod-wiring », 24 septembre 2026).

Le Pod control plane v2 n'a pas Java/JTOpen : la sonde IBM i (TLS, empreinte
de certificat, authentification, version, QTIMZON) et la découverte de
tables (catalogue) exigent toutes deux l'image de capture — la seule à
embarquer le worker Java. Ce module lance un Job court et jetable avec
cette image, lui passe les paramètres non sensibles en arguments et le mot
de passe IBM i via un Secret Kubernetes éphémère (jamais en argument de
commande ni journalisé), attend sa fin (délai borné), relit son résultat et
nettoie systématiquement le Job et le Secret — y compris en cas d'échec ou
de dépassement de délai.

Mécanisme de retour du résultat retenu : les journaux du pod du Job
(``kubectl logs`` équivalent, via ``KubernetesPodsClient.read_pod_log``).
Alternative écartée : écrire le résultat dans un second Secret/ConfigMap
créé par le Job lui-même — cela exigerait de donner au Job un accès en
écriture à l'API Kubernetes (un droit de plus, sur une charge qui parle déjà
à un système externe non fiabilisé), alors que la lecture de journaux est
déjà le mécanisme utilisé par ``services/logs_kubernetes.py`` pour un besoin
symétrique (lire le résultat d'un pod déjà terminé) et ne demande qu'un
droit `get` supplémentaire (``pods/log``) au ServiceAccount du control
plane, jamais un droit d'écriture à la charge diagnostiquée.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
import uuid
from typing import Callable, Mapping

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ... import k8s_jobs as _k8s_jobs
from ... import k8s_pods as _k8s_pods
from ... import k8s_secrets as _k8s_secrets
from ...fleet_job_launcher import job_is_terminal
from .. import schema as v2_schema
from ..crypto import SecretBox
from ..services.source_probe import (
    ProbeOutcome,
    SourceProbeRequest,
    SourceProbeResult,
    build_probe_result,
)
from ..services.tables import DiscoveredTable
from .manifests import (
    IBMI_CA_SECRET_KEY,
    SourceProbeJobSpec,
    TableDiscoveryJobSpec,
    build_source_probe_job,
    build_table_discovery_job,
    source_probe_job_name,
    table_discovery_job_name,
)

ENV_KUBERNETES_HOST = "KUBERNETES_SERVICE_HOST"


def in_cluster() -> bool:
    """Vrai si ce processus tourne dans un Pod Kubernetes.

    Même signal que le client Jobs (``k8s_jobs.client_from_environment``) :
    la variable posée par Kubernetes dans tout Pod. Utilisé par
    ``entrypoint.py`` pour ne jamais brancher d'adaptateur réel hors cluster
    (local, tests)."""

    import os

    return bool(os.environ.get(ENV_KUBERNETES_HOST, "").strip())


def clients_from_environment(
    *, timeout_seconds: float = 10.0
) -> tuple[_k8s_jobs.KubernetesJobsClient, _k8s_pods.KubernetesPodsClient, _k8s_secrets.KubernetesSecretsClient]:
    """Assemble les trois clients réels depuis l'identité du ServiceAccount monté.

    Même contexte (jeton, autorité de certification, namespace) pour les
    trois — une seule identité pour tout le module de diagnostic, comme
    ``k8s_deployments.py`` le fait déjà pour l'exécuteur de pipelines.
    """

    import os

    # Référence dynamique (pas un défaut lié à la définition) : les tests
    # substituent ``k8s_jobs.SERVICE_ACCOUNT_ROOT`` pour injecter une
    # identité de ServiceAccount factice.
    context = _k8s_jobs.ServiceAccountContext.load(_k8s_jobs.SERVICE_ACCOUNT_ROOT)
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    raw_port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT") or "443"
    port = int(raw_port)
    jobs_transport = _k8s_jobs.https_transport(context, host=host, port=port, timeout_seconds=timeout_seconds)
    jobs_client = _k8s_jobs.KubernetesJobsClient(jobs_transport, context.namespace)
    secrets_client = _k8s_secrets.KubernetesSecretsClient(jobs_transport, context.namespace)
    pods_transport = _k8s_pods.https_pods_transport(context, host=host, port=port, timeout_seconds=timeout_seconds)
    pods_client = _k8s_pods.KubernetesPodsClient(pods_transport, context.namespace)
    return jobs_client, pods_client, secrets_client


DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

KEY_ISERIES_PASSWORD = "ISERIES_PASSWORD"

_PROBE_RESULT_PREFIX = "quadringent_probe_result="
_DISCOVER_RESULT_PREFIX = "quadringent_discover_result="


class DiagnosticJobError(RuntimeError):
    """Erreur de Job de diagnostic réduite à un code sûr — jamais de détail distant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _secret_ref(prefix: str, run_id: str) -> str:
    return f"{prefix}-{run_id[:8]}"


class _PinnedCaSecret:
    """Provisionne (et nettoie toujours) le Secret CA épinglé le temps d'un
    Job de diagnostic — jamais pour une autorité publique ou encore
    ``"unknown"`` (``pinned_pem`` absent : pas de Secret créé, ``ref`` reste
    ``None`` et aucun montage n'est ajouté au manifeste, voir
    ``manifests.py::_with_ca_mount``). Portée du nom de Secret : le
    ``run_id`` de ce Job précis (même convention que le mot de passe IBM i,
    ``_secret_ref``) — jamais le ``source_id`` seul, pour ne jamais faire
    collision entre deux sondes concurrentes sur la même source."""

    def __init__(self, secrets_client: _k8s_secrets.KubernetesSecretsClient, *, ref: str | None, pinned_pem: str | None) -> None:
        self._secrets = secrets_client
        self._pinned_pem = pinned_pem
        self.ref = ref if pinned_pem else None

    def __enter__(self) -> str | None:
        if self.ref is not None and self._pinned_pem is not None:
            self._secrets.upsert_secret(self.ref, {IBMI_CA_SECRET_KEY: self._pinned_pem})
        return self.ref

    def __exit__(self, *exc: object) -> None:
        if self.ref is not None:
            try:
                self._secrets.delete_secret(self.ref)
            except _k8s_secrets.SecretsApiError:
                pass


def run_diagnostic_job(
    *,
    jobs_client: _k8s_jobs.KubernetesJobsClient,
    pods_client: _k8s_pods.KubernetesPodsClient,
    secrets_client: _k8s_secrets.KubernetesSecretsClient,
    manifest: Mapping[str, object],
    job_name: str,
    secret_name: str,
    secret_password: str,
    timeout_seconds: float,
    result_prefix: str,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Provisionne le Secret, lance le Job, attend, relit, nettoie — toujours.

    Rend la ligne de résultat (préfixée par ``result_prefix``, sans le
    préfixe) extraite des journaux du pod. Lève :

    - ``DiagnosticJobError("timeout", ...)`` si le Job n'est pas terminé dans
      le délai imparti ;
    - ``DiagnosticJobError("executor_unavailable", ...)`` pour toute panne de
      l'API Kubernetes, un Job qui échoue, ou un résultat introuvable/
      illisible dans les journaux.

    Le Job et le Secret sont toujours supprimés avant de rendre la main ou
    de lever une exception (``finally``) — y compris un Secret dont le Job
    n'a jamais pu être créé.
    """

    secrets_client.upsert_secret(secret_name, {KEY_ISERIES_PASSWORD: secret_password})
    try:
        try:
            jobs_client.create_job(manifest)
        except _k8s_jobs.JobAlreadyExists:
            pass
        except _k8s_jobs.JobsApiError as error:
            raise DiagnosticJobError("executor_unavailable", "création du Job de diagnostic refusée") from error

        deadline = monotonic() + timeout_seconds
        job: Mapping[str, object] | None = None
        while True:
            try:
                job = jobs_client.read_job(job_name)
            except _k8s_jobs.JobsApiError as error:
                raise DiagnosticJobError("executor_unavailable", "lecture du Job de diagnostic impossible") from error
            if job is not None and job_is_terminal(job):
                break
            if monotonic() >= deadline:
                raise DiagnosticJobError("timeout", "le Job de diagnostic n'a pas terminé dans le délai imparti")
            sleep(poll_interval_seconds)

        status = job.get("status", {}) if isinstance(job, Mapping) else {}
        failed = isinstance(status, Mapping) and isinstance(status.get("failed"), int) and status.get("failed", 0) > 0
        succeeded = isinstance(status, Mapping) and isinstance(status.get("succeeded"), int) and status.get("succeeded", 0) > 0

        try:
            pod_names = pods_client.list_pod_names(
                label_selector=f"job-name={job_name}", limit=1
            )
        except _k8s_pods.PodsApiError as error:
            raise DiagnosticJobError("executor_unavailable", "lecture des pods du Job de diagnostic impossible") from error
        if not pod_names:
            raise DiagnosticJobError("executor_unavailable", "aucun pod trouvé pour le Job de diagnostic")
        try:
            raw_log = pods_client.read_pod_log(pod_names[0], tail_lines=200)
        except _k8s_pods.PodsApiError as error:
            raise DiagnosticJobError("executor_unavailable", "lecture des journaux du Job de diagnostic impossible") from error
        if raw_log is None:
            raise DiagnosticJobError("executor_unavailable", "journal du Job de diagnostic introuvable")

        result_line = None
        for line in raw_log.splitlines():
            _, _, text = line.partition(" ")  # horodatage k8s en tête de ligne (timestamps=true)
            candidate = text if text.startswith(result_prefix) else line
            if candidate.startswith(result_prefix):
                result_line = candidate[len(result_prefix):]
        if result_line is None or (failed and not succeeded):
            raise DiagnosticJobError("executor_unavailable", "le Job de diagnostic n'a produit aucun résultat exploitable")
        return result_line
    finally:
        try:
            jobs_client.delete_job(job_name)
        except _k8s_jobs.JobsApiError:
            pass
        try:
            secrets_client.delete_secret(secret_name)
        except _k8s_secrets.SecretsApiError:
            pass


@dataclass(frozen=True)
class DiagnosticJobConfig:
    namespace: str
    image: str
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS
    discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS


class SourceProbeUnavailableError(RuntimeError):
    """La sonde via Job a échoué — code sûr porté par ``.code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class KubernetesJobSourceProbe:
    """``SourceProbeProtocol`` réel — sonde IBM i via un Job Kubernetes éphémère."""

    def __init__(
        self,
        *,
        jobs_client: _k8s_jobs.KubernetesJobsClient,
        pods_client: _k8s_pods.KubernetesPodsClient,
        secrets_client: _k8s_secrets.KubernetesSecretsClient,
        config: DiagnosticJobConfig,
        run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._jobs = jobs_client
        self._pods = pods_client
        self._secrets = secrets_client
        self._config = config
        self._run_id_factory = run_id_factory

    def probe(self, request: SourceProbeRequest) -> SourceProbeResult:
        run_id = self._run_id_factory()
        secret_name = _secret_ref("qdt-probe-secret", run_id)
        ca_secret_name = _secret_ref("qdt-probe-ca", run_id)
        with _PinnedCaSecret(self._secrets, ref=ca_secret_name, pinned_pem=request.pinned_pem) as ca_ref:
            manifest = build_source_probe_job(
                SourceProbeJobSpec(
                    run_id=run_id,
                    ibmi_host=request.ibmi_host,
                    ibmi_user=request.ibmi_user,
                    tls_trust=request.tls_trust,
                    pinned_fingerprint=request.pinned_fingerprint,
                    image=self._config.image,
                    namespace=self._config.namespace,
                    secret_ref=secret_name,
                    active_deadline_seconds=int(self._config.probe_timeout_seconds),
                    ca_secret_ref=ca_ref,
                )
            )
            try:
                result_line = run_diagnostic_job(
                    jobs_client=self._jobs,
                    pods_client=self._pods,
                    secrets_client=self._secrets,
                    manifest=manifest,
                    job_name=source_probe_job_name(run_id),
                    secret_name=secret_name,
                    secret_password=request.secret_value,
                    timeout_seconds=self._config.probe_timeout_seconds,
                    result_prefix=_PROBE_RESULT_PREFIX,
                )
            except DiagnosticJobError as error:
                raise SourceProbeUnavailableError(error.code, str(error)) from error
        payload = json.loads(result_line)
        return build_probe_result(
            network=ProbeOutcome(ok=payload["network"]["ok"], detail=payload["network"]["detail"]),
            tls=ProbeOutcome(ok=payload["tls"]["ok"], detail=payload["tls"]["detail"]),
            tls_fingerprint=payload["tls"].get("fingerprint"),
            authentication=ProbeOutcome(
                ok=payload["authentication"]["ok"], detail=payload["authentication"]["detail"]
            ),
            ibmi_version=payload.get("ibmi_version"),
            qtimzon=payload.get("qtimzon"),
            tls_trust=payload["tls"].get("trust"),
            tls_certificate_pem=payload["tls"].get("certificate_pem"),
        )


class TableDiscoveryUnavailableError(RuntimeError):
    """Aucune source unique résolue pour la découverte — voir docstring de la classe."""


class KubernetesJobTableDiscoveryClient:
    """``TableDiscoveryClientProtocol`` réel — découverte via un Job Kubernetes éphémère.

    ``TableDiscoveryClientProtocol.discover`` (``services/tables.py``) ne
    reçoit aucun ``source_id`` — contrat hérité du modèle actuel (une
    installation = une source IBM i, cf. ``entrypoint.py::ensure_organization``
    et le site unique du lecteur v1). Cet adaptateur résout donc *la* source
    déclarée (la plus ancienne si plusieurs existent, avec refus explicite
    au-delà d'une) à chaque appel — jamais mise en cache, pour ne jamais
    sonder un mot de passe périmé après une rotation.
    """

    def __init__(
        self,
        engine: Engine,
        secret_box: SecretBox,
        *,
        jobs_client: _k8s_jobs.KubernetesJobsClient,
        pods_client: _k8s_pods.KubernetesPodsClient,
        secrets_client: _k8s_secrets.KubernetesSecretsClient,
        config: DiagnosticJobConfig,
        run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._engine = engine
        self._secret_box = secret_box
        self._jobs = jobs_client
        self._pods = pods_client
        self._secrets = secrets_client
        self._config = config
        self._run_id_factory = run_id_factory

    def _resolve_source(self) -> tuple[str, str, str, str | None]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        v2_schema.sources.c.id,
                        v2_schema.sources.c.ibmi_host,
                        v2_schema.sources.c.ibmi_user,
                        v2_schema.sources.c.secret_ciphertext,
                        v2_schema.sources.c.tls_pinned_pem,
                    ).order_by(v2_schema.sources.c.created_at)
                )
                .mappings()
                .all()
            )
        if not rows:
            raise TableDiscoveryUnavailableError("aucune source déclarée pour la découverte de tables")
        if len(rows) > 1:
            raise TableDiscoveryUnavailableError(
                "plusieurs sources déclarées : la découverte via Job ne sait résoudre qu'une source unique "
                "pour l'instant — voir docstring de KubernetesJobTableDiscoveryClient"
            )
        row = rows[0]
        return (
            row["ibmi_host"],
            row["ibmi_user"],
            self._secret_box.decrypt(row["secret_ciphertext"]),
            row["tls_pinned_pem"],
        )

    def discover(
        self, *, libraries: tuple[str, ...] | None, limit: int, search: str | None
    ) -> tuple[DiscoveredTable, ...]:
        from quadringent.table_discovery import parse_discover_output

        host, user, password, pinned_pem = self._resolve_source()
        run_id = self._run_id_factory()
        secret_name = _secret_ref("qdt-discover-secret", run_id)
        ca_secret_name = _secret_ref("qdt-discover-ca", run_id)
        with _PinnedCaSecret(self._secrets, ref=ca_secret_name, pinned_pem=pinned_pem) as ca_ref:
            manifest = build_table_discovery_job(
                TableDiscoveryJobSpec(
                    run_id=run_id,
                    ibmi_host=host,
                    ibmi_user=user,
                    libraries=libraries,
                    limit=limit,
                    search=search,
                    image=self._config.image,
                    namespace=self._config.namespace,
                    secret_ref=secret_name,
                    active_deadline_seconds=int(self._config.discovery_timeout_seconds),
                    ca_secret_ref=ca_ref,
                )
            )
            try:
                result_line = run_diagnostic_job(
                    jobs_client=self._jobs,
                    pods_client=self._pods,
                    secrets_client=self._secrets,
                    manifest=manifest,
                    job_name=table_discovery_job_name(run_id),
                    secret_name=secret_name,
                    secret_password=password,
                    timeout_seconds=self._config.discovery_timeout_seconds,
                    result_prefix=_DISCOVER_RESULT_PREFIX,
                )
            except DiagnosticJobError as error:
                raise TableDiscoveryUnavailableError(str(error)) from error
        raw_output = json.loads(result_line)["raw_output"]
        return parse_discover_output(raw_output)
