from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .site_config import SnowflakeScope
from .snowflake_loader import assert_declared_destination

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Une image initiale porte un receveur SNAPSHOT:<…> : c'est une lecture
# ordinale, pas une position de journal. Melanger les deux dans un tri par
# sequence n'a pas de sens, donc ils sont distingues partout ou ils comptent.
SNAPSHOT_RECEIVER_PREFIX = "SNAPSHOT:"


@dataclass(frozen=True)
class SnowflakeBusinessMergePlan:
    """Generate a guarded C/U/D merge from the canonical event ledger.

    The target is intentionally a typed-by-later ``VARIANT`` snapshot. The
    business key is supplied by the table owner instead of being guessed from
    IBM i column names. Technical ``u_before``/``u_after`` rows are accepted
    as separate image roles, and an update that changes its key expands to a
    delete of the old key plus an upsert of the new key.
    """

    scope: SnowflakeScope
    canonical_table: str
    target_table: str
    source_library: str
    source_table: str
    key_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        for name, value in (
            ("canonical_table", self.canonical_table),
            ("target_table", self.target_table),
            ("source_library", self.source_library),
            ("source_table", self.source_table),
        ):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"invalid Snowflake identifier: {name}")
        if not self.key_columns:
            raise ValueError("at least one business key column is required")
        if len(set(self.key_columns)) != len(self.key_columns):
            raise ValueError("business key columns must be unique")
        for column in self.key_columns:
            if not isinstance(column, str) or not _IDENTIFIER.fullmatch(column):
                raise ValueError(f"invalid business key column: {column}")
        assert_declared_destination(
            self.scope,
            self.scope.database,
            self.scope.schema,
            self.canonical_table,
            self.target_table,
        )

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    @property
    def qualified_canonical_table(self) -> str:
        return _qualified(self.scope.database, self.scope.schema, self.canonical_table)

    @property
    def qualified_target_table(self) -> str:
        return _qualified(self.scope.database, self.scope.schema, self.target_table)

    def statements_for(self) -> tuple[str, str, str, str]:
        """Return session guard, DDL, validation query and deterministic MERGE."""

        return (
            "ALTER SESSION SET ERROR_ON_NONDETERMINISTIC_MERGE = TRUE",
            self._create_target_statement(),
            self._validation_statement(),
            self._merge_statement(),
        )

    def target_ddl_statement(self) -> str:
        """DDL de la table metier, idempotent."""

        return self._create_target_statement()

    def validation_statement(self) -> str:
        """Controle du contrat de cle : a executer avant tout rejeu."""

        return self._validation_statement()

    def merge_statement(self, ingested_after: str | None = None) -> str:
        """Rejeu complet, ou restreint aux cles touchees apres un filigrane."""

        return self._merge_statement(ingested_after)

    def incremental_merge_statement(self, ingested_after: str) -> str:
        """Rejoue seulement les clés touchées par les événements nouvellement ingérés.

        `ingested_after` est un horodatage littéral, comparé à `INGESTED_AT` du
        périmètre canonique. La sélection est faite sur l'**heure d'ingestion**,
        jamais sur la position de journal : Snowpipe charge en asynchrone, donc
        un fichier peut arriver après un plus récent, et un filigrane de
        position manquerait cet événement sans que rien ne le signale.

        Pour chaque clé touchée, le gagnant est recalculé sur **tout son
        historique**, pas seulement sur les événements neufs : un événement
        tardif peut changer le gagnant d'une clé déjà présente.
        """

        # Les clés touchées se lisent sur les empreintes des images : une
        # modification qui change la clé touche deux clés, l'ancienne et la
        # nouvelle.
        return self._merge_statement(ingested_after)

    def execute(self, cursor: Any) -> None:
        """Run the plan and abort before MERGE when the key contract is invalid."""

        session_guard, create_target, validation, merge = self.statements_for()
        cursor.execute(session_guard)
        cursor.execute(create_target)
        cursor.execute(validation)
        row = cursor.fetchone()
        if row is None or not row:
            raise RuntimeError("Snowflake business merge validation returned no result")
        invalid_count = int(row[0])
        if invalid_count:
            raise ValueError(
                f"Snowflake business merge validation rejected {invalid_count} events"
            )
        cursor.execute(merge)

    def _create_target_statement(self) -> str:
        return f"""CREATE TABLE IF NOT EXISTS {self.qualified_target_table} (
    BUSINESS_KEY_FINGERPRINT VARCHAR NOT NULL,
    BUSINESS_KEY VARIANT NOT NULL,
    ROW_DATA VARIANT NOT NULL,
    LAST_EVENT_ID VARCHAR NOT NULL,
    LAST_OPERATION VARCHAR NOT NULL,
    JOURNAL_RECEIVER VARCHAR NOT NULL,
    JOURNAL_SEQUENCE NUMBER(38, 0) NOT NULL,
    COMMIT_TIMESTAMP TIMESTAMP_LTZ
)"""

    def _validation_statement(self) -> str:
        operation = "LOWER(OPERATION)"
        after_missing = self._any_key_is_null("after")
        before_missing = self._any_key_is_null("before")
        after_image_missing = "(PAYLOAD:after IS NULL OR IS_NULL_VALUE(PAYLOAD:after))"
        before_image_missing = "(PAYLOAD:before IS NULL OR IS_NULL_VALUE(PAYLOAD:before))"
        after_image_present = "(PAYLOAD:after IS NOT NULL AND NOT IS_NULL_VALUE(PAYLOAD:after))"
        before_image_present = "(PAYLOAD:before IS NOT NULL AND NOT IS_NULL_VALUE(PAYLOAD:before))"
        # The current business plan has no receiver-chain rank. Refuse a
        # mixed-receiver canonical scope instead of ordering unrelated
        # sequences and risking a silently incorrect snapshot.
        # Le perimetre canonique peut legitimement porter UNE image initiale et
        # UNE chaine de journal : c'est l'etat normal apres une bascule, et le
        # refuser rendait la table injouable des que la copie historique etait
        # chargee (mesure du 17/09 : deux tables sur treize refusees).
        # Ce qui reste interdit est different :
        #   - plusieurs receveurs de JOURNAL, dont les sequences ne sont pas
        #     comparables entre eux ;
        #   - plusieurs images initiales, qui rendraient le choix arbitraire.
        # Le perimetre canonique peut porter UNE image initiale et PLUSIEURS
        # receveurs de journal : c'est l'etat normal des que la capture franchit
        # une rotation, et le refus rendait la table injouable des le premier
        # franchissement (mesure du 17/09 : environ sept receveurs par jour).
        #
        # Ce qui reste interdit est l'AMBIGUITE. Les sequences ne sont pas
        # globalement croissantes — mesure : un receveur finissait a 901 994 124
        # et le suivant commencait a 1 — donc le rang du receveur dans la chaine est
        # necessaire. Il est derive de l'horodatage du plus ancien evenement de
        # chaque receveur : un journal ecrit dans un receveur a la fois, donc ces
        # horodatages croissent le long de la chaine, et ils sont insensibles a
        # une reinitialisation de sequence.
        #
        # Si deux receveurs partageaient le meme horodatage minimal, le rang
        # serait arbitraire : le rejeu refuse plutot que de choisir.
        ambiguous_rank_guard = f"""(
      (SELECT COUNT(*) FROM (
         SELECT PLUS_ANCIEN, COUNT(*) AS RECEVEURS
         FROM (
            SELECT MIN(PAYLOAD:commit_timestamp::TIMESTAMP_LTZ) AS PLUS_ANCIEN
            FROM {self.qualified_canonical_table}
            WHERE PAYLOAD:library::VARCHAR = '{_sql_literal(self.source_library)}'
              AND PAYLOAD:table::VARCHAR = '{_sql_literal(self.source_table)}'
              AND JOURNAL_RECEIVER NOT LIKE '{SNAPSHOT_RECEIVER_PREFIX}%'
            GROUP BY JOURNAL_RECEIVER
         )
         GROUP BY PLUS_ANCIEN
       ) WHERE RECEVEURS > 1) > 0
  )"""
        snapshot_receivers_guard = f"""(
      (SELECT COUNT(DISTINCT JOURNAL_RECEIVER)
       FROM {self.qualified_canonical_table}
       WHERE PAYLOAD:library::VARCHAR = '{_sql_literal(self.source_library)}'
         AND PAYLOAD:table::VARCHAR = '{_sql_literal(self.source_table)}'
         AND JOURNAL_RECEIVER LIKE '{SNAPSHOT_RECEIVER_PREFIX}%') > 1
  )"""
        # Sous identite RRN, un delete ne cite que la position physique : elle
        # doit avoir ete inseree ou copiee dans le meme perimetre. Un _rrn
        # inconnu signifie que les positions ont ete reecrites hors flux (par
        # exemple une reorganisation RGZPFM) : rejouer supprimerait a tort ou
        # laisserait des fantomes — on refuse plutot que de corriger a vue.
        rrn_delete_guard = ""
        if self.key_columns == ("_rrn",):
            # "prior" est exige : l'image qui prouve la position doit preceder
            # le delete dans le temps du journal. Un _rrn qui n'apparait que
            # plus tard est une position reecrite (reorganisation), pas la
            # ligne supprimee. Le commit_timestamp du journal croit le long
            # de la chaine de receveurs, et l'image initiale precede toujours
            # la capture : la comparaison est donc fiable entre perimetres.
            rrn_delete_guard = f"""
      OR ({operation} = 'd' AND NOT EXISTS (
          SELECT 1
          FROM {self.qualified_canonical_table} AS known
          WHERE known.PAYLOAD:library::VARCHAR = '{_sql_literal(self.source_library)}'
            AND known.PAYLOAD:table::VARCHAR = '{_sql_literal(self.source_table)}'
            AND (known.PAYLOAD:after._rrn)::NUMBER(38, 0)
                = (scope_events.PAYLOAD:before._rrn)::NUMBER(38, 0)
            AND known.PAYLOAD:commit_timestamp::TIMESTAMP_LTZ
                <= scope_events.PAYLOAD:commit_timestamp::TIMESTAMP_LTZ
      ))"""
        return f"""SELECT COUNT(*) AS INVALID_EVENT_COUNT
FROM {self.qualified_canonical_table} AS scope_events
WHERE PAYLOAD:library::VARCHAR = '{_sql_literal(self.source_library)}'
  AND PAYLOAD:table::VARCHAR = '{_sql_literal(self.source_table)}'
  AND (
      {ambiguous_rank_guard}
      OR
      {snapshot_receivers_guard}
      OR
      {operation} NOT IN ('c', 'u', 'u_before', 'u_after', 'd')
      OR ({operation} = 'c' AND ({after_image_missing} OR {after_missing}))
      OR ({operation} = 'u' AND (
          {before_image_missing} OR {after_image_missing}
          OR {before_missing} OR {after_missing}
      ))
      OR ({operation} = 'u_before' AND (
          {before_image_missing} OR {after_image_present} OR {before_missing}
      ))
      OR ({operation} = 'u_after' AND (
          {after_image_missing} OR {before_image_present} OR {after_missing}
      ))
      OR ({operation} = 'd' AND ({before_image_missing} OR {before_missing}))
      {rrn_delete_guard}
  )"""

    def _merge_statement(self, ingested_after: str | None = None) -> str:
        """Rejoue la table metier, entierement ou sur les cles touchees.

        Sans filigrane, le perimetre est complet : c'est le rejeu de reference,
        utilise pour la copie initiale et pour la preuve. Avec un filigrane, le
        perimetre se limite aux cles touchees par les evenements nouvellement
        ingeres, mais le gagnant est recalcule sur **tout leur historique**.
        """

        source_filter = (
            f"PAYLOAD:library::VARCHAR = '{_sql_literal(self.source_library)}' "
            f"AND PAYLOAD:table::VARCHAR = '{_sql_literal(self.source_table)}'"
        )
        projection = self._event_projection_columns()
        before_fingerprint_expr = self._key_fingerprint("before")
        after_fingerprint_expr = self._key_fingerprint("after")
        if ingested_after is None:
            scope = f"""source_events AS (
        SELECT {projection}
        FROM {self.qualified_canonical_table}
        WHERE {source_filter}
    )"""
        else:
            # Les cles touchees se lisent sur les empreintes des images : une
            # modification qui change la cle touche l'ancienne ET la nouvelle.
            scope = f"""new_events AS (
        SELECT {projection}
        FROM {self.qualified_canonical_table}
        WHERE {source_filter}
          AND INGESTED_AT > '{_sql_literal(ingested_after)}'::TIMESTAMP_LTZ
    ), touched_keys AS (
        SELECT BEFORE_FINGERPRINT AS FINGERPRINT FROM new_events
        WHERE BEFORE_FINGERPRINT IS NOT NULL
        UNION
        SELECT AFTER_FINGERPRINT FROM new_events
        WHERE AFTER_FINGERPRINT IS NOT NULL
    ), source_events AS (
        SELECT {projection}
        FROM {self.qualified_canonical_table}
        WHERE {source_filter}
          AND ({before_fingerprint_expr} IN (SELECT FINGERPRINT FROM touched_keys)
               OR {after_fingerprint_expr} IN (SELECT FINGERPRINT FROM touched_keys))
    )"""
        return f"""MERGE INTO {self.qualified_target_table} AS target
USING (
    WITH {scope}, rangs AS (
        -- Rang du receveur dans la chaine, derive de l'horodatage de son plus
        -- ancien evenement. Un journal ecrit dans un receveur a la fois : ces
        -- horodatages croissent donc le long de la chaine, et ils ne dependent
        -- pas du numero de sequence — qui, lui, peut redemarrer.
        SELECT JOURNAL_RECEIVER,
               DENSE_RANK() OVER (
                   ORDER BY MIN(COMMIT_TIMESTAMP)
               ) AS RANG_RECEVEUR
        FROM source_events
        WHERE IS_SNAPSHOT = 0
        GROUP BY JOURNAL_RECEIVER
    ), events_with_rank AS (
        SELECT source_events.*, rangs.RANG_RECEVEUR
        FROM source_events
        LEFT JOIN rangs ON rangs.JOURNAL_RECEIVER = source_events.JOURNAL_RECEIVER
    ), operations AS (
        SELECT
            AFTER_FINGERPRINT AS BUSINESS_KEY_FINGERPRINT,
            AFTER_KEY AS BUSINESS_KEY,
            AFTER_DATA AS ROW_DATA,
            EVENT_ID,
            'upsert' AS APPLY_OPERATION,
            IS_SNAPSHOT,
            RANG_RECEVEUR,
            JOURNAL_RECEIVER,
            JOURNAL_SEQUENCE,
            COMMIT_TIMESTAMP,
            SOURCE_FILE
        FROM events_with_rank
        WHERE OPERATION IN ('c', 'u', 'u_after')
        UNION ALL
        SELECT
            BEFORE_FINGERPRINT AS BUSINESS_KEY_FINGERPRINT,
            BEFORE_KEY AS BUSINESS_KEY,
            NULL AS ROW_DATA,
            EVENT_ID,
            'delete' AS APPLY_OPERATION,
            IS_SNAPSHOT,
            RANG_RECEVEUR,
            JOURNAL_RECEIVER,
            JOURNAL_SEQUENCE,
            COMMIT_TIMESTAMP,
            SOURCE_FILE
        FROM events_with_rank
        WHERE OPERATION = 'd'
        UNION ALL
        SELECT
            BEFORE_FINGERPRINT AS BUSINESS_KEY_FINGERPRINT,
            BEFORE_KEY AS BUSINESS_KEY,
            NULL AS ROW_DATA,
            EVENT_ID,
            'delete' AS APPLY_OPERATION,
            IS_SNAPSHOT,
            RANG_RECEVEUR,
            JOURNAL_RECEIVER,
            JOURNAL_SEQUENCE,
            COMMIT_TIMESTAMP,
            SOURCE_FILE
        FROM events_with_rank
        WHERE OPERATION = 'u_before'
        UNION ALL
        SELECT
            BEFORE_FINGERPRINT AS BUSINESS_KEY_FINGERPRINT,
            BEFORE_KEY AS BUSINESS_KEY,
            NULL AS ROW_DATA,
            EVENT_ID,
            'delete' AS APPLY_OPERATION,
            IS_SNAPSHOT,
            RANG_RECEVEUR,
            JOURNAL_RECEIVER,
            JOURNAL_SEQUENCE,
            COMMIT_TIMESTAMP,
            SOURCE_FILE
        FROM events_with_rank
        WHERE OPERATION = 'u'
          AND BEFORE_FINGERPRINT <> AFTER_FINGERPRINT
    ), latest_operation AS (
        SELECT *
        FROM operations
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY BUSINESS_KEY_FINGERPRINT
            ORDER BY IS_SNAPSHOT ASC,
                     -- D'abord la position dans la chaine de receveurs : deux
                     -- sequences venant de receveurs differents ne sont pas
                     -- comparables (mesure : un receveur finissait a 901 994 124,
                     -- le suivant commencait a 1).
                     RANG_RECEVEUR DESC NULLS LAST,
                     JOURNAL_SEQUENCE DESC,
                     CASE APPLY_OPERATION WHEN 'delete' THEN 1 ELSE 2 END DESC,
                     COMMIT_TIMESTAMP DESC,
                     EVENT_ID DESC,
                     SOURCE_FILE DESC
        ) = 1
    )
    SELECT * FROM latest_operation
) AS source
ON target.BUSINESS_KEY_FINGERPRINT = source.BUSINESS_KEY_FINGERPRINT
WHEN MATCHED AND source.APPLY_OPERATION = 'delete' THEN DELETE
WHEN MATCHED AND source.APPLY_OPERATION = 'upsert' THEN UPDATE SET
    BUSINESS_KEY = source.BUSINESS_KEY,
    ROW_DATA = source.ROW_DATA,
    LAST_EVENT_ID = source.EVENT_ID,
    LAST_OPERATION = source.APPLY_OPERATION,
    JOURNAL_RECEIVER = source.JOURNAL_RECEIVER,
    JOURNAL_SEQUENCE = source.JOURNAL_SEQUENCE,
    COMMIT_TIMESTAMP = source.COMMIT_TIMESTAMP
WHEN NOT MATCHED AND source.APPLY_OPERATION = 'upsert' THEN INSERT (
    BUSINESS_KEY_FINGERPRINT, BUSINESS_KEY, ROW_DATA,
    LAST_EVENT_ID, LAST_OPERATION, JOURNAL_RECEIVER, JOURNAL_SEQUENCE, COMMIT_TIMESTAMP
) VALUES (
    source.BUSINESS_KEY_FINGERPRINT, source.BUSINESS_KEY, source.ROW_DATA,
    source.EVENT_ID, source.APPLY_OPERATION, source.JOURNAL_RECEIVER,
    source.JOURNAL_SEQUENCE, source.COMMIT_TIMESTAMP
)"""

    def _event_projection_columns(self) -> str:
        """Colonnes communes aux deux lectures d'evenements.

        Partager la projection garantit que la portee incrementale et la lecture
        complete voient exactement la meme forme.
        """

        operation = "LOWER(PAYLOAD:operation::VARCHAR)"
        return f"""
            EVENT_ID,
            JOURNAL_RECEIVER,
            JOURNAL_SEQUENCE,
            SOURCE_FILE,
            PAYLOAD:commit_timestamp::TIMESTAMP_LTZ AS COMMIT_TIMESTAMP,
            IFF(JOURNAL_RECEIVER LIKE '{SNAPSHOT_RECEIVER_PREFIX}%', 1, 0) AS IS_SNAPSHOT,
            {operation} AS OPERATION,
            {self._key_object("before")} AS BEFORE_KEY,
            {self._key_object("after")} AS AFTER_KEY,
            {self._key_fingerprint("before")} AS BEFORE_FINGERPRINT,
            {self._key_fingerprint("after")} AS AFTER_FINGERPRINT,
            PAYLOAD:before AS BEFORE_DATA,
            PAYLOAD:after AS AFTER_DATA
    """

    def _key_object(self, image: str) -> str:
        pairs = ", ".join(
            f"'{_sql_literal(column)}', PAYLOAD:{image}.{column}"
            for column in self.key_columns
        )
        return f"OBJECT_CONSTRUCT_KEEP_NULL({pairs})"

    def _key_fingerprint(self, image: str) -> str:
        values = ", ".join(f"PAYLOAD:{image}.{column}" for column in self.key_columns)
        return f"SHA2(TO_JSON(ARRAY_CONSTRUCT({values})), 256)"

    def _any_key_is_null(self, image: str) -> str:
        return " OR ".join(
            f"(PAYLOAD:{image}.{column} IS NULL OR "
            f"IS_NULL_VALUE(PAYLOAD:{image}.{column}))"
            for column in self.key_columns
        )


def _qualified(*parts: str) -> str:
    return ".".join(f'"{part}"' for part in parts)


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")
