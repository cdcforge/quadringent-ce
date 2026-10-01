"""Configuration déclarée du site — aucune valeur d'installation n'est codée ici.

Toute valeur propre à un déploiement (compte AWS, bucket, bibliothèque source,
destination Snowflake, manifeste de tables, identités de pipeline) arrive par
les variables d'environnement ``QUADRINGENT_*`` — injectées par la chart depuis
les values du site — ou par :func:`install` dans les tests et les outils. Une
valeur absente ou invalide est un refus explicite, jamais un défaut silencieux.

Les seules constantes de ce module sont des invariantes produit : la liste des
environnements autorisés (aucun chemin de promotion PROD n'existe), les bornes
de validation et les conventions de nommage dérivées.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
import os
import re
import threading
from typing import Iterator, Mapping, Sequence


class SiteConfigurationError(ValueError):
    """La configuration de site est absente, incohérente ou invalide."""


# Aucun chemin de promotion production : l'inventaire des environnements est un
# invariant produit, borné, et ne contient jamais « prod ».
ALLOWED_ENVIRONMENTS = frozenset({"dev", "int", "test", "staging"})

_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
_SNOWFLAKE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_AWS_ACCOUNT = re.compile(r"^[0-9]{12}$")
_AWS_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
_S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SITE_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$")
_DESTINATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TLS_CA_FILE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_SNOWFLAKE_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_CLIENT_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IBMI_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_PREFIX_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_PROOF_DOC_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}\.json$")
_FRAGMENT = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MAX_PREFIX_SEGMENTS = 8
_MAX_FLEET_TABLES = 64
_MAX_LIST_ENTRIES = 64

ENVIRONMENT_VARIABLES: tuple[str, ...] = (
    "QUADRINGENT_ENVIRONMENT",
    "QUADRINGENT_SITE_ID",
    "QUADRINGENT_STORAGE_BACKEND",
    "QUADRINGENT_AWS_ACCOUNT_ID",
    "QUADRINGENT_AWS_REGION",
    "QUADRINGENT_RAW_BUCKET",
    "QUADRINGENT_RAW_PREFIX_ROOT",
    "QUADRINGENT_CHECKPOINT_TABLE",
    "QUADRINGENT_SOURCE_SCHEMA",
    "QUADRINGENT_PROOF_TABLE",
    "QUADRINGENT_JOURNAL_NAME",
    "QUADRINGENT_DESTINATION_DATABASE",
    "QUADRINGENT_DESTINATION_SCHEMA",
    "QUADRINGENT_DESTINATION_ID",
    "QUADRINGENT_FLEET_TABLES",
    "QUADRINGENT_KEYED_TABLES",
    "QUADRINGENT_PROVISIONED_STAGES",
    "QUADRINGENT_RESERVABLE_TABLES",
    "QUADRINGENT_PROOF_KEY_COLUMNS",
    "QUADRINGENT_FORBIDDEN_FRAGMENTS",
    "QUADRINGENT_IBMI_TLS_CA_FILE",
    "QUADRINGENT_SNOWFLAKE_ACCOUNT",
    "QUADRINGENT_SNOWFLAKE_CONNECTION",
    "QUADRINGENT_SNOWFLAKE_WAREHOUSE",
    "QUADRINGENT_AWS_PROFILE",
    "QUADRINGENT_IBMI_HOST",
    "QUADRINGENT_IBMI_USER",
    "QUADRINGENT_IBMI_PASSWORD_SECRET",
    "QUADRINGENT_IBMI_PASSWORD_KEY",
    "QUADRINGENT_AUTONOMOUS_PROOF_NAME",
    "QUADRINGENT_DESTINATION_PREFIX",
    "QUADRINGENT_SNOWFLAKE_CREDIT_PRICE",
    "QUADRINGENT_COST_CURRENCY",
)


@dataclass(frozen=True)
class SiteConfig:
    """Identité et périmètres déclarés d'un déploiement.

    Aucun champ n'a de valeur par défaut : un déploiement doit déclarer
    explicitement chaque élément de son périmètre. Les propriétés dérivent les
    noms d'objets par convention, jamais par littéral d'installation.
    """

    environment: str
    site_id: str
    aws_account_id: str
    aws_region: str
    raw_bucket: str
    raw_prefix_root: str
    checkpoint_table: str
    source_schema: str
    proof_table: str
    journal_name: str
    destination_database: str
    destination_schema: str
    destination_id: str
    tls_ca_file: str
    snowflake_account: str
    ibmi_host: str
    ibmi_user: str
    fleet_tables: tuple[str, ...]
    snowflake_connection: str | None = None
    aws_profile: str | None = None
    ibmi_password_secret: str | None = None
    ibmi_password_key: str | None = None
    keyed_tables: tuple[str, ...] = ()
    provisioned_stages: tuple[str, ...] = ()
    reservable_tables: tuple[str, ...] | None = None
    proof_key_columns: tuple[str, ...] = ()
    forbidden_fragments: tuple[str, ...] = ()
    autonomous_proof_name: str = "quadringent-autonomous-latest.json"
    # Préfixe des objets Snowflake provisionnés (tables, warehouse, rôle
    # vérificateur) : contrat d'infra du site, comme le nom de la preuve.
    destination_prefix: str = "QUADRINGENT"
    # Une valeur explicite désigne un warehouse existant géré hors du produit.
    snowflake_warehouse: str | None = None
    snowflake_credit_price: str | None = None
    cost_currency: str | None = None
    cost_namespace: str | None = None
    # Backend de stockage durable déclaré par la chart (storage.backend) :
    # détermine si aws_account_id/aws_region s'appliquent. "aws" par défaut
    # pour la compatibilité ascendante des sites déjà déployés sans cette
    # variable (avant son ajout, voir docs/product/install-default.md gap (c)).
    storage_backend: str = "aws"
    # Voie de matérialisation historique/miroir Snowflake : "copy_merge"
    # (chemin COPY INTO + MERGE existant, ledger canonique) ou "streaming"
    # (option B de docs/decisions/2026-09-23-miroir-snowflake.md — Snowpipe
    # Streaming + MERGE miroir loader-driven). "copy_merge" par défaut pour
    # la compatibilité ascendante des sites déjà déployés sans cette variable.
    destination_mode: str = "copy_merge"
    # Chemin du fichier ``profile.json`` du SDK Snowpipe Streaming (clé privée
    # + compte), monté en volume — jamais un secret en variable d'environnement
    # brute. Requis seulement si destination_mode=streaming.
    streaming_profile_json: str | None = None

    def __post_init__(self) -> None:
        if self.snowflake_warehouse is not None:
            if (
                not isinstance(self.snowflake_warehouse, str)
                or _SNOWFLAKE_IDENTIFIER.fullmatch(self.snowflake_warehouse) is None
            ):
                raise SiteConfigurationError(
                    "QUADRINGENT_SNOWFLAKE_WAREHOUSE doit être un identifiant Snowflake borné"
                )
            object.__setattr__(self, "snowflake_warehouse", self.snowflake_warehouse.upper())
        if self.cost_namespace is not None and not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", self.cost_namespace):
            raise SiteConfigurationError("namespace de coûts invalide")
        price, currency = self.snowflake_credit_price, self.cost_currency
        if (price is None) != (currency is None):
            raise SiteConfigurationError("Le prix du crédit et sa devise doivent être déclarés ensemble")
        if price is not None and (
            not isinstance(price, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,7})(?:[.][0-9]{1,6})?", price) is None
            or not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None
        ):
            raise SiteConfigurationError("Prix du crédit fini et positif ou nul, et devise à trois lettres, requis")
        if (
            not isinstance(self.environment, str)
            or self.environment not in ALLOWED_ENVIRONMENTS
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_ENVIRONMENT doit être déclaré dans "
                + ",".join(sorted(ALLOWED_ENVIRONMENTS))
            )
        if not isinstance(self.site_id, str) or _SITE_ID.fullmatch(self.site_id) is None:
            raise SiteConfigurationError(
                "QUADRINGENT_SITE_ID doit être un identifiant minuscule borné"
            )
        if (
            not isinstance(self.storage_backend, str)
            or self.storage_backend not in ("aws", "gcs")
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_STORAGE_BACKEND doit valoir aws ou gcs"
            )
        if (
            not isinstance(self.destination_mode, str)
            or self.destination_mode not in ("copy_merge", "streaming")
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_DESTINATION_MODE doit valoir copy_merge ou streaming"
            )
        if self.destination_mode == "streaming":
            if (
                not isinstance(self.streaming_profile_json, str)
                or _TLS_CA_FILE.fullmatch(self.streaming_profile_json) is None
            ):
                raise SiteConfigurationError(
                    "QUADRINGENT_STREAMING_PROFILE_JSON doit être un chemin absolu borné "
                    "quand destination_mode=streaming"
                )
        elif self.streaming_profile_json:
            raise SiteConfigurationError(
                "QUADRINGENT_STREAMING_PROFILE_JSON ne s'applique qu'à destination_mode=streaming"
            )
        # Compte et région AWS ne concernent que storage_backend=aws — gap (c)
        # de docs/product/install-default.md, corrigé côté chart puis ici côté
        # runtime : les consommateurs AWS-only (s3_snowpipe_notification,
        # infrastructure_costs) restent responsables de leur propre refus.
        if self.storage_backend == "aws":
            if (
                not isinstance(self.aws_account_id, str)
                or _AWS_ACCOUNT.fullmatch(self.aws_account_id) is None
            ):
                raise SiteConfigurationError(
                    "QUADRINGENT_AWS_ACCOUNT_ID doit être un compte AWS de douze chiffres"
                )
            if (
                not isinstance(self.aws_region, str)
                or _AWS_REGION.fullmatch(self.aws_region) is None
            ):
                raise SiteConfigurationError(
                    "QUADRINGENT_AWS_REGION doit être une région AWS valide"
                )
        else:
            if self.aws_account_id:
                raise SiteConfigurationError(
                    "QUADRINGENT_AWS_ACCOUNT_ID ne s'applique qu'à storage_backend=aws"
                )
            if self.aws_region:
                raise SiteConfigurationError(
                    "QUADRINGENT_AWS_REGION ne s'applique qu'à storage_backend=aws"
                )
        if not isinstance(self.raw_bucket, str) or _S3_BUCKET.fullmatch(self.raw_bucket) is None:
            raise SiteConfigurationError(
                "QUADRINGENT_RAW_BUCKET doit être un nom de bucket S3 valide"
            )
        object.__setattr__(
            self, "raw_prefix_root", _validated_prefix_root(self.raw_prefix_root)
        )
        # Table DynamoDB de checkpoints : ne concerne que storage_backend=aws,
        # même gap (c) que aws_account_id/aws_region ci-dessus. GCS n'a pas de
        # table de checkpoints séparée (voir docs/product/install-default.md).
        if self.storage_backend == "aws":
            if (
                not isinstance(self.checkpoint_table, str)
                or _DESTINATION_ID.fullmatch(self.checkpoint_table) is None
            ):
                raise SiteConfigurationError(
                    "QUADRINGENT_CHECKPOINT_TABLE doit être un identifiant borné"
                )
        elif self.checkpoint_table:
            raise SiteConfigurationError(
                "QUADRINGENT_CHECKPOINT_TABLE ne s'applique qu'à storage_backend=aws"
            )
        if (
            not isinstance(self.source_schema, str)
            or _IDENTIFIER.fullmatch(self.source_schema) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_SOURCE_SCHEMA doit être un identifiant IBM i valide"
            )
        if (
            not isinstance(self.proof_table, str)
            or _IDENTIFIER.fullmatch(self.proof_table) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_PROOF_TABLE doit être un identifiant IBM i valide"
            )
        if (
            not isinstance(self.journal_name, str)
            or _IDENTIFIER.fullmatch(self.journal_name) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_JOURNAL_NAME doit être un identifiant IBM i valide"
            )
        if (
            not isinstance(self.destination_database, str)
            or _SNOWFLAKE_IDENTIFIER.fullmatch(self.destination_database) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_DESTINATION_DATABASE doit être un identifiant Snowflake"
            )
        if (
            not isinstance(self.destination_schema, str)
            or _SNOWFLAKE_IDENTIFIER.fullmatch(self.destination_schema) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_DESTINATION_SCHEMA doit être un identifiant Snowflake"
            )
        if (
            not isinstance(self.destination_id, str)
            or _DESTINATION_ID.fullmatch(self.destination_id) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_DESTINATION_ID doit être un identifiant borné"
            )
        if (
            not isinstance(self.tls_ca_file, str)
            or _TLS_CA_FILE.fullmatch(self.tls_ca_file) is None
            or ".." in self.tls_ca_file
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_IBMI_TLS_CA_FILE doit être un chemin absolu borné"
            )
        if (
            not isinstance(self.snowflake_account, str)
            or _SNOWFLAKE_ACCOUNT.fullmatch(self.snowflake_account) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_SNOWFLAKE_ACCOUNT doit être un identifiant de compte borné"
            )
        if self.snowflake_connection is not None and (
            not isinstance(self.snowflake_connection, str)
            or _CLIENT_ALIAS.fullmatch(self.snowflake_connection) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_SNOWFLAKE_CONNECTION doit être un alias borné"
            )
        if self.aws_profile is not None and (
            not isinstance(self.aws_profile, str)
            or _CLIENT_ALIAS.fullmatch(self.aws_profile) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_AWS_PROFILE doit être un alias borné"
            )
        if (
            not isinstance(self.ibmi_host, str)
            or _IBMI_HOST.fullmatch(self.ibmi_host) is None
            or ".." in self.ibmi_host
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_IBMI_HOST doit être un hôte IBM i borné"
            )
        if (
            not isinstance(self.ibmi_user, str)
            or _IDENTIFIER.fullmatch(self.ibmi_user) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_IBMI_USER doit être un identifiant IBM i valide"
            )
        # La référence au Secret est un couple indissociable : un nom sans
        # clé (ou l'inverse) décrirait une référence incomplète, et le pod ne
        # pourrait pas résoudre ISERIES_PASSWORD — refus explicite.
        if (self.ibmi_password_secret is None) != (self.ibmi_password_key is None):
            raise SiteConfigurationError(
                "QUADRINGENT_IBMI_PASSWORD_SECRET et QUADRINGENT_IBMI_PASSWORD_KEY "
                "doivent être déclarés ensemble ou absents ensemble"
            )
        if self.ibmi_password_secret is not None and (
            not isinstance(self.ibmi_password_secret, str)
            or _K8S_NAME.fullmatch(self.ibmi_password_secret) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_IBMI_PASSWORD_SECRET doit être un nom Kubernetes borné"
            )
        if self.ibmi_password_key is not None and (
            not isinstance(self.ibmi_password_key, str)
            or _CLIENT_ALIAS.fullmatch(self.ibmi_password_key) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_IBMI_PASSWORD_KEY doit être un nom de clé borné"
            )
        object.__setattr__(
            self,
            "fleet_tables",
            _validated_tables("QUADRINGENT_FLEET_TABLES", self.fleet_tables),
        )
        if not self.fleet_tables:
            raise SiteConfigurationError(
                "QUADRINGENT_FLEET_TABLES doit déclarer au moins une table"
            )
        if self.proof_table not in self.fleet_tables:
            raise SiteConfigurationError(
                "QUADRINGENT_PROOF_TABLE doit appartenir au manifeste déclaré"
            )
        object.__setattr__(
            self,
            "keyed_tables",
            _validated_tables(
                "QUADRINGENT_KEYED_TABLES", self.keyed_tables, allow_empty=True
            ),
        )
        unknown_keyed = [name for name in self.keyed_tables if name not in self.fleet_tables]
        if unknown_keyed:
            raise SiteConfigurationError(
                "QUADRINGENT_KEYED_TABLES doit rester dans le manifeste déclaré"
            )
        object.__setattr__(
            self,
            "provisioned_stages",
            _validated_tables(
                "QUADRINGENT_PROVISIONED_STAGES",
                self.provisioned_stages,
                allow_empty=True,
            ),
        )
        unknown_stages = [
            name for name in self.provisioned_stages if name not in self.fleet_tables
        ]
        if unknown_stages:
            raise SiteConfigurationError(
                "QUADRINGENT_PROVISIONED_STAGES doit rester dans le manifeste déclaré"
            )
        # Sans liste déclarée, seule la voie de preuve est réservable — un
        # repli dérivé de la déclaration, jamais un défaut d'installation.
        reservable = (
            (self.proof_table,) if self.reservable_tables is None else self.reservable_tables
        )
        object.__setattr__(
            self,
            "reservable_tables",
            _validated_tables(
                "QUADRINGENT_RESERVABLE_TABLES", reservable, allow_empty=False
            ),
        )
        unknown_reservable = [
            name for name in reservable if name not in self.fleet_tables
        ]
        if unknown_reservable:
            raise SiteConfigurationError(
                "QUADRINGENT_RESERVABLE_TABLES doit rester dans le manifeste déclaré"
            )
        object.__setattr__(
            self,
            "proof_key_columns",
            _validated_tables(
                "QUADRINGENT_PROOF_KEY_COLUMNS",
                self.proof_key_columns,
                allow_empty=True,
            ),
        )
        if len(self.proof_key_columns) > 16:
            raise SiteConfigurationError(
                "QUADRINGENT_PROOF_KEY_COLUMNS reste borné à seize colonnes"
            )
        object.__setattr__(
            self,
            "forbidden_fragments",
            _validated_fragments(self.forbidden_fragments),
        )
        # Nom de document de preuve autonome : un objet S3 nu (aucun « / »)
        # dont le nom fait partie du contrat d'infrastructure du site — les
        # politiques IAM du site autorisent des clés exactes. Le site le
        # déclare donc comme le reste de son identité.
        if (
            not isinstance(self.autonomous_proof_name, str)
            or _PROOF_DOC_NAME.fullmatch(self.autonomous_proof_name) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_AUTONOMOUS_PROOF_NAME doit être un nom de document .json borné, sans chemin"
            )
        # Préfixe des objets Snowflake (tables, warehouse, rôle) : contrat
        # d'infra du site — les objets provisionnés peuvent porter un autre
        # préfixe que le nom produit (renommage, convention existante).
        if (
            not isinstance(self.destination_prefix, str)
            or _SNOWFLAKE_IDENTIFIER.fullmatch(self.destination_prefix) is None
        ):
            raise SiteConfigurationError(
                "QUADRINGENT_DESTINATION_PREFIX doit être un identifiant Snowflake borné"
            )

    # -- Identités dérivées -------------------------------------------------

    @property
    def fleet_environment(self) -> str:
        """Environnement tel que publié dans les documents de flotte."""

        return self.environment.upper()

    @property
    def fleet_id(self) -> str:
        return f"{self.site_id}-{self.environment}"

    @property
    def pipeline_id(self) -> str:
        """Identifiant de pipeline de la voie de preuve (capture continue)."""

        return f"{self.environment}-{self.proof_table.lower()}"

    @property
    def sidecar_pipeline_id(self) -> str:
        """Identifiant de pipeline du document sidecar de flotte."""

        return f"{self.environment}-{self.site_id}"

    @property
    def destination_namespace(self) -> str:
        return f"{self.destination_database}.{self.destination_schema}"

    @property
    def snowflake_scope(self) -> "SnowflakeScope":
        """Périmètre Snowflake déclaré, tel que vérifié par les plans."""

        return SnowflakeScope(
            database=self.destination_database,
            schema=self.destination_schema,
            forbidden_fragments=self.forbidden_fragments,
        )

    # -- Objets S3 dérivés --------------------------------------------------

    @property
    def stream_prefix(self) -> str:
        """Préfixe du flux de preuve : ``<racine>/<table de preuve>``."""

        return f"{self.raw_prefix_root}/{self.proof_table.lower()}"

    @property
    def journal_prefix(self) -> str:
        return self.journal_prefix_for(self.proof_table)

    def journal_prefix_for(self, table: str) -> str:
        """Préfixe du flux journal d'une table déclarée (sans slash final)."""

        return f"{self.raw_prefix_root}/{_require_fleet_table(self, table).lower()}/journal"

    @property
    def history_progress_prefix(self) -> str:
        """Préfixe des documents de progression historique de la flotte."""

        return f"{self.raw_prefix_root}/fleet/history-progress/"

    @property
    def autonomous_proof_key(self) -> str:
        return f"{self.stream_prefix}/proofs/{self.autonomous_proof_name}"

    @property
    def autonomous_proof_s3_uri(self) -> str:
        return f"s3://{self.raw_bucket}/{self.autonomous_proof_key}"

    # -- Objets Snowflake dérivés -------------------------------------------

    def snowflake_stage_for(self, table: str) -> str:
        """Stage externe d'une table : ``<schéma>_<TABLE>_EXTERNAL_STAGE``."""

        return f"{self.destination_schema}_{_require_fleet_table(self, table)}_EXTERNAL_STAGE"

    def snowflake_raw_table_for(self, table: str) -> str:
        return f"{self.destination_prefix}_{_require_fleet_table(self, table)}_RAW"

    def snowflake_canonical_for(self, table: str) -> str:
        return f"{self.destination_prefix}_{_require_fleet_table(self, table)}_CANONICAL"

    def snowflake_pipe_for(self, table: str) -> str:
        return f"{self.destination_prefix}_{_require_fleet_table(self, table)}_PIPE"

    @property
    def proof_stage(self) -> str:
        return self.snowflake_stage_for(self.proof_table)

    @property
    def proof_raw_table(self) -> str:
        return self.snowflake_raw_table_for(self.proof_table)

    @property
    def proof_canonical_table(self) -> str:
        return self.snowflake_canonical_for(self.proof_table)

    @property
    def proof_pipe(self) -> str:
        return self.snowflake_pipe_for(self.proof_table)

    @property
    def warehouse_name(self) -> str:
        if self.snowflake_warehouse is not None:
            return self.snowflake_warehouse
        return f"{self.destination_prefix}_{self.fleet_environment}_WH"

    @property
    def manages_warehouse(self) -> bool:
        return self.snowflake_warehouse is None

    @property
    def metering_name(self) -> str:
        return f"{self.warehouse_name}_METERING"

    @property
    def verifier_role_name(self) -> str:
        """Rôle Snowflake du vérificateur : ``<PRÉFIXE>_<ENV>_VERIFIER_ROLE``."""

        return f"{self.destination_prefix}_{self.fleet_environment}_VERIFIER_ROLE"

    @property
    def integration_name(self) -> str:
        """Intégration de stockage : ``<ENV>_<SCHÉMA>_S3_INT``."""

        return f"{self.fleet_environment}_{self.destination_schema}_S3_INT"

    def qualified_name(self, object_name: str) -> str:
        """Nom pleinement qualifié ``DB.SCHEMA.OBJET`` d'un objet déclaré."""

        if _SNOWFLAKE_IDENTIFIER.fullmatch(object_name) is None:
            raise SiteConfigurationError("identifiant Snowflake invalide")
        return f"{self.destination_database}.{self.destination_schema}.{object_name}"

    @property
    def proof_pipe_fqn(self) -> str:
        return self.qualified_name(self.proof_pipe)

    @property
    def proof_canonical_fqn(self) -> str:
        return self.qualified_name(self.proof_canonical_table)

    @property
    def metering_fqn(self) -> str:
        return self.qualified_name(self.metering_name)

    # -- Notifications et jetons -------------------------------------------

    def snowpipe_notification_id(self, table: str | None = None) -> str:
        """Identifiant de notification S3 : ``quadringent-<site>-<table>-snowpipe``.

        La forme de l'identifiant est validée, pas l'appartenance au manifeste :
        retirer la notification d'une table sortie du manifeste doit rester
        possible.
        """

        name = self.proof_table if table is None else table
        if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None:
            raise SiteConfigurationError("identifiant de table invalide")
        return f"quadringent-{self.site_id}-{name.lower()}-snowpipe"

    def confirmation_token(self, action: str) -> str:
        """Jeton de confirmation opérateur : ``<SCHÉMA>_<ACTION>_<ENV>``."""

        if _IDENTIFIER.fullmatch(action) is None:
            raise SiteConfigurationError("action de confirmation invalide")
        return f"{self.destination_schema}_{action}_{self.fleet_environment}"

    @property
    def publish_confirmation_token(self) -> str:
        return f"PUBLISH_{self.destination_schema}_AUTONOMOUS_PROOF_{self.fleet_environment}"

    @property
    def fault_confirmation_token(self) -> str:
        return f"FAULT_INJECTION_{self.destination_schema}_{self.fleet_environment}"

    @property
    def observability_refresh_token(self) -> str:
        return f"REFRESH_{self.destination_schema}_OBSERVABILITY_{self.fleet_environment}"

    # -- Attendus de preuve --------------------------------------------------

    def event_scope(self, table: str | None = None) -> tuple[str, str, str, str]:
        """Quadruplet (système, journal, bibliothèque, table) attendu."""

        name = self.proof_table if table is None else _require_fleet_table(self, table)
        return ("ibmi", self.journal_name, self.source_schema, name)


