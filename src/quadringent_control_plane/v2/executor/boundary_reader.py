"""Lecteur de frontière réel (`BoundaryReaderProtocol`, chantier « pipeline-exec »).

Décision retenue : **in-process, jamais un Job Kubernetes**.

L'image du control plane (``docker/control-plane.Dockerfile``) embarque déjà
le JRE, ``probe.jar`` et JTOpen (``AS400_JAVA``/``AS400_JAVA_CLASSPATH``) —
exactement ce que ``quadringent_control_plane.v2.executor.diagnostic_jobs``
utilise pour la sonde de source et la découverte de tables, mais *via* un Job
éphémère. La lecture de frontière (``read_boundary``) est un cas différent :

- elle est appelée à **haute fréquence** (à chaque tour de réconciliation du
  lecteur, pour chaque table ``copying``/``live`` d'une source — voir
  ``KubernetesPipelineExecutor._desired_reader_manifests``), jamais une
  opération ponctuelle déclenchée par un opérateur ;
- un Job Kubernetes par lecture coûterait un cycle de scheduling/pull/JVM à
  chaque tour, pour une requête JDBC qui prend quelques centaines de
  millisecondes — un surcoût et une latence inutiles, sans bénéfice
  d'isolation supplémentaire (le control plane décrypte déjà les secrets de
  connexion en mémoire pour les Jobs de diagnostic, cf.
  ``diagnostic_jobs.py::KubernetesJobTableDiscoveryClient._resolve_source`` —
  ce n'est donc pas une frontière de confiance nouvelle) ;
- ``quadringent.java_catalog.JavaReceiverCatalog`` est *déjà* le lecteur
  in-process minimal pour ce besoin exact : un processus JVM **court et
  jetable** (``subprocess.run``, pas de session persistante comme
  ``PersistentJavaWorker``) qui exécute
  ``io.quadringent.as400.ReadOnlyReceiverCatalog`` et rend le catalogue des
  receveurs (bibliothèque, nom, statut, première/dernière séquence) — jamais
  de ligne de donnée métier. Le réutiliser ici évite une troisième
  implémentation du même appel JDBC en lecture seule (après
  ``ReadOnlyReceiverCatalog.java`` et ``JavaWindowRunner``/
  ``WorkerReceiverCatalog`` côté capture v1).

``PersistentJavaWorker`` (session JDBC tenue ouverte) a été écarté ici : son
protocole ``catalog``/``tail`` ne prend pas la bibliothèque/le nom de journal
en paramètre de commande — ils viennent de l'environnement du processus
(``AS400_JOURNAL_LIBRARY``/``AS400_JOURNAL_NAME``), posé une fois au
démarrage d'un pod de capture qui ne sert qu'*un seul* journal. Le control
plane doit au contraire lire la frontière de plusieurs sources/journaux dans
le même processus : maintenir un worker persistant par journal (avec sa
connexion JDBC ouverte en permanence, y compris pour des sources inactives)
ajouterait un état à gérer et une connexion IBM i par source qui reste
ouverte sans raison entre deux tours de réconciliation. ``JavaReceiverCatalog``
n'ouvre une connexion JDBC que le temps de la requête.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import stat
import tempfile
from typing import Callable, Iterator, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine

from quadringent.continuous import ReceiverSnapshot
from quadringent.java_catalog import JavaReceiverCatalog

from .. import schema as v2_schema
from ..crypto import SecretBox
from .boundary import BoundaryError, JournalBoundary

ENV_AS400_JAVA = "AS400_JAVA"
ENV_AS400_JAVA_CLASSPATH = "AS400_JAVA_CLASSPATH"
DEFAULT_JAVA = "java"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RECEIVER_METADATA_LIMIT = 8


@contextmanager
def _pinned_ca_file(pem: str | None) -> Iterator[str | None]:
    """Fichier temporaire 0600 portant le PEM épinglé, nettoyé toujours.

    ``None`` sans PEM (autorité publique ou encore ``"unknown"`` — jamais de
    fichier créé, ``JavaReceiverCatalog`` utilisera alors le magasin de
    confiance système/JVM par défaut, objectif A). Le fichier n'est jamais
    laissé sur disque au-delà de l'appel JDBC ponctuel qui l'utilise — y
    compris si ``catalog.snapshot()`` lève."""

    if pem is None:
        yield None
        return
    descriptor, path = tempfile.mkstemp(prefix="qdt-ibmi-ca-", suffix=".pem")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — jamais lisible par un autre utilisateur
        with os.fdopen(descriptor, "w", encoding="us-ascii") as handle:
            handle.write(pem)
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class BoundaryUnavailableError(RuntimeError):
    """Impossible de lire la frontière — code sûr porté par ``.code``, jamais
    de détail JDBC (cohérent avec ``DiagnosticJobError``/``ExecutorError``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReceiverCatalogFactory(Protocol):
    """Fabrique injectée — un faux catalogue déterministe suffit en tests."""

    def __call__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        journal_library: str,
        journal_name: str,
        ca_file: str | None = None,
    ) -> "_CatalogProtocol": ...


class _CatalogProtocol(Protocol):
    def snapshot(self, required_receiver: str | None = None) -> Sequence[ReceiverSnapshot]: ...


def _default_catalog_factory(
    java: str, classpath: str, timeout_seconds: float, limit: int
) -> ReceiverCatalogFactory:
    def factory(
        *,
        host: str,
        user: str,
        password: str,
        journal_library: str,
        journal_name: str,
        ca_file: str | None = None,
    ) -> JavaReceiverCatalog:
        return JavaReceiverCatalog(
            java=java,
            classpath=classpath,
            host=host,
            user=user,
            journal_library=journal_library,
            journal_name=journal_name,
            limit=limit,
            timeout_seconds=timeout_seconds,
            password=password,
            ca_file=ca_file,
        )

    return factory


