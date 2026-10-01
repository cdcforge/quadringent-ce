"""Construction pure des objets Kubernetes désirés (chantier 4).

Aucune fonction de ce module ne fait d'appel réseau : elle prend en entrée
l'état déclaré (source, table, pipeline, bootstrap) et rend un dictionnaire
JSON prêt à être appliqué par ``executor.kubernetes``. Les noms sont
déterministes (dérivés des identifiants, jamais aléatoires) pour que la
réconciliation reste idempotente : appliquer deux fois la même intention
produit le même nom, donc le même objet.

Une capture Deployment couvre toutes les tables *live* d'un même
``(source_id, journal_library, journal_name)`` — un seul lecteur par
journal, comme l'exige le design (§1 : « un lecteur par flux sous bail »).
Une copie initiale est un Job par table, jamais partagé.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Mapping, Sequence

from quadringent.storage_layout import journal_prefix as _journal_prefix

from .boundary import JournalBoundary

DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

READER_PREFIX = "qdt-reader"
COPY_PREFIX = "qdt-copy"
REPLAY_PREFIX = "qdt-replay"
LOADER_PREFIX = "qdt-loader"
PROBE_PREFIX = "qdt-probe"
DISCOVER_PREFIX = "qdt-discover"

# Jobs de diagnostic (sonde de source, découverte de tables) : opérations
# courtes et jetables — même image de capture (Java/JTOpen), mais jamais de
# ServiceAccount ni de ressources durables. L'UID/GID non-root correspond à
# ``USER 10001`` de ``docker/Dockerfile`` (image de capture) — un
# ``runAsNonRoot`` sans ``runAsUser`` échoue au démarrage (leçon déjà tirée
# pour Postgres, voir ``tests/test_chart_kubernetes_names.py``).
DIAGNOSTIC_RUN_AS_USER = 10001
DIAGNOSTIC_RESOURCES: Mapping[str, object] = {
    "requests": {"cpu": "100m", "memory": "256Mi"},
    "limits": {"cpu": "500m", "memory": "512Mi"},
}

# Charges longues (lecteur, copie initiale, rejeu, chargeur de destination) :
# même image de capture, même convention d'UID/GID non-root que les Jobs de
# diagnostic (``docker/Dockerfile`` : ``USER 10001``). Contrairement aux
# Jobs de diagnostic, ces charges tournent avec le ServiceAccount à identité
# cloud du site (``serviceAccount.name`` de la chart, jamais celui du
# control plane — voir ``chart/templates/control-plane.yaml``) : c'est cette
# identité qui porte les droits d'écriture S3/GCS/DynamoDB, pas le control
# plane lui-même.
CAPTURE_RUN_AS_USER = DIAGNOSTIC_RUN_AS_USER


def _capture_security_context() -> dict[str, object]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": False,
        "runAsNonRoot": True,
        "runAsUser": CAPTURE_RUN_AS_USER,
    }


def _require_service_account_name(value: str) -> None:
    if not value.strip():
        raise ManifestError(
            "service_account_name est obligatoire — les charges de capture doivent tourner "
            "avec le ServiceAccount à identité cloud du site, jamais le ServiceAccount par défaut"
        )

LABEL_MANAGED_BY = "quadringent.io/managed-by"
LABEL_COMPONENT = "quadringent.io/component"
LABEL_SOURCE = "quadringent.io/source-id"
LABEL_TABLE = "quadringent.io/table-id"
LABEL_PIPELINE = "quadringent.io/pipeline-id"
LABEL_JOURNAL = "quadringent.io/journal-key"
LABEL_DESTINATION = "quadringent.io/destination-id"

ANNOTATION_TABLE_SET = "quadringent.io/table-ids"
ANNOTATION_BOUNDARY = "quadringent.io/bootstrap-boundary"
ANNOTATION_SPEC_HASH = "quadringent.io/spec-sha256"
ANNOTATION_RUN_ID = "quadringent.io/run-id"

ENV_STORAGE_BACKEND = "QUADRINGENT_STORAGE_BACKEND"
ENV_SOURCE_TIME_ZONE = "AS400_SOURCE_TIME_ZONE"
ENV_READER_TIMEOUT_SECONDS = "AS400_READER_TIMEOUT_SECONDS"
ENV_MAX_SECONDS = "AS400_MAX_SECONDS"
ENV_TABLE_SET = "AS400_FLEET_TABLES"
ENV_TABLE_BOOTSTRAP_JSON = "AS400_TABLE_BOOTSTRAP_JSON"

# Contrat requis par ``scripts/as400_continuous_capture.py`` (constaté au
# premier démarrage réel, 2026-09-24) : ces variables sont lues *avant* tout
# I/O IBM i (``_required``/``fleet_mode_from_environment``) et manquaient au
# Deployment de lecteur, qui redémarrait en boucle sur
# ``FleetConfigurationError``/``ValueError`` selon la variable manquante.
ENV_ISERIES_HOST = "ISERIES_HOST"
ENV_ISERIES_USER = "ISERIES_USER"
ENV_ISERIES_SCHEMA = "ISERIES_SCHEMA"
ENV_ISERIES_TABLE = "ISERIES_TABLE"
ENV_STREAM_KEY = "AS400_STREAM_KEY"
# ``quadringent.fleet_capture.fleet_mode_from_environment`` exige les deux
# variables ensemble (``FleetConfigurationError`` sinon) et au moins deux
# tables — jamais posées pour une seule table couverte par le lecteur.
ENV_FLEET_TABLE_ROOT = "AS400_FLEET_TABLE_ROOT"
ENV_RAW_PREFIX = "AS400_RAW_PREFIX"
# Constat du 24 septembre 2026 (voir quadringent.storage_layout) : le mode
# une seule table ne passe jamais par ``fleet_mode_from_environment``
# (``ENV_FLEET_TABLE_ROOT``/``ENV_TABLE_SET`` restent absentes — voir
# ``test_reader_deployment_env_satisfies_fleet_mode_resolution_single_
# table``), donc ``ContinuousCaptureService`` n'activait jamais les reçus de
# fenêtre pour lui. Cette variable les active indépendamment du mode flotte.
ENV_RECEIPTED_SCANS = "AS400_RECEIPTED_SCANS"
ENV_SNAPSHOT_RUN_ID = "AS400_SNAPSHOT_RUN_ID"
ENV_SNAPSHOT_OUTPUT_DIR = "AS400_SNAPSHOT_OUTPUT_DIR"
ENV_BOOTSTRAP_RECEIVER = "AS400_BOOTSTRAP_RECEIVER"
ENV_BOOTSTRAP_RECEIVER_LIBRARY = "AS400_BOOTSTRAP_RECEIVER_LIBRARY"
ENV_BOOTSTRAP_SEQUENCE = "AS400_BOOTSTRAP_SEQUENCE"
ENV_BOOTSTRAP_OBSERVED_AT = "AS400_BOOTSTRAP_OBSERVED_AT"
ENV_EVIDENCE_KEY = "AS400_EVIDENCE_KEY"
ENV_PIPELINE_ID = "AS400_PIPELINE_ID"
ENV_TABLE_ID = "AS400_TABLE_ID"
# Contrat requis par ``java.quadringent.as400.ReadOnlyTableSnapshot`` (100%
# piloté par l'environnement, ``Settings.fromEnvironment`` — jamais
# d'argument CLI) et lu par ``scripts/quadringent_initial_copy_job.py``.
ENV_SNAPSHOT_ISERIES_HOST = "ISERIES_HOST"
ENV_SNAPSHOT_ISERIES_USER = "ISERIES_USER"
ENV_SNAPSHOT_ISERIES_SCHEMA = "ISERIES_SCHEMA"
ENV_SNAPSHOT_ISERIES_TABLE = "ISERIES_TABLE"
ENV_SNAPSHOT_RAW_BUCKET = "AS400_RAW_BUCKET"
ENV_REPLAY_FROM = "AS400_REPLAY_FROM_SEQUENCE"
ENV_REPLAY_TO = "AS400_REPLAY_TO_SEQUENCE"

# Chargeur de destination (historique Snowpipe Streaming + MERGE miroir).
# AS400_RAW_BUCKET/le nom de checkpoint suivent la même convention que
# quadringent.storage_backend.StorageBackend.from_environment (lecteur v1) ;
# le Deployment de lecteur orchestré par v2 (build_reader_deployment) ne les
# porte pas encore lui-même — écart pré-existant, hors périmètre ici — mais
# le chargeur de destination en a besoin dès sa première version pour lire
# les mêmes lots bruts.
ENV_RAW_BUCKET = "AS400_RAW_BUCKET"
ENV_CHECKPOINT_TABLE = "AS400_CHECKPOINT_TABLE"
ENV_CHECKPOINT_BUCKET = "AS400_CHECKPOINT_BUCKET"
ENV_DESTINATION_DATABASE = "QUADRINGENT_DESTINATION_DATABASE"
ENV_DESTINATION_SCHEMA = "QUADRINGENT_DESTINATION_SCHEMA"
ENV_LOADER_TABLE_SET_JSON = "QUADRINGENT_LOADER_TABLE_SET_JSON"

# Marge minimale imposée entre le budget d'exécution du lecteur (le temps
# maximal qu'un poll peut occuper avant de rendre la main) et le délai de
# vie du Job/Deployment (leçon de qualification réelle : un budget égal ou
# inférieur au délai du lecteur tronque le dernier poll en plein vol).
MIN_BUDGET_MARGIN_SECONDS = 30


class ManifestError(ValueError):
    """Entrée invalide pour la construction d'un manifeste."""


