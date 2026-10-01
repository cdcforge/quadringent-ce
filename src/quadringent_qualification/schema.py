"""Description générique d'une table cible et normalisation canonique des valeurs.

Ce module ne connaît aucun site réel : la table, ses colonnes et leurs types
SQL sont décrits par une :class:`TableSchema` fournie par la configuration du
run (``config.py``). La forme canonique d'une ligne est fixée par le type SQL
de chaque colonne, indépendamment du lecteur qui l'a produite — c'est ce qui
permet de comparer l'oracle, la source relue et l'état matérialisé côté
entrepôt sans jamais reconstruire l'un à partir d'un autre.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re


class CanonicalisationError(ValueError):
    """Levée quand une valeur ne peut pas être normalisée sans ambiguïté."""


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


@dataclass(frozen=True)
class Column:
    """Une colonne de la table cible, avec son type SQL générique."""

    name: str
    kind: str  # "integer" | "char" | "varchar" | "decimal" | "date" | "timestamp"
    length: int | None = None  # CHAR(n) / VARCHAR(n)
    precision: int | None = None  # DECIMAL(p, s)
    scale: int | None = None
    timestamp_precision: int = 6  # TIMESTAMP(n), en chiffres de fraction de seconde

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _IDENTIFIER.fullmatch(self.name) is None:
            raise ValueError("nom de colonne non sûr")
        valid = {"integer", "char", "varchar", "decimal", "date", "timestamp"}
        if self.kind not in valid:
            raise ValueError(f"type de colonne inconnu : {self.kind!r} (attendu parmi {sorted(valid)})")
        if self.kind == "char" and not self.length:
            raise ValueError(f"colonne CHAR {self.name!r} sans longueur")
        if self.kind == "decimal" and (self.scale is None):
            raise ValueError(f"colonne DECIMAL {self.name!r} sans échelle")


@dataclass(frozen=True)
class TableSchema:
    """Table qualifiée : nom pleinement qualifié + colonnes + clé primaire."""

    qualified_name: str
    columns: tuple[Column, ...]
    primary_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.qualified_name, str):
            raise ValueError("nom de table qualifiée non sûr")
        parts = self.qualified_name.split(".")
        if len(parts) != 2 or any(_IDENTIFIER.fullmatch(part) is None for part in parts):
            raise ValueError("nom de table qualifiée non sûr")
        names = [c.name for c in self.columns]
        if self.primary_key not in names:
            raise ValueError(f"clé primaire {self.primary_key!r} absente des colonnes {names}")
        if len(names) != len(set(names)):
            raise ValueError("noms de colonnes dupliqués")

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


# --- Dates : format IBM i ambigu (2 chiffres d'année) explicitement refusé -----

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMBIGUOUS_SHORT_YEAR_DATE = re.compile(r"^\d{2}-\d{2}-\d{2}$")


def parse_ibmi_date(value: object) -> str:
    """Normalise une date en ``YYYY-MM-DD``.

    Un format à année sur deux chiffres (``YY-MM-DD``, produit par certains
    extracteurs IBM i en format *EUR* ou *JIS* tronqué) est refusé
    explicitement : un bug réel du harnais privé consistait à l'interpréter
    silencieusement, ce qui décalait les dates de 2000 ans lors du
    rapprochement. Il doit être reformaté en amont par l'appelant, avec le
    siècle qu'il connaît, avant d'atteindre cette fonction.
    """
    text = str(value)
    if _AMBIGUOUS_SHORT_YEAR_DATE.match(text):
        raise CanonicalisationError(
            f"date {text!r} au format année sur 2 chiffres (YY-MM-DD) refusée : ambiguë, "
            "reformater explicitement en YYYY-MM-DD avant normalisation"
        )
    if _ISO_DATE.match(text):
        return text
    # Formats acceptés en entrée : datetime.date-like avec isoformat(), ou texte
    # ISO plus long (ex. horodatage) dont on ne garde que les 10 premiers
    # caractères, à condition qu'ils soient déjà au format YYYY-MM-DD.
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())[:10]
    if len(text) >= 10 and _ISO_DATE.match(text[:10]):
        return text[:10]
    raise CanonicalisationError(f"date {text!r} dans un format non reconnu")


def _normalise_timestamp_text(text: str, precision: int) -> str:
    text = text.strip()
    if "T" in text:
        text = text.replace("T", " ")
    # Format IBM i natif : 2026-09-23-08.00.01.234567
    if len(text) >= 19 and text[10] == "-" and text[13] == "." and text[16] == ".":
        text = text[:10] + " " + text[11:].replace(".", ":", 2)
    if " " not in text and len(text) >= 10:
        text = text[:10] + " " + text[10:].lstrip("-T ")
    date_part, _, time_part = text.partition(" ")
    if not _ISO_DATE.match(date_part):
        raise CanonicalisationError(f"horodatage {text!r} : partie date non reconnue")
    if "." in time_part:
        hms, frac = time_part.split(".", 1)
    else:
        hms, frac = time_part, ""
    frac = (frac + "0" * precision)[:precision]
    return f"{date_part} {hms}.{frac}"


def parse_ibmi_timestamp(value: object, precision: int = 6) -> str:
    """Normalise un horodatage en ``YYYY-MM-DD HH:MM:SS.ffffff`` (précision fixe).

    Accepte un objet ``datetime``-like (via ``strftime``), le format IBM i natif
    ``YYYY-MM-DD-HH.MM.SS.ffffff`` et le format ISO ``YYYY-MM-DDTHH:MM:SS[.ffffff]``.
    Les microsecondes sont toujours tronquées/complétées à la précision demandée :
    un lecteur qui renvoie ``08:00:01.2`` et un autre ``08:00:01.200000``
    normalisent vers la même valeur.
    """
    strftime = getattr(value, "strftime", None)
    if callable(strftime):
        text = strftime(f"%Y-%m-%d %H:%M:%S.%{precision}f" if False else "%Y-%m-%d %H:%M:%S.%f")
        date_part, time_part = text.split(" ")
        hms, frac = time_part.split(".")
        frac = (frac + "0" * precision)[:precision]
        return f"{date_part} {hms}.{frac}"
    return _normalise_timestamp_text(str(value), precision)


def _to_decimal(value: object, scale: int) -> Decimal:
    """Convertit une valeur JSON (float, int, str, Decimal) en Decimal à l'échelle fixée.

    Passe toujours par ``repr``/``str`` avant ``Decimal`` : un ``float`` JSON
    comme ``-99999.99`` doit produire ``Decimal("-99999.99")`` et non
    l'approximation binaire de ce float construite directement.
    """
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, float):
        decimal_value = Decimal(repr(value))
    else:
        try:
            decimal_value = Decimal(str(value))
        except InvalidOperation as error:
            raise CanonicalisationError(f"valeur décimale {value!r} invalide") from error
    quantum = Decimal(1).scaleb(-scale)
    return decimal_value.quantize(quantum)


def canonical_value(value: object, column: Column) -> object:
    """Normalise une valeur unique selon le type SQL de sa colonne."""
    if value is None:
        return None
    if column.kind == "integer":
        return int(value)
    if column.kind == "char":
        return str(value).ljust(column.length or 0)
    if column.kind == "varchar":
        return str(value)
    if column.kind == "decimal":
        return str(_to_decimal(value, column.scale or 0))
    if column.kind == "date":
        return parse_ibmi_date(value)
    if column.kind == "timestamp":
        return parse_ibmi_timestamp(value, column.timestamp_precision)
    raise AssertionError(f"type de colonne non géré : {column.kind}")  # pragma: no cover


def canonical(row: dict[str, object], schema: TableSchema) -> dict[str, object]:
    """Forme canonique d'une ligne, fixée par les types SQL de ``schema``."""
    return {c.name: canonical_value(row.get(c.name), c) for c in schema.columns}


