"""Générateur synthétique déterministe (l'oracle) et étapes de DML.

Reprend, en générique et sans identifiant de site réel, le principe du
harnais privé : un jeu de lignes couvrant NULL, chaîne vide, accents/
apostrophe, CHAR complété d'espaces, décimaux signés, date et horodatage ;
puis des étapes ``seed``/``changes1``/``changes2``/``changes3`` qui insèrent,
modifient et suppriment des lignes en construisant à la fois les instructions
SQL et la mutation correspondante de l'oracle en mémoire.

Tout est pur et déterministe : pour un ``ORDER_ID`` donné, :func:`generated_row`
renvoie toujours la même ligne, quel que soit l'environnement d'exécution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Iterable

from .schema import TableSchema, canonical, delete_sql, insert_sql, update_sql

Oracle = dict[int, dict[str, object]]
Mutator = Callable[[Oracle], None]

# Colonnes génériques attendues par le générateur par défaut. Une configuration
# peut fournir un autre TableSchema tant que ces noms de colonnes existent.
ORDER_ID = "ORDER_ID"
LABEL = "LABEL"
CODE = "CODE"
AMOUNT = "AMOUNT"
EVENT_DATE = "EVENT_DATE"
UPDATED_AT = "UPDATED_AT"
NOTE = "NOTE"

REQUIRED_COLUMNS = (ORDER_ID, LABEL, CODE, AMOUNT, EVENT_DATE, UPDATED_AT, NOTE)


def freshness_markers(run_id: str) -> tuple[str, str, str]:
    """Marqueurs uniques au run pour trois écritures de latence isolées."""
    return (f"Q-{run_id}-1", f"Q-{run_id}-2", f"Q-{run_id}-3")


def require_default_columns(schema: TableSchema) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in schema.column_names]
    if missing:
        raise ValueError(
            f"schéma incompatible avec le générateur par défaut : colonnes manquantes {missing}"
        )


def generated_row(i: int, base_date: date = date(2026, 1, 1), base_time: datetime = datetime(2026, 1, 1, 8, 0, 0)) -> dict[str, object]:
    """Ligne synthétique déterministe n°``i`` (i >= 1).

    Couvre volontairement : NULL (``i % k == 0``), chaîne vide, accents et
    apostrophe, décimaux signés, et laisse passer des valeurs qui exercent le
    padding CHAR(8) et la troncature/complétion des microsecondes.
    """
    if i < 1:
        raise ValueError("i doit être >= 1")
    label = None if i % 10 == 0 else "" if i % 10 == 1 else f"Café l'été n°{i}" if i % 10 == 2 else f"Commande {i:03d}"
    code = None if i % 7 == 0 else "AB" if i % 7 == 1 else f"C{i:05d}"
    amount = None if i % 9 == 0 else (Decimal(i * 12345 % 10_000_000) / 100) * (-1 if i % 2 else 1)
    event_date = None if i % 11 == 0 else base_date + timedelta(days=i)
    updated = None if i % 13 == 0 else base_time + timedelta(microseconds=i * 1_234_567)
    note = None if i % 5 == 1 else "Größe ÄÖÜ äöü ß é à ç 'ok'" if i % 5 == 0 else f"note {i}  "
    return {
        ORDER_ID: i, LABEL: label, CODE: code, AMOUNT: amount,
        EVENT_DATE: event_date, UPDATED_AT: updated, NOTE: note,
    }


@dataclass(frozen=True)
class StepPlan:
    """Instructions SQL d'une étape + mutation correspondante de l'oracle."""

    name: str
    statements: tuple[str, ...]
    apply: Mutator


def _seed(schema: TableSchema, count: int = 100, start: int = 1) -> StepPlan:
    rows = [generated_row(i) for i in range(start, start + count)]

    def apply(oracle: Oracle) -> None:
        for r in rows:
            oracle[r[ORDER_ID]] = r

    return StepPlan("seed", tuple(insert_sql(schema, r) for r in rows), apply)


def _changes1(schema: TableSchema) -> StepPlan:
    inserts = [generated_row(i) for i in range(101, 111)]
    updates = {i: {LABEL: f"Modifié n°{i}", AMOUNT: Decimal("-0.01") * i,
                    UPDATED_AT: datetime(2026, 9, 23, 12, 0, 0, i)} for i in range(1, 11)}
    deletes = list(range(91, 96))
    statements = (
        tuple(insert_sql(schema, r) for r in inserts)
        + tuple(update_sql(schema, i, c) for i, c in updates.items())
        + tuple(delete_sql(schema, i) for i in deletes)
    )

    def apply(oracle: Oracle) -> None:
        for r in inserts:
            oracle[r[ORDER_ID]] = r
        for i, c in updates.items():
            oracle[i] = {**oracle[i], **c}
        for i in deletes:
            del oracle[i]

    return StepPlan("changes1", statements, apply)


def _changes2(schema: TableSchema) -> StepPlan:
    inserts = [generated_row(i) for i in range(111, 116)]
    updates = {i: {NOTE: f"Reprise é {i}", CODE: f"R{i}", EVENT_DATE: None} for i in range(11, 16)}
    statements = tuple(insert_sql(schema, r) for r in inserts) + tuple(
        update_sql(schema, i, c) for i, c in updates.items()
    )

    def apply(oracle: Oracle) -> None:
        for r in inserts:
            oracle[r[ORDER_ID]] = r
        for i, c in updates.items():
            oracle[i] = {**oracle[i], **c}

    return StepPlan("changes2", statements, apply)


def _changes3(schema: TableSchema) -> StepPlan:
    updates = {
        20: {AMOUNT: Decimal("-99999.99"), LABEL: "Après rotation"},
        21: {NOTE: None, UPDATED_AT: datetime(2026, 9, 23, 13, 0, 0, 999999)},
    }
    statements = tuple(update_sql(schema, i, c) for i, c in updates.items())

    def apply(oracle: Oracle) -> None:
        for i, c in updates.items():
            oracle[i] = {**oracle[i], **c}

    return StepPlan("changes3", statements, apply)


_BUILDERS: dict[str, Callable[[TableSchema], StepPlan]] = {
    "seed": _seed,
    "changes1": _changes1,
    "changes2": _changes2,
    "changes3": _changes3,
}

DML_STEP_NAMES: tuple[str, ...] = tuple(_BUILDERS)


def step_plan(name: str, schema: TableSchema) -> StepPlan:
    """Instructions SQL et mutation d'oracle pour l'étape ``name``.

    Lève ``ValueError`` pour un nom d'étape inconnu.
    """
    require_default_columns(schema)
    try:
        builder = _BUILDERS[name]
    except KeyError as error:
        raise ValueError(f"étape DML inconnue : {name!r} (attendu parmi {DML_STEP_NAMES})") from error
    return builder(schema)


def build_oracle(step_names: Iterable[str], schema: TableSchema) -> Oracle:
    """Rejoue une suite d'étapes DML sur un oracle vide et renvoie sa forme canonique."""
    oracle: Oracle = {}
    for name in step_names:
        step_plan(name, schema).apply(oracle)
    return {key: canonical(row, schema) for key, row in sorted(oracle.items())}