def destination_secret_ref(destination_id: str) -> str:
    """Nom déterministe du Secret Kubernetes portant les identifiants Snowflake.

    Convention partagée entre la construction des manifestes et le
    provisionnement réel du Secret (``executor/secrets_provisioner.py``) —
    une seule fonction, jamais deux formats qui pourraient diverger.
    """

    return f"qdt-destination-{destination_id}"


def ibmi_secret_ref(source_id: str) -> str:
    """Nom déterministe du Secret Kubernetes portant le mot de passe IBM i."""

    return f"qdt-source-{source_id}"


def ibmi_ca_secret_ref(source_id: str) -> str:
    """Nom déterministe du Secret Kubernetes portant le PEM CA épinglé d'une source.

    Provisionné seulement quand la source a une confiance ``"pinned"``
    (``sources.tls_pinned_pem`` non ``NULL``, voir la migration
    ``0013_source_tls_pin``) — jamais pour une autorité publique (aucun
    fichier requis, voir ``TlsTrust.java``/``quadringent_source_probe_job.
    py::tls_probe``) ni pour une autorité encore ``"unknown"`` (jamais
    montée tant que l'opérateur ne l'a pas explicitement épinglée).
    """

    return f"qdt-source-ca-{source_id}"


IBMI_CA_MOUNT_PATH = "/etc/quadringent/ibmi-ca"
IBMI_CA_FILE_NAME = "ca.pem"
IBMI_CA_SECRET_KEY = "ca.pem"
ENV_IBMI_TLS_CA_FILE = "AS400_TLS_CA_FILE"


