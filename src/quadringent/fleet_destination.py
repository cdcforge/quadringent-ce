"""Plan fail-closed de la destination Snowflake des flux bruts d'une flotte.

Convention, identique à celle du dépôt :

    s3://<bucket>/<racine produit>/<table>/journal/    un flux, un curseur
    s3://<bucket>/<racine produit>/fleet/runs/<run>/   copie de la fenêtre lue

Chaque table reçoit trois objets Snowflake dans le schéma de destination
déclaré, nommés par le préfixe de destination du site
(``destination_prefix``, ``QUADRINGENT`` par défaut) :

    <SCHÉMA>_<TABLE>_EXTERNAL_STAGE   zone de dépôt sur son seul préfixe
    <PRÉFIXE>_<TABLE>_RAW             journal brut append-only (charge utile + origine)
    <PRÉFIXE>_<TABLE>_PIPE            tuyau AUTO_INGEST sur ce préfixe

L'élargissement de l'intégration de stockage porte sur le préfixe produit
entier, jamais sur une table : ajouter une table ne demande plus aucune
modification Snowflake. La barrière d'accès réelle reste la politique IAM du
rôle d'approvisionnement, exprimée par forme de chemin
(`<racine>/*/journal/*`, `<racine>/fleet/*`).

Le plan n'ouvre aucune connexion et n'écrit rien : il produit des instructions
ordonnées et idempotentes, dont l'effet est constaté par relecture après
exécution. Aucune instruction ne vise un autre schéma que celui déclaré par la
configuration du site.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

from .fleet_capture import parse_fleet_tables
from .site_config import SiteConfig


_JSONL_PATTERN = ".*[.]jsonl$"
_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
# Invariant produit, jamais une valeur de site : la destination déclarée ne
# doit pas porter un marqueur de production.
_UNSAFE_NAMESPACE_MARKERS = ("PROD", "PRD_")
_STAGE_SUFFIX = "_EXTERNAL_STAGE"


class FleetDestinationError(ValueError):
    """Un réglage de destination serait ambigu, hors périmètre ou hors produit."""


def _checked_identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise FleetDestinationError(f"invalid Snowflake identifier: {name}")
    return value


@dataclass(frozen=True)
class FleetTableObjects:
    """Les trois objets d'une table, plus son préfixe brut."""

    table: str
    prefix: str
    stage: str
    raw_table: str
    canonical_view: str
    pipe: str