@dataclass(frozen=True)
class _SourceCredentials:
    ibmi_host: str
    ibmi_user: str
    password: str
    # PEM CA épinglé de la source (``sources.tls_pinned_pem``) — ``None``
    # pour une autorité publique ou encore ``"unknown"`` (jamais montée sans
    # épinglage explicite, voir migration 0013_source_tls_pin).
    tls_pinned_pem: str | None = None


@dataclass(frozen=True)
class _TableJournal:
    source_id: str
    journal_library: str
    journal_name: str


class JavaBoundaryReader:
    """``BoundaryReaderProtocol`` réel — ``JavaReceiverCatalog`` in-process.

    Les identifiants IBM i ne sont jamais journalisés ici ni transmis en
    clair : ils sont déchiffrés en mémoire (``SecretBox``, même magasin que
    ``executor/secrets_provisioner.py``) puis passés directement en variables
    d'environnement du sous-processus JVM éphémère — jamais un argument de
    commande, jamais une ligne de log.
    """

    def __init__(
        self,
        engine: Engine,
        secret_box: SecretBox,
        *,
        java: str | None = None,
        classpath: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        limit: int = DEFAULT_RECEIVER_METADATA_LIMIT,
        catalog_factory: ReceiverCatalogFactory | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._engine = engine
        self._secret_box = secret_box
        self._now = now
        if catalog_factory is not None:
            self._catalog_factory = catalog_factory
        else:
            resolved_java = java or os.environ.get(ENV_AS400_JAVA, DEFAULT_JAVA)
            resolved_classpath = classpath or os.environ.get(ENV_AS400_JAVA_CLASSPATH, "")
            if not resolved_classpath.strip():
                raise BoundaryUnavailableError(
                    "capability_unavailable",
                    f"{ENV_AS400_JAVA_CLASSPATH} est requis pour lire la frontière du journal",
                )
            self._catalog_factory = _default_catalog_factory(
                resolved_java, resolved_classpath, timeout_seconds, limit
            )

    def read_boundary(self, *, source_id: str, table_id: str) -> JournalBoundary:
        journal = self._table_journal(table_id)
        if journal.source_id != source_id:
            raise BoundaryUnavailableError(
                "not_found", "la table ne correspond pas à la source déclarée"
            )
        credentials = self._source_credentials(source_id)
        with _pinned_ca_file(credentials.tls_pinned_pem) as ca_file:
            catalog = self._catalog_factory(
                host=credentials.ibmi_host,
                user=credentials.ibmi_user,
                password=credentials.password,
                journal_library=journal.journal_library,
                journal_name=journal.journal_name,
                ca_file=ca_file,
            )
            try:
                snapshots = catalog.snapshot()
            except Exception as error:  # noqa: BLE001 — jamais de détail JDBC distant
                raise BoundaryUnavailableError(
                    "executor_unavailable", "lecture de la position du journal impossible"
                ) from error
        attached = _attached_receiver(snapshots)
        if attached is None:
            raise BoundaryUnavailableError(
                "capability_unavailable", "aucun receveur ATTACHED pour ce journal"
            )
        # Receveur fraîchement attaché sans entrée encore écrite : la
        # capture continue doit démarrer à sa première séquence, jamais à
        # une valeur inventée — voir ``JournalBoundary.bootstrap_sequence``
        # (démarre à ``last_sequence + 1``).
        last_sequence = attached.last_sequence
        if last_sequence is None:
            if attached.first_sequence is None:
                raise BoundaryUnavailableError(
                    "capability_unavailable",
                    "le receveur ATTACHED ne porte encore aucune séquence exploitable",
                )
            last_sequence = attached.first_sequence - 1
        try:
            return JournalBoundary(
                receiver_library=attached.receiver_library,
                receiver_name=attached.receiver,
                last_sequence=last_sequence,
                observed_at=self._now(),
            )
        except BoundaryError as error:
            raise BoundaryUnavailableError(
                "executor_unavailable", "position de journal invalide"
            ) from error

    def _table_journal(self, table_id: str) -> _TableJournal:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        v2_schema.tables.c.source_id,
                        v2_schema.tables.c.journal_library,
                        v2_schema.tables.c.journal_name,
                    ).where(v2_schema.tables.c.id == table_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise BoundaryUnavailableError("not_found", "table introuvable pour la lecture de frontière")
        if not row["journal_library"] or not row["journal_name"]:
            raise BoundaryUnavailableError(
                "capability_unavailable", "aucun journal découvert pour cette table"
            )
        return _TableJournal(
            source_id=row["source_id"],
            journal_library=row["journal_library"],
            journal_name=row["journal_name"],
        )

    def _source_credentials(self, source_id: str) -> _SourceCredentials:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        v2_schema.sources.c.ibmi_host,
                        v2_schema.sources.c.ibmi_user,
                        v2_schema.sources.c.secret_ciphertext,
                        v2_schema.sources.c.tls_pinned_pem,
                    ).where(v2_schema.sources.c.id == source_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise BoundaryUnavailableError("not_found", "source introuvable pour la lecture de frontière")
        password = self._secret_box.decrypt(row["secret_ciphertext"])
        return _SourceCredentials(
            ibmi_host=row["ibmi_host"],
            ibmi_user=row["ibmi_user"],
            password=password,
            tls_pinned_pem=row["tls_pinned_pem"],
        )


def _attached_receiver(snapshots: Sequence[ReceiverSnapshot]) -> ReceiverSnapshot | None:
    for item in snapshots:
        if item.status == "ATTACHED":
            return item
    return None