def _with_ca_mount(
    container: dict[str, object], pod_spec: dict[str, object], ca_secret_ref: str | None
) -> None:
    """Monte le Secret CA épinglé (s'il y en a un) et pose ``AS400_TLS_CA_FILE``.

    Mutation en place — appelée juste avant de renvoyer le manifeste. Sans
    ``ca_secret_ref`` (source non épinglée), ni volume ni variable ne sont
    ajoutés : la charge utilise alors le magasin de confiance système/JVM
    par défaut (objectif A), jamais un chemin implicite qui pourrait ne pas
    exister dans l'image.
    """

    if not ca_secret_ref:
        return
    env = container.setdefault("env", [])
    assert isinstance(env, list)
    env.append({"name": ENV_IBMI_TLS_CA_FILE, "value": f"{IBMI_CA_MOUNT_PATH}/{IBMI_CA_FILE_NAME}"})
    volume_mounts = container.setdefault("volumeMounts", [])
    assert isinstance(volume_mounts, list)
    volume_mounts.append({"name": "ibmi-ca", "mountPath": IBMI_CA_MOUNT_PATH, "readOnly": True})
    volumes = pod_spec.setdefault("volumes", [])
    assert isinstance(volumes, list)
    volumes.append(
        {
            "name": "ibmi-ca",
            "secret": {
                "secretName": ca_secret_ref,
                "items": [{"key": IBMI_CA_SECRET_KEY, "path": IBMI_CA_FILE_NAME}],
            },
        }
    )


def journal_key(journal_library: str, journal_name: str) -> str:
    if not journal_library.strip() or not journal_name.strip():
        raise ManifestError("bibliothèque et nom de journal requis")
    return f"{journal_library.strip().upper()}.{journal_name.strip().upper()}"


def _short_hash(*parts: str, length: int = 10) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def _dns_name(prefix: str, *parts: str) -> str:
    name = f"{prefix}-{_short_hash(*parts)}"
    if not DNS_LABEL.match(name):
        raise ManifestError(f"nom Kubernetes invalide : {name!r}")
    return name


def reader_deployment_name(source_id: str, journal_library: str, journal_name: str) -> str:
    return _dns_name(READER_PREFIX, source_id, journal_key(journal_library, journal_name))


def initial_copy_job_name(table_id: str, run_id: str) -> str:
    return _dns_name(COPY_PREFIX, table_id, run_id)


def replay_job_name(pipeline_id: str, from_sequence: int, to_sequence: int) -> str:
    return _dns_name(REPLAY_PREFIX, pipeline_id, str(from_sequence), str(to_sequence))


def loader_deployment_name(destination_id: str) -> str:
    return _dns_name(LOADER_PREFIX, destination_id)


@dataclass(frozen=True)
class TableBootstrap:
    """Position de bascule d'une table, telle qu'enregistrée par sa copie."""

    table_id: str
    schema_name: str
    table_name: str
    boundary: JournalBoundary

    def to_env_entry(self) -> dict[str, object]:
        return {
            "table_id": self.table_id,
            "schema": self.schema_name,
            "table": self.table_name,
            "receiver": self.boundary.receiver_name,
            "receiver_library": self.boundary.receiver_library,
            "sequence": self.boundary.bootstrap_sequence,
        }


@dataclass(frozen=True)
class ReaderDesiredSpec:
    """Ce que le Deployment de capture d'un journal doit contenir."""

    source_id: str
    journal_library: str
    journal_name: str
    image: str
    namespace: str
    storage_backend: str
    source_time_zone: str
    raw_prefix: str
    reader_timeout_seconds: int
    tables: tuple[TableBootstrap, ...]
    destination_secret_ref: str
    ibmi_secret_ref: str
    service_account_name: str
    # Hôte et utilisateur IBM i : pas des identifiants (le mot de passe seul
    # vient du Secret ``ibmi_secret_ref``), mais ``as400_continuous_capture.
    # py`` les exige (``_required("ISERIES_HOST")``/``_required("ISERIES_USER")``)
    # avant tout I/O — jamais posés par le Deployment avant ce correctif.
    ibmi_host: str = ""
    ibmi_user: str = ""
    # Bucket brut et emplacement de checkpoint : mêmes variables que le
    # chargeur de destination (``LoaderDesiredSpec``), exigées par
    # ``_object_store``/``_checkpoint_store`` du script de capture — écart
    # déjà documenté ci-dessus avant ce correctif.
    raw_bucket: str = ""
    checkpoint_location: str = ""
    paused: bool = False
    resources: Mapping[str, object] = field(default_factory=dict)
    extra_env: Mapping[str, str] = field(default_factory=dict)
    # PEM CA épinglé de la source (``ibmi_ca_secret_ref``) — ``None`` pour
    # une source à autorité publique ou encore non épinglée (objectif A/B).
    ca_secret_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.tables:
            raise ManifestError("un lecteur doit couvrir au moins une table")
        if self.reader_timeout_seconds <= 0:
            raise ManifestError("AS400_READER_TIMEOUT_SECONDS doit être positif")
        if not self.source_time_zone.strip():
            raise ManifestError("AS400_SOURCE_TIME_ZONE est obligatoire (fuseau IANA)")
        if not self.destination_secret_ref.strip() or not self.ibmi_secret_ref.strip():
            raise ManifestError(
                "les identifiants Snowflake et IBM i doivent venir d'un Secret référencé, jamais en clair"
            )
        if not self.ibmi_host.strip() or not self.ibmi_user.strip():
            raise ManifestError("ISERIES_HOST et ISERIES_USER sont obligatoires pour le lecteur")
        if not self.raw_bucket.strip() or not self.checkpoint_location.strip():
            raise ManifestError("raw_bucket et checkpoint_location sont obligatoires pour le lecteur")
        schemas = {t.schema_name.strip().upper() for t in self.tables}
        if len(schemas) > 1:
            raise ManifestError(
                "toutes les tables d'un même lecteur (source+journal) doivent partager le même schéma"
            )
        _require_service_account_name(self.service_account_name)