def sql_literal(value: object, column: Column | None = None) -> str:
    """Littéral SQL sûr pour ``value``.

    Les chaînes sont échappées en doublant les apostrophes (échappement SQL
    standard), ce qui couvre accents et apostrophes du jeu synthétique
    (« Größe ÄÖÜ äöü ß », « l'été », etc.). Les décimaux sont émis avec
    l'échelle de la colonne quand elle est connue, sinon celle induite par la
    valeur.
    """
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        scale = column.scale if column and column.scale is not None else -value.as_tuple().exponent
        return str(_to_decimal(value, scale))
    if isinstance(value, bool):  # avant int : bool est une sous-classe d'int
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat) and hasattr(value, "hour"):  # datetime
        return "TIMESTAMP '" + value.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
    if callable(isoformat):  # date
        return "DATE '" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def insert_sql(table: TableSchema, row: dict[str, object]) -> str:
    columns = table.column_names
    values = ", ".join(sql_literal(row[c], table.column(c)) for c in columns)
    return f"INSERT INTO {table.qualified_name} ({', '.join(columns)}) VALUES ({values})"


def update_sql(table: TableSchema, key: object, changes: dict[str, object]) -> str:
    assignments = ", ".join(f"{k} = {sql_literal(v, table.column(k))}" for k, v in changes.items())
    return f"UPDATE {table.qualified_name} SET {assignments} WHERE {table.primary_key} = {sql_literal(key)}"


def delete_sql(table: TableSchema, key: object) -> str:
    return f"DELETE FROM {table.qualified_name} WHERE {table.primary_key} = {sql_literal(key)}"