class FleetDestinationPlan:
    """Objets Snowflake d'une flotte, dérivés du site déclaré et des tables."""

    def __init__(
        self,
        *,
        tables: tuple[str, ...],
        site: SiteConfig,
        pre_existing_lanes: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(site, SiteConfig):
            raise FleetDestinationError("the declared site configuration is required")
        self.site = site
        self.product_prefix = site.raw_prefix_root
        self.bucket = site.raw_bucket
        self.database = _checked_identifier("database", site.destination_database)
        self.schema = _checked_identifier("schema", site.destination_schema)
        self.integration = _checked_identifier("integration", site.integration_name)
        self.warehouse = _checked_identifier("warehouse", site.warehouse_name)
        namespace = f"{self.database}.{self.schema}.{self.integration}".upper()
        for forbidden in (
            *(fragment.upper() for fragment in site.forbidden_fragments),
            *_UNSAFE_NAMESPACE_MARKERS,
        ):
            if forbidden in namespace:
                raise FleetDestinationError("fleet destination must stay in its own space")
        # parse_fleet_tables refuse une liste vide, non unique ou hors forme.
        self.tables = parse_fleet_tables(tables)
        if len(self.tables) < 2:
            raise FleetDestinationError("fleet destination requires at least two tables")
        lanes = parse_fleet_tables(pre_existing_lanes) if pre_existing_lanes else ()
        unknown = [name for name in lanes if name not in self.tables]
        if unknown:
            raise FleetDestinationError("pre-existing lane is not part of the fleet")
        self.pre_existing_lanes = lanes

    # -- Convention de nommage, dérivée de la seule table ------------------

    def table_objects(self, table: str) -> FleetTableObjects:
        name = parse_fleet_tables([table])[0]
        return FleetTableObjects(
            table=name,
            prefix=self.table_prefix(name),
            stage=self.site.snowflake_stage_for(name),
            raw_table=self.site.snowflake_raw_table_for(name),
            canonical_view=self.site.snowflake_canonical_for(name),
            pipe=self.site.snowflake_pipe_for(name),
        )

    def table_prefix(self, table: str) -> str:
        """Préfixe S3 de la table, aligné sur le runtime de capture."""

        name = parse_fleet_tables([table])[0]
        return f"{self.product_prefix}/{name.lower()}/journal"

    def stage_url(self, table: str) -> str:
        return f"s3://{self.bucket}/{self.table_prefix(table)}/"

    def objects(self) -> tuple[FleetTableObjects, ...]:
        return tuple(self.table_objects(table) for table in self.tables)

    # -- Instructions -------------------------------------------------------

    def integration_statement(self) -> str:
        """Élargit l'intégration au préfixe produit, sans wildcard.

        Snowflake n'accepte aucun `*` dans `STORAGE_ALLOWED_LOCATIONS` : le
        préfixe produit entier est donc déclaré, et l'accès réel reste borné
        par la politique IAM du rôle d'approvisionnement.
        """

        return (
            f"ALTER STORAGE INTEGRATION {self.integration} SET\n"
            f"  STORAGE_ALLOWED_LOCATIONS = "
            f"('s3://{self.bucket}/{self.product_prefix}/')"
        )

    def create_stage_statement(self, table: str) -> str:
        objects = self.table_objects(table)
        return (
            f"CREATE STAGE IF NOT EXISTS {self._q(objects.stage)}\n"
            f"  URL = '{self.stage_url(table)}'\n"
            f"  STORAGE_INTEGRATION = {self.integration}\n"
            f"  FILE_FORMAT = (TYPE = JSON)"
        )

    def create_raw_table_statement(self, table: str) -> str:
        objects = self.table_objects(table)
        return f"""CREATE TABLE IF NOT EXISTS {self._q(objects.raw_table)} (
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL,
    SOURCE_ROW_NUMBER NUMBER(38, 0) NOT NULL,
    INGESTED_AT TIMESTAMP_LTZ NOT NULL
)"""

    def create_canonical_view_statement(self, table: str) -> str:
        """Vue canonique d'une table : une ligne technique par événement.

        Sans elle, le rejeu métier n'a rien à lire : le chargement brut est
        nécessaire mais pas suffisant. Le plan de la flotte la créait pour la
        seule voie SALE ; mesuré le 17/09, les douze autres tables n'en avaient
        aucune, donc aucun rejeu n'était possible pour elles.

        La déduplication par identité d'événement rend la vue idempotente : un
        lot rechargé ne produit pas deux lignes techniques.
        """

        objects = self.table_objects(table)
        raw = self._q(objects.raw_table)
        view = self._q(objects.canonical_view)
        return f"""CREATE OR REPLACE VIEW {view} (
    EVENT_ID, JOURNAL_RECEIVER, JOURNAL_SEQUENCE, OPERATION,
    PAYLOAD, SOURCE_FILE, SOURCE_ROW_NUMBER, INGESTED_AT
) AS
SELECT
    PAYLOAD:event_id::VARCHAR AS EVENT_ID,
    PAYLOAD:journal_receiver::VARCHAR AS JOURNAL_RECEIVER,
    PAYLOAD:journal_sequence::NUMBER(38, 0) AS JOURNAL_SEQUENCE,
    PAYLOAD:operation::VARCHAR AS OPERATION,
    PAYLOAD AS PAYLOAD,
    SOURCE_FILE,
    SOURCE_ROW_NUMBER,
    INGESTED_AT
FROM {raw}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY EVENT_ID
    ORDER BY JOURNAL_SEQUENCE DESC, INGESTED_AT DESC, SOURCE_FILE DESC
) = 1"""

    def create_pipe_statement(self, table: str) -> str:
        objects = self.table_objects(table)
        raw = self._q(objects.raw_table)
        stage = f"@{self._q(objects.stage)}"
        # La clause `PIPE_EXECUTION_PAUSED` du CREATE est ignoree par
        # Snowflake : mesure du 17/09, un tuyau cree avec cette clause est
        # RUNNING immediatement. La mise en pause est donc une instruction
        # separee, emise par `pause_new_pipes_statements`.
        return f"""CREATE OR ALTER PIPE {self._q(objects.pipe)}
AUTO_INGEST = TRUE
AS
COPY INTO {raw} (PAYLOAD, SOURCE_FILE, SOURCE_ROW_NUMBER, INGESTED_AT)
FROM (
    SELECT
        $1,
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER,
        METADATA$START_SCAN_TIME
    FROM {stage}
)
PATTERN = '{_JSONL_PATTERN}'
FILE_FORMAT = (TYPE = JSON STRIP_OUTER_ARRAY = FALSE)"""

    def new_lanes(self) -> tuple[str, ...]:
        """Tables dont les trois objets sont créés par ce plan.

        Une voie préexistante (ici la voie SALE historique, dont la zone de
        dépôt plus large sert aussi les fichiers de run) n'est jamais
        redéfinie : un `CREATE OR ALTER PIPE` ne peut pas modifier le COPY
        d'un tuyau existant, et le recréer perdrait son historique de
        chargement. Ces voies sont vérifiées, jamais réécrites.
        """

        return tuple(name for name in self.tables if name not in self.pre_existing_lanes)

    def pause_new_pipes_statements(
        self, only: Sequence[str] | None = None
    ) -> tuple[str, ...]:
        """Suspend les tuyaux réellement créés par cette installation.

        Le `CREATE PIPE` ne peut pas naitre en pause : Snowflake ignore la
        clause. Chaque tuyau est donc créé puis suspendu, ce qui laisse
        l'installation sans consommation ni chargement tant que la mise en
        route n'est pas demandée.

        `only` restreint la suspension aux tuyaux qui venaient d'être créés.
        Sans cette restriction, réinstaller la flotte suspendrait des flux
        déjà en service : mesuré le 17/09, une réinstallation a arrêté CNTR et
        ORDER alors qu'ils chargeaient. Une installation ne doit jamais
        interrompre un flux qui tournait.
        """

        if only is None:
            allowed = self.new_lanes()
        else:
            # Une réinstallation ne crée aucun tuyau : la liste peut être vide,
            # et « rien à suspendre » est un résultat normal, pas une erreur.
            requested = tuple(only)
            allowed = tuple(parse_fleet_tables(requested)) if requested else ()
        unknown = [name for name in allowed if name not in self.tables]
        if unknown:
            raise FleetDestinationError("pause target is not part of the fleet")
        return tuple(
            f"ALTER PIPE {self._q(objects.pipe)} SET PIPE_EXECUTION_PAUSED = TRUE"
            for objects in self.objects()
            if objects.table in allowed
        )

    def resume_statements(self) -> tuple[str, ...]:
        return tuple(
            f"ALTER PIPE {self._q(objects.pipe)} SET PIPE_EXECUTION_PAUSED = FALSE"
            for objects in self.objects()
            if objects.table in self.new_lanes()
        )

    def pause_statements(self) -> tuple[str, ...]:
        """Suspend les tuyaux créés par ce plan, sans supprimer aucun objet."""

        statements = [
            f"ALTER PIPE IF EXISTS {self._q(objects.pipe)} SET PIPE_EXECUTION_PAUSED = TRUE"
            for objects in self.objects()
            if objects.table in self.new_lanes()
        ]
        suspend = self.suspend_warehouse_statement()
        if suspend is not None:
            statements.append(suspend)
        return tuple(statements)

    def suspend_warehouse_statement(self) -> str | None:
        if not self.site.manages_warehouse:
            return None
        return f"ALTER WAREHOUSE IF EXISTS {self.warehouse} SUSPEND"

    def statements(self) -> tuple[str, ...]:
        """Instructions d'installation, sans mise en route.

        Une table reçoit sa zone de dépôt avant sa table brute et son tuyau :
        un tuyau ne précède jamais l'objet qu'il alimente. Chaque tuyau naît
        en pause : l'installation ne consomme rien et ne peut pas charger un
        fichier tant que l'autorisation de lecture du préfixe n'est pas
        constatée. La mise en route est une instruction distincte.

        La suspension du warehouse n'est pas ici : elle est déjà acquise par
        `AUTO_SUSPEND` et un second `SUSPEND` sur un warehouse déjà arrêté est
        refusé par Snowflake. Elle reste disponible comme instruction
        séparée, exécutée sans échec bloquant.
        """

        ordered: list[str] = [self.integration_statement()]
        for table in self.new_lanes():
            ordered.append(self.create_stage_statement(table))
            ordered.append(self.create_raw_table_statement(table))
            ordered.append(self.create_canonical_view_statement(table))
            ordered.append(self.create_pipe_statement(table))
        ordered.extend(self.grant_verifier_statements())
        return tuple(ordered)

    def grant_verifier_statements(self) -> tuple[str, ...]:
        """Accorde au rôle vérificateur la mesure de chaque voie déclarée.

        Sans ces instructions, Snowflake masque les objets au rôle de
        mesure (« does not exist ») : la sonde de livraison conclurait à
        `destination_configuration_missing` sur une destination pourtant
        saine. Toutes les voies sont couvertes — héritées comprises — car
        chacune doit être mesurable. La réémission est sans effet : un
        grant déjà présent est accepté. Les grants restent en fin de
        séquence : les objets existent avant le premier grant, et un rôle
        vérificateur absent n'interrompt pas la création.
        """

        role = _checked_identifier("verifier_role", self.site.verifier_role_name)
        statements: list[str] = []
        for table in self.tables:
            objects = self.table_objects(table)
            statements.append(
                f"GRANT MONITOR ON PIPE {self._q(objects.pipe)} TO ROLE {role}"
            )
            statements.append(
                f"GRANT SELECT ON TABLE {self._q(objects.raw_table)} TO ROLE {role}"
            )
            statements.append(
                f"GRANT SELECT ON VIEW {self._q(objects.canonical_view)} TO ROLE {role}"
            )
        return tuple(statements)

    # -- Aides --------------------------------------------------------------

    def _q(self, name: str) -> str:
        return f'"{self.database}"."{self.schema}"."{name}"'

    def covers(self, stage_url: str, table: str) -> bool:
        """Vrai si une zone de dépôt existante couvre bien le flux de la table.

        `sale/` couvre `sale/journal/` : une zone héritée plus large reste
        valide, mais une zone voisine ne l'est pas.
        """

        return self.stage_url(table).startswith(stage_url)


def __getattr__(name: str):
    """Compatibilité paresseuse : les identifiants du site à l'appel."""

    from .site_config import current as _current  # noqa: PLC0415

    if name == "DATABASE":
        return _current().destination_database
    if name == "SCHEMA":
        return _current().destination_schema
    raise AttributeError(name)