def build_reader_deployment(spec: ReaderDesiredSpec) -> dict[str, object]:
    """Rend le Deployment (un par source+journal, un seul réplica = un lecteur).

    ``spec.paused`` scale le Deployment à zéro réplica sans supprimer l'objet
    ni son historique de rollout — les checkpoints durables (hors
    Kubernetes, dans le stockage objet/DynamoDB) ne sont jamais perdus par
    cette mise en pause : ``set_reader_table_set``/reconcile ne fait que
    changer ``replicas`` et l'environnement du même Deployment.
    """

    name = reader_deployment_name(spec.source_id, spec.journal_library, spec.journal_name)
    j_key = journal_key(spec.journal_library, spec.journal_name)
    table_ids = ",".join(sorted(t.table_id for t in spec.tables))
    labels = {
        LABEL_MANAGED_BY: "quadringent-control-plane",
        LABEL_COMPONENT: "reader",
        LABEL_SOURCE: spec.source_id,
        LABEL_JOURNAL: j_key,
    }
    annotations = {
        ANNOTATION_TABLE_SET: table_ids,
    }
    sorted_tables = sorted(spec.tables, key=lambda t: t.table_id)
    schema = sorted_tables[0].schema_name.strip().upper()
    # ``ISERIES_TABLE`` reste requis même en mode flotte : c'est la table
    # « ancre » que ``fleet_mode_from_environment`` exige comme membre de
    # ``AS400_FLEET_TABLES`` (``as400_continuous_capture.py`` : « ISERIES_TABLE
    # must belong to the fleet tables »). Choix déterministe : la première
    # table par nom, jamais un ordre dépendant de l'itération du dict.
    table_names = sorted({t.table_name.strip().upper() for t in sorted_tables})
    primary_table = table_names[0]
    checkpoint_env_name = ENV_CHECKPOINT_BUCKET if spec.storage_backend == "gcs" else ENV_CHECKPOINT_TABLE
    env = {
        ENV_STORAGE_BACKEND: spec.storage_backend,
        ENV_SOURCE_TIME_ZONE: spec.source_time_zone,
        ENV_READER_TIMEOUT_SECONDS: str(spec.reader_timeout_seconds),
        # Le budget d'un poll doit toujours excéder le délai du lecteur : une
        # égalité tronque le dernier poll (leçon de qualification réelle).
        ENV_MAX_SECONDS: str(spec.reader_timeout_seconds + MIN_BUDGET_MARGIN_SECONDS),
        ENV_ISERIES_HOST: spec.ibmi_host,
        ENV_ISERIES_USER: spec.ibmi_user,
        ENV_ISERIES_SCHEMA: schema,
        ENV_ISERIES_TABLE: primary_table,
        ENV_STREAM_KEY: j_key.lower(),
        # Disposition unique (quadringent.storage_layout) : en mode une seule
        # table, le lecteur écrit directement sous le préfixe par table
        # (``<racine>/<table>/journal``, même formule que le mode flotte),
        # jamais à la racine brute du site — corrigé le 24 septembre 2026 (le
        # chargeur ne trouvait rien : voir le module storage_layout).
        ENV_RAW_PREFIX: (
            _journal_prefix(spec.raw_prefix, primary_table)
            if len(table_names) == 1
            else spec.raw_prefix.rstrip("/")
        ),
        ENV_RAW_BUCKET: spec.raw_bucket,
        checkpoint_env_name: spec.checkpoint_location,
        # Reçus de fenêtre : nécessaires au chargeur (``list_receipt_keys``)
        # que la table soit seule ou en flotte — voir ENV_RECEIPTED_SCANS.
        ENV_RECEIPTED_SCANS: "true",
        ENV_TABLE_BOOTSTRAP_JSON: json.dumps(
            [table.to_env_entry() for table in sorted_tables],
            separators=(",", ":"),
        ),
        **dict(spec.extra_env),
    }
    # Position de départ lue par ``as400_continuous_capture._bootstrap_position``
    # (seule interface du script : ``AS400_TABLE_BOOTSTRAP_JSON`` n'est lu par
    # aucun script). Un seul lecteur par journal : la plus ancienne frontière
    # couvre toutes les tables, les doublons sont écartés par ``event_id`` côté
    # destination. Le script ne l'utilise qu'en l'absence de checkpoint : un
    # redémarrage ne recule jamais.
    # La séquence repart à 1 après une rotation de receveur : comparer les
    # seules séquences choisirait alors le receveur le plus récent et pourrait
    # sauter des événements si le checkpoint du lecteur devait être restauré.
    earliest = min(
        sorted_tables,
        key=lambda table: (table.boundary.observed_at, table.boundary.bootstrap_sequence),
    )
    env[ENV_BOOTSTRAP_RECEIVER] = earliest.boundary.receiver_name
    env[ENV_BOOTSTRAP_RECEIVER_LIBRARY] = earliest.boundary.receiver_library
    env[ENV_BOOTSTRAP_SEQUENCE] = str(earliest.boundary.bootstrap_sequence)
    if len(table_names) > 1:
        # Mode flotte : ``fleet_mode_from_environment`` exige les deux
        # variables ensemble et au moins deux tables — jamais posées seules
        # pour une seule table (c'est exactement le bug constaté le 24
        # septembre 2026 : ``AS400_FLEET_TABLES`` était posée seule).
        env[ENV_FLEET_TABLE_ROOT] = spec.raw_prefix.strip("/")
        env[ENV_TABLE_SET] = ",".join(table_names)
    container: dict[str, object] = {
        "name": "reader",
        "image": spec.image,
        "env": [{"name": key, "value": value} for key, value in env.items()],
        # Identifiants Snowflake et IBM i : toujours une référence de Secret
        # Kubernetes, jamais une valeur en clair dans le manifeste (repris de
        # `fleet_job_launcher.py::_assert_env_has_no_plaintext_secret`).
        "envFrom": [
            {"secretRef": {"name": spec.destination_secret_ref}},
            {"secretRef": {"name": spec.ibmi_secret_ref}},
        ],
        "securityContext": _capture_security_context(),
    }
    if spec.resources:
        container["resources"] = dict(spec.resources)
    pod_spec: dict[str, object] = {
        "restartPolicy": "Always",
        # Identité cloud du site (S3/GCS/DynamoDB) — jamais celle du control
        # plane, qui n'a pas ces droits (voir ``ExecutorConfig``).
        "serviceAccountName": spec.service_account_name,
        "securityContext": {"runAsNonRoot": True, "runAsUser": CAPTURE_RUN_AS_USER},
        "containers": [container],
    }
    _with_ca_mount(container, pod_spec, spec.ca_secret_ref)
    replicas = 0 if spec.paused else 1
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": spec.namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "replicas": replicas,
            # Un seul réplica actif à la fois : c'est le mécanisme qui
            # garantit « un lecteur par journal ». `Recreate` évite une
            # fenêtre à deux pods actifs pendant un rollout (`RollingUpdate`
            # créerait transitoirement un second lecteur sur le même
            # journal, violant l'exclusivité).
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {LABEL_SOURCE: spec.source_id, LABEL_JOURNAL: j_key}},
            "template": {
                "metadata": {
                    "labels": {**labels},
                    "annotations": annotations,
                },
                "spec": pod_spec,
            },
        },
    }


