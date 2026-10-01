"""Plan de flotte DEV, dérivé d'un catalogue metadata-only fail-closed."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from quadringent.site_config import SiteConfig
from quadringent.site_config import current as _current_site
from quadringent_control_plane import fleet as _fleet
from quadringent_control_plane.fleet import (
    MAX_CONCURRENCY,
    MIN_CONCURRENCY,
    FleetError,
    JournalCheckpoint,
)


def _site() -> SiteConfig:
    return _current_site()


def _source_schema() -> str:
    """Bibliothèque source déclarée — jamais un défaut d'installation."""

    return _site().source_schema


def __getattr__(name: str):
    """Compatibilité paresseuse : les identités de site se résolvent à l'appel."""

    if name == "SOURCE_SCHEMA":
        return _source_schema()
    raise AttributeError(name)


CATALOG_FORMAT_VERSION = "quadringent-fleet-catalog-v1"
PLAN_FORMAT_VERSION = "quadringent-fleet-plan-v1"
READER_KIND = "multi_object"
IDENTITY_KEYED = "keyed"
IDENTITY_RRN = "rrn"
IDENTITY_BLOCKED = "blocked"
RRN_IDENTITY_COLUMN = "_rrn"
RRN_IDENTITY_KEY = (RRN_IDENTITY_COLUMN,)
CONTINUITY_PROVEN = "proven"
CONTINUITY_UNCERTAIN = "uncertain"
CONTINUITY_BROKEN = "broken"
ALLOWED_CONTINUITY = frozenset({CONTINUITY_PROVEN, CONTINUITY_UNCERTAIN, CONTINUITY_BROKEN})
ALLOWED_JOURNAL_IMAGES = frozenset({"*AFTER", "*BOTH", "*BEFORE"})
PRIMARY_TYPES = frozenset({"PRIMARY", "PRIMARY KEY", "PRIMARY_KEY"})
UNIQUE_TYPES = frozenset({"UNIQUE", *PRIMARY_TYPES})
ATTACHED_STATUS = "ATTACHED"

_CATALOG_KEYS = (
    "format_version",
    "observed_at",
    "environment",
    "source_schema",
    "journals",
    "tables",
)
_JOURNAL_KEYS = ("library", "name", "continuity", "receivers")
_RECEIVER_KEYS = (
    "library",
    "name",
    "status",
    "first_sequence",
    "last_sequence",
    "attach_timestamp",
    "detach_timestamp",
    "previous_library",
    "previous_name",
)
_TABLE_KEYS = (
    "name",
    "row_count",
    "data_size",
    "member_count",
    "journal_library",
    "journal_name",
    "journal_images",
    "columns",
    "constraints",
    "indexes",
)
_COLUMN_KEYS = (
    "name",
    "type",
    "length",
    "numeric_precision",
    "numeric_scale",
    "ccsid",
    "nullable",
    "ordinal",
)
_CONSTRAINT_KEYS = ("schema", "name", "type", "columns")
_INDEX_KEYS = ("schema", "name", "unique", "sparse", "select_omit", "columns")
_INDEX_COLUMN_KEYS = ("name", "ordinal", "ordering")
_PLAN_KEYS = (
    "format_version",
    "environment",
    "source_schema",
    "destination_namespace",
    "observed_at",
    "max_concurrency",
    "historical_byte_budget",
    "observed_row_count",
    "observed_data_size",
    "cutover_checkpoint",
    "cutover_required_before_history",
    "continuity",
    "live_blocked",
    "certification_blocked",
    "journal_groups",
    "historical_lanes",
    "tables",
)
_GROUP_KEYS = (
    "library",
    "name",
    "reader_kind",
    "reader_count",
    "table_names",
    "continuity",
)
_LANE_KEYS = ("slot", "tables", "row_count", "data_size")
_TABLE_PLAN_KEYS = (
    "name",
    "row_count",
    "data_size",
    "journal_library",
    "journal_name",
    "journal_images",
    "identity_status",
    "identity_source",
    "candidate_key",
    "certification_possible",
    "live_possible",
    "blocked_reasons",
    "historical_lane",
)
_CHECKPOINT_KEYS = ("receiver", "sequence")
_JSON_SCALARS = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class CatalogColumn:
    name: str
    type: str
    length: int
    numeric_precision: int | None
    numeric_scale: int | None
    ccsid: int | None
    nullable: bool
    ordinal: int


@dataclass(frozen=True)
class CatalogConstraint:
    schema: str
    name: str
    type: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class CatalogIndexColumn:
    name: str
    ordinal: int
    ordering: str


@dataclass(frozen=True)
class CatalogIndex:
    schema: str
    name: str
    unique: bool
    sparse: bool
    select_omit: str | None
    columns: tuple[CatalogIndexColumn, ...]


@dataclass(frozen=True)
class CatalogReceiver:
    library: str
    name: str
    status: str
    first_sequence: int
    last_sequence: int
    attach_timestamp: str
    detach_timestamp: str | None
    previous_library: str | None
    previous_name: str | None


@dataclass(frozen=True)
class CatalogJournal:
    library: str
    name: str
    continuity: str
    receivers: tuple[CatalogReceiver, ...]


@dataclass(frozen=True)
class CatalogTable:
    name: str
    row_count: int
    data_size: int
    member_count: int
    journal_library: str
    journal_name: str
    journal_images: str
    columns: tuple[CatalogColumn, ...]
    constraints: tuple[CatalogConstraint, ...]
    indexes: tuple[CatalogIndex, ...]


