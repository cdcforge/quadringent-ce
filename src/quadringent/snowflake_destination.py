"""Génération DDL Snowflake pour la table d'historique et la table miroir.

Ce module ne connaît aucune connexion Snowflake : il traduit une liste de
colonnes IBM i découvertes (types SQL natifs, tels que rapportés par le
catalogue ``QSYS2.SYSCOLUMNS``/le worker de découverte) en instructions
``CREATE TABLE`` Snowflake, pour les deux tables que le produit livre par
table source : l'historique (append-only, un rang par changement) et le
miroir (une ligne par clé). Un type source sans correspondance Snowflake
déclarée lève :class:`UnsupportedColumnTypeError` — jamais une conversion
silencieuse tronquée ou approximative.

Politique CLOB/BLOB : IBM i ne borne pas nécessairement ces colonnes à une
taille compatible Snowflake. On les fait correspondre respectivement à
``VARCHAR``/``BINARY`` dans la limite Snowflake documentée (16 Mo pour un
``VARCHAR``, 8 Mo pour un ``BINARY``) ; au-delà, le type est refusé
explicitement plutôt que tronqué.
"""

from __future__ import annotations

from dataclasses import dataclass

from .site_config import SnowflakeScope
from .snowflake_loader import _IDENTIFIER as _SF_IDENTIFIER
from .snowflake_loader import _qualified, assert_declared_destination

# --- Familles de types IBM i reconnues ----------------------------------

_CHAR_KINDS = frozenset({"char", "varchar"})
_GRAPHIC_KINDS = frozenset({"graphic", "vargraphic"})
_INTEGER_KINDS = frozenset({"smallint", "integer", "bigint"})
_DECIMAL_KINDS = frozenset({"decimal", "numeric"})
_DATE_KINDS = frozenset({"date"})
_TIME_KINDS = frozenset({"time"})
_TIMESTAMP_KINDS = frozenset({"timestamp"})
_BINARY_KINDS = frozenset({"binary", "varbinary"})
_LOB_KINDS = frozenset({"clob", "dbclob", "blob"})

_KNOWN_KINDS = (
    _CHAR_KINDS
    | _GRAPHIC_KINDS
    | _INTEGER_KINDS
    | _DECIMAL_KINDS
    | _DATE_KINDS
    | _TIME_KINDS
    | _TIMESTAMP_KINDS
    | _BINARY_KINDS
    | _LOB_KINDS
)

# CCSID IBM i signalant des données binaires/mixtes non converties (« hex »
# côté catalogue) : une colonne caractère dans ce CCSID n'a pas de
# correspondance texte fiable et doit être reclassée en BINARY côté site.
_BINARY_CCSID = 65535

_MAX_SNOWFLAKE_VARCHAR = 16_777_216  # caractères, limite documentée Snowflake
_MAX_SNOWFLAKE_BINARY = 8_388_608  # octets, limite documentée Snowflake
_MAX_SNOWFLAKE_NUMBER_PRECISION = 38
_MAX_SNOWFLAKE_TIMESTAMP_PRECISION = 9


class UnsupportedColumnTypeError(ValueError):
    """Le type source IBM i n'a pas de correspondance Snowflake supportée."""


@dataclass(frozen=True)
class IbmiColumnType:
    """Type SQL natif d'une colonne IBM i, tel que rapporté par le catalogue.

    ``length`` est en caractères pour CHAR/VARCHAR/GRAPHIC/CLOB, en octets
    pour BINARY/VARBINARY/BLOB. ``ccsid`` est le CCSID de colonne rapporté
    par le catalogue ; seul le cas binaire non converti (65535) est
    significatif ici.
    """

    kind: str
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    timestamp_precision: int = 6
    ccsid: int | None = None

    def __post_init__(self) -> None:
        kind = self.kind.lower()
        object.__setattr__(self, "kind", kind)
        if kind not in _KNOWN_KINDS:
            raise UnsupportedColumnTypeError(
                f"type IBM i inconnu : {self.kind!r} (aucune correspondance Snowflake déclarée)"
            )
        if kind in (_CHAR_KINDS | _GRAPHIC_KINDS | _BINARY_KINDS | _LOB_KINDS):
            if not self.length or self.length <= 0:
                raise ValueError(f"type {kind} sans longueur déclarée")
        if kind in _DECIMAL_KINDS:
            if self.precision is None or self.scale is None:
                raise ValueError(f"type {kind} sans précision/échelle déclarée")
        if kind in _CHAR_KINDS and self.ccsid == _BINARY_CCSID:
            raise UnsupportedColumnTypeError(
                f"colonne caractère en CCSID {_BINARY_CCSID} (binaire non converti) : "
                "reclasser en BINARY/VARBINARY côté découverte avant génération DDL"
            )
        if not (0 <= self.timestamp_precision <= _MAX_SNOWFLAKE_TIMESTAMP_PRECISION):
            raise UnsupportedColumnTypeError(
                f"précision TIMESTAMP {self.timestamp_precision} hors plage Snowflake "
                f"(0 à {_MAX_SNOWFLAKE_TIMESTAMP_PRECISION})"
            )


