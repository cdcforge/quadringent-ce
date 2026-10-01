"""Provisionnement des Secrets Kubernetes référencés (chantier 4, tâche 3).

``build_reader_deployment``/``build_initial_copy_job``/``build_replay_job``
(``manifests.py``) référencent deux Secrets par ``envFrom.secretRef`` —
``qdt-source-<id>`` (mot de passe IBM i) et ``qdt-destination-<id>`` (compte
et clé privée Snowflake) — sans jamais les provisionner eux-mêmes : c'est le
rôle de ce module. Les valeurs sont déchiffrées ici (via ``SecretBox``,
déjà utilisé par ``services/sources.py``/``services/destinations.py``) puis
transmises telles quelles au client Kubernetes injecté — **jamais
journalisées**, jamais retournées à un appelant HTTP, jamais écrites sur
disque.

Noms de clé à l'intérieur des Secrets : ``ISERIES_PASSWORD`` (mot de passe
IBM i, même variable que le lecteur v1 résout déjà via
``QUADRINGENT_IBMI_PASSWORD_SECRET``/``_KEY`` — voir ``site_config.py``) ;
``SNOWFLAKE_ACCOUNT`` et ``SNOWFLAKE_PRIVATE_KEY_PEM`` pour la destination
(nouvelle convention v2, aucun équivalent v1 à reprendre puisque le
chargeur v1 ne connaît pas encore de compte par destination).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import SecretBox
from .manifests import IBMI_CA_SECRET_KEY, destination_secret_ref, ibmi_ca_secret_ref, ibmi_secret_ref

KEY_ISERIES_PASSWORD = "ISERIES_PASSWORD"
KEY_SNOWFLAKE_ACCOUNT = "SNOWFLAKE_ACCOUNT"
KEY_SNOWFLAKE_PRIVATE_KEY_PEM = "SNOWFLAKE_PRIVATE_KEY_PEM"
# Depuis 0011_dest_service_identity : le chargeur de destination a besoin de
# l'utilisateur et du rôle de service pour ouvrir une connexion
# snowflake-connector-python (MERGE miroir) ou construire le profil
# Snowpipe Streaming (historique) — le compte et la clé privée seuls n'y
# suffisent pas.
KEY_SNOWFLAKE_USER = "SNOWFLAKE_USER"
KEY_SNOWFLAKE_ROLE = "SNOWFLAKE_ROLE"


class SecretsClientProtocol:
    """Sous-ensemble de ``KubernetesSecretsClient`` requis ici (duck typing)."""

    def upsert_secret(self, name: str, string_data: dict[str, str]) -> None: ...


class SourceNotFoundError(LookupError):
    """Aucune source pour cet identifiant — appelant responsable du 404 HTTP."""


class DestinationNotFoundError(LookupError):
    """Aucune destination pour cet identifiant — appelant responsable du 404 HTTP."""


class DestinationServiceIdentityMissingError(ValueError):
    """Destination créée avant 0011_dest_service_identity : pas d'utilisateur/rôle
    persisté — refus explicite plutôt que de provisionner un Secret Snowflake
    incomplet (le chargeur de destination échouerait plus tard, sans indice)."""


class SecretsProvisioner:
    """Déchiffre les identifiants en base et les pousse vers Kubernetes.

    Jamais de journalisation des valeurs déchiffrées — seul le nom du
    Secret provisionné est un identifiant sûr à tracer (audit, logs).
    """

    def __init__(self, engine: Engine, secret_box: SecretBox, *, secrets_client: SecretsClientProtocol) -> None:
        self._engine = engine
        self._secret_box = secret_box
        self._secrets_client = secrets_client

    def provision_source_secret(self, source_id: str) -> str:
        """Provisionne (ou met à jour) le Secret IBM i ; renvoie son nom."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.sources.c.secret_ciphertext).where(
                        v2_schema.sources.c.id == source_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise SourceNotFoundError(source_id)
        password = self._secret_box.decrypt(row["secret_ciphertext"])
        name = ibmi_secret_ref(source_id)
        self._secrets_client.upsert_secret(name, {KEY_ISERIES_PASSWORD: password})
        return name

    def provision_source_ca_secret(self, source_id: str) -> str | None:
        """Maintient à jour le Secret CA épinglé de la source ; ``None`` sans épinglage.

        Nom stable par source (``ibmi_ca_secret_ref``, jamais un nom
        éphémère par ``run_id`` comme pour les Jobs de diagnostic — voir
        ``executor/diagnostic_jobs.py::_PinnedCaSecret``) : les charges
        longues (lecteur, chargeur, copie, rejeu) le référencent en continu,
        il doit donc survivre à la réconciliation qui l'a créé, pas à une
        seule exécution. Sans PEM épinglé (``sources.tls_pinned_pem`` NULL —
        autorité publique ou encore ``"unknown"``), ne crée jamais de Secret
        et n'en supprime jamais un existant : un Secret déjà en place peut
        être référencé par une charge en cours, et un dé-épinglage explicite
        n'est pas de la responsabilité de ce provisionneur (voir la
        docstring du module — jamais de suppression proactive ici).
        """

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.sources.c.tls_pinned_pem).where(v2_schema.sources.c.id == source_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise SourceNotFoundError(source_id)
        pinned_pem = row["tls_pinned_pem"]
        if not pinned_pem:
            return None
        name = ibmi_ca_secret_ref(source_id)
        self._secrets_client.upsert_secret(name, {IBMI_CA_SECRET_KEY: pinned_pem})
        return name

    def provision_destination_secret(self, destination_id: str) -> str:
        """Provisionne (ou met à jour) le Secret Snowflake ; renvoie son nom."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        v2_schema.destinations.c.snowflake_account,
                        v2_schema.destinations.c.key_pair_ciphertext,
                        v2_schema.destinations.c.service_user,
                        v2_schema.destinations.c.service_role,
                    ).where(v2_schema.destinations.c.id == destination_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DestinationNotFoundError(destination_id)
        if not row["service_user"] or not row["service_role"]:
            raise DestinationServiceIdentityMissingError(destination_id)
        private_key_pem = self._secret_box.decrypt(row["key_pair_ciphertext"])
        name = destination_secret_ref(destination_id)
        self._secrets_client.upsert_secret(
            name,
            {
                KEY_SNOWFLAKE_ACCOUNT: row["snowflake_account"],
                KEY_SNOWFLAKE_PRIVATE_KEY_PEM: private_key_pem,
                KEY_SNOWFLAKE_USER: row["service_user"],
                KEY_SNOWFLAKE_ROLE: row["service_role"],
            },
        )
        return name