@dataclass(frozen=True)
class LoaderTableSpec:
    """Une table couverte par le chargeur de destination.

    ``columns`` reprend tel quel ``TableRecord.discovered_columns``
    (``services/tables.py::set_discovered_columns``, déjà validé contre
    ``quadringent.snowflake_destination.IbmiColumnType``) — jamais
    redevinée ici. Le chargeur refuse de démarrer une table dont les
    colonnes ne sont pas déclarées.
    """

    table_id: str
    schema_name: str
    table_name: str
    key_columns: tuple[str, ...]
    columns: tuple[Mapping[str, object], ...]
    # Clé de la preuve de copie initiale (``executor.evidence.evidence_key``)
    # — posée par l'appelant (``kubernetes.py::_desired_loader_manifest``)
    # dès que le pipeline porte un ``active_run_id``, avant même que la
    # table soit ``live`` : le chargeur relit lui-même la preuve et ne charge
    # l'instantané que si elle est effectivement écrite (voir
    # ``quadringent_destination_loader.load_snapshot_once``). ``None`` pour
    # une table sans copie initiale connue.
    evidence_key: str | None = None

    def to_env_entry(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "table_id": self.table_id,
            "schema": self.schema_name,
            "table": self.table_name,
            "key_columns": list(self.key_columns),
            "columns": [dict(c) for c in self.columns],
        }
        if self.evidence_key:
            entry["evidence_key"] = self.evidence_key
        return entry


@dataclass(frozen=True)
class LoaderDesiredSpec:
    """Ce que le Deployment du chargeur d'une destination doit contenir.

    Un seul Deployment par destination (pas par table) : l'historique et le
    miroir de toutes les tables *live*/``copying`` de cette destination sont
    chargés par le même processus, chacun sur son propre canal Snowpipe
    Streaming (voir ``snowflake_streaming_loader.channel_name_for``).
    """

    destination_id: str
    image: str
    namespace: str
    storage_backend: str
    raw_bucket: str
    raw_prefix: str
    checkpoint_location: str
    destination_database: str
    destination_schema: str
    tables: tuple[LoaderTableSpec, ...]
    destination_secret_ref: str
    service_account_name: str
    paused: bool = False
    resources: Mapping[str, object] = field(default_factory=dict)
    extra_env: Mapping[str, str] = field(default_factory=dict)
    # Défaut compatible pour les appelants historiques à scope unique.
    mirror_schema: str | None = None

    def __post_init__(self) -> None:
        if not self.tables:
            raise ManifestError("un chargeur de destination doit couvrir au moins une table")
        if not self.destination_secret_ref.strip():
            raise ManifestError(
                "les identifiants Snowflake doivent venir d'un Secret référencé, jamais en clair"
            )
        if not self.raw_bucket.strip() or not self.checkpoint_location.strip():
            raise ManifestError("raw_bucket et checkpoint_location sont obligatoires")
        _require_service_account_name(self.service_account_name)