@dataclass(frozen=True)
class FleetCatalog:
    format_version: str
    observed_at: str
    environment: str
    source_schema: str
    journals: tuple[CatalogJournal, ...]
    tables: tuple[CatalogTable, ...]

    def __post_init__(self) -> None:
        if self.format_version != CATALOG_FORMAT_VERSION:
            raise FleetError("invalid_catalog", "Version de catalogue inconnue")
        if self.environment != _fleet.ENVIRONMENT:
            raise FleetError("invalid_environment", "Environnement hors site déclaré")
        if self.source_schema != _source_schema():
            raise FleetError("invalid_source", "Source hors périmètre déclaré")
        _require_token(self.observed_at, "invalid_catalog", "Horodatage de catalogue invalide")
        if type(self.journals) is not tuple or len(self.journals) != 1:
            raise FleetError("invalid_journal", "Un seul journal de flotte est exigé")
        journal = self.journals[0]
        if type(journal) is not CatalogJournal:
            raise FleetError("invalid_journal", "Journal de catalogue invalide")
        if type(self.tables) is not tuple or len(self.tables) != _fleet.TABLE_COUNT:
            raise FleetError("invalid_manifest", "Manifeste du site incomplet")
        names = tuple(table.name for table in self.tables)
        if names != _fleet.MANIFEST:
            raise FleetError("invalid_manifest", "Manifeste du site hors ordre")
        attached = [receiver for receiver in journal.receivers if receiver.status == ATTACHED_STATUS]
        if len(attached) != 1:
            raise FleetError("invalid_checkpoint", "Receiver attaché de cutover absent")
        for table in self.tables:
            if type(table) is not CatalogTable:
                raise FleetError("invalid_manifest", "Table de catalogue invalide")
            if table.journal_library != journal.library or table.journal_name != journal.name:
                raise FleetError("invalid_journal", "Table hors journal unique")

    @property
    def observed_row_count(self) -> int:
        return sum(table.row_count for table in self.tables)

    @property
    def observed_data_size(self) -> int:
        return sum(table.data_size for table in self.tables)


@dataclass(frozen=True)
class IdentityChoice:
    status: str
    source: str | None
    columns: tuple[str, ...] | None


@dataclass(frozen=True)
class JournalGroupPlan:
    library: str
    name: str
    reader_kind: str
    reader_count: int
    table_names: tuple[str, ...]
    continuity: str

    def __post_init__(self) -> None:
        _require_token(self.library, "invalid_journal", "Bibliothèque de journal invalide")
        _require_token(self.name, "invalid_journal", "Nom de journal invalide")
        if self.reader_kind != READER_KIND:
            raise FleetError("invalid_journal", "Lecteur journal non multi-objets")
        _require_int(self.reader_count, "invalid_journal", "Nombre de lecteurs invalide")
        if self.reader_count != 1:
            raise FleetError("invalid_journal", "Un seul lecteur journal est autorisé")
        if self.table_names != _fleet.MANIFEST:
            raise FleetError("invalid_manifest", "Groupe journal hors manifeste")
        if self.continuity not in ALLOWED_CONTINUITY:
            raise FleetError("invalid_journal", "Continuité de journal inconnue")

    def to_dict(self) -> dict[str, object]:
        return {
            "library": self.library,
            "name": self.name,
            "reader_kind": self.reader_kind,
            "reader_count": self.reader_count,
            "table_names": list(self.table_names),
            "continuity": self.continuity,
        }


@dataclass(frozen=True)
class HistoricalLane:
    slot: int
    tables: tuple[str, ...]
    row_count: int
    data_size: int

    def __post_init__(self) -> None:
        _require_int(self.slot, "invalid_concurrency", "Slot historique invalide")
        if self.slot < MIN_CONCURRENCY or self.slot > MAX_CONCURRENCY:
            raise FleetError("invalid_concurrency", "Slot historique hors borne 1..4")
        if type(self.tables) is not tuple or not self.tables:
            raise FleetError("invalid_lane", "Voie historique vide")
        seen: set[str] = set()
        for name in self.tables:
            if name not in _fleet.MANIFEST:
                raise FleetError("unknown_table", "Table hors manifeste")
            if name in seen:
                raise FleetError("duplicate_table", "Table historique dupliquée")
            seen.add(name)
        _require_int(self.row_count, "invalid_lane", "Volume de voie invalide")
        _require_int(self.data_size, "invalid_lane", "Taille de voie invalide")
        if self.row_count < 0 or self.data_size < 0:
            raise FleetError("invalid_lane", "Volume de voie négatif")

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "tables": list(self.tables),
            "row_count": self.row_count,
            "data_size": self.data_size,
        }


