"""Service Table v2 (tâche 4) : liste/filtre, rafraîchissement de découverte,
choix de clé (``PATCH``).

``refresh`` ne parle jamais réseau lui-même : il délègue à un
``TableDiscoveryClientProtocol`` injecté (le vrai client câble
``PersistentJavaWorker.discover`` + ``table_discovery.parse_discover_output``
hors périmètre de ce module ; les tests injectent un faux client). La
classification de disponibilité réutilise
``quadringent.table_discovery.classify_table`` — une seule vérité entre le
worker/CLI et l'API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from quadringent.snowflake_destination import ColumnDefinition, IbmiColumnType, UnsupportedColumnTypeError
from quadringent.table_discovery import (
    DiscoveredColumn,
    DiscoveredTable,
    classify_table,
    discovered_column_to_type_kwargs,
)

from .. import schema as v2_schema

KEY_STRATEGIES = ("primary", "unique_index", "rrn")

# Champs de dict attendus par colonne pour set_discovered_columns — reflète
# quadringent.snowflake_destination.IbmiColumnType/ColumnDefinition, aucun
# champ ne peut être inventé ici : le worker de découverte ne rapporte
# encore aucun type de colonne (table_discovery.py, métadonnées de table
# seulement), donc ce catalogue est déclaré explicitement par l'appelant.
_COLUMN_TYPE_FIELDS = ("kind", "length", "precision", "scale", "timestamp_precision", "ccsid")


class TableNotFoundError(LookupError):
    """Aucune table pour cet identifiant — 404 ``not_found``."""


class SourceNotFoundError(LookupError):
    """Aucune source pour cet identifiant — 404 ``not_found``."""


class TableValidationError(ValueError):
    """Un champ du corps ``refresh``/``PATCH`` est hors contrat."""


class TableDiscoveryClientProtocol(Protocol):
    """Câblé vers le worker Java réel ailleurs ; les tests injectent un faux client."""

    def discover(
        self, *, libraries: tuple[str, ...] | None, limit: int, search: str | None
    ) -> tuple[DiscoveredTable, ...]:
        ...


@dataclass(frozen=True)
class TableRecord:
    id: str
    source_id: str
    schema_name: str
    table_name: str
    journal_status: str
    readiness: str
    key_strategy: str
    key_columns: tuple[str, ...]
    discovered_row_count: int | None
    discovered_size_bytes: int | None
    journal_library: str | None
    journal_name: str | None
    images: str | None
    cl_fix_commands: list[dict[str, str]]
    discovered_at: str | None
    # 0012_table_columns : colonnes métier déclarées (nom + type IBM i),
    # requises par le chargeur de destination pour générer le DDL
    # historique/miroir. None tant qu'aucun appel à
    # ``set_discovered_columns`` n'a été fait pour cette table.
    discovered_columns: list[dict[str, object]] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "schema_name": self.schema_name,
            "table_name": self.table_name,
            "journal_status": self.journal_status,
            "readiness": self.readiness,
            "key_strategy": self.key_strategy,
            "key_columns": list(self.key_columns),
            "discovered_row_count": self.discovered_row_count,
            "discovered_size_bytes": self.discovered_size_bytes,
            "journal_library": self.journal_library,
            "journal_name": self.journal_name,
            "images": self.images,
            "cl_fix_commands": self.cl_fix_commands,
            "discovered_at": self.discovered_at,
            "discovered_columns": self.discovered_columns,
        }


class TablesService:
    """Façade Postgres/SQLite (SQLAlchemy Core) pour la ressource Table."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _require_source(self, source_id: str) -> None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(v2_schema.sources.c.id).where(v2_schema.sources.c.id == source_id)
            ).first()
        if row is None:
            raise SourceNotFoundError(source_id)

    def list(
        self,
        source_id: str,
        *,
        search: str | None = None,
        library: str | None = None,
        readiness: str | None = None,
    ) -> tuple[TableRecord, ...]:
        self._require_source(source_id)
        query = select(v2_schema.tables).where(v2_schema.tables.c.source_id == source_id)
        if library:
            query = query.where(v2_schema.tables.c.schema_name == library)
        if readiness:
            query = query.where(v2_schema.tables.c.readiness == readiness)
        query = query.order_by(v2_schema.tables.c.schema_name, v2_schema.tables.c.table_name)
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        records = [_to_record(row) for row in rows]
        if search:
            needle = search.strip().upper()
            records = [record for record in records if needle in record.table_name.upper()]
        return tuple(records)

    def get(self, table_id: str) -> TableRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(select(v2_schema.tables).where(v2_schema.tables.c.id == table_id))
                .mappings()
                .first()
            )
        if row is None:
            raise TableNotFoundError(table_id)
        return _to_record(row)

    def refresh(
        self,
        source_id: str,
        *,
        discovery_client: TableDiscoveryClientProtocol,
        libraries: tuple[str, ...] | None = None,
        limit: int = 500,
        search: str | None = None,
        now: datetime | None = None,
    ) -> tuple[TableRecord, ...]:
        """Relance la découverte, classe chaque table et upsert le catalogue local.

        Une table déjà connue (même ``source_id``/``schema_name``/
        ``table_name``) est mise à jour sans perdre son ``key_strategy``
        choisi par un précédent ``PATCH`` — seuls les champs issus du
        catalogue IBM i (readiness, journal, taille…) sont rafraîchis.
        """

        self._require_source(source_id)
        discovered = discovery_client.discover(libraries=libraries, limit=limit, search=search)
        journaled_libraries = {
            table.library for table in discovered if table.journaled
        }
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            for table in discovered:
                readiness_result = classify_table(
                    table, has_journal_in_library=table.library in journaled_libraries
                )
                existing = (
                    connection.execute(
                        select(v2_schema.tables.c.id, v2_schema.tables.c.key_strategy).where(
                            v2_schema.tables.c.source_id == source_id,
                            v2_schema.tables.c.schema_name == table.library,
                            v2_schema.tables.c.table_name == table.system_name,
                        )
                    )
                    .mappings()
                    .first()
                )
                cl_commands = [
                    {"command": cmd.command, "reason": cmd.reason} for cmd in readiness_result.cl_commands
                ]
                values = {
                    "journal_status": "journaled" if table.journaled else "unknown",
                    "discovered_row_count": table.row_count,
                    "discovered_size_bytes": table.size_bytes,
                    "discovered_at": moment,
                    "readiness": readiness_result.state,
                    "key_columns": ",".join(table.key_columns) if table.key_columns else None,
                    "journal_library": table.journal_library,
                    "journal_name": table.journal_name,
                    "images": table.images,
                    "cl_fix_commands": cl_commands,
                }
                # Déclare automatiquement les colonnes que ce refresh vient de
                # trouver, si leur type est entièrement reconnu (voir
                # ``_auto_discovered_columns``) — jamais un écrasement d'une
                # déclaration existante (manuelle ou d'un refresh précédent)
                # par une valeur inconnue : sans correspondance sûre, la clé
                # est simplement omise et la valeur en base reste inchangée.
                auto_columns = _auto_discovered_columns(table.columns)
                if auto_columns is not None:
                    values["discovered_columns"] = auto_columns
                if existing is None:
                    connection.execute(
                        insert(v2_schema.tables),
                        {
                            "id": uuid.uuid4().hex,
                            "source_id": source_id,
                            "schema_name": table.library,
                            "table_name": table.system_name,
                            "key_strategy": "unique_index" if table.has_key else "rrn",
                            **values,
                        },
                    )
                else:
                    connection.execute(
                        update(v2_schema.tables).where(v2_schema.tables.c.id == existing["id"]).values(**values)
                    )
        return self.list(source_id)

    def choose_key(
        self,
        table_id: str,
        *,
        key_strategy: object,
        key_columns: object = None,
        acknowledge_rrn: object = False,
    ) -> TableRecord:
        """Pose ``key_strategy``/``key_columns`` puis recalcule ``readiness``.

        Une table ``no_key`` posait sa clé sans jamais recalculer
        ``readiness`` : elle restait ``no_key`` côté serveur jusqu'au
        prochain ``refresh`` (écart backend documenté). La reclassification
        réutilise ``quadringent.table_discovery.classify_table`` — la même
        fonction que ``refresh`` — pour ne jamais dériver une seconde règle
        de disponibilité : ``has_key`` vaut vrai dès qu'une clé valide vient
        d'être choisie (``primary``/``unique_index`` avec colonnes, ou
        ``rrn`` acquitté), les autres facteurs (journalisation, images)
        restent ceux du dernier ``refresh``.
        """

        if key_strategy not in KEY_STRATEGIES:
            raise TableValidationError(f"key_strategy doit être l'un de {KEY_STRATEGIES}")
        if key_strategy == "rrn" and not acknowledge_rrn:
            raise TableValidationError(
                "key_strategy=rrn exige acknowledge_rrn=true : la table sera identifiée par sa "
                "position physique, une réorganisation (RGZPFM, CLRPFM) exigera une resynchronisation"
            )
        columns: tuple[str, ...] = ()
        if key_strategy in ("primary", "unique_index"):
            if not isinstance(key_columns, list) or not key_columns or not all(
                isinstance(c, str) and c for c in key_columns
            ):
                raise TableValidationError("key_columns doit être une liste non vide de noms de colonnes")
            columns = tuple(key_columns)
        with self._engine.begin() as connection:
            row = (
                connection.execute(select(v2_schema.tables).where(v2_schema.tables.c.id == table_id))
                .mappings()
                .first()
            )
            if row is None:
                raise TableNotFoundError(table_id)
            record = _to_record(row)
            has_key = bool(columns) or (key_strategy == "rrn" and bool(acknowledge_rrn))
            synthetic = DiscoveredTable(
                library=record.schema_name,
                system_name=record.table_name,
                sql_name=record.table_name,
                text=None,
                row_count=record.discovered_row_count,
                size_bytes=record.discovered_size_bytes,
                has_key=has_key,
                key_columns=columns,
                journaled=record.journal_status == "journaled",
                journal_library=record.journal_library,
                journal_name=record.journal_name,
                images=record.images,
                omitted=False,
            )
            readiness_result = classify_table(synthetic, has_journal_in_library=True)
            connection.execute(
                update(v2_schema.tables)
                .where(v2_schema.tables.c.id == table_id)
                .values(
                    key_strategy=key_strategy,
                    key_columns=",".join(columns) if columns else None,
                    readiness=readiness_result.state,
                )
            )
        return self.get(table_id)

    def set_discovered_columns(self, table_id: str, *, columns: object) -> TableRecord:
        """Déclare les colonnes métier (nom + type IBM i) d'une table.

        Aucune découverte automatique de type de colonne n'existe encore
        (``table_discovery.py`` ne rapporte que des métadonnées de table) :
        ``columns`` est donc une déclaration explicite de l'appelant, jamais
        dérivée d'une autre source. Chaque entrée est validée via
        ``quadringent.snowflake_destination.ColumnDefinition``/
        ``IbmiColumnType`` — le même chemin qui validera le DDL généré par
        le chargeur de destination, pour ne jamais accepter ici un type que
        la génération DDL refuserait ensuite.
        """

        validated = _validate_discovered_columns(columns)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(v2_schema.tables)
                .where(v2_schema.tables.c.id == table_id)
                .values(discovered_columns=validated)
            )
            if result.rowcount == 0:
                raise TableNotFoundError(table_id)
        return self.get(table_id)