@dataclass(frozen=True)
class SnowflakeScope:
    """Destination Snowflake déclarée : base, schéma et fragments interdits.

    Les plans de chargement portent ce périmètre au lieu d'acceptor des
    chaînes libres : un plan ne peut exprimer que la destination déclarée.
    """

    database: str
    schema: str
    forbidden_fragments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.database, str)
            or _SNOWFLAKE_IDENTIFIER.fullmatch(self.database) is None
        ):
            raise SiteConfigurationError("destination database invalide")
        if (
            not isinstance(self.schema, str)
            or _SNOWFLAKE_IDENTIFIER.fullmatch(self.schema) is None
        ):
            raise SiteConfigurationError("destination schema invalide")
        object.__setattr__(
            self, "forbidden_fragments", _validated_fragments(self.forbidden_fragments)
        )

    @property
    def namespace(self) -> str:
        return f"{self.database}.{self.schema}"


def _validated_prefix_root(value: str) -> str:
    if not isinstance(value, str):
        raise SiteConfigurationError(
            "QUADRINGENT_RAW_PREFIX_ROOT doit être un préfixe relatif"
        )
    cleaned = value.strip().strip("/")
    if (
        not cleaned
        or value.startswith("/")
        or value != cleaned
        or len(cleaned.split("/")) > _MAX_PREFIX_SEGMENTS
        or any(
            _PREFIX_SEGMENT.fullmatch(segment) is None or segment in {".", ".."}
            for segment in cleaned.split("/")
        )
    ):
        raise SiteConfigurationError(
            "QUADRINGENT_RAW_PREFIX_ROOT doit rester un préfixe relatif borné"
        )
    return cleaned