def lane_composition(
    lanes: tuple[HistoricalLane, ...],
) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Identité d'affectation des voies : slot et tables, sans les volumes.

    `row_count`/`data_size` sont des estimations re-mesurées à chaque
    relevé catalogue : elles avancent sans changer l'intention de
    lancement. Deux plans qui ne diffèrent que par ces volumes portent la
    même composition — les reçus persistés et la garde de swap doivent
    comparer la composition, jamais l'objet entier.
    """

    return tuple((lane.slot, lane.tables) for lane in lanes)


@dataclass(frozen=True)
class TablePlan:
    name: str
    row_count: int
    data_size: int
    journal_library: str
    journal_name: str
    journal_images: str
    identity_status: str
    identity_source: str | None
    candidate_key: tuple[str, ...] | None
    certification_possible: bool
    live_possible: bool
    blocked_reasons: tuple[str, ...]
    historical_lane: int | None

    def __post_init__(self) -> None:
        if self.name not in _fleet.MANIFEST:
            raise FleetError("unknown_table", "Table hors manifeste")
        _require_int(self.row_count, "invalid_catalog", "Nombre de lignes invalide")
        _require_int(self.data_size, "invalid_catalog", "Taille observée invalide")
        if self.row_count < 0 or self.data_size < 0:
            raise FleetError("invalid_catalog", "Volume observé négatif")
        _require_token(self.journal_library, "invalid_journal", "Bibliothèque de journal invalide")
        _require_token(self.journal_name, "invalid_journal", "Nom de journal invalide")
        if self.journal_images not in ALLOWED_JOURNAL_IMAGES:
            raise FleetError("invalid_journal", "Images de journal inconnues")
        if self.identity_status not in {IDENTITY_KEYED, IDENTITY_RRN, IDENTITY_BLOCKED}:
            raise FleetError("invalid_identity", "Statut d'identité inconnu")
        if self.identity_status == IDENTITY_KEYED:
            if self.identity_source is None or self.candidate_key is None or not self.candidate_key:
                raise FleetError("invalid_identity", "Clé candidate absente")
            if any(column in {"RRN", "HASH", "hash", "rrn", RRN_IDENTITY_COLUMN} for column in self.candidate_key):
                raise FleetError("invalid_identity", "Clé candidate inventée")
        elif self.identity_status == IDENTITY_RRN:
            # L'identite physique n'a qu'une forme : le RRN du journal, porte
            # par le champ reserve. Toute autre declaration est inventee.
            if self.identity_source != "journal_rrn" or self.candidate_key != RRN_IDENTITY_KEY:
                raise FleetError("invalid_identity", "Identité RSN mal déclarée")
        else:
            if self.identity_source is not None or self.candidate_key is not None:
                raise FleetError("invalid_identity", "Clé candidate résiduelle")
            if self.live_possible or self.certification_possible:
                raise FleetError("invalid_identity", "Certification impossible sans clé unique")
        if type(self.certification_possible) is not bool or type(self.live_possible) is not bool:
            raise FleetError("invalid_identity", "Indicateur d'identité invalide")
        if type(self.blocked_reasons) is not tuple:
            raise FleetError("invalid_identity", "Raisons de blocage invalides")
        if self.historical_lane is not None:
            _require_int(self.historical_lane, "invalid_concurrency", "Voie historique invalide")
            if self.historical_lane < MIN_CONCURRENCY or self.historical_lane > MAX_CONCURRENCY:
                raise FleetError("invalid_concurrency", "Voie historique hors borne 1..4")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "data_size": self.data_size,
            "journal_library": self.journal_library,
            "journal_name": self.journal_name,
            "journal_images": self.journal_images,
            "identity_status": self.identity_status,
            "identity_source": self.identity_source,
            "candidate_key": None if self.candidate_key is None else list(self.candidate_key),
            "certification_possible": self.certification_possible,
            "live_possible": self.live_possible,
            "blocked_reasons": list(self.blocked_reasons),
            "historical_lane": self.historical_lane,
        }


@dataclass(frozen=True)
class FleetPlan:
    environment: str
    source_schema: str
    destination_namespace: str
    observed_at: str
    max_concurrency: int
    historical_byte_budget: int | None
    observed_row_count: int
    observed_data_size: int
    cutover_checkpoint: JournalCheckpoint
    cutover_required_before_history: bool
    continuity: str
    live_blocked: bool
    certification_blocked: bool
    journal_groups: tuple[JournalGroupPlan, ...]
    historical_lanes: tuple[HistoricalLane, ...]
    tables: tuple[TablePlan, ...]

    def __post_init__(self) -> None:
        if self.environment != _fleet.ENVIRONMENT:
            raise FleetError("invalid_environment", "Environnement hors site déclaré")
        if self.source_schema != _source_schema():
            raise FleetError("invalid_source", "Source hors périmètre déclaré")
        if self.destination_namespace != _fleet.DESTINATION_NAMESPACE:
            raise FleetError("invalid_destination", "Espace de destination hors site déclaré")
        _require_token(self.observed_at, "invalid_catalog", "Horodatage de plan invalide")
        _require_int(self.max_concurrency, "invalid_concurrency", "Concurrence invalide")
        if self.max_concurrency < MIN_CONCURRENCY or self.max_concurrency > MAX_CONCURRENCY:
            raise FleetError("invalid_concurrency", "Concurrence hors borne 1..4")
        if self.historical_byte_budget is not None:
            object.__setattr__(
                self,
                "historical_byte_budget",
                _require_byte_budget(self.historical_byte_budget),
            )
        _require_int(self.observed_row_count, "invalid_catalog", "Total de lignes invalide")
        _require_int(self.observed_data_size, "invalid_catalog", "Total d'octets invalide")
        if type(self.cutover_checkpoint) is not JournalCheckpoint:
            raise FleetError("invalid_checkpoint", "Checkpoint de cutover invalide")
        if self.cutover_required_before_history is not True:
            raise FleetError("missing_start_checkpoint", "Cutover obligatoire avant toute histoire")
        if self.continuity not in ALLOWED_CONTINUITY:
            raise FleetError("invalid_journal", "Continuité de journal inconnue")
        if type(self.live_blocked) is not bool or type(self.certification_blocked) is not bool:
            raise FleetError("invalid_journal", "Indicateur de continuité invalide")
        continuity_blocks = self.continuity != CONTINUITY_PROVEN
        if self.live_blocked is not continuity_blocks or self.certification_blocked is not continuity_blocks:
            raise FleetError("invalid_journal", "Blocage live/certifié incohérent")
        if type(self.journal_groups) is not tuple or len(self.journal_groups) != 1:
            raise FleetError("invalid_journal", "Un seul groupe journal est autorisé")
        group = self.journal_groups[0]
        if type(group) is not JournalGroupPlan:
            raise FleetError("invalid_journal", "Groupe journal invalide")
        if group.continuity != self.continuity:
            raise FleetError("invalid_journal", "Continuité de groupe incohérente")
        if type(self.tables) is not tuple or len(self.tables) != _fleet.TABLE_COUNT:
            raise FleetError("invalid_manifest", "Manifeste du site incomplet")
        names = tuple(table.name for table in self.tables)
        if names != _fleet.MANIFEST:
            raise FleetError("invalid_manifest", "Manifeste du site hors ordre")
        derived_rows = sum(table.row_count for table in self.tables)
        derived_size = sum(table.data_size for table in self.tables)
        if self.observed_row_count != derived_rows or self.observed_data_size != derived_size:
            raise FleetError("invalid_catalog", "Totaux observés incohérents")
        if type(self.historical_lanes) is not tuple:
            raise FleetError("invalid_lane", "Voies historiques invalides")
        if len(self.historical_lanes) > self.max_concurrency:
            raise FleetError("concurrency_exceeded", "Concurrence historique dépassée")
        assigned: list[str] = []
        consumed_bytes = 0
        for index, lane in enumerate(self.historical_lanes):
            if type(lane) is not HistoricalLane:
                raise FleetError("invalid_lane", "Voie historique invalide")
            if lane.slot != index + 1:
                raise FleetError("invalid_lane", "Slots historiques non contigus")
            lane_rows = 0
            lane_size = 0
            for name in lane.tables:
                table = self.tables[_fleet.MANIFEST.index(name)]
                if table.historical_lane != lane.slot:
                    raise FleetError("invalid_lane", "Affectation de voie incohérente")
                if table.journal_library != group.library or table.journal_name != group.name:
                    raise FleetError("invalid_journal", "Table hors journal unique")
                lane_rows += table.row_count
                lane_size += table.data_size
                consumed_bytes += table.data_size
                assigned.append(name)
            if lane.row_count != lane_rows or lane.data_size != lane_size:
                raise FleetError("invalid_lane", "Volume de voie incohérent")
        if len(assigned) != len(set(assigned)):
            raise FleetError("duplicate_table", "Table historique dupliquée")
        if self.historical_byte_budget is not None and consumed_bytes > self.historical_byte_budget:
            raise FleetError("admission_refused", "Budget d'octets historique dépassé")
        assigned_set = set(assigned)
        for table in self.tables:
            if type(table) is not TablePlan:
                raise FleetError("invalid_manifest", "Plan de table invalide")
            if table.journal_library != group.library or table.journal_name != group.name:
                raise FleetError("invalid_journal", "Table hors journal unique")
            if table.historical_lane is None:
                if table.name in assigned_set:
                    raise FleetError("invalid_lane", "Table exclue encore affectée")
            elif table.name not in assigned_set:
                raise FleetError("invalid_lane", "Table affectée absente des voies")
            if table.live_possible and self.live_blocked:
                raise FleetError("unproven_continuity", "Passage live bloqué par la continuité")
            if table.certification_possible and self.certification_blocked:
                raise FleetError("unproven_continuity", "Certification bloquée par la continuité")
            if table.identity_status == IDENTITY_BLOCKED and table.certification_possible:
                raise FleetError("invalid_identity", "Certification impossible sans clé unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": PLAN_FORMAT_VERSION,
            "environment": self.environment,
            "source_schema": self.source_schema,
            "destination_namespace": self.destination_namespace,
            "observed_at": self.observed_at,
            "max_concurrency": self.max_concurrency,
            "historical_byte_budget": self.historical_byte_budget,
            "observed_row_count": self.observed_row_count,
            "observed_data_size": self.observed_data_size,
            "cutover_checkpoint": self.cutover_checkpoint.to_dict(),
            "cutover_required_before_history": self.cutover_required_before_history,
            "continuity": self.continuity,
            "live_blocked": self.live_blocked,
            "certification_blocked": self.certification_blocked,
            "journal_groups": [group.to_dict() for group in self.journal_groups],
            "historical_lanes": [lane.to_dict() for lane in self.historical_lanes],
            "tables": [table.to_dict() for table in self.tables],
        }


def parse_fleet_catalog(payload: Mapping[str, object]) -> FleetCatalog:
    data = _closed_mapping(payload, _CATALOG_KEYS, code="invalid_catalog")
    if data["format_version"] != CATALOG_FORMAT_VERSION:
        raise FleetError("invalid_catalog", "Version de catalogue inconnue")
    if data["environment"] != _fleet.ENVIRONMENT:
        raise FleetError("invalid_environment", "Environnement hors site déclaré")
    if data["source_schema"] != _source_schema():
        raise FleetError("invalid_source", "Source hors périmètre déclaré")
    journals_payload = data["journals"]
    tables_payload = data["tables"]
    if type(journals_payload) is not list or type(tables_payload) is not list:
        raise FleetError("invalid_catalog", "Catalogue JSON non autorisé")
    journals = tuple(_parse_journal(item) for item in journals_payload)
    tables = tuple(_parse_table(item) for item in tables_payload)
    _reject_duplicates([journal.library + "/" + journal.name for journal in journals], "duplicate_journal")
    _reject_duplicates([table.name for table in tables], "duplicate_table")
    return FleetCatalog(
        format_version=_require_str(data["format_version"], "invalid_catalog", "Version de catalogue invalide"),
        observed_at=_require_token(data["observed_at"], "invalid_catalog", "Horodatage de catalogue invalide"),
        environment=_require_str(data["environment"], "invalid_environment", "Environnement invalide"),
        source_schema=_require_str(data["source_schema"], "invalid_source", "Source invalide"),
        journals=journals,
        tables=tables,
    )


def build_fleet_plan(
    catalog: FleetCatalog,
    *,
    max_concurrency: int = MAX_CONCURRENCY,
    historical_byte_budget: int | None = None,
) -> FleetPlan:
    if type(catalog) is not FleetCatalog:
        raise FleetError("invalid_catalog", "Catalogue de flotte invalide")
    _require_int(max_concurrency, "invalid_concurrency", "Concurrence invalide")
    if max_concurrency < MIN_CONCURRENCY or max_concurrency > MAX_CONCURRENCY:
        raise FleetError("invalid_concurrency", "Concurrence hors borne 1..4")
    budget = None if historical_byte_budget is None else _require_byte_budget(historical_byte_budget)
    journal = catalog.journals[0]
    cutover = _cutover_checkpoint(journal)
    continuity_blocks = journal.continuity != CONTINUITY_PROVEN
    identities = {table.name: _choose_identity(table) for table in catalog.tables}
    admitted = _admit_historical(catalog.tables, budget)
    lanes = _assign_lanes(admitted, max_concurrency)
    lane_by_table = {
        name: lane.slot for lane in lanes for name in lane.tables
    }
    table_plans = []
    for table in catalog.tables:
        identity = identities[table.name]
        reasons: list[str] = []
        if identity.status == IDENTITY_BLOCKED:
            # Seul *BEFORE mene ici : ni la cle ni le RRN ne peuvent
            # reconstruire un update sans image "apres".
            reasons.append(
                "unsupported_journal_images" if table.journal_images == "*BEFORE" else "missing_unique_key"
            )
        if continuity_blocks:
            reasons.append("uncertain_continuity" if journal.continuity == CONTINUITY_UNCERTAIN else "broken_continuity")
        if table.name not in lane_by_table:
            reasons.append("byte_budget_excluded")
        # Une table est capturable des qu'elle a une identite exploitable :
        # cle metier quand elle existe, position physique (RRN) sinon ou sous
        # *AFTER. Seul le mode *BEFORE reste sans recours.
        addressable = identity.status in {IDENTITY_KEYED, IDENTITY_RRN}
        live_possible = addressable and not continuity_blocks
        table_plans.append(
            TablePlan(
                name=table.name,
                row_count=table.row_count,
                data_size=table.data_size,
                journal_library=table.journal_library,
                journal_name=table.journal_name,
                journal_images=table.journal_images,
                identity_status=identity.status,
                identity_source=identity.source,
                candidate_key=identity.columns,
                certification_possible=live_possible,
                live_possible=live_possible,
                blocked_reasons=tuple(reasons),
                historical_lane=lane_by_table.get(table.name),
            )
        )
    return FleetPlan(
        environment=_fleet.ENVIRONMENT,
        source_schema=_source_schema(),
        destination_namespace=_fleet.DESTINATION_NAMESPACE,
        observed_at=catalog.observed_at,
        max_concurrency=max_concurrency,
        historical_byte_budget=budget,
        observed_row_count=catalog.observed_row_count,
        observed_data_size=catalog.observed_data_size,
        cutover_checkpoint=cutover,
        cutover_required_before_history=True,
        continuity=journal.continuity,
        live_blocked=continuity_blocks,
        certification_blocked=continuity_blocks,
        journal_groups=(
            JournalGroupPlan(
                library=journal.library,
                name=journal.name,
                reader_kind=READER_KIND,
                reader_count=1,
                table_names=_fleet.MANIFEST,
                continuity=journal.continuity,
            ),
        ),
        historical_lanes=lanes,
        tables=tuple(table_plans),
    )


def serialize_fleet_plan(plan: FleetPlan) -> dict[str, object]:
    if type(plan) is not FleetPlan:
        raise FleetError("invalid_serialization", "Plan de flotte invalide")
    payload = plan.to_dict()
    _assert_json_safe(payload)
    return payload


def deserialize_fleet_plan(payload: Mapping[str, object]) -> FleetPlan:
    data = _closed_mapping(payload, _PLAN_KEYS, code="invalid_serialization")
    if data["format_version"] != PLAN_FORMAT_VERSION:
        raise FleetError("invalid_serialization", "Version de plan inconnue")
    groups_payload = data["journal_groups"]
    lanes_payload = data["historical_lanes"]
    tables_payload = data["tables"]
    if type(groups_payload) is not list or type(lanes_payload) is not list or type(tables_payload) is not list:
        raise FleetError("invalid_serialization", "Plan JSON non autorisé")
    return FleetPlan(
        environment=_require_str(data["environment"], "invalid_environment", "Environnement invalide"),
        source_schema=_require_str(data["source_schema"], "invalid_source", "Source invalide"),
        destination_namespace=_require_str(
            data["destination_namespace"], "invalid_destination", "Destination invalide"
        ),
        observed_at=_require_token(data["observed_at"], "invalid_catalog", "Horodatage de plan invalide"),
        max_concurrency=data["max_concurrency"],
        historical_byte_budget=data["historical_byte_budget"],
        observed_row_count=data["observed_row_count"],
        observed_data_size=data["observed_data_size"],
        cutover_checkpoint=_deserialize_checkpoint(data["cutover_checkpoint"]),
        cutover_required_before_history=data["cutover_required_before_history"],
        continuity=_require_str(data["continuity"], "invalid_journal", "Continuité invalide"),
        live_blocked=data["live_blocked"],
        certification_blocked=data["certification_blocked"],
        journal_groups=tuple(_deserialize_group(item) for item in groups_payload),
        historical_lanes=tuple(_deserialize_lane(item) for item in lanes_payload),
        tables=tuple(_deserialize_table_plan(item) for item in tables_payload),
    )


def _parse_journal(payload: object) -> CatalogJournal:
    data = _closed_mapping(payload, _JOURNAL_KEYS, code="invalid_journal")
    receivers_payload = data["receivers"]
    if type(receivers_payload) is not list or not receivers_payload:
        raise FleetError("invalid_journal", "Receivers de journal absents")
    receivers = tuple(_parse_receiver(item) for item in receivers_payload)
    _reject_duplicates([receiver.name for receiver in receivers], "duplicate_receiver")
    continuity = _require_token(data["continuity"], "invalid_journal", "Continuité de journal invalide")
    if continuity not in ALLOWED_CONTINUITY:
        raise FleetError("invalid_journal", "Continuité de journal inconnue")
    return CatalogJournal(
        library=_require_token(data["library"], "invalid_journal", "Bibliothèque de journal invalide"),
        name=_require_token(data["name"], "invalid_journal", "Nom de journal invalide"),
        continuity=continuity,
        receivers=receivers,
    )


def _parse_receiver(payload: object) -> CatalogReceiver:
    data = _closed_mapping(payload, _RECEIVER_KEYS, code="invalid_journal")
    first_sequence = _require_sequence(data["first_sequence"], "invalid_journal", "Séquence de receiver invalide")
    last_sequence = _require_sequence(data["last_sequence"], "invalid_journal", "Séquence de receiver invalide")
    if last_sequence < first_sequence:
        raise FleetError("invalid_journal", "Borne de receiver invalide")
    detach = data["detach_timestamp"]
    previous_library = data["previous_library"]
    previous_name = data["previous_name"]
    if detach is not None:
        detach = _require_token(detach, "invalid_journal", "Détachement de receiver invalide")
    if previous_library is not None:
        previous_library = _require_token(previous_library, "invalid_journal", "Receiver précédent invalide")
    if previous_name is not None:
        previous_name = _require_token(previous_name, "invalid_journal", "Receiver précédent invalide")
    return CatalogReceiver(
        library=_require_token(data["library"], "invalid_journal", "Bibliothèque de receiver invalide"),
        name=_require_token(data["name"], "invalid_journal", "Nom de receiver invalide"),
        status=_require_token(data["status"], "invalid_journal", "Statut de receiver invalide"),
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        attach_timestamp=_require_token(data["attach_timestamp"], "invalid_journal", "Attachement de receiver invalide"),
        detach_timestamp=detach,
        previous_library=previous_library,
        previous_name=previous_name,
    )


def _parse_table(payload: object) -> CatalogTable:
    data = _closed_mapping(payload, _TABLE_KEYS, code="invalid_catalog")
    columns_payload = data["columns"]
    constraints_payload = data["constraints"]
    indexes_payload = data["indexes"]
    if type(columns_payload) is not list or not columns_payload:
        raise FleetError("invalid_catalog", "Colonnes de table absentes")
    if type(constraints_payload) is not list or type(indexes_payload) is not list:
        raise FleetError("invalid_catalog", "Métadonnées de table invalides")
    columns = tuple(_parse_column(item) for item in columns_payload)
    _reject_duplicates([column.name for column in columns], "duplicate_column")
    ordinals = tuple(column.ordinal for column in columns)
    if ordinals != tuple(range(1, len(columns) + 1)):
        raise FleetError("invalid_catalog", "Ordinaux de colonnes invalides")
    constraints = tuple(_parse_constraint(item, {column.name for column in columns}) for item in constraints_payload)
    _reject_duplicates([constraint.name for constraint in constraints], "duplicate_constraint")
    indexes = tuple(_parse_index(item, {column.name for column in columns}) for item in indexes_payload)
    _reject_duplicates([index.name for index in indexes], "duplicate_index")
    return CatalogTable(
        name=_require_token(data["name"], "unknown_table", "Table hors manifeste"),
        row_count=_require_count(data["row_count"], "Nombre de lignes invalide"),
        data_size=_require_count(data["data_size"], "Taille observée invalide"),
        member_count=_require_count(data["member_count"], "Nombre de membres invalide"),
        journal_library=_require_token(data["journal_library"], "invalid_journal", "Bibliothèque de journal invalide"),
        journal_name=_require_token(data["journal_name"], "invalid_journal", "Nom de journal invalide"),
        journal_images=_require_journal_images(data["journal_images"]),
        columns=columns,
        constraints=constraints,
        indexes=indexes,
    )


def _parse_column(payload: object) -> CatalogColumn:
    data = _closed_mapping(payload, _COLUMN_KEYS, code="invalid_catalog")
    return CatalogColumn(
        name=_require_token(data["name"], "invalid_catalog", "Nom de colonne invalide"),
        type=_require_token(data["type"], "invalid_catalog", "Type de colonne invalide"),
        length=_require_count(data["length"], "Longueur de colonne invalide"),
        numeric_precision=_optional_count(data["numeric_precision"], "Précision numérique invalide"),
        numeric_scale=_optional_count(data["numeric_scale"], "Échelle numérique invalide"),
        ccsid=_optional_count(data["ccsid"], "CCSID invalide"),
        nullable=_require_bool(data["nullable"], "invalid_catalog", "Nullabilité invalide"),
        ordinal=_require_positive_int(data["ordinal"], "Ordinal de colonne invalide"),
    )


def _parse_constraint(payload: object, column_names: set[str]) -> CatalogConstraint:
    data = _closed_mapping(payload, _CONSTRAINT_KEYS, code="invalid_catalog")
    columns_payload = data["columns"]
    if type(columns_payload) is not list or not columns_payload:
        raise FleetError("invalid_identity", "Contrainte sans colonnes")
    columns = tuple(
        _require_token(column, "invalid_identity", "Colonne de contrainte invalide") for column in columns_payload
    )
    _reject_duplicates(list(columns), "duplicate_column")
    for column in columns:
        if column not in column_names:
            raise FleetError("invalid_identity", "Contrainte hors colonnes observées")
    return CatalogConstraint(
        schema=_require_token(data["schema"], "invalid_catalog", "Schéma de contrainte invalide"),
        name=_require_token(data["name"], "invalid_catalog", "Nom de contrainte invalide"),
        type=_require_token(data["type"], "invalid_identity", "Type de contrainte invalide"),
        columns=columns,
    )


def _parse_index(payload: object, column_names: set[str]) -> CatalogIndex:
    data = _closed_mapping(payload, _INDEX_KEYS, code="invalid_catalog")
    columns_payload = data["columns"]
    if type(columns_payload) is not list or not columns_payload:
        raise FleetError("invalid_identity", "Index sans colonnes")
    columns = tuple(_parse_index_column(item) for item in columns_payload)
    _reject_duplicates([column.name for column in columns], "duplicate_column")
    ordinals = tuple(column.ordinal for column in columns)
    if ordinals != tuple(range(1, len(columns) + 1)):
        raise FleetError("invalid_identity", "Ordinaux d'index invalides")
    for column in columns:
        if column.name not in column_names:
            raise FleetError("invalid_identity", "Index hors colonnes observées")
    select_omit = data["select_omit"]
    if select_omit is not None:
        select_omit = _require_token(select_omit, "invalid_identity", "SELECT/OMIT d'index invalide")
    return CatalogIndex(
        schema=_require_token(data["schema"], "invalid_catalog", "Schéma d'index invalide"),
        name=_require_token(data["name"], "invalid_catalog", "Nom d'index invalide"),
        unique=_require_bool(data["unique"], "invalid_identity", "Indicateur d'unicité invalide"),
        sparse=_require_bool(data["sparse"], "invalid_identity", "Indicateur sparse invalide"),
        select_omit=select_omit,
        columns=columns,
    )


def _parse_index_column(payload: object) -> CatalogIndexColumn:
    data = _closed_mapping(payload, _INDEX_COLUMN_KEYS, code="invalid_catalog")
    ordering = _require_token(data["ordering"], "invalid_identity", "Ordre d'index invalide")
    if ordering not in {"A", "D"}:
        raise FleetError("invalid_identity", "Ordre d'index inconnu")
    return CatalogIndexColumn(
        name=_require_token(data["name"], "invalid_identity", "Colonne d'index invalide"),
        ordinal=_require_positive_int(data["ordinal"], "Ordinal d'index invalide"),
        ordering=ordering,
    )


def table_merge_key(catalog: FleetCatalog, table: str) -> tuple[str, ...]:
    """Clé canonique d'une table, exactement celle que le plan déclare.

    Les scripts de rejeu l'utilisent au lieu de deviner la clé depuis le
    catalogue : sous ``*AFTER`` ou sans clé métier, la clé est la position
    physique ``("_rrn",)``, pas un index unique qui ne pourrait pas être
    cité par un delete.
    """

    name = str(table).strip().upper()
    entry = next((item for item in catalog.tables if item.name == name), None)
    if entry is None:
        raise FleetError("invalid_table", "Table absente du catalogue")
    identity = _choose_identity(entry)
    if identity.columns is None:
        raise FleetError(
            "invalid_identity",
            "Table sans identité exploitable (journal en *BEFORE)",
        )
    return identity.columns


def _choose_identity(table: CatalogTable) -> IdentityChoice:
    # *BEFORE ne journalise que l'ancien etat des lignes : un update n'y a
    # pas d'image "apres", donc la ligne nouvelle n'est pas reconstructible.
    # Aucun mode d'identite ne peut sauver ce cas.
    if table.journal_images == "*BEFORE":
        return IdentityChoice(status=IDENTITY_BLOCKED, source=None, columns=None)
    # Sous *AFTER, un delete n'a pas d'image du tout : il ne porte que la
    # position physique (RRN) dans l'en-tete du journal. L'identite de la
    # table doit donc etre le RRN, meme si une cle metier existe — un delete
    # ne pourrait pas la citer.
    if table.journal_images == "*AFTER":
        return IdentityChoice(status=IDENTITY_RRN, source="journal_rrn", columns=RRN_IDENTITY_KEY)
    candidates: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    for constraint in table.constraints:
        if constraint.type in PRIMARY_TYPES:
            rank = 0
            source = "primary_key"
        elif constraint.type == "UNIQUE":
            rank = 1
            source = "unique_constraint"
        else:
            continue
        candidates.append((rank, len(constraint.columns), constraint.name, source, constraint.columns))
    for index in table.indexes:
        if index.unique is not True or index.sparse is not False or index.select_omit is not None:
            continue
        columns = tuple(column.name for column in sorted(index.columns, key=lambda item: item.ordinal))
        candidates.append((2, len(columns), index.name, "unique_index", columns))
    if not candidates:
        # *BOTH sans cle metier : les images sont completes mais rien ne sert
        # de cle de correspondance. Le RRN couvre aussi ce cas.
        return IdentityChoice(status=IDENTITY_RRN, source="journal_rrn", columns=RRN_IDENTITY_KEY)
    candidates.sort()
    _rank, _width, _name, source, columns = candidates[0]
    if any(column in {"RRN", "HASH", "hash", "rrn"} for column in columns):
        raise FleetError("invalid_identity", "Clé candidate inventée")
    return IdentityChoice(status=IDENTITY_KEYED, source=source, columns=columns)


def _cutover_checkpoint(journal: CatalogJournal) -> JournalCheckpoint:
    attached = [receiver for receiver in journal.receivers if receiver.status == ATTACHED_STATUS]
    if len(attached) != 1:
        raise FleetError("invalid_checkpoint", "Receiver attaché de cutover absent")
    receiver = attached[0]
    return JournalCheckpoint(receiver=receiver.name, sequence=receiver.last_sequence)


def _admit_historical(
    tables: tuple[CatalogTable, ...],
    historical_byte_budget: int | None,
) -> tuple[CatalogTable, ...]:
    ordered = tuple(sorted(tables, key=lambda table: (table.row_count, table.data_size, table.name)))
    if historical_byte_budget is None:
        return ordered
    admitted: list[CatalogTable] = []
    consumed_bytes = 0
    for table in ordered:
        if consumed_bytes + table.data_size > historical_byte_budget:
            continue
        admitted.append(table)
        consumed_bytes += table.data_size
    return tuple(admitted)


def _assign_lanes(tables: tuple[CatalogTable, ...], max_concurrency: int) -> tuple[HistoricalLane, ...]:
    buckets: list[list[CatalogTable]] = [[] for _ in range(max_concurrency)]
    for index, table in enumerate(tables):
        buckets[index % max_concurrency].append(table)
    lanes: list[HistoricalLane] = []
    slot = 1
    for bucket in buckets:
        if not bucket:
            continue
        lanes.append(
            HistoricalLane(
                slot=slot,
                tables=tuple(table.name for table in bucket),
                row_count=sum(table.row_count for table in bucket),
                data_size=sum(table.data_size for table in bucket),
            )
        )
        slot += 1
    return tuple(lanes)


def _deserialize_checkpoint(payload: object) -> JournalCheckpoint:
    data = _closed_mapping(payload, _CHECKPOINT_KEYS, code="invalid_serialization")
    return JournalCheckpoint(receiver=data["receiver"], sequence=data["sequence"])


def _deserialize_group(payload: object) -> JournalGroupPlan:
    data = _closed_mapping(payload, _GROUP_KEYS, code="invalid_serialization")
    names = data["table_names"]
    if type(names) is not list:
        raise FleetError("invalid_manifest", "Groupe journal invalide")
    return JournalGroupPlan(
        library=_require_str(data["library"], "invalid_journal", "Bibliothèque de journal invalide"),
        name=_require_str(data["name"], "invalid_journal", "Nom de journal invalide"),
        reader_kind=_require_str(data["reader_kind"], "invalid_journal", "Lecteur journal invalide"),
        reader_count=data["reader_count"],
        table_names=tuple(_require_str(name, "unknown_table", "Table hors manifeste") for name in names),
        continuity=_require_str(data["continuity"], "invalid_journal", "Continuité invalide"),
    )


def _deserialize_lane(payload: object) -> HistoricalLane:
    data = _closed_mapping(payload, _LANE_KEYS, code="invalid_serialization")
    names = data["tables"]
    if type(names) is not list:
        raise FleetError("invalid_lane", "Voie historique invalide")
    return HistoricalLane(
        slot=data["slot"],
        tables=tuple(_require_str(name, "unknown_table", "Table hors manifeste") for name in names),
        row_count=data["row_count"],
        data_size=data["data_size"],
    )


def _deserialize_table_plan(payload: object) -> TablePlan:
    data = _closed_mapping(payload, _TABLE_PLAN_KEYS, code="invalid_serialization")
    key_payload = data["candidate_key"]
    if key_payload is None:
        candidate_key = None
    else:
        if type(key_payload) is not list or not key_payload:
            raise FleetError("invalid_identity", "Clé candidate invalide")
        candidate_key = tuple(
            _require_token(column, "invalid_identity", "Colonne de clé invalide") for column in key_payload
        )
    reasons_payload = data["blocked_reasons"]
    if type(reasons_payload) is not list:
        raise FleetError("invalid_identity", "Raisons de blocage invalides")
    identity_source = data["identity_source"]
    if identity_source is not None:
        identity_source = _require_token(identity_source, "invalid_identity", "Source d'identité invalide")
    return TablePlan(
        name=_require_str(data["name"], "unknown_table", "Table hors manifeste"),
        row_count=data["row_count"],
        data_size=data["data_size"],
        journal_library=_require_str(data["journal_library"], "invalid_journal", "Bibliothèque de journal invalide"),
        journal_name=_require_str(data["journal_name"], "invalid_journal", "Nom de journal invalide"),
        journal_images=_require_str(data["journal_images"], "invalid_journal", "Images de journal invalides"),
        identity_status=_require_str(data["identity_status"], "invalid_identity", "Statut d'identité invalide"),
        identity_source=identity_source,
        candidate_key=candidate_key,
        certification_possible=data["certification_possible"],
        live_possible=data["live_possible"],
        blocked_reasons=tuple(
            _require_token(reason, "invalid_identity", "Raison de blocage invalide") for reason in reasons_payload
        ),
        historical_lane=data["historical_lane"],
    )


def _require_journal_images(value: object) -> str:
    images = _require_token(value, "invalid_journal", "Images de journal invalides")
    if images not in ALLOWED_JOURNAL_IMAGES:
        raise FleetError("invalid_journal", "Images de journal inconnues")
    return images


def _reject_duplicates(values: list[str], code: str) -> None:
    if len(values) != len(set(values)):
        raise FleetError(code, "Entrée de catalogue dupliquée")


def _closed_mapping(value: object, keys: tuple[str, ...], *, code: str) -> dict[str, object]:
    if type(value) is not dict:
        raise FleetError(code, "Objet JSON non autorisé")
    allowed = set(keys)
    actual = set(value)
    if actual != allowed:
        raise FleetError(code, "Schéma JSON non autorisé")
    for key in value:
        if type(key) is not str:
            raise FleetError(code, "Clé JSON non autorisée")
    return value


def _assert_json_safe(value: object) -> None:
    if type(value) in _JSON_SCALARS:
        if type(value) is float and not math.isfinite(value):
            raise FleetError("invalid_serialization", "Nombre JSON non fini")
        return
    if type(value) is list:
        for item in value:
            _assert_json_safe(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise FleetError("invalid_serialization", "Clé JSON non sûre")
            lowered = key.lower()
            if any(token in lowered for token in ("host", "user", "password", "secret", "token", "credential")):
                raise FleetError("invalid_serialization", "Champ sensible non autorisé")
            _assert_json_safe(item)
        return
    raise FleetError("invalid_serialization", "Type JSON non autorisé")


def _require_byte_budget(value: object) -> int:
    number = _require_int(value, "invalid_byte_budget", "Budget d'octets historique invalide")
    if number < 0:
        raise FleetError("invalid_byte_budget", "Budget d'octets historique négatif")
    return number


def _require_count(value: object, message: str) -> int:
    number = _require_int(value, "invalid_catalog", message)
    if number < 0:
        raise FleetError("invalid_catalog", message)
    return number


def _optional_count(value: object, message: str) -> int | None:
    if value is None:
        return None
    return _require_count(value, message)


def _require_positive_int(value: object, message: str) -> int:
    number = _require_int(value, "invalid_catalog", message)
    if number <= 0:
        raise FleetError("invalid_catalog", message)
    return number


def _require_sequence(value: object, code: str, message: str) -> int:
    if type(value) is int:
        if value < 0:
            raise FleetError(code, message)
        return value
    if type(value) is str and value.isdigit() and value == str(int(value)):
        return int(value)
    raise FleetError(code, message)


def _require_int(value: object, code: str, message: str) -> int:
    if type(value) is not int:
        raise FleetError(code, message)
    return value


def _require_bool(value: object, code: str, message: str) -> bool:
    if type(value) is not bool:
        raise FleetError(code, message)
    return value


def _require_str(value: object, code: str, message: str) -> str:
    if type(value) is not str:
        raise FleetError(code, message)
    return value


def _require_token(value: object, code: str, message: str) -> str:
    text = _require_str(value, code, message)
    if not text.strip() or text != text.strip():
        raise FleetError(code, message)
    return text