def _auto_discovered_columns(columns: tuple[DiscoveredColumn, ...]) -> list[dict[str, object]] | None:
    """Déclare automatiquement les colonnes qu'un ``refresh`` vient de trouver.

    Traduit chaque ``DiscoveredColumn`` (type SQL natif du catalogue) en le
    même format que ``set_discovered_columns``/``PUT .../discovered-columns``
    — validé par le même chemin (``IbmiColumnType``/``ColumnDefinition``),
    pour ne jamais accepter ici un type que la génération DDL refuserait
    ensuite. Un seul type non reconnu dans la table entière fait renoncer à
    l'auto-déclaration pour *toute* la table (jamais une déclaration
    partielle silencieuse) : ``discovered_columns`` reste alors ce qu'il
    était (probablement absent), à déclarer via ``PUT`` — la surcharge reste
    toujours possible.
    """

    if not columns:
        return None
    declared: list[dict[str, object]] = []
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            return None
        seen.add(column.name)
        type_kwargs = discovered_column_to_type_kwargs(column)
        if type_kwargs is None:
            return None
        try:
            column_type = IbmiColumnType(**type_kwargs)
            ColumnDefinition(name=column.name, type=column_type, nullable=column.nullable)
        except (ValueError, UnsupportedColumnTypeError):
            return None
        declared.append(
            {
                "name": column.name,
                "kind": column_type.kind,
                "length": column_type.length,
                "precision": column_type.precision,
                "scale": column_type.scale,
                "timestamp_precision": column_type.timestamp_precision,
                "ccsid": column_type.ccsid,
                "nullable": column.nullable,
            }
        )
    return declared


