"""Rejeu metier incremental, avec filigrane d'ingestion durable.

Le rejeu complet produit la bonne table metier, mais il relit tout le perimetre
canonique a chaque execution. Sur une table de 57 millions de lignes, le
declencher toutes les dix minutes reproduirait le probleme de cout que les
dynamic tables avaient deja pose.

Ce module ajoute un **filigrane d'ingestion** : chaque execution ne rejoue que
les cles touchees par les evenements nouvellement ingeres, et laisse les autres
intactes.

Trois choix de conception, chacun pour une raison mesuree :

- le filigrane est un **horodatage d'ingestion**, pas une position de journal.
  Snowpipe charge en asynchrone : un fichier peut etre ingere apres un plus
  recent, et un filigrane de position manquerait cet evenement sans rien
  signaler ;
- pour chaque cle touchee, le gagnant est recalcule sur **tout son historique**,
  pas seulement sur les evenements neufs : un evenement tardif peut changer le
  gagnant d'une cle deja presente ;
- le filigrane n'avance **qu'apres** un rejeu reussi. Une panne entre le rejeu
  et l'avancement fait simplement rejouer la meme fenetre, ce qui est sans
  effet puisque le rejeu est idempotent ; l'inverse perdrait des evenements.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .site_config import SnowflakeScope
from .snowflake_business import SnowflakeBusinessMergePlan
from .snowflake_loader import assert_declared_destination


_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
_TABLE = re.compile(r"^[A-Z0-9_]{1,64}$")


@dataclass(frozen=True)
class IncrementalReplayPlan:
    """Rejeu incremental d'une table, avec son filigrane persistant."""

    business: SnowflakeBusinessMergePlan
    state_table: str

    def __post_init__(self) -> None:
        if not isinstance(self.business, SnowflakeBusinessMergePlan):
            raise ValueError("incremental replay requires a business merge plan")
        if _TABLE.fullmatch(self.state_table) is None:
            raise ValueError("invalid state table identifier")
        assert_declared_destination(
            self.business.scope,
            self.business.scope.database,
            self.business.scope.schema,
            self.state_table,
        )

    @classmethod
    def for_table(
        cls,
        *,
        table: str,
        key_columns: tuple[str, ...],
        scope: SnowflakeScope,
        source_library: str,
        destination_prefix: str = "QUADRINGENT",
    ) -> "IncrementalReplayPlan":
        """Construit le plan d'une table a partir de son seul nom.

        ``scope``, ``source_library`` et ``destination_prefix`` portent la
        déclaration du site : rien n'est déduit d'une installation.
        """

        name = str(table).strip().upper()
        if _TABLE.fullmatch(name) is None:
            raise ValueError("invalid table identifier")
        return cls(
            business=SnowflakeBusinessMergePlan(
                scope=scope,
                canonical_table=f"{destination_prefix}_{name}_CANONICAL",
                target_table=f"{destination_prefix}_{name}_ROLLUP",
                source_library=source_library,
                source_table=name,
                key_columns=key_columns,
            ),
            state_table=f"{destination_prefix}_{name}_REPLAY_STATE",
        )

    # -- Objets ------------------------------------------------------------

    @property
    def qualified_state_table(self) -> str:
        business = self.business
        return f'"{business.database}"."{business.schema}"."{self.state_table}"'

    def create_state_statement(self) -> str:
        """Une ligne par flux : le filigrane, et de quoi l'expliquer."""

        business = self.business
        return f"""CREATE TABLE IF NOT EXISTS {self.qualified_state_table} (
    SOURCE_TABLE VARCHAR NOT NULL,
    WATERMARK TIMESTAMP_LTZ NOT NULL,
    LAST_EVENT_ID VARCHAR,
    LAST_APPLIED_ROWS NUMBER(38, 0),
    UPDATED_AT TIMESTAMP_LTZ NOT NULL
)"""

    def read_watermark_statement(self) -> str:
        """Filigrane courant, ou aucune ligne si le flux n'a jamais ete rejoue."""

        business = self.business
        return (
            "SELECT TO_VARCHAR(WATERMARK, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM') "
            f"FROM {self.qualified_state_table} "
            f"WHERE SOURCE_TABLE = '{business.source_table}'"
        )

    def latest_ingested_statement(self) -> str:
        """Horodatage du dernier evenement ingere, candidat au filigrane."""

        business = self.business
        return (
            "SELECT TO_VARCHAR(MAX(INGESTED_AT), 'YYYY-MM-DD\"T\"HH24:MI:SS.FF3TZH:TZM') "
            f"FROM {business.qualified_canonical_table} "
            f"WHERE PAYLOAD:library::VARCHAR = '{business.source_library}' "
            f"AND PAYLOAD:table::VARCHAR = '{business.source_table}'"
        )

    def advance_watermark_statement(
        self, watermark: str, applied_rows: int
    ) -> str:
        """Enregistre le nouveau filigrane, apres un rejeu reussi.

        `applied_rows` est le nombre de lignes lues dans la table metier apres
        le rejeu : il est enregistre pour qu'une execution suivante puisse
        constater une variation anormale sans avoir a relire l'historique.
        """

        business = self.business
        if not isinstance(applied_rows, int) or applied_rows < 0:
            raise ValueError("applied rows must be a non-negative integer")
        return f"""MERGE INTO {self.qualified_state_table} AS state
USING (
    SELECT '{business.source_table}' AS SOURCE_TABLE,
           '{watermark}'::TIMESTAMP_LTZ AS WATERMARK,
           CURRENT_TIMESTAMP() AS UPDATED_AT
) AS source
ON state.SOURCE_TABLE = source.SOURCE_TABLE
WHEN MATCHED THEN UPDATE SET
    WATERMARK = source.WATERMARK,
    LAST_APPLIED_ROWS = {applied_rows},
    UPDATED_AT = source.UPDATED_AT
WHEN NOT MATCHED THEN INSERT (
    SOURCE_TABLE, WATERMARK, LAST_APPLIED_ROWS, UPDATED_AT
) VALUES (
    source.SOURCE_TABLE, source.WATERMARK, {applied_rows}, source.UPDATED_AT
)"""

    def count_target_statement(self) -> str:
        business = self.business
        return f"SELECT COUNT(*) FROM {business.qualified_target_table}"

    # -- Execution ---------------------------------------------------------

    def steps(self, watermark: str | None, latest: str | None) -> tuple[str, ...]:
        """Instructions à exécuter, dans l'ordre, pour une exécution.

        Cette méthode ne touche à aucune connexion : elle rend la suite
        d'instructions, ce qui la rend vérifiable sans base. Une exécution se
        contente de les jouer dans l'ordre.

        L'ordre porte la garantie : le rejeu vient **avant** l'avancement du
        filigrane. Si l'exécution s'interrompt entre les deux, la fenêtre sera
        simplement rejouée, et le rejeu étant idempotent, cela ne change rien.
        """

        business = self.business
        steps: list[str] = [
            "ALTER SESSION SET ERROR_ON_NONDETERMINISTIC_MERGE = TRUE",
            business.target_ddl_statement(),
            self.create_state_statement(),
        ]
        if latest is None:
            # Aucun événement : rien à rejouer, et le filigrane reste où il est.
            return tuple(steps)
        if watermark is not None and latest <= watermark:
            return tuple(steps)
        steps.append(business.validation_statement())
        steps.append(
            business.merge_statement(watermark)
        )
        return tuple(steps)