def snowflake_type_for(column_type: IbmiColumnType) -> str:
    """Traduit un type IBM i en fragment de type Snowflake pour un ``CREATE TABLE``.

    Lève :class:`UnsupportedColumnTypeError` si la valeur dépasse une limite
    Snowflake documentée (précision NUMBER, longueur VARCHAR/BINARY) — jamais
    de troncature silencieuse.
    """

    kind = column_type.kind

    if kind in _CHAR_KINDS or kind in _GRAPHIC_KINDS or kind == "clob" or kind == "dbclob":
        length = column_type.length
        assert length is not None
        if length > _MAX_SNOWFLAKE_VARCHAR:
            raise UnsupportedColumnTypeError(
                f"longueur {length} dépasse la limite Snowflake VARCHAR "
                f"({_MAX_SNOWFLAKE_VARCHAR} caractères) : politique produit = rejet explicite"
            )
        return f"VARCHAR({length})"

    if kind in _BINARY_KINDS or kind == "blob":
        length = column_type.length
        assert length is not None
        if length > _MAX_SNOWFLAKE_BINARY:
            raise UnsupportedColumnTypeError(
                f"longueur {length} dépasse la limite Snowflake BINARY "
                f"({_MAX_SNOWFLAKE_BINARY} octets) : politique produit = rejet explicite"
            )
        return f"BINARY({length})"

    if kind == "smallint":
        return "NUMBER(5, 0)"
    if kind == "integer":
        return "NUMBER(10, 0)"
    if kind == "bigint":
        return "NUMBER(19, 0)"

    if kind in _DECIMAL_KINDS:
        precision, scale = column_type.precision, column_type.scale
        assert precision is not None and scale is not None
        if not (1 <= precision <= _MAX_SNOWFLAKE_NUMBER_PRECISION):
            raise UnsupportedColumnTypeError(
                f"précision {precision} hors plage Snowflake NUMBER "
                f"(1 à {_MAX_SNOWFLAKE_NUMBER_PRECISION})"
            )
        if not (0 <= scale <= precision):
            raise UnsupportedColumnTypeError(
                f"échelle {scale} invalide pour une précision de {precision}"
            )
        return f"NUMBER({precision}, {scale})"

    if kind == "date":
        return "DATE"
    if kind == "time":
        return "TIME"
    if kind == "timestamp":
        return f"TIMESTAMP_NTZ({column_type.timestamp_precision})"

    raise UnsupportedColumnTypeError(f"type IBM i non supporté : {kind}")


@dataclass(frozen=True)
class ColumnDefinition:
    """Une colonne métier, avec son type IBM i source et sa nullabilité Snowflake."""

    name: str
    type: IbmiColumnType
    nullable: bool = True

    def __post_init__(self) -> None:
        if not _SF_IDENTIFIER.fullmatch(self.name):
            raise ValueError(f"nom de colonne Snowflake invalide : {self.name!r}")
        if self.name.upper() in _RESERVED_TECHNICAL_COLUMNS:
            raise ValueError(
                f"nom de colonne {self.name!r} réservé aux colonnes techniques "
                f"({sorted(_RESERVED_TECHNICAL_COLUMNS)})"
            )

    def snowflake_column_sql(self) -> str:
        sql_type = snowflake_type_for(self.type)
        suffix = "" if self.nullable else " NOT NULL"
        return f"    {self.name} {sql_type}{suffix}"


# --- Colonnes techniques communes aux deux tables ------------------------

_HISTORY_TECHNICAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("EVENT_ID", "VARCHAR NOT NULL"),
    ("OPERATION", "VARCHAR NOT NULL"),
    ("JOURNAL_RECEIVER", "VARCHAR NOT NULL"),
    ("JOURNAL_SEQUENCE", "NUMBER(38, 0) NOT NULL"),
    ("COMMIT_TIMESTAMP", "TIMESTAMP_NTZ(6) NOT NULL"),
    # DEFAULT explicite : les lignes streamées par le SDK Snowpipe Streaming
    # ne portent jamais cette colonne elles-mêmes (aucune expression SQL
    # évaluée côté client, seulement des valeurs) — sans défaut serveur, la
    # contrainte NOT NULL bloque silencieusement la matérialisation de la
    # ligne (constaté en vérification réelle : COPY INTO exécuté,
    # pendingFileCount retombé à 0, mais zéro ligne visible, aucune erreur
    # remontée par le SDK ni par COPY_HISTORY).
    ("INGESTED_AT", "TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()"),
)

_MIRROR_TECHNICAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("EVENT_ID", "VARCHAR NOT NULL"),
    ("JOURNAL_RECEIVER", "VARCHAR NOT NULL"),
    ("JOURNAL_SEQUENCE", "NUMBER(38, 0) NOT NULL"),
    ("COMMIT_TIMESTAMP", "TIMESTAMP_NTZ(6) NOT NULL"),
    ("MIRROR_UPDATED_AT", "TIMESTAMP_LTZ NOT NULL"),
)