def build_loader_deployment(spec: LoaderDesiredSpec) -> dict[str, object]:
    """Rend le Deployment du chargeur de destination (un par destination).

    Même garantie de réplica unique que le lecteur (``build_reader_
    deployment``) : ``replicas`` à 0 ou 1 et stratégie ``Recreate`` — jamais
    deux chargeurs actifs sur la même destination, sans dépendre d'un bail
    applicatif distinct (le canal Snowpipe Streaming et le MERGE miroir
    n'ont qu'un seul écrivain légitime à la fois, comme le lecteur de
    journal n'a qu'un seul lecteur actif par journal).
    """

    name = loader_deployment_name(spec.destination_id)
    table_ids = ",".join(sorted(t.table_id for t in spec.tables))
    labels = {
        LABEL_MANAGED_BY: "quadringent-control-plane",
        LABEL_COMPONENT: "destination-loader",
        LABEL_DESTINATION: spec.destination_id,
    }
    annotations = {ANNOTATION_TABLE_SET: table_ids}
    checkpoint_env_name = ENV_CHECKPOINT_BUCKET if spec.storage_backend == "gcs" else ENV_CHECKPOINT_TABLE
    env = {
        ENV_STORAGE_BACKEND: spec.storage_backend,
        ENV_RAW_BUCKET: spec.raw_bucket,
        ENV_RAW_PREFIX: spec.raw_prefix.rstrip("/"),
        checkpoint_env_name: spec.checkpoint_location,
        ENV_DESTINATION_DATABASE: spec.destination_database,
        ENV_DESTINATION_SCHEMA: spec.destination_schema,
        ENV_LOADER_TABLE_SET_JSON: json.dumps(
            [table.to_env_entry() for table in sorted(spec.tables, key=lambda t: t.table_id)],
            separators=(",", ":"),
        ),
        **dict(spec.extra_env),
    }
    # Ces champs d'identité ne peuvent être remplacés par extra_env.
    env[ENV_DESTINATION_DATABASE] = spec.destination_database
    env[ENV_DESTINATION_SCHEMA] = spec.destination_schema
    env["QUADRINGENT_MIRROR_SCHEMA"] = spec.mirror_schema if spec.mirror_schema is not None else spec.destination_schema
    container: dict[str, object] = {
        "name": "destination-loader",
        "image": spec.image,
        # Sans ``command`` explicite, le pod hérite de l'ENTRYPOINT par
        # défaut de l'image de capture (``as400_continuous_capture.py``,
        # docker/Dockerfile) — jamais le bon script. Constaté au premier
        # démarrage réel sur GKE le 24 septembre 2026.
        "command": ["python", "-P", "/app/quadringent_destination_loader.py"],
        "env": [{"name": key, "value": value} for key, value in env.items()],
        # Compte, clé privée, utilisateur et rôle Snowflake : toujours une
        # référence de Secret, jamais une valeur en clair (même discipline
        # que build_reader_deployment).
        "envFrom": [{"secretRef": {"name": spec.destination_secret_ref}}],
        "securityContext": _capture_security_context(),
    }
    if spec.resources:
        container["resources"] = dict(spec.resources)
    pod_spec: dict[str, object] = {
        "restartPolicy": "Always",
        "serviceAccountName": spec.service_account_name,
        "securityContext": {"runAsNonRoot": True, "runAsUser": CAPTURE_RUN_AS_USER},
        "containers": [container],
    }
    replicas = 0 if spec.paused else 1
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": spec.namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": {
            "replicas": replicas,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {LABEL_DESTINATION: spec.destination_id}},
            "template": {
                "metadata": {
                    "labels": {**labels},
                    "annotations": annotations,
                },
                "spec": pod_spec,
            },
        },
    }


@dataclass(frozen=True)
class InitialCopyDesiredSpec:
    """Ce que le Job de copie initiale d'une table doit contenir."""

    pipeline_id: str
    table_id: str
    schema_name: str
    table_name: str
    source_id: str
    image: str
    namespace: str
    storage_backend: str
    source_time_zone: str
    raw_prefix: str
    boundary: JournalBoundary
    run_id: str
    evidence_key: str
    destination_secret_ref: str
    ibmi_secret_ref: str
    service_account_name: str
    # Hôte/utilisateur IBM i et bucket brut : mêmes champs, même raison que
    # ``ReaderDesiredSpec`` — ``ReadOnlyTableSnapshot.Settings.fromEnvironment``
    # (Java) et ``as400_snapshot_publish.py`` les exigent avant tout I/O.
    ibmi_host: str = ""
    ibmi_user: str = ""
    raw_bucket: str = ""
    active_deadline_seconds: int = 3600
    resources: Mapping[str, object] = field(default_factory=dict)
    ca_secret_ref: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", self.run_id):
            raise ManifestError("run_id doit être un UUID — cf. ReadOnlyTableSnapshot")
        if not self.ibmi_host.strip() or not self.ibmi_user.strip():
            raise ManifestError("ISERIES_HOST et ISERIES_USER sont obligatoires pour la copie initiale")
        if not self.raw_bucket.strip():
            raise ManifestError("raw_bucket est obligatoire pour la copie initiale")
        _require_service_account_name(self.service_account_name)