def _validate_discovered_columns(columns: object) -> list[dict[str, object]]:
    if not isinstance(columns, list) or not columns:
        raise TableValidationError("discovered_columns doit être une liste non vide")
    names: set[str] = set()
    validated: list[dict[str, object]] = []
    for entry in columns:
        if not isinstance(entry, dict) or "name" not in entry:
            raise TableValidationError("chaque colonne déclarée doit être un objet avec au moins 'name'")
        name = entry["name"]
        if not isinstance(name, str) or not name:
            raise TableValidationError("le nom de colonne doit être une chaîne non vide")
        if name in names:
            raise TableValidationError(f"colonne déclarée en double : {name!r}")
        names.add(name)
        type_kwargs = {field: entry[field] for field in _COLUMN_TYPE_FIELDS if field in entry}
        try:
            column_type = IbmiColumnType(**type_kwargs)
            ColumnDefinition(name=name, type=column_type, nullable=bool(entry.get("nullable", True)))
        except (ValueError, UnsupportedColumnTypeError) as error:
            raise TableValidationError(f"colonne {name!r} invalide : {error}") from error
        validated.append(
            {
                "name": name,
                "kind": column_type.kind,
                "length": column_type.length,
                "precision": column_type.precision,
                "scale": column_type.scale,
                "timestamp_precision": column_type.timestamp_precision,
                "ccsid": column_type.ccsid,
                "nullable": bool(entry.get("nullable", True)),
            }
        )
    return validated


def _to_record(row: object) -> TableRecord:
    key_columns_raw = row["key_columns"]
    key_columns = tuple(key_columns_raw.split(",")) if key_columns_raw else ()
    cl_fix_commands = row["cl_fix_commands"] or []
    return TableRecord(
        id=row["id"],
        source_id=row["source_id"],
        schema_name=row["schema_name"],
        table_name=row["table_name"],
        journal_status=row["journal_status"],
        readiness=row["readiness"],
        key_strategy=row["key_strategy"],
        key_columns=key_columns,
        discovered_row_count=row["discovered_row_count"],
        discovered_size_bytes=row["discovered_size_bytes"],
        journal_library=row["journal_library"],
        journal_name=row["journal_name"],
        images=row["images"],
        cl_fix_commands=cl_fix_commands,
        discovered_at=_iso(row["discovered_at"]),
        discovered_columns=row["discovered_columns"],
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()