def _validated_tables(
    name: str, values: Sequence[str], *, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or isinstance(values, str):
        raise SiteConfigurationError(f"{name} doit être une liste d'identifiants")
    if len(values) > _MAX_FLEET_TABLES:
        raise SiteConfigurationError(f"{name} dépasse la borne du manifeste")
    if not values and not allow_empty:
        raise SiteConfigurationError(f"{name} ne doit pas être vide")
    tables = tuple(values)
    if any(
        not isinstance(table, str) or _IDENTIFIER.fullmatch(table) is None
        for table in tables
    ):
        raise SiteConfigurationError(
            f"{name} contient un identifiant de table invalide"
        )
    if len(set(tables)) != len(tables):
        raise SiteConfigurationError(f"{name} contient un doublon")
    return tables


def _validated_fragments(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or isinstance(values, str):
        raise SiteConfigurationError(
            "QUADRINGENT_FORBIDDEN_FRAGMENTS doit être une liste de fragments"
        )
    if len(values) > _MAX_LIST_ENTRIES:
        raise SiteConfigurationError("QUADRINGENT_FORBIDDEN_FRAGMENTS est borné")
    fragments = tuple(values)
    if any(
        not isinstance(fragment, str) or _FRAGMENT.fullmatch(fragment) is None
        for fragment in fragments
    ):
        raise SiteConfigurationError(
            "QUADRINGENT_FORBIDDEN_FRAGMENTS contient un fragment invalide"
        )
    if len(set(fragments)) != len(fragments):
        raise SiteConfigurationError("QUADRINGENT_FORBIDDEN_FRAGMENTS contient un doublon")
    return fragments


def _require_fleet_table(site: "SiteConfig", table: str) -> str:
    if (
        not isinstance(table, str)
        or _IDENTIFIER.fullmatch(table) is None
        or table not in site.fleet_tables
    ):
        raise SiteConfigurationError("table hors du manifeste déclaré")
    return table


def _csv(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    raw = environ.get(name, "")
    if raw == "":
        return ()
    return tuple(part.strip() for part in raw.split(","))


def from_environment(environ: Mapping[str, str] | None = None) -> SiteConfig:
    """Construit la configuration depuis les variables ``QUADRINGENT_*``.

    Toute variable obligatoire absente ou vide produit un
    :class:`SiteConfigurationError` qui liste les clés manquantes — jamais un
    défaut d'installation.
    """

    values = os.environ if environ is None else environ
    # Absente (sites déployés avant l'ajout de cette variable) : "aws", pour
    # rester compatible avec le comportement historique.
    storage_backend = (values.get("QUADRINGENT_STORAGE_BACKEND", "") or "aws").strip().lower()
    required = (
        "QUADRINGENT_ENVIRONMENT",
        "QUADRINGENT_SITE_ID",
        "QUADRINGENT_RAW_BUCKET",
        "QUADRINGENT_RAW_PREFIX_ROOT",
        "QUADRINGENT_SOURCE_SCHEMA",
        "QUADRINGENT_PROOF_TABLE",
        "QUADRINGENT_JOURNAL_NAME",
        "QUADRINGENT_DESTINATION_DATABASE",
        "QUADRINGENT_DESTINATION_SCHEMA",
        "QUADRINGENT_DESTINATION_ID",
        "QUADRINGENT_IBMI_TLS_CA_FILE",
        "QUADRINGENT_SNOWFLAKE_ACCOUNT",
        "QUADRINGENT_IBMI_HOST",
        "QUADRINGENT_IBMI_USER",
        "QUADRINGENT_FLEET_TABLES",
    )
    if storage_backend == "aws":
        # Compte, région AWS et table de checkpoints DynamoDB ne sont
        # obligatoires que pour ce backend — voir SiteConfig.__post_init__ et
        # docs/product/install-default.md gap (c).
        required = required + ("QUADRINGENT_AWS_ACCOUNT_ID", "QUADRINGENT_AWS_REGION", "QUADRINGENT_CHECKPOINT_TABLE")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise SiteConfigurationError(
            "configuration de site incomplète : " + ",".join(missing)
        )
    return SiteConfig(
        snowflake_credit_price=values.get("QUADRINGENT_SNOWFLAKE_CREDIT_PRICE", "").strip() or None,
        cost_currency=values.get("QUADRINGENT_COST_CURRENCY", "").strip() or None,
        cost_namespace=values.get("QUADRINGENT_COST_NAMESPACE", "").strip() or None,
        storage_backend=storage_backend,
        environment=values["QUADRINGENT_ENVIRONMENT"].strip().lower(),
        site_id=values["QUADRINGENT_SITE_ID"].strip(),
        aws_account_id=values.get("QUADRINGENT_AWS_ACCOUNT_ID", "").strip(),
        aws_region=values.get("QUADRINGENT_AWS_REGION", "").strip(),
        raw_bucket=values["QUADRINGENT_RAW_BUCKET"].strip(),
        raw_prefix_root=values["QUADRINGENT_RAW_PREFIX_ROOT"].strip(),
        checkpoint_table=values.get("QUADRINGENT_CHECKPOINT_TABLE", "").strip(),
        source_schema=values["QUADRINGENT_SOURCE_SCHEMA"].strip(),
        proof_table=values["QUADRINGENT_PROOF_TABLE"].strip(),
        journal_name=values["QUADRINGENT_JOURNAL_NAME"].strip(),
        destination_database=values["QUADRINGENT_DESTINATION_DATABASE"].strip(),
        destination_schema=values["QUADRINGENT_DESTINATION_SCHEMA"].strip(),
        destination_id=values["QUADRINGENT_DESTINATION_ID"].strip(),
        tls_ca_file=values["QUADRINGENT_IBMI_TLS_CA_FILE"].strip(),
        snowflake_account=values["QUADRINGENT_SNOWFLAKE_ACCOUNT"].strip(),
        ibmi_host=values["QUADRINGENT_IBMI_HOST"].strip(),
        ibmi_user=values["QUADRINGENT_IBMI_USER"].strip(),
        fleet_tables=_csv(values, "QUADRINGENT_FLEET_TABLES"),
        snowflake_warehouse=values.get("QUADRINGENT_SNOWFLAKE_WAREHOUSE") or None,
        snowflake_connection=(
            values.get("QUADRINGENT_SNOWFLAKE_CONNECTION", "").strip() or None
        ),
        aws_profile=(values.get("QUADRINGENT_AWS_PROFILE", "").strip() or None),
        ibmi_password_secret=(
            values.get("QUADRINGENT_IBMI_PASSWORD_SECRET", "").strip() or None
        ),
        ibmi_password_key=(
            values.get("QUADRINGENT_IBMI_PASSWORD_KEY", "").strip() or None
        ),
        keyed_tables=_csv(values, "QUADRINGENT_KEYED_TABLES"),
        provisioned_stages=_csv(values, "QUADRINGENT_PROVISIONED_STAGES"),
        reservable_tables=_csv(values, "QUADRINGENT_RESERVABLE_TABLES") or None,
        proof_key_columns=_csv(values, "QUADRINGENT_PROOF_KEY_COLUMNS"),
        forbidden_fragments=_csv(values, "QUADRINGENT_FORBIDDEN_FRAGMENTS"),
        autonomous_proof_name=(
            values.get("QUADRINGENT_AUTONOMOUS_PROOF_NAME", "").strip()
            or "quadringent-autonomous-latest.json"
        ),
        destination_prefix=(
            values.get("QUADRINGENT_DESTINATION_PREFIX", "").strip().upper()
            or "QUADRINGENT"
        ),
        destination_mode=(
            values.get("QUADRINGENT_DESTINATION_MODE", "").strip().lower() or "copy_merge"
        ),
        streaming_profile_json=(
            values.get("QUADRINGENT_STREAMING_PROFILE_JSON", "").strip() or None
        ),
    )


_LOCK = threading.Lock()
_CURRENT: SiteConfig | None = None


class SiteRegistry:
    """Registre de plusieurs :class:`SiteConfig`, indexées par ``site_id``.

    Le registre est un magasin en mémoire pur — aucune I/O, aucune notion de
    processus par site. Il coexiste avec le singleton historique (``install``/
    ``current``) : tant qu'aucun contexte (:func:`use_site`) n'est actif, la
    résolution legacy reste seule maîtresse, ce qui garantit que les 14
    appelants historiques de ``current()`` ne voient jamais le registre.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sites: dict[str, SiteConfig] = {}

    def register(self, config: SiteConfig, *, replace: bool = False) -> SiteConfig:
        """Ajoute un site au registre ; refuse un doublon sans ``replace``."""

        if not isinstance(config, SiteConfig):
            raise SiteConfigurationError("une SiteConfig validée est requise")
        with self._lock:
            if not replace and config.site_id in self._sites:
                raise SiteConfigurationError(
                    f"site déjà enregistré : {config.site_id}"
                )
            self._sites[config.site_id] = config
        return config

    def unregister(self, site_id: str) -> None:
        with self._lock:
            self._sites.pop(site_id, None)

    def get(self, site_id: str) -> SiteConfig:
        """Site enregistré, ou refus explicite — jamais de site implicite."""

        with self._lock:
            site = self._sites.get(site_id)
        if site is None:
            raise SiteConfigurationError(f"site inconnu du registre : {site_id}")
        return site

    def __contains__(self, site_id: object) -> bool:
        with self._lock:
            return site_id in self._sites

    def site_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._sites))


# Registre process-global — toujours instancié, jamais None : seule sa
# consultation est conditionnelle (au contexte actif), pas son existence.
_REGISTRY = SiteRegistry()

# Résolution par contexte : un ``site_id`` actif prime sur le singleton et
# sur l'environnement. Par défaut (aucun contexte entré), la valeur est
# ``None`` et ``current()`` retombe intégralement sur le comportement
# historique — un thread ou une tâche asynchrone qui n'a jamais appelé
# ``use_site`` ne voit jamais le registre.
_SITE_CONTEXT: ContextVar[str | None] = ContextVar("quadringent_site_context", default=None)


def registry() -> SiteRegistry:
    """Registre process-global des sites — pour l'enregistrement et les tests."""

    return _REGISTRY


@contextmanager
def use_site(site_id: str) -> Iterator[SiteConfig]:
    """Rend ``site_id`` courant pour la durée du bloc ``with`` (et lui seul).

    Le site doit déjà être enregistré dans :func:`registry` — un identifiant
    inconnu échoue immédiatement, avant même d'entrer le bloc, plutôt que de
    différer le refus au premier ``current()`` interne au contexte. Le
    contexte est porté par une ``ContextVar`` : il ne fuit pas entre threads
    ni entre tâches asyncio qui ne l'ont pas explicitement hérité.
    """

    if not isinstance(site_id, str) or not site_id:
        raise SiteConfigurationError("identifiant de site invalide pour le contexte")
    site = _REGISTRY.get(site_id)
    token: Token[str | None] = _SITE_CONTEXT.set(site_id)
    try:
        yield site
    finally:
        _SITE_CONTEXT.reset(token)


def install(config: SiteConfig) -> SiteConfig:
    """Installe la configuration du processus ; l'argument est requis."""

    if not isinstance(config, SiteConfig):
        raise SiteConfigurationError("une SiteConfig validée est requise")
    global _CURRENT
    with _LOCK:
        _CURRENT = config
    return config


def uninstall() -> None:
    global _CURRENT
    with _LOCK:
        _CURRENT = None


def current() -> SiteConfig:
    """Site actif : contexte (:func:`use_site`) en premier, puis legacy.

    Sans contexte actif, le comportement est inchangé depuis l'introduction
    du multi-site : configuration installée via :func:`install`, sinon
    variables d'environnement. C'est cette retombée qui garantit que les
    appelants historiques et la suite de tests existante n'ont rien à
    changer — le registre ne s'active qu'à l'intérieur d'un ``use_site``.
    """

    site_id = _SITE_CONTEXT.get()
    if site_id is not None:
        return _REGISTRY.get(site_id)
    with _LOCK:
        installed = _CURRENT
    if installed is not None:
        return installed
    return from_environment()
