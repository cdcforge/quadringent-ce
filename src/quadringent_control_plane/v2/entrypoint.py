"""Entrée du control plane v2 : migrations verrouillées puis service uvicorn.

Processus autonome, indépendant du serveur v1 (``quadringent_control_plane.
server``, jamais importé ni modifié ici) : la chart le lance comme un second
conteneur dans le même Pod que le control plane v1 — deux surfaces, deux
ports, deux sondes, un seul Pod. Toute la configuration vient de
l'environnement, jamais d'un défaut de production inventé.

Variables d'environnement consommées :

- ``QUADRINGENT_V2_DATABASE_URL`` (obligatoire) : DSN SQLAlchemy Postgres
  (``postgresql+psycopg://...``).
- ``QUADRINGENT_V2_SECRET_KEY``/``QUADRINGENT_V2_SECRET_KEY_FILE`` : clé
  Fernet de chiffrement des secrets (voir ``crypto.py``).
- ``QUADRINGENT_V2_TOKEN_PEPPER``/``QUADRINGENT_V2_TOKEN_PEPPER_FILE`` :
  pepper serveur des jetons d'agent, réutilisé pour la signature des cookies
  de session (voir ``crypto.py``).
- ``QUADRINGENT_V2_ORG_ID`` (optionnel) : identifiant d'organisation ; à
  défaut, ``QUADRINGENT_SITE_ID`` (site du control plane v1) puis
  ``"default"``.
- ``QUADRINGENT_V2_HOST``/``QUADRINGENT_V2_PORT`` (optionnels) : adresse
  d'écoute uvicorn — défauts loopback (même posture que le v1 sans proxy de
  confiance).
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Mapping

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import db, schema
from .app import create_v2_app
from .crypto import SecretBox, load_token_pepper
from .executor.boundary_reader import JavaBoundaryReader
from .executor.diagnostic_jobs import (
    DiagnosticJobConfig,
    KubernetesJobSourceProbe,
    KubernetesJobTableDiscoveryClient,
    clients_from_environment,
    in_cluster,
)
from .services.destination_verifier import SnowflakeKeyPairVerifier
from .services.loader_telemetry import KubernetesLoaderTelemetry, resolve_loader_identity
from ..k8s_jobs import JobsApiError

_LOGGER = logging.getLogger(__name__)
from .executor.evidence import EvidenceReader
from .executor.kubernetes import ExecutorConfig, KubernetesPipelineExecutor
from .executor.secrets_provisioner import SecretsProvisioner

ENV_DATABASE_URL = "QUADRINGENT_V2_DATABASE_URL"
ENV_HOST = "QUADRINGENT_V2_HOST"
ENV_PORT = "QUADRINGENT_V2_PORT"
ENV_ORG_ID = "QUADRINGENT_V2_ORG_ID"
ENV_SITE_ID = "QUADRINGENT_SITE_ID"
ENV_CAPTURE_IMAGE = "QUADRINGENT_V2_CAPTURE_IMAGE"
# Exécuteur de pipeline v2 (chantier « pipeline-exec ») : ServiceAccount à
# identité cloud (S3/GCS/DynamoDB) des charges de capture — jamais celui du
# control plane (voir ``chart/templates/control-plane.yaml``,
# ``serviceAccount.name`` de la chart, distinct de
# ``controlPlane.serviceAccount.name``). Les autres paramètres
# (bucket/préfixe/backend/base et schéma Snowflake) sont déjà publiés par le
# ConfigMap ``-site`` (``QUADRINGENT_RAW_BUCKET``, etc.) — voir
# ``build_pipeline_executor``.
ENV_CAPTURE_SERVICE_ACCOUNT = "QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT"
ENV_READER_TIMEOUT_SECONDS = "QUADRINGENT_V2_READER_TIMEOUT_SECONDS"
ENV_RECONCILIATION_INTERVAL_SECONDS = "QUADRINGENT_V2_RECONCILIATION_INTERVAL_SECONDS"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8845
DEFAULT_READER_TIMEOUT_SECONDS = 300
DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 15.0


class ConfigurationError(RuntimeError):
    """Configuration du control plane v2 absente ou invalide — refus explicite."""


def _require(name: str, environ: Mapping[str, str]) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise ConfigurationError(
            f"{name} est obligatoire pour démarrer le control plane v2"
        )
    return value


def resolve_org_id(environ: Mapping[str, str]) -> str:
    return (
        (environ.get(ENV_ORG_ID) or "").strip()
        or (environ.get(ENV_SITE_ID) or "").strip()
        or "default"
    )


def ensure_organization(engine: Engine, org_id: str) -> None:
    """Une installation = une organisation : sa ligne doit exister avant tout
    utilisateur (clé étrangère). Idempotent, sûr en cas de démarrages
    concurrents (l'insertion perdante est ignorée)."""
    with engine.connect() as connection:
        exists = connection.execute(
            select(schema.organizations.c.id).where(schema.organizations.c.id == org_id)
        ).first()
    if exists:
        return
    try:
        with engine.begin() as connection:
            connection.execute(schema.organizations.insert(), {"id": org_id, "name": org_id})
    except IntegrityError:
        pass


def build_diagnostic_adapters(
    values: Mapping[str, str], engine: Engine, secret_box: SecretBox
) -> tuple[object | None, object | None]:
    """Sonde de source et découverte de tables réelles (chantier « prod-wiring »).

    Câblées seulement en cluster (``KUBERNETES_SERVICE_HOST`` présent, posé
    par Kubernetes dans tout Pod) **et** avec ``QUADRINGENT_V2_CAPTURE_IMAGE``
    déclarée — l'image de capture (Java/JTOpen), la seule à savoir parler à
    IBM i (le Pod control plane ne l'a pas). Hors cluster ou sans image
    déclarée, rend ``(None, None)`` : les routes retombent alors sur leur
    comportement déjà éprouvé (``reachable: "unknown"``,
    ``discovery_unavailable``) — jamais un défaut de production inventé.
    """

    if not in_cluster():
        return None, None
    image = (values.get(ENV_CAPTURE_IMAGE) or "").strip()
    if not image:
        return None, None
    try:
        jobs_client, pods_client, secrets_client = clients_from_environment()
    except JobsApiError:
        # Jeton de ServiceAccount absent : l'API démarre sans adaptateurs
        # Kubernetes plutôt que de planter (constaté sur GKE).
        _LOGGER.warning("jeton de ServiceAccount illisible : sonde et découverte débranchées")
        return None, None
    config = DiagnosticJobConfig(namespace=jobs_client.namespace, image=image)
    source_probe = KubernetesJobSourceProbe(
        jobs_client=jobs_client, pods_client=pods_client, secrets_client=secrets_client, config=config
    )
    table_discovery_client = KubernetesJobTableDiscoveryClient(
        engine,
        secret_box,
        jobs_client=jobs_client,
        pods_client=pods_client,
        secrets_client=secrets_client,
        config=config,
    )
    return source_probe, table_discovery_client


def _build_destination_verifier() -> SnowflakeKeyPairVerifier:
    """Vérificateur Snowflake réel par défaut (chantier « backend gaps »,
    item 1) — fonction séparée pour limiter le diff pendant qu'un autre
    chantier câble en parallèle les autres ports de ``build_app``."""

    return SnowflakeKeyPairVerifier()


def _executor_clients_from_environment(
    *, timeout_seconds: float = 10.0
):
    """Jobs/Deployments/Secrets pour l'exécuteur de pipeline — même identité
    de ServiceAccount que ``diagnostic_jobs.clients_from_environment``
    (aucun droit distinct pour ce module), étendue au client Deployments,
    qu'un lecteur de journal exige (``KubernetesDeploymentsClient``,
    absent des adaptateurs de diagnostic)."""

    import os

    from .. import k8s_deployments as _k8s_deployments
    from .. import k8s_jobs as _k8s_jobs
    from .. import k8s_secrets as _k8s_secrets

    context = _k8s_jobs.ServiceAccountContext.load(_k8s_jobs.SERVICE_ACCOUNT_ROOT)
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    raw_port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT") or "443"
    port = int(raw_port)
    transport = _k8s_jobs.https_transport(context, host=host, port=port, timeout_seconds=timeout_seconds)
    jobs_client = _k8s_jobs.KubernetesJobsClient(transport, context.namespace)
    deployments_client = _k8s_deployments.KubernetesDeploymentsClient(transport, context.namespace)
    secrets_client = _k8s_secrets.KubernetesSecretsClient(transport, context.namespace)
    return jobs_client, deployments_client, secrets_client


def build_pipeline_executor(
    values: Mapping[str, str], engine: Engine, secret_box: SecretBox
) -> tuple[object | None, object | None]:
    """Exécuteur de pipeline réel et exécuteur de réconciliation (chantier
    « pipeline-exec ») : démarrer/mettre en pause/reprendre un pipeline pilote
    des Deployments/Jobs Kubernetes réels — jusqu'ici seules la sonde de
    source et la découverte de tables l'étaient (voir
    ``build_diagnostic_adapters``).

    Câblé seulement en cluster et avec ``QUADRINGENT_V2_CAPTURE_IMAGE``
    déclarée — même garde que ``build_diagnostic_adapters``, jamais un
    défaut de production inventé. Rend ``(None, None)`` sinon : les routes
    d'actions retombent alors sur leur comportement déjà éprouvé (refus
    explicite, aucun exécuteur injecté).

    Le lecteur de frontière (``JavaBoundaryReader``) tourne *dans ce même
    processus* — pas de Job Kubernetes dédié, voir la docstring de
    ``executor/boundary_reader.py`` pour la justification du choix.
    """

    if not in_cluster():
        return None, None
    image = (values.get(ENV_CAPTURE_IMAGE) or "").strip()
    if not image:
        return None, None
    # Comme ``image`` ci-dessus : sans ServiceAccount à identité cloud
    # déclaré, l'exécuteur reste débranché (jamais un défaut de production
    # inventé, jamais une erreur fatale qui empêcherait le reste du control
    # plane — y compris la sonde/découverte — de démarrer).
    service_account_name = (values.get(ENV_CAPTURE_SERVICE_ACCOUNT) or "").strip()
    if not service_account_name:
        return None, None
    raw_bucket = (values.get("QUADRINGENT_RAW_BUCKET") or "").strip()
    raw_prefix_root = (values.get("QUADRINGENT_RAW_PREFIX_ROOT") or "").strip()
    if not raw_bucket or not raw_prefix_root:
        return None, None
    try:
        jobs_client, deployments_client, secrets_client = _executor_clients_from_environment()
    except JobsApiError:
        _LOGGER.warning("jeton de ServiceAccount illisible : exécuteur de pipelines débranché")
        return None, None
    boundary_reader = JavaBoundaryReader(engine, secret_box)
    storage_backend = (values.get("QUADRINGENT_STORAGE_BACKEND") or "aws").strip().lower()
    checkpoint_location = (
        values.get("QUADRINGENT_CHECKPOINT_BUCKET")
        if storage_backend == "gcs"
        else values.get("QUADRINGENT_CHECKPOINT_TABLE")
    ) or ""
    evidence_reader = EvidenceReader(
        _storage_backend_object_store(storage_backend, raw_bucket, checkpoint_location)
    )
    config = ExecutorConfig(
        namespace=jobs_client.namespace,
        reader_image=image,
        copy_image=image,
        replay_image=image,
        raw_prefix_root=raw_prefix_root,
        storage_backend=storage_backend,
        service_account_name=service_account_name,
        reader_timeout_seconds=int(values.get(ENV_READER_TIMEOUT_SECONDS) or DEFAULT_READER_TIMEOUT_SECONDS),
        reader_poll_seconds=float(values.get("QUADRINGENT_V2_READER_POLL_SECONDS") or "5"),
        loader_poll_seconds=float(values.get("QUADRINGENT_V2_LOADER_POLL_SECONDS") or "10"),
        loader_flush_each_batch=(
            (values.get("QUADRINGENT_V2_LOADER_FLUSH_EACH_BATCH") or "false").strip().lower() == "true"
        ),
        loader_history_mode=(values.get("QUADRINGENT_V2_LOADER_HISTORY_MODE") or "streaming").strip().lower(),
        destination_database=(values.get("QUADRINGENT_DESTINATION_DATABASE") or "QUADRINGENT"),
        destination_schema=(values.get("QUADRINGENT_DESTINATION_SCHEMA") or "CURATED"),
        # Chargeur de destination (historique + miroir Snowflake) : il vit
        # dans l'image du control plane, passée par la chart.
        loader_image=(values.get("QUADRINGENT_V2_LOADER_IMAGE") or "").strip(),
        raw_bucket=raw_bucket,
        checkpoint_location=checkpoint_location,
    )
    secrets_provisioner = SecretsProvisioner(engine, secret_box, secrets_client=secrets_client)
    executor = KubernetesPipelineExecutor(
        engine,
        jobs_client=jobs_client,
        deployments_client=deployments_client,
        boundary_reader=boundary_reader,
        evidence_reader=evidence_reader,
        config=config,
        secrets_provisioner=secrets_provisioner,
    )
    return executor, executor


def _storage_backend_object_store(storage_backend: str, raw_bucket: str, checkpoint_location: str) -> object:
    """Magasin objet racine (aucun préfixe additionnel) pour lire la preuve
    de copie initiale — ``evidence_key`` porte déjà le chemin complet (voir
    ``executor/evidence.py``). Construit directement le dataclass plutôt que
    ``StorageBackend.from_environment`` : les noms de variables du control
    plane (``QUADRINGENT_*``, ConfigMap ``-site``) diffèrent de ceux attendus
    par ce constructeur (``AS400_*``, propres aux pods de capture)."""

    from quadringent.storage_backend import StorageBackend

    return StorageBackend(kind=storage_backend, raw_bucket=raw_bucket, state_location=checkpoint_location).object_store("")


def build_loader_telemetry(engine: Engine) -> KubernetesLoaderTelemetry | None:
    """Branche les journaux du chargeur avec l'identité Kubernetes du site."""

    if not in_cluster():
        return None
    try:
        _jobs, pods, _secrets = clients_from_environment()
    except JobsApiError:
        _LOGGER.warning("jeton de ServiceAccount illisible : télémétrie du chargeur débranchée")
        return None
    return KubernetesLoaderTelemetry(pods, resolve=lambda pipeline_id: resolve_loader_identity(engine, pipeline_id))


def build_app(environ: Mapping[str, str] | None = None) -> tuple[FastAPI, Engine]:
    """Applique les migrations (verrouillées) puis construit l'application v2.

    N'ouvre aucun socket réseau — utilisable en test comme en production,
    avant l'appel à ``uvicorn.run``.
    """

    values = dict(os.environ) if environ is None else dict(environ)
    dsn = _require(ENV_DATABASE_URL, values)
    db.run_migrations_locked(dsn)
    engine = db.create_engine_for(dsn)
    org_id = resolve_org_id(values)
    ensure_organization(engine, org_id)
    secret_box = SecretBox.from_environment(values)
    token_pepper = load_token_pepper(values)
    source_probe, table_discovery_client = build_diagnostic_adapters(values, engine, secret_box)
    pipeline_executor, reconciliation_executor = build_pipeline_executor(values, engine, secret_box)
    loader_telemetry = build_loader_telemetry(engine)
    app = create_v2_app(
        engine=engine,
        secret_box=secret_box,
        org_id=org_id,
        source_probe=source_probe,
        table_discovery_client=table_discovery_client,
        pipeline_executor=pipeline_executor,
        reconciliation_executor=reconciliation_executor,
        pipeline_observation_provider=loader_telemetry,
        log_source=loader_telemetry,
        reconciliation_interval_seconds=(
            DEFAULT_RECONCILIATION_INTERVAL_SECONDS if reconciliation_executor is not None else None
        ),
        token_pepper=token_pepper,
        # Le control plane est servi derrière le proxy/tunnel du site — voir
        # controlPlane.host dans la chart : jamais de cookie de session en
        # clair sur le réseau.
        session_cookie_secure=True,
        # Tâche « auth-login » : en production, l'anonyme n'est plus admin
        # implicite — voir ``auth.py::resolve_identity`` et
        # ``docs/api-v2.md``. Les routes publiques (setup, activation,
        # login/logout, healthz, openapi) restent accessibles sans identité.
        require_authentication=True,
        destination_verifier=_build_destination_verifier(),
    )
    return app, engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Control plane Quadringent v2 (FastAPI/uvicorn) — voir docs/api-v2.md"
    )
    parser.add_argument("--host", default=os.environ.get(ENV_HOST, DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get(ENV_PORT, DEFAULT_PORT) or DEFAULT_PORT))
    arguments = parser.parse_args(argv)

    import uvicorn

    app, _engine = build_app()
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="info")
    return 0