def build_initial_copy_job(spec: InitialCopyDesiredSpec) -> dict[str, object]:
    """Rend le Job de copie initiale : snapshot -> publication -> preuve.

    Le répertoire de sortie de la copie est dérivé du ``run_id`` (UUID) et
    n'est jamais réutilisé entre deux tentatives : ``restart_initial_copy``
    génère toujours un nouveau ``run_id``, donc un nouveau répertoire vide —
    condition posée par ``ReadOnlyTableSnapshot`` (répertoire non vide =
    échec immédiat).
    """

    name = initial_copy_job_name(spec.table_id, spec.run_id)
    labels = {
        LABEL_MANAGED_BY: "quadringent-control-plane",
        LABEL_COMPONENT: "initial-copy",
        LABEL_SOURCE: spec.source_id,
        LABEL_TABLE: spec.table_id,
        LABEL_PIPELINE: spec.pipeline_id,
    }
    annotations = {
        ANNOTATION_BOUNDARY: f"{spec.boundary.receiver_name}:{spec.boundary.bootstrap_sequence}",
        ANNOTATION_RUN_ID: spec.run_id,
    }
    snapshot_output_dir = f"/var/run/quadringent/snapshot/{spec.run_id}"
    env = {
        ENV_STORAGE_BACKEND: spec.storage_backend,
        ENV_SOURCE_TIME_ZONE: spec.source_time_zone,
        ENV_RAW_PREFIX: spec.raw_prefix.rstrip("/"),
        ENV_RAW_BUCKET: spec.raw_bucket,
        ENV_SNAPSHOT_RUN_ID: spec.run_id,
        ENV_SNAPSHOT_OUTPUT_DIR: snapshot_output_dir,
        ENV_SNAPSHOT_ISERIES_HOST: spec.ibmi_host,
        ENV_SNAPSHOT_ISERIES_USER: spec.ibmi_user,
        ENV_SNAPSHOT_ISERIES_SCHEMA: spec.schema_name,
        ENV_SNAPSHOT_ISERIES_TABLE: spec.table_name,
        ENV_BOOTSTRAP_RECEIVER: spec.boundary.receiver_name,
        ENV_BOOTSTRAP_RECEIVER_LIBRARY: spec.boundary.receiver_library,
        ENV_BOOTSTRAP_SEQUENCE: str(spec.boundary.bootstrap_sequence),
        ENV_BOOTSTRAP_OBSERVED_AT: spec.boundary.observed_at.isoformat(),
        ENV_EVIDENCE_KEY: spec.evidence_key,
        ENV_PIPELINE_ID: spec.pipeline_id,
        ENV_TABLE_ID: spec.table_id,
    }
    container: dict[str, object] = {
        "name": "initial-copy",
        "image": spec.image,
        # Sans ``command`` explicite, le pod hérite de l'ENTRYPOINT de capture
        # continue (``as400_continuous_capture.py``) qui refuse tout argument
        # de copie — constaté au premier démarrage réel sur GKE le 24
        # septembre 2026 (``unrecognized arguments: --schema ... --table
        # ...``). Le script cible est 100 % piloté par l'environnement, comme
        # ``ReadOnlyTableSnapshot`` (Java) qu'il invoque : aucun argument.
        "command": ["/opt/venv/bin/python", "/app/quadringent_initial_copy_job.py"],
        "env": [{"name": key, "value": value} for key, value in env.items()],
        "envFrom": [
            {"secretRef": {"name": spec.destination_secret_ref}},
            {"secretRef": {"name": spec.ibmi_secret_ref}},
        ],
        "securityContext": _capture_security_context(),
        "volumeMounts": [{"name": "snapshot-work", "mountPath": "/var/run/quadringent/snapshot"}],
    }
    if spec.resources:
        container["resources"] = dict(spec.resources)
    pod_spec = {
        "restartPolicy": "Never",
        "serviceAccountName": spec.service_account_name,
        "securityContext": {"runAsNonRoot": True, "runAsUser": CAPTURE_RUN_AS_USER},
        "containers": [container],
        # Répertoire de sortie de l'instantané local, vide à chaque tentative
        # (exigé par ``ReadOnlyTableSnapshot``) : un ``emptyDir`` dédié, pas le
        # système de fichiers racine partagé du conteneur.
        "volumes": [{"name": "snapshot-work", "emptyDir": {}}],
    }
    _with_ca_mount(container, pod_spec, spec.ca_secret_ref)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": spec.namespace, "labels": labels, "annotations": annotations},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": spec.active_deadline_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": pod_spec,
            },
        },
    }


@dataclass(frozen=True)
class ReplayDesiredSpec:
    """Rejeu borné d'une plage de séquences journal, pour une table."""

    pipeline_id: str
    table_id: str
    schema_name: str
    table_name: str
    source_id: str
    image: str
    namespace: str
    storage_backend: str
    source_time_zone: str
    raw_prefix: str
    receiver_name: str
    from_sequence: int
    to_sequence: int
    destination_secret_ref: str
    ibmi_secret_ref: str
    service_account_name: str
    active_deadline_seconds: int = 3600
    ca_secret_ref: str | None = None

    def __post_init__(self) -> None:
        if self.from_sequence < 0 or self.to_sequence < self.from_sequence:
            raise ManifestError("plage de rejeu invalide")
        _require_service_account_name(self.service_account_name)


def build_replay_job(spec: ReplayDesiredSpec) -> dict[str, object]:
    name = replay_job_name(spec.pipeline_id, spec.from_sequence, spec.to_sequence)
    labels = {
        LABEL_MANAGED_BY: "quadringent-control-plane",
        LABEL_COMPONENT: "replay",
        LABEL_SOURCE: spec.source_id,
        LABEL_TABLE: spec.table_id,
        LABEL_PIPELINE: spec.pipeline_id,
    }
    env = {
        ENV_STORAGE_BACKEND: spec.storage_backend,
        ENV_SOURCE_TIME_ZONE: spec.source_time_zone,
        ENV_RAW_PREFIX: spec.raw_prefix.rstrip("/"),
        ENV_BOOTSTRAP_RECEIVER: spec.receiver_name,
        ENV_REPLAY_FROM: str(spec.from_sequence),
        ENV_REPLAY_TO: str(spec.to_sequence),
    }
    container = {
        "name": "replay",
        "image": spec.image,
        "args": ["--schema", spec.schema_name, "--table", spec.table_name],
        "env": [{"name": key, "value": value} for key, value in env.items()],
        "envFrom": [
            {"secretRef": {"name": spec.destination_secret_ref}},
            {"secretRef": {"name": spec.ibmi_secret_ref}},
        ],
        "securityContext": _capture_security_context(),
    }
    pod_spec = {
        "restartPolicy": "Never",
        "serviceAccountName": spec.service_account_name,
        "securityContext": {"runAsNonRoot": True, "runAsUser": CAPTURE_RUN_AS_USER},
        "containers": [container],
    }
    _with_ca_mount(container, pod_spec, spec.ca_secret_ref)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": spec.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": spec.active_deadline_seconds,
            "ttlSecondsAfterFinished": 3600,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def source_probe_job_name(run_id: str) -> str:
    return _dns_name(PROBE_PREFIX, run_id)


def table_discovery_job_name(run_id: str) -> str:
    return _dns_name(DISCOVER_PREFIX, run_id)