_RESERVED_TECHNICAL_COLUMNS = frozenset(
    name for name, _ in _HISTORY_TECHNICAL_COLUMNS + _MIRROR_TECHNICAL_COLUMNS
)


@dataclass(frozen=True)
class TableDestinationPlan:
    """DDL pour la paire historique/miroir d'une table source IBM i.

    ``key_columns`` porte la clé métier utilisée pour le MERGE miroir
    (:mod:`quadringent.snowflake_streaming_loader`) ; ce module ne l'utilise
    que pour la déclaration ``PRIMARY KEY`` informative (non contrainte côté
    Snowflake, mais documentée pour l'optimiseur et les outils tiers).
    """

    scope: SnowflakeScope
    history_table: str
    mirror_table: str
    columns: tuple[ColumnDefinition, ...]
    key_columns: tuple[str, ...]
    # Sans scope miroir distinct, conserver les plans historiques à schéma unique.
    mirror_scope: SnowflakeScope | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        for name, value in (
            ("history_table", self.history_table),
            ("mirror_table", self.mirror_table),
        ):
            if not _SF_IDENTIFIER.fullmatch(value):
                raise ValueError(f"invalid Snowflake identifier: {name}")
        assert_declared_destination(
            self.scope, self.database, self.schema, self.history_table, self.mirror_table
        )
        mirror_scope = self.mirror_scope if self.mirror_scope is not None else self.scope
        if not isinstance(mirror_scope, SnowflakeScope) or mirror_scope.database != self.database:
            raise ValueError("le miroir doit rester dans la base Snowflake déclarée")
        assert_declared_destination(mirror_scope, self.database, mirror_scope.schema, self.mirror_table)
        restrictions = tuple(dict.fromkeys(self.scope.forbidden_fragments + mirror_scope.forbidden_fragments))
        for destination_scope in (self.scope, mirror_scope):
            checked_scope = SnowflakeScope(database=destination_scope.database, schema=destination_scope.schema,
                                          forbidden_fragments=restrictions)
            assert_declared_destination(checked_scope, self.database, destination_scope.schema,
                                        self.history_table, self.mirror_table)
        if not self.columns:
            raise ValueError("au moins une colonne métier est requise")
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("noms de colonnes métier dupliqués")
        if not self.key_columns:
            raise ValueError("au moins une colonne de clé est requise pour le miroir")
        unknown_keys = set(self.key_columns) - set(names)
        if unknown_keys:
            raise ValueError(f"colonnes de clé absentes des colonnes déclarées : {sorted(unknown_keys)}")

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    @property
    def qualified_history_table(self) -> str:
        return _qualified(self.database, self.schema, self.history_table)

    @property
    def qualified_mirror_table(self) -> str:
        scope = self.mirror_scope if self.mirror_scope is not None else self.scope
        return _qualified(scope.database, scope.schema, self.mirror_table)

    def history_ddl(self) -> str:
        """``CREATE TABLE IF NOT EXISTS`` pour la table d'historique (append-only)."""

        technical = ",\n".join(f"    {name} {sql}" for name, sql in _HISTORY_TECHNICAL_COLUMNS)
        business = ",\n".join(c.snowflake_column_sql() for c in self.columns)
        return (
            f"CREATE TABLE IF NOT EXISTS {self.qualified_history_table} (\n"
            f"{technical},\n{business}\n)"
        )

    def mirror_ddl(self) -> str:
        """``CREATE TABLE IF NOT EXISTS`` pour la table miroir (une ligne par clé)."""

        technical = ",\n".join(f"    {name} {sql}" for name, sql in _MIRROR_TECHNICAL_COLUMNS)
        business = ",\n".join(c.snowflake_column_sql() for c in self.columns)
        key_list = ", ".join(self.key_columns)
        return (
            f"CREATE TABLE IF NOT EXISTS {self.qualified_mirror_table} (\n"
            f"{technical},\n{business},\n"
            f"    PRIMARY KEY ({key_list})\n)"
        )

    @property
    def business_column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


_MAX_SNOWFLAKE_IDENTIFIER = 62  # cf. snowflake_loader._IDENTIFIER


def default_history_table_name(table_name: str) -> str:
    """Nom de table d'historique par défaut — déterministe, jamais aléatoire.

    Le chargeur de destination l'utilise quand le site n'en déclare pas un
    explicitement : deux exécutions du même flux visent donc toujours la
    même table.
    """

    return _default_destination_table_name(table_name, suffix="_HISTORY")


def default_mirror_table_name(table_name: str) -> str:
    """Nom de table miroir par défaut — voir :func:`default_history_table_name`."""

    return _default_destination_table_name(table_name, suffix="_MIRROR")


def _default_destination_table_name(table_name: str, *, suffix: str) -> str:
    name = table_name.strip().upper()
    if not name:
        raise ValueError("table_name must not be empty")
    candidate = f"{name}{suffix}"
    if len(candidate) > _MAX_SNOWFLAKE_IDENTIFIER:
        raise ValueError(
            f"nom de table dérivé {candidate!r} dépasse {_MAX_SNOWFLAKE_IDENTIFIER} caractères : "
            "déclarer un nom explicite côté site"
        )
    return candidate