@dataclass(frozen=True)
class SourceProbeJobSpec:
    """Ce que le Job de sonde de source doit contenir (chantier « prod-wiring »).

    Opération courte, jetable : jamais de mot de passe en argument de
    commande — seulement via ``secret_ref`` (``envFrom.secretRef``), jamais
    journalisé. ``run_id`` rend le nom déterministe et évite toute collision
    entre deux sondes concurrentes sur la même source.
    """

    run_id: str
    ibmi_host: str
    ibmi_user: str
    tls_trust: str
    pinned_fingerprint: str | None
    image: str
    namespace: str
    secret_ref: str
    active_deadline_seconds: int = 60
    ca_secret_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.ibmi_host.strip() or not self.ibmi_user.strip():
            raise ManifestError("hôte et utilisateur IBM i requis pour la sonde")
        if not self.secret_ref.strip():
            raise ManifestError("le mot de passe IBM i doit venir d'un Secret référencé, jamais en clair")


def build_source_probe_job(spec: SourceProbeJobSpec) -> dict[str, object]:
    """Job éphémère : sonde réseau/TLS/authentification/version/QTIMZON.

    Utilise l'image de capture (Java/JTOpen) — la seule à savoir parler à
    IBM i — via ``scripts/quadringent_source_probe_job.py``. Le résultat
    (JSON, une ligne) est écrit sur stdout, relu depuis les journaux du pod
    du Job (voir ``executor/diagnostic_jobs.py`` pour la justification de ce
    mécanisme de retour).
    """

    name = source_probe_job_name(spec.run_id)
    labels = {
        LABEL_MANAGED_BY: "quadringent-control-plane",
        LABEL_COMPONENT: "source-probe",
    }
    args = ["--host", spec.ibmi_host, "--user", spec.ibmi_user, "--tls-trust", spec.tls_trust]
    if spec.pinned_fingerprint:
        args += ["--pinned-fingerprint", spec.pinned_fingerprint]
    container: dict[str, object] = {
        "name": "source-probe",
        "image": spec.image,
        "command": ["/opt/venv/bin/python", "/app/quadringent_source_probe_job.py"],
        "args": args,
        "envFrom": [{"secretRef": {"name": spec.secret_ref}}],
        "resources": dict(DIAGNOSTIC_RESOURCES),
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": False,
            "runAsNonRoot": True,
            "runAsUser": DIAGNOSTIC_RUN_AS_USER,
        },
    }
    pod_spec: dict[str, object] = {
        "restartPolicy": "Never",
        "securityContext": {"runAsNonRoot": True, "runAsUser": DIAGNOSTIC_RUN_AS_USER},
        "containers": [container],
    }
    _with_ca_mount(container, pod_spec, spec.ca_secret_ref)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": spec.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": spec.active_deadline_seconds,
            "ttlSecondsAfterFinished": 120,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


@dataclass(frozen=True)
class TableDiscoveryJobSpec:
    """Ce que le Job de découverte de tables doit contenir."""

    run_id: str
    ibmi_host: str
    ibmi_user: str
    libraries: tuple[str, ...] | None
    limit: int
    search: str | None
    image: str
    namespace: str
    secret_ref: str
    active_deadline_seconds: int = 120
    ca_secret_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.ibmi_host.strip() or not self.ibmi_user.strip():
            raise ManifestError("hôte et utilisateur IBM i requis pour la découverte")
        if not self.secret_ref.strip():
            raise ManifestError("le mot de passe IBM i doit venir d'un Secret référencé, jamais en clair")
        if not 1 <= self.limit <= 5000:
            raise ManifestError("limit doit être compris entre 1 et 5000")


def build_table_discovery_job(spec: TableDiscoveryJobSpec) -> dict[str, object]:
    """Job éphémère : découverte de tables (catalogue IBM i, jamais de ligne métier).

    Même mécanique que ``build_source_probe_job`` : image de capture, mot de
    passe uniquement via Secret, sortie relue depuis les journaux du pod.
    """

    name = table_discovery_job_name(spec.run_id)
    labels = {
        LABEL_MANAGED_BY: "quadringent-control-plane",
        LABEL_COMPONENT: "table-discovery",
    }
    args = ["--host", spec.ibmi_host, "--user", spec.ibmi_user, "--limit", str(spec.limit)]
    if spec.libraries:
        args += ["--libraries", ",".join(spec.libraries)]
    if spec.search:
        args += ["--search", spec.search]
    container: dict[str, object] = {
        "name": "table-discovery",
        "image": spec.image,
        "command": ["/opt/venv/bin/python", "/app/quadringent_table_discovery_job.py"],
        "args": args,
        "envFrom": [{"secretRef": {"name": spec.secret_ref}}],
        "resources": dict(DIAGNOSTIC_RESOURCES),
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": False,
            "runAsNonRoot": True,
            "runAsUser": DIAGNOSTIC_RUN_AS_USER,
        },
    }
    pod_spec: dict[str, object] = {
        "restartPolicy": "Never",
        "securityContext": {"runAsNonRoot": True, "runAsUser": DIAGNOSTIC_RUN_AS_USER},
        "containers": [container],
    }
    _with_ca_mount(container, pod_spec, spec.ca_secret_ref)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": spec.namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": spec.active_deadline_seconds,
            "ttlSecondsAfterFinished": 120,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }


def spec_hash(spec: Mapping[str, object]) -> str:
    """Empreinte stable de ``spec`` — pas de la ``metadata`` ni du nom.

    Utilisée par la réconciliation pour décider si un objet existant doit
    être mis à jour (`kubectl apply`-like) sans recréer l'objet : deux
    manifestes avec la même ``spec`` produisent la même empreinte, donc pas
    de mise à jour inutile au redémarrage du control plane.
    """

    payload = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def with_spec_hash(manifest: Mapping[str, object]) -> dict[str, object]:
    """Copie ``manifest`` en posant l'annotation d'empreinte de ``spec``."""

    result = json.loads(json.dumps(manifest))
    annotations = result.setdefault("metadata", {}).setdefault("annotations", {})
    annotations[ANNOTATION_SPEC_HASH] = spec_hash(result["spec"])
    return result
