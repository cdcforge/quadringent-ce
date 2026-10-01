"""Domaine d'orchestration borné, indépendant du fournisseur, pour le manifeste déclaré.

Aucune valeur d'installation n'est codée dans ce module : le manifeste de
tables, l'environnement publié et l'espace de destination proviennent de la
configuration de site déclarée (:mod:`quadringent.site_config`). Sans site
installé ni variables ``QUADRINGENT_*``, tout accès à ces périmètres échoue
explicitement — jamais de défaut silencieux.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from typing import Mapping

from quadringent.site_config import SiteConfig, current as _current_site


# Périmètres déclarés — résolus à l'usage depuis le site installé (voir
# ``__getattr__``). Les annotations sans valeur gardent les noms visibles pour
# l'analyse statique sans figer une installation dans le code.
MANIFEST: tuple[str, ...]
TABLE_COUNT: int
ENVIRONMENT: str
DESTINATION_NAMESPACE: str

FORMAT_VERSION = "quadringent-fleet-v1"
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 4


def __getattr__(name: str) -> object:
    """Périmètres du site déclaré, résolus à l'accès — jamais figés au code."""

    site_attributes = {
        "MANIFEST": _manifest,
        "TABLE_COUNT": _table_count,
        "ENVIRONMENT": _environment,
        "DESTINATION_NAMESPACE": _destination_namespace,
    }
    resolver = site_attributes.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver()


def _site() -> SiteConfig:
    return _current_site()


def _manifest() -> tuple[str, ...]:
    return _site().fleet_tables


def _table_count() -> int:
    return len(_site().fleet_tables)


def _environment() -> str:
    return _site().fleet_environment


def _destination_namespace() -> str:
    return _site().destination_namespace

_FLEET_KEYS = (
    "format_version",
    "environment",
    "destination_namespace",
    "max_concurrency",
    "credit_budget",
    "consumed_credits",
    "reserved_credits",
    "tables",
)
_TABLE_KEYS = (
    "name",
    "phase",
    "start_checkpoint",
    "current_checkpoint",
    "journal_tail",
    "receiver_chain",
    "copied_rows",
    "total_rows",
    "continuity_proven",
    "gap",
    "proof_window",
    "paused_from",
    "blocked_reason",
    "admitted",
    "estimated_credits",
    "reserved_credits",
    "actual_credits",
    "reconciliation_proof",
)
_CHECKPOINT_KEYS = ("receiver", "sequence")
_SPAN_KEYS = ("receiver", "first_sequence", "last_sequence")
_WINDOW_KEYS = ("start_utc", "end_utc")
_PROOF_KEYS = (
    "window",
    "source_count",
    "target_count",
    "missing",
    "extra",
    "duplicates",
    "source_hash",
    "target_hash",
    "destination_freshness_seconds",
    "freshness_slo_seconds",
    "latency_seconds",
    "throughput_rows_per_second",
    "cost_units",
)
_JSON_SCALARS = (str, int, float, bool, type(None))


class Phase(str, Enum):
    NOT_PREPARED = "NOT_PREPARED"
    READY = "READY"
    HISTORICAL = "HISTORICAL"
    CATCHING_UP = "CATCHING_UP"
    LIVE = "LIVE"
    RECONCILING = "RECONCILING"
    CERTIFIED = "CERTIFIED"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"


BACKFILL_PHASES = frozenset({Phase.HISTORICAL, Phase.CATCHING_UP})


class BlockedReason(str, Enum):
    RECEIVER_DISCONTINUITY = "receiver_discontinuity"
    SEQUENCE_GAP = "sequence_gap"
    UNKNOWN_HISTORY_PROGRESS = "unknown_history_progress"
    MISSING_START_CHECKPOINT = "missing_start_checkpoint"
    UNPROVEN_CONTINUITY = "unproven_continuity"
    JOURNAL_TAIL_UNOBSERVED = "journal_tail_unobserved"
    COST_OVERRUN = "cost_overrun"
    UNKNOWN_COST = "unknown_cost"
    OPERATOR_STOP = "operator_stop"


class SafeAction(str, Enum):
    PREPARE = "PREPARE"
    ADMIT_HISTORICAL = "ADMIT_HISTORICAL"
    RECORD_HISTORY_PROGRESS = "RECORD_HISTORY_PROGRESS"
    PROVE_CONTINUITY = "PROVE_CONTINUITY"
    CATCH_UP_TO_TAIL = "CATCH_UP_TO_TAIL"
    RECORD_ACTUAL_COST = "RECORD_ACTUAL_COST"
    OPEN_RECONCILIATION = "OPEN_RECONCILIATION"
    CERTIFY = "CERTIFY"
    RESUME = "RESUME"
    INSPECT_BLOCKED = "INSPECT_BLOCKED"
    NONE = "NONE"


class FleetError(ValueError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class JournalCheckpoint:
    receiver: str
    sequence: int

    def __post_init__(self) -> None:
        _require_token(self.receiver, "invalid_checkpoint", "Receiver de journal invalide")
        _require_int(self.sequence, "invalid_checkpoint", "Séquence de journal invalide")
        if self.sequence < 0:
            raise FleetError("invalid_checkpoint", "Séquence de journal invalide")

    def to_dict(self) -> dict[str, object]:
        return {"receiver": self.receiver, "sequence": self.sequence}


@dataclass(frozen=True)
class ReceiverSpan:
    receiver: str
    first_sequence: int
    last_sequence: int
    # Un receiver ATTACHED grandit entre deux relevés de catalogue : sa
    # borne haute est un instantané, pas un plafond. Seule la dernière
    # span d'une chaîne peut être ouverte.
    open: bool = False

    def __post_init__(self) -> None:
        _require_token(self.receiver, "invalid_receiver_span", "Receiver invalide")
        _require_int(self.first_sequence, "invalid_receiver_span", "Borne de receiver invalide")
        _require_int(self.last_sequence, "invalid_receiver_span", "Borne de receiver invalide")
        if type(self.open) is not bool:
            raise FleetError("invalid_receiver_span", "Borne de receiver invalide")
        if self.first_sequence < 0 or self.last_sequence < self.first_sequence:
            raise FleetError("invalid_receiver_span", "Borne de receiver invalide")

    def contains(self, checkpoint: JournalCheckpoint) -> bool:
        return (
            self.receiver == checkpoint.receiver
            and self.first_sequence <= checkpoint.sequence
            and (self.open or checkpoint.sequence <= self.last_sequence)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "receiver": self.receiver,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "open": self.open,
        }


@dataclass(frozen=True)
class ReceiverChain:
    spans: tuple[ReceiverSpan, ...]

    def __post_init__(self) -> None:
        if type(self.spans) is not tuple or not self.spans:
            raise FleetError("invalid_receiver_chain", "Chaîne de receivers invalide")
        names = [span.receiver for span in self.spans]
        if len(set(names)) != len(names):
            raise FleetError("invalid_receiver_chain", "Chaîne de receivers dupliquée")
        for span in self.spans:
            if type(span) is not ReceiverSpan:
                raise FleetError("invalid_receiver_chain", "Chaîne de receivers invalide")
        if any(span.open for span in self.spans[:-1]):
            raise FleetError("invalid_receiver_chain", "Chaîne de receivers invalide")
        # IBM i peut relancer la séquence à 1 après IPL/rotation. L'ordre est
        # celui fourni; la continuité ne se déduit pas des numéros.

    def rank(self, checkpoint: JournalCheckpoint) -> tuple[int, int]:
        for index, span in enumerate(self.spans):
            if span.contains(checkpoint):
                return (index, checkpoint.sequence)
        raise FleetError("receiver_discontinuity", "Checkpoint hors chaîne de receivers")

    def to_dict(self) -> list[dict[str, object]]:
        return [span.to_dict() for span in self.spans]


@dataclass(frozen=True)
class ProofWindow:
    start_utc: str
    end_utc: str

    def __post_init__(self) -> None:
        start = _parse_aligned_utc(self.start_utc)
        end = _parse_aligned_utc(self.end_utc)
        if end <= start:
            raise FleetError("invalid_proof_window", "Fenêtre de preuve non fermée")

    def to_dict(self) -> dict[str, object]:
        return {"start_utc": self.start_utc, "end_utc": self.end_utc}


@dataclass(frozen=True)
class ReconciliationProof:
    window: ProofWindow
    source_count: int
    target_count: int
    missing: int
    extra: int
    duplicates: int
    source_hash: str
    target_hash: str
    destination_freshness_seconds: float
    freshness_slo_seconds: float
    latency_seconds: float
    throughput_rows_per_second: float
    cost_units: float

    def __post_init__(self) -> None:
        if type(self.window) is not ProofWindow:
            raise FleetError("invalid_reconciliation", "Fenêtre de réconciliation absente")
        for field_name in ("source_count", "target_count", "missing", "extra", "duplicates"):
            value = getattr(self, field_name)
            _require_int(value, "invalid_reconciliation", "Compte de réconciliation invalide")
            if value < 0:
                raise FleetError("invalid_reconciliation", "Compte de réconciliation invalide")
        _require_token(self.source_hash, "invalid_reconciliation", "Empreinte source vide")
        _require_token(self.target_hash, "invalid_reconciliation", "Empreinte cible vide")
        for field_name in (
            "destination_freshness_seconds",
            "freshness_slo_seconds",
            "latency_seconds",
            "throughput_rows_per_second",
            "cost_units",
        ):
            _require_finite_number(
                getattr(self, field_name),
                "invalid_reconciliation",
                "Mesure de réconciliation absente",
            )
        if self.destination_freshness_seconds < 0:
            raise FleetError("invalid_reconciliation", "Fraîcheur destination invalide")
        if self.freshness_slo_seconds <= 0:
            raise FleetError("invalid_reconciliation", "SLO de fraîcheur invalide")
        if self.latency_seconds < 0 or self.throughput_rows_per_second < 0 or self.cost_units < 0:
            raise FleetError("invalid_reconciliation", "Mesure opérationnelle invalide")
        object.__setattr__(self, "destination_freshness_seconds", float(self.destination_freshness_seconds))
        object.__setattr__(self, "freshness_slo_seconds", float(self.freshness_slo_seconds))
        object.__setattr__(self, "latency_seconds", float(self.latency_seconds))
        object.__setattr__(self, "throughput_rows_per_second", float(self.throughput_rows_per_second))
        object.__setattr__(self, "cost_units", float(self.cost_units))

    def to_dict(self) -> dict[str, object]:
        return {
            "window": self.window.to_dict(),
            "source_count": self.source_count,
            "target_count": self.target_count,
            "missing": self.missing,
            "extra": self.extra,
            "duplicates": self.duplicates,
            "source_hash": self.source_hash,
            "target_hash": self.target_hash,
            "destination_freshness_seconds": self.destination_freshness_seconds,
            "freshness_slo_seconds": self.freshness_slo_seconds,
            "latency_seconds": self.latency_seconds,
            "throughput_rows_per_second": self.throughput_rows_per_second,
            "cost_units": self.cost_units,
        }


@dataclass(frozen=True)
class TableState:
    name: str
    phase: Phase
    start_checkpoint: JournalCheckpoint | None = None
    current_checkpoint: JournalCheckpoint | None = None
    journal_tail: JournalCheckpoint | None = None
    receiver_chain: ReceiverChain | None = None
    copied_rows: int | None = None
    total_rows: int | None = None
    continuity_proven: bool | None = None
    gap: bool | None = None
    proof_window: ProofWindow | None = None
    paused_from: Phase | None = None
    blocked_reason: str | None = None
    admitted: bool = False
    estimated_credits: float | None = None
    reserved_credits: float | None = None
    actual_credits: float | None = None
    reconciliation_proof: ReconciliationProof | None = None

    def __post_init__(self) -> None:
        if self.name not in _manifest():
            raise FleetError("unknown_table", "Table hors manifeste")
        if type(self.phase) is not Phase:
            raise FleetError("invalid_phase", "Phase inconnue")
        _optional_checkpoint(self.start_checkpoint)
        _optional_checkpoint(self.current_checkpoint)
        _optional_checkpoint(self.journal_tail)
        if self.receiver_chain is not None and type(self.receiver_chain) is not ReceiverChain:
            raise FleetError("invalid_receiver_chain", "Chaîne de receivers invalide")
        _optional_count(self.copied_rows, "copied_rows")
        _optional_count(self.total_rows, "total_rows")
        if (
            self.copied_rows is not None
            and self.total_rows is not None
            and self.copied_rows > self.total_rows
        ):
            raise FleetError("invalid_history_progress", "Progression historique incohérente")
        _optional_bool(self.continuity_proven, "continuity_proven")
        _optional_bool(self.gap, "gap")
        if self.proof_window is not None and type(self.proof_window) is not ProofWindow:
            raise FleetError("invalid_proof_window", "Fenêtre de preuve invalide")
        if self.paused_from is not None:
            if type(self.paused_from) is not Phase or self.paused_from in {
                Phase.PAUSED,
                Phase.BLOCKED,
                Phase.CERTIFIED,
            }:
                raise FleetError("invalid_pause", "Phase pré-pause invalide")
        if type(self.admitted) is not bool:
            raise FleetError("invalid_admission", "Indicateur d'admission invalide")
        estimated = _optional_cost(self.estimated_credits, "estimated_credits")
        reserved = _optional_cost(self.reserved_credits, "reserved_credits")
        actual = _optional_cost(self.actual_credits, "actual_credits")
        object.__setattr__(self, "estimated_credits", estimated)
        object.__setattr__(self, "reserved_credits", reserved)
        object.__setattr__(self, "actual_credits", actual)
        if self.phase is Phase.NOT_PREPARED and self.start_checkpoint is not None:
            raise FleetError("invalid_prepare", "Checkpoint de départ prématuré")
        if self.phase is not Phase.NOT_PREPARED and self.phase is not Phase.BLOCKED:
            if self.phase is not Phase.PAUSED and self.start_checkpoint is None:
                raise FleetError("missing_start_checkpoint", "Checkpoint de départ absent")
        if self.phase is Phase.PAUSED:
            if self.paused_from is None:
                raise FleetError("invalid_pause", "Phase pré-pause absente")
        elif self.paused_from is not None:
            raise FleetError("invalid_pause", "Phase pré-pause résiduelle")
        if self.phase is Phase.BLOCKED:
            _parse_blocked_reason(self.blocked_reason)
        elif self.blocked_reason is not None:
            raise FleetError("invalid_block", "Raison de blocage résiduelle")
        if self.phase in {Phase.HISTORICAL, Phase.CATCHING_UP, Phase.LIVE, Phase.RECONCILING, Phase.CERTIFIED}:
            if not self.admitted:
                raise FleetError("invalid_admission", "Phase active sans admission")
        backfill_phase = self.phase in BACKFILL_PHASES or (
            self.phase is Phase.PAUSED and self.paused_from in BACKFILL_PHASES
        )
        if backfill_phase:
            if self.estimated_credits is None or self.reserved_credits is None:
                raise FleetError("unknown_cost", "Coût de backfill inconnu")
            if self.estimated_credits <= 0 or self.reserved_credits < 0:
                raise FleetError("invalid_credit_budget", "Réservation de backfill invalide")
        if self.phase in {Phase.LIVE, Phase.RECONCILING, Phase.CERTIFIED}:
            if self.actual_credits is None:
                raise FleetError("unknown_cost", "Coût réel absent")
            if self.reserved_credits != 0.0:
                raise FleetError("invalid_credit_budget", "Réservation non libérée après LIVE")
        if self.reconciliation_proof is not None and type(self.reconciliation_proof) is not ReconciliationProof:
            raise FleetError("invalid_reconciliation", "Preuve de réconciliation invalide")
        if self.phase in {Phase.RECONCILING, Phase.CERTIFIED}:
            if self.proof_window is None:
                raise FleetError("invalid_proof_window", "Fenêtre de preuve absente")
            if self.phase is Phase.CERTIFIED and self.reconciliation_proof is None:
                raise FleetError("invalid_reconciliation", "Preuve de réconciliation absente")
            if (
                self.reconciliation_proof is not None
                and self.reconciliation_proof.window != self.proof_window
            ):
                raise FleetError("window_mismatch", "Preuve hors fenêtre alignée")
        elif self.reconciliation_proof is not None:
            raise FleetError("invalid_reconciliation", "Preuve de réconciliation prématurée")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "phase": self.phase.value,
            "start_checkpoint": None if self.start_checkpoint is None else self.start_checkpoint.to_dict(),
            "current_checkpoint": None if self.current_checkpoint is None else self.current_checkpoint.to_dict(),
            "journal_tail": None if self.journal_tail is None else self.journal_tail.to_dict(),
            "receiver_chain": None if self.receiver_chain is None else self.receiver_chain.to_dict(),
            "copied_rows": self.copied_rows,
            "total_rows": self.total_rows,
            "continuity_proven": self.continuity_proven,
            "gap": self.gap,
            "proof_window": None if self.proof_window is None else self.proof_window.to_dict(),
            "paused_from": None if self.paused_from is None else self.paused_from.value,
            "blocked_reason": self.blocked_reason,
            "admitted": self.admitted,
            "estimated_credits": self.estimated_credits,
            "reserved_credits": self.reserved_credits,
            "actual_credits": self.actual_credits,
            "reconciliation_proof": None
            if self.reconciliation_proof is None
            else self.reconciliation_proof.to_dict(),
        }


@dataclass(frozen=True)
class FleetRun:
    environment: str
    destination_namespace: str
    max_concurrency: int
    credit_budget: float
    consumed_credits: float
    reserved_credits: float
    tables: tuple[TableState, ...]

    def __post_init__(self) -> None:
        if self.environment != _environment():
            raise FleetError("invalid_environment", "Environnement hors site déclaré")
        if self.destination_namespace != _destination_namespace():
            raise FleetError("invalid_destination", "Espace de destination hors site déclaré")
        _require_int(self.max_concurrency, "invalid_concurrency", "Concurrence invalide")
        if self.max_concurrency < MIN_CONCURRENCY or self.max_concurrency > MAX_CONCURRENCY:
            raise FleetError("invalid_concurrency", "Concurrence hors borne 1..4")
        budget = _require_cost(self.credit_budget, "credit_budget")
        consumed = _require_cost(self.consumed_credits, "consumed_credits")
        reserved = _require_cost(self.reserved_credits, "reserved_credits")
        object.__setattr__(self, "credit_budget", budget)
        object.__setattr__(self, "consumed_credits", consumed)
        object.__setattr__(self, "reserved_credits", reserved)
        if reserved < 0:
            raise FleetError("invalid_credit_budget", "Réservation négative")
        if consumed < 0:
            raise FleetError("invalid_credit_budget", "Consommation négative")
        # Un dépassement observé peut laisser consumed > budget, et un actual
        # supérieur à l'estimation peut laisser consumed + reserved > budget.
        if type(self.tables) is not tuple or len(self.tables) != _table_count():
            raise FleetError("invalid_manifest", "Manifeste du site incomplet")
        names = tuple(table.name for table in self.tables)
        if names != _manifest():
            raise FleetError("invalid_manifest", "Manifeste du site hors ordre")
        for table in self.tables:
            if type(table) is not TableState:
                raise FleetError("invalid_manifest", "État de table invalide")
        summed_reserved = _sum_reservations(self.tables)
        if summed_reserved != reserved:
            raise FleetError("invalid_credit_budget", "Réservations de flotte incohérentes")
        if _backfill_count(self) > self.max_concurrency:
            raise FleetError("concurrency_exceeded", "Concurrence de backfill dépassée")

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": FORMAT_VERSION,
            "environment": self.environment,
            "destination_namespace": self.destination_namespace,
            "max_concurrency": self.max_concurrency,
            "credit_budget": self.credit_budget,
            "consumed_credits": self.consumed_credits,
            "reserved_credits": self.reserved_credits,
            "tables": [table.to_dict() for table in self.tables],
        }


@dataclass(frozen=True)
class FleetSummary:
    certified_count: int
    table_count: int
    running_count: int
    admitted_count: int
    known_copied_rows: int | None
    known_total_rows: int | None
    credit_budget: float
    consumed_credits: float
    reserved_credits: float
    over_budget: bool
    next_action: str
    next_reason: str
    next_table: str | None

    def __post_init__(self) -> None:
        _require_int(self.certified_count, "invalid_summary", "Compte certifié invalide")
        _require_int(self.table_count, "invalid_summary", "Compte de tables invalide")
        _require_int(self.running_count, "invalid_summary", "Compte d'exécution invalide")
        _require_int(self.admitted_count, "invalid_summary", "Compte d'admission invalide")
        if self.table_count != _table_count():
            raise FleetError("invalid_summary", "Compte de tables hors manifeste")
        if not (0 <= self.certified_count <= _table_count()):
            raise FleetError("invalid_summary", "Compte certifié hors borne")
        _optional_count(self.known_copied_rows, "known_copied_rows")
        _optional_count(self.known_total_rows, "known_total_rows")
        object.__setattr__(self, "credit_budget", _require_cost(self.credit_budget, "credit_budget"))
        object.__setattr__(self, "consumed_credits", _require_cost(self.consumed_credits, "consumed_credits"))
        object.__setattr__(self, "reserved_credits", _require_cost(self.reserved_credits, "reserved_credits"))
        if type(self.over_budget) is not bool:
            raise FleetError("invalid_summary", "Indicateur de dépassement invalide")
        if self.over_budget != (self.consumed_credits > self.credit_budget):
            raise FleetError("invalid_summary", "Dépassement de budget incohérent")
        if self.next_action not in {action.value for action in SafeAction}:
            raise FleetError("invalid_summary", "Action suivante inconnue")
        _require_token(self.next_reason, "invalid_summary", "Raison suivante absente")
        if self.next_table is not None and self.next_table not in _manifest():
            raise FleetError("invalid_summary", "Table suivante hors manifeste")

    def to_dict(self) -> dict[str, object]:
        return {
            "certified_count": self.certified_count,
            "table_count": self.table_count,
            "running_count": self.running_count,
            "admitted_count": self.admitted_count,
            "known_copied_rows": self.known_copied_rows,
            "known_total_rows": self.known_total_rows,
            "credit_budget": self.credit_budget,
            "consumed_credits": self.consumed_credits,
            "reserved_credits": self.reserved_credits,
            "over_budget": self.over_budget,
            "next_action": self.next_action,
            "next_reason": self.next_reason,
            "next_table": self.next_table,
        }


def create_fleet(*, max_concurrency: int, credit_budget: float) -> FleetRun:
    budget = _require_cost(credit_budget, "credit_budget")
    return FleetRun(
        environment=_environment(),
        destination_namespace=_destination_namespace(),
        max_concurrency=max_concurrency,
        credit_budget=budget,
        consumed_credits=0.0,
        reserved_credits=0.0,
        tables=tuple(TableState(name=name, phase=Phase.NOT_PREPARED) for name in _manifest()),
    )


def get_table(fleet: FleetRun, name: str) -> TableState:
    _require_fleet(fleet)
    if name not in _manifest():
        raise FleetError("unknown_table", "Table hors manifeste")
    return fleet.tables[_manifest().index(name)]


def prepare_table(fleet: FleetRun, name: str, start_checkpoint: JournalCheckpoint) -> FleetRun:
    _require_fleet(fleet)
    if type(start_checkpoint) is not JournalCheckpoint:
        raise FleetError("invalid_checkpoint", "Checkpoint de départ invalide")
    table = get_table(fleet, name)
    if table.phase is not Phase.NOT_PREPARED:
        raise FleetError("invalid_prepare", "Préparation déjà effectuée")
    prepared = replace(table, phase=Phase.READY, start_checkpoint=start_checkpoint)
    return _replace_table(fleet, prepared)


def admission_candidates(fleet: FleetRun) -> tuple[str, ...]:
    _require_fleet(fleet)
    if any(table.phase is Phase.NOT_PREPARED for table in fleet.tables):
        return ()
    if fleet.consumed_credits + fleet.reserved_credits >= fleet.credit_budget:
        return ()
    slots = fleet.max_concurrency - _backfill_count(fleet)
    if slots <= 0:
        return ()
    ready = [table.name for table in fleet.tables if table.phase is Phase.READY]
    return tuple(ready[:slots])


def admit_next(fleet: FleetRun, *, estimated_credits: float) -> FleetRun:
    candidates = admission_candidates(fleet)
    if not candidates:
        raise FleetError("admission_refused", "Aucune admission historique sûre")
    estimate = _require_positive_cost(estimated_credits, "estimated_credits")
    table = get_table(fleet, candidates[0])
    if table.estimated_credits is not None and table.estimated_credits != estimate:
        raise FleetError("contradictory_cost", "Estimation de coût contradictoire")
    projected = fleet.consumed_credits + fleet.reserved_credits + estimate
    if projected > fleet.credit_budget:
        raise FleetError("admission_refused", "Budget de crédits insuffisant")
    admitted = replace(
        table,
        phase=Phase.HISTORICAL,
        admitted=True,
        estimated_credits=estimate,
        reserved_credits=estimate,
    )
    return _with_tables(fleet, _swap_table(fleet.tables, admitted))


def record_history_progress(
    fleet: FleetRun,
    name: str,
    *,
    copied_rows: int | None,
    total_rows: int | None,
) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase is not Phase.HISTORICAL:
        raise FleetError("invalid_history_progress", "Progression hors phase historique")
    _optional_count(copied_rows, "copied_rows")
    _optional_count(total_rows, "total_rows")
    if copied_rows is not None and total_rows is not None and copied_rows > total_rows:
        raise FleetError("invalid_history_progress", "Progression historique incohérente")
    if table.copied_rows is not None and copied_rows is not None and copied_rows < table.copied_rows:
        raise FleetError("invalid_history_progress", "Progression historique reculée")
    if table.total_rows is not None and total_rows is not None and total_rows != table.total_rows:
        raise FleetError("invalid_history_progress", "Total historique contradictoire")
    if table.copied_rows is not None and copied_rows is None:
        raise FleetError("invalid_history_progress", "Progression connue effacée")
    if table.total_rows is not None and total_rows is None:
        raise FleetError("invalid_history_progress", "Total connu effacé")
    updated = replace(table, copied_rows=copied_rows, total_rows=total_rows)
    return _apply_table(fleet, _evaluate(updated))


def record_journal_evidence(
    fleet: FleetRun,
    name: str,
    *,
    current_checkpoint: JournalCheckpoint | None,
    journal_tail: JournalCheckpoint | None,
    receiver_chain: ReceiverChain | None,
    continuity_proven: bool | None,
    gap: bool | None,
) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase not in {Phase.HISTORICAL, Phase.CATCHING_UP}:
        raise FleetError("invalid_journal_evidence", "Preuve journal hors phase active")
    _optional_checkpoint(current_checkpoint)
    _optional_checkpoint(journal_tail)
    if receiver_chain is not None and type(receiver_chain) is not ReceiverChain:
        raise FleetError("invalid_receiver_chain", "Chaîne de receivers invalide")
    _optional_bool(continuity_proven, "continuity_proven")
    _optional_bool(gap, "gap")
    if continuity_proven is True:
        if receiver_chain is None or table.start_checkpoint is None:
            raise FleetError("unproven_continuity", "Continuité déclarée sans preuve")
        if gap is True:
            raise FleetError("invalid_journal_evidence", "Continuité et trou incompatibles")
    if receiver_chain is not None:
        if table.start_checkpoint is not None:
            try:
                start_rank = receiver_chain.rank(table.start_checkpoint)
            except FleetError as error:
                if error.code == "receiver_discontinuity":
                    return _apply_table(fleet, _block(table, BlockedReason.RECEIVER_DISCONTINUITY))
                raise
        else:
            start_rank = None
        if current_checkpoint is not None:
            current_rank = receiver_chain.rank(current_checkpoint)
            if start_rank is not None and current_rank < start_rank:
                raise FleetError("invalid_journal_evidence", "Checkpoint reculé avant le départ")
        if journal_tail is not None:
            tail_rank = receiver_chain.rank(journal_tail)
            # Sur une chaîne ouverte (receiver attaché encore en vie), une
            # position courante au-delà de la queue cataloguée est un relevé
            # plus frais que le catalogue — jamais une contradiction. La
            # garde ne survit qu'aux chaînes closes, où la queue est réelle.
            if (
                current_checkpoint is not None
                and not receiver_chain.spans[-1].open
                and receiver_chain.rank(current_checkpoint) > tail_rank
            ):
                raise FleetError("invalid_journal_evidence", "Checkpoint au-delà de la queue observée")
    updated = replace(
        table,
        current_checkpoint=current_checkpoint,
        journal_tail=journal_tail,
        receiver_chain=receiver_chain,
        continuity_proven=continuity_proven,
        gap=gap,
    )
    return _apply_table(fleet, _evaluate(updated))


def record_actual_cost(fleet: FleetRun, name: str, actual_credits: float) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase not in BACKFILL_PHASES:
        raise FleetError("invalid_cost", "Coût réel hors travail coûteux")
    actual = _require_cost(actual_credits, "actual_credits")
    if table.reserved_credits is None or table.estimated_credits is None:
        raise FleetError("unknown_cost", "Réservation absente")
    if table.actual_credits is not None and actual < table.actual_credits:
        raise FleetError("contradictory_cost", "Coût réel reculé")
    other_reserved = fleet.reserved_credits - table.reserved_credits
    updated = replace(table, actual_credits=actual)
    if fleet.consumed_credits + other_reserved + actual > fleet.credit_budget:
        blocked = _block(updated, BlockedReason.COST_OVERRUN)
        return _with_tables(
            fleet,
            _swap_table(fleet.tables, blocked),
            consumed_credits=fleet.consumed_credits + actual,
        )
    held = replace(updated, reserved_credits=actual)
    return _replace_table(fleet, held)


def begin_reconciliation(fleet: FleetRun, name: str, window: ProofWindow) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase is Phase.LIVE:
        pass
    elif table.phase is Phase.RECONCILING:
        # Ré-ancrage : une mesure de certification plus récente porte sa
        # propre fenêtre — la certification exige toujours que la preuve
        # réponde exactement à la fenêtre déclarée, le ré-ancrage ne relâche
        # rien.
        pass
    else:
        raise FleetError("invalid_reconciliation", "Réconciliation hors phase LIVE")
    if type(window) is not ProofWindow:
        raise FleetError("invalid_proof_window", "Fenêtre de preuve invalide")
    return _replace_table(fleet, replace(table, phase=Phase.RECONCILING, proof_window=window))


def certify_table(fleet: FleetRun, name: str, proof: ReconciliationProof) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase is not Phase.RECONCILING:
        raise FleetError("invalid_certification", "Certification hors phase RECONCILING")
    if type(proof) is not ReconciliationProof:
        raise FleetError("invalid_reconciliation", "Preuve de réconciliation invalide")
    if table.proof_window is None or proof.window != table.proof_window:
        raise FleetError("window_mismatch", "Preuve hors fenêtre alignée")
    if proof.source_count != proof.target_count:
        raise FleetError("count_mismatch", "Comptes source et cible divergents")
    if proof.missing != 0:
        raise FleetError("missing_rows", "Lignes manquantes dans la fenêtre")
    if proof.extra != 0:
        raise FleetError("extra_rows", "Lignes en trop dans la fenêtre")
    if proof.duplicates != 0:
        raise FleetError("duplicate_rows", "Doublons dans la fenêtre")
    if proof.source_hash != proof.target_hash:
        raise FleetError("hash_mismatch", "Empreintes de fenêtre divergentes")
    if proof.destination_freshness_seconds > proof.freshness_slo_seconds:
        raise FleetError("freshness_slo_breached", "Fraîcheur hors SLO déclaré")
    if table.actual_credits is None:
        raise FleetError("unknown_cost", "Coût réel absent")
    if proof.cost_units != table.actual_credits:
        raise FleetError("cost_mismatch", "Coût de preuve distinct du coût mesuré")
    certified = replace(table, phase=Phase.CERTIFIED, reconciliation_proof=proof)
    return _replace_table(fleet, certified)


def pause_table(fleet: FleetRun, name: str) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase in {Phase.PAUSED, Phase.BLOCKED, Phase.CERTIFIED, Phase.NOT_PREPARED}:
        raise FleetError("invalid_pause", "Pause impossible dans cette phase")
    paused = replace(table, phase=Phase.PAUSED, paused_from=table.phase)
    return _replace_table(fleet, paused)


def resume_table(fleet: FleetRun, name: str) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase is not Phase.PAUSED or table.paused_from is None:
        raise FleetError("invalid_resume", "Reprise sans phase pré-pause")
    restored_phase = table.paused_from
    if restored_phase in BACKFILL_PHASES:
        if _backfill_count(fleet) >= fleet.max_concurrency:
            raise FleetError("concurrency_exceeded", "Reprise impossible: concurrence saturée")
    resumed = replace(table, phase=restored_phase, paused_from=None)
    return _replace_table(fleet, resumed)


def block_table(fleet: FleetRun, name: str, reason: str | BlockedReason) -> FleetRun:
    table = get_table(fleet, name)
    if table.phase is Phase.CERTIFIED:
        raise FleetError("invalid_block", "Une table certifiée ne peut pas être bloquée")
    if table.phase is Phase.BLOCKED:
        raise FleetError("invalid_block", "Table déjà bloquée")
    return _apply_table(fleet, _block(table, reason))


def summarize(fleet: FleetRun) -> FleetSummary:
    _require_fleet(fleet)
    certified_count = sum(1 for table in fleet.tables if table.phase is Phase.CERTIFIED)
    admitted_count = sum(1 for table in fleet.tables if table.admitted)
    copied_values = [table.copied_rows for table in fleet.tables]
    total_values = [table.total_rows for table in fleet.tables]
    action, reason, table_name = _next_safe_action(fleet)
    return FleetSummary(
        certified_count=certified_count,
        table_count=_table_count(),
        running_count=_backfill_count(fleet),
        admitted_count=admitted_count,
        known_copied_rows=_known_sum(copied_values),
        known_total_rows=_known_sum(total_values),
        credit_budget=fleet.credit_budget,
        consumed_credits=fleet.consumed_credits,
        reserved_credits=fleet.reserved_credits,
        over_budget=fleet.consumed_credits > fleet.credit_budget,
        next_action=action.value,
        next_reason=reason,
        next_table=table_name,
    )


def serialize_fleet(fleet: FleetRun) -> dict[str, object]:
    _require_fleet(fleet)
    payload = fleet.to_dict()
    _assert_json_safe(payload)
    return payload


def deserialize_fleet(payload: Mapping[str, object]) -> FleetRun:
    data = _closed_mapping(payload, _FLEET_KEYS, code="invalid_serialization")
    if data["format_version"] != FORMAT_VERSION:
        raise FleetError("invalid_serialization", "Version de flotte inconnue")
    tables_payload = data["tables"]
    if type(tables_payload) is not list or len(tables_payload) != _table_count():
        raise FleetError("invalid_manifest", "Manifeste du site incomplet")
    tables = tuple(_deserialize_table(item) for item in tables_payload)
    return FleetRun(
        environment=_require_str(data["environment"], "invalid_environment", "Environnement invalide"),
        destination_namespace=_require_str(
            data["destination_namespace"], "invalid_destination", "Destination invalide"
        ),
        max_concurrency=data["max_concurrency"],
        credit_budget=data["credit_budget"],
        consumed_credits=data["consumed_credits"],
        reserved_credits=data["reserved_credits"],
        tables=tables,
    )


def _evaluate(table: TableState) -> TableState:
    if table.phase is Phase.HISTORICAL and table.continuity_proven is False:
        return _block(table, BlockedReason.UNPROVEN_CONTINUITY)
    if table.phase is Phase.HISTORICAL and table.gap is True:
        return _block(table, BlockedReason.SEQUENCE_GAP)
    if table.phase is Phase.HISTORICAL and _can_enter_catching_up(table):
        table = replace(table, phase=Phase.CATCHING_UP)
    if table.phase is Phase.CATCHING_UP and table.continuity_proven is False:
        return _block(table, BlockedReason.UNPROVEN_CONTINUITY)
    if table.phase is Phase.CATCHING_UP and table.gap is True:
        return _block(table, BlockedReason.SEQUENCE_GAP)
    if table.phase is Phase.CATCHING_UP and _can_enter_live(table):
        table = replace(table, phase=Phase.LIVE, reserved_credits=0.0)
    return table


def _can_enter_catching_up(table: TableState) -> bool:
    if _history_complete(table) is not True:
        return False
    if table.start_checkpoint is None or table.receiver_chain is None:
        return False
    if table.continuity_proven is not True:
        return False
    table.receiver_chain.rank(table.start_checkpoint)
    return True


def _can_enter_live(table: TableState) -> bool:
    if table.current_checkpoint is None or table.journal_tail is None:
        return False
    if table.receiver_chain is None or table.start_checkpoint is None:
        return False
    if table.continuity_proven is not True or table.gap is not False:
        return False
    if table.actual_credits is None:
        return False
    start_rank = table.receiver_chain.rank(table.start_checkpoint)
    current_rank = table.receiver_chain.rank(table.current_checkpoint)
    tail_rank = table.receiver_chain.rank(table.journal_tail)
    return start_rank <= current_rank and current_rank >= tail_rank


def _history_complete(table: TableState) -> bool | None:
    if table.copied_rows is None or table.total_rows is None:
        return None
    return table.copied_rows == table.total_rows


def _block(table: TableState, reason: str | BlockedReason) -> TableState:
    code = _parse_blocked_reason(reason)
    return replace(
        table,
        phase=Phase.BLOCKED,
        paused_from=None,
        blocked_reason=code,
        reserved_credits=0.0 if table.reserved_credits is not None else None,
    )


def _next_safe_action(fleet: FleetRun) -> tuple[SafeAction, str, str | None]:
    candidates = admission_candidates(fleet)
    ready_waiting = any(table.phase is Phase.READY for table in fleet.tables)
    for table in fleet.tables:
        if table.phase is Phase.NOT_PREPARED:
            return SafeAction.PREPARE, "missing_start_checkpoint", table.name
        if table.phase is Phase.BLOCKED:
            return SafeAction.INSPECT_BLOCKED, table.blocked_reason or "operator_stop", table.name
        if table.phase is Phase.PAUSED:
            return SafeAction.RESUME, "restore_pre_pause_phase", table.name
        if table.phase is Phase.READY:
            if candidates and candidates[0] == table.name:
                if fleet.consumed_credits + fleet.reserved_credits >= fleet.credit_budget:
                    return SafeAction.NONE, "credit_budget_exhausted", table.name
                return SafeAction.ADMIT_HISTORICAL, "historical_admission_available", table.name
            if fleet.consumed_credits + fleet.reserved_credits >= fleet.credit_budget:
                return SafeAction.NONE, "credit_budget_exhausted", table.name
            return SafeAction.NONE, "concurrency_saturated", table.name
        if table.phase is Phase.HISTORICAL:
            completeness = _history_complete(table)
            if completeness is None:
                return SafeAction.RECORD_HISTORY_PROGRESS, "unknown_history_progress", table.name
            if completeness is False:
                return SafeAction.RECORD_HISTORY_PROGRESS, "historical_copy_incomplete", table.name
            return SafeAction.PROVE_CONTINUITY, "start_checkpoint_continuity_unproven", table.name
        if table.phase is Phase.CATCHING_UP:
            if not _tail_reached(table):
                return SafeAction.CATCH_UP_TO_TAIL, "journal_tail_not_reached", table.name
            if table.actual_credits is None:
                return SafeAction.RECORD_ACTUAL_COST, "unknown_cost", table.name
            return SafeAction.CATCH_UP_TO_TAIL, "journal_tail_not_reached", table.name
        if table.phase is Phase.LIVE:
            return SafeAction.OPEN_RECONCILIATION, "closed_utc_window_required", table.name
        if table.phase is Phase.RECONCILING:
            return SafeAction.CERTIFY, "same_window_reconciliation_required", table.name
    if all(table.phase is Phase.CERTIFIED for table in fleet.tables):
        return SafeAction.NONE, "fleet_certified", None
    if ready_waiting and fleet.consumed_credits + fleet.reserved_credits >= fleet.credit_budget:
        return SafeAction.NONE, "credit_budget_exhausted", None
    if ready_waiting:
        return SafeAction.NONE, "concurrency_saturated", None
    return SafeAction.NONE, "no_safe_action", None


def _tail_reached(table: TableState) -> bool:
    if (
        table.current_checkpoint is None
        or table.journal_tail is None
        or table.receiver_chain is None
        or table.start_checkpoint is None
        or table.continuity_proven is not True
        or table.gap is not False
    ):
        return False
    start_rank = table.receiver_chain.rank(table.start_checkpoint)
    current_rank = table.receiver_chain.rank(table.current_checkpoint)
    tail_rank = table.receiver_chain.rank(table.journal_tail)
    return start_rank <= current_rank and current_rank >= tail_rank


def _backfill_count(fleet: FleetRun) -> int:
    return sum(1 for table in fleet.tables if table.phase in BACKFILL_PHASES)


def _known_sum(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known)


def _sum_reservations(tables: tuple[TableState, ...]) -> float:
    total = 0.0
    for table in tables:
        if table.reserved_credits is not None:
            total += table.reserved_credits
    return total


def _swap_table(tables: tuple[TableState, ...], updated: TableState) -> tuple[TableState, ...]:
    return tuple(updated if table.name == updated.name else table for table in tables)


def _replace_table(fleet: FleetRun, updated: TableState) -> FleetRun:
    return _with_tables(fleet, _swap_table(fleet.tables, updated))


def _with_tables(
    fleet: FleetRun,
    tables: tuple[TableState, ...],
    *,
    consumed_credits: float | None = None,
) -> FleetRun:
    reserved = _sum_reservations(tables)
    consumed = fleet.consumed_credits if consumed_credits is None else consumed_credits
    return replace(fleet, tables=tables, reserved_credits=reserved, consumed_credits=consumed)


def _apply_table(fleet: FleetRun, updated: TableState) -> FleetRun:
    previous = get_table(fleet, updated.name)
    if updated.phase is Phase.LIVE and previous.phase is not Phase.LIVE:
        return _commit_live_cost(fleet, updated)
    if updated.phase is Phase.BLOCKED and previous.phase is not Phase.BLOCKED:
        released = replace(
            updated,
            reserved_credits=0.0 if updated.reserved_credits is not None else None,
        )
        measured = updated.actual_credits if updated.actual_credits is not None else previous.actual_credits
        consumed = fleet.consumed_credits
        committed = previous.phase in {Phase.LIVE, Phase.RECONCILING, Phase.CERTIFIED} or (
            previous.phase is Phase.PAUSED
            and previous.paused_from in {Phase.LIVE, Phase.RECONCILING, Phase.CERTIFIED}
        )
        if measured is not None and not committed:
            consumed = consumed + measured
            if consumed < fleet.consumed_credits:
                raise FleetError("cost_overrun", "Consommation reculée au blocage")
        return _with_tables(fleet, _swap_table(fleet.tables, released), consumed_credits=consumed)
    return _replace_table(fleet, updated)


def _commit_live_cost(fleet: FleetRun, table: TableState) -> FleetRun:
    if table.actual_credits is None:
        raise FleetError("unknown_cost", "Coût réel absent avant LIVE")
    new_consumed = fleet.consumed_credits + table.actual_credits
    if new_consumed < fleet.consumed_credits:
        raise FleetError("cost_overrun", "Consommation reculée")
    if new_consumed > fleet.credit_budget:
        blocked = _block(table, BlockedReason.COST_OVERRUN)
        return _with_tables(
            fleet,
            _swap_table(fleet.tables, blocked),
            consumed_credits=new_consumed,
        )
    live = replace(table, reserved_credits=0.0)
    return _with_tables(fleet, _swap_table(fleet.tables, live), consumed_credits=new_consumed)


def _require_fleet(fleet: FleetRun) -> None:
    if type(fleet) is not FleetRun:
        raise FleetError("invalid_fleet", "Flotte invalide")


def _optional_checkpoint(value: JournalCheckpoint | None) -> None:
    if value is not None and type(value) is not JournalCheckpoint:
        raise FleetError("invalid_checkpoint", "Checkpoint de journal invalide")


def _optional_count(value: int | None, field_name: str) -> None:
    if value is None:
        return
    _require_int(value, "invalid_history_progress", f"Compte {field_name} invalide")
    if value < 0:
        raise FleetError("invalid_history_progress", f"Compte {field_name} négatif")


def _optional_bool(value: bool | None, field_name: str) -> None:
    if value is not None and type(value) is not bool:
        raise FleetError("invalid_journal_evidence", f"Indicateur {field_name} invalide")


def _optional_cost(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _require_cost(value, field_name)


def _require_cost(value: object, field_name: str) -> float:
    number = _require_finite_number(value, "invalid_credit_budget", f"Coût {field_name} invalide")
    if number < 0:
        raise FleetError("invalid_credit_budget", f"Coût {field_name} négatif")
    return number


def _require_positive_cost(value: object, field_name: str) -> float:
    number = _require_cost(value, field_name)
    if number <= 0:
        raise FleetError("invalid_credit_budget", f"Coût {field_name} non positif")
    return number


def _require_int(value: object, code: str, message: str) -> int:
    if type(value) is not int:
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


def _require_finite_number(value: object, code: str, message: str) -> float:
    if type(value) is bool or type(value) not in (int, float):
        raise FleetError(code, message)
    number = float(value)
    if not math.isfinite(number):
        raise FleetError(code, message)
    return number


def _parse_blocked_reason(value: str | BlockedReason | None) -> str:
    if isinstance(value, BlockedReason):
        return value.value
    if type(value) is not str:
        raise FleetError("invalid_block", "Raison de blocage absente")
    try:
        return BlockedReason(value).value
    except ValueError:
        raise FleetError("invalid_block", "Raison de blocage non autorisée") from None


def _parse_aligned_utc(value: object) -> datetime:
    if type(value) is not str or not value.strip():
        raise FleetError("invalid_proof_window", "Horodatage UTC invalide")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FleetError("invalid_proof_window", "Horodatage UTC invalide") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise FleetError("invalid_proof_window", "Horodatage UTC invalide")
    if parsed.microsecond != 0:
        raise FleetError("unaligned_window", "Fenêtre UTC non alignée")
    return parsed.astimezone(timezone.utc)


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
            _assert_json_safe(item)
        return
    raise FleetError("invalid_serialization", "Type JSON non autorisé")


def _deserialize_table(payload: object) -> TableState:
    data = _closed_mapping(payload, _TABLE_KEYS, code="invalid_serialization")
    try:
        phase = Phase(data["phase"])
    except ValueError:
        raise FleetError("invalid_phase", "Phase inconnue") from None
    paused_from_raw = data["paused_from"]
    if paused_from_raw is None:
        paused_from = None
    else:
        try:
            paused_from = Phase(paused_from_raw)
        except ValueError:
            raise FleetError("invalid_pause", "Phase pré-pause invalide") from None
    return TableState(
        name=_require_str(data["name"], "unknown_table", "Table hors manifeste"),
        phase=phase,
        start_checkpoint=_deserialize_checkpoint(data["start_checkpoint"]),
        current_checkpoint=_deserialize_checkpoint(data["current_checkpoint"]),
        journal_tail=_deserialize_checkpoint(data["journal_tail"]),
        receiver_chain=_deserialize_chain(data["receiver_chain"]),
        copied_rows=data["copied_rows"],
        total_rows=data["total_rows"],
        continuity_proven=data["continuity_proven"],
        gap=data["gap"],
        proof_window=_deserialize_window(data["proof_window"]),
        paused_from=paused_from,
        blocked_reason=data["blocked_reason"],
        admitted=data["admitted"],
        estimated_credits=data["estimated_credits"],
        reserved_credits=data["reserved_credits"],
        actual_credits=data["actual_credits"],
        reconciliation_proof=_deserialize_proof(data["reconciliation_proof"]),
    )


def _deserialize_checkpoint(payload: object) -> JournalCheckpoint | None:
    if payload is None:
        return None
    data = _closed_mapping(payload, _CHECKPOINT_KEYS, code="invalid_serialization")
    return JournalCheckpoint(receiver=data["receiver"], sequence=data["sequence"])


def _deserialize_chain(payload: object) -> ReceiverChain | None:
    if payload is None:
        return None
    if type(payload) is not list or not payload:
        raise FleetError("invalid_receiver_chain", "Chaîne de receivers invalide")
    spans = []
    for item in payload:
        if type(item) is not dict:
            raise FleetError("invalid_serialization", "Objet JSON non autorisé")
        keys = set(item)
        if not keys.issubset(set(_SPAN_KEYS) | {"open"}) or not keys.issuperset(_SPAN_KEYS):
            raise FleetError("invalid_serialization", "Schéma JSON non autorisé")
        # « open » est absent des états persistés avant l'extension — une
        # span sans le marqueur est close, comme elle l'a toujours été.
        open_flag = item.get("open", False)
        if type(open_flag) is not bool:
            raise FleetError("invalid_serialization", "Schéma JSON non autorisé")
        spans.append(
            ReceiverSpan(
                receiver=item["receiver"],
                first_sequence=item["first_sequence"],
                last_sequence=item["last_sequence"],
                open=open_flag,
            )
        )
    return ReceiverChain(tuple(spans))


def _deserialize_window(payload: object) -> ProofWindow | None:
    if payload is None:
        return None
    data = _closed_mapping(payload, _WINDOW_KEYS, code="invalid_serialization")
    return ProofWindow(start_utc=data["start_utc"], end_utc=data["end_utc"])


def _deserialize_proof(payload: object) -> ReconciliationProof | None:
    if payload is None:
        return None
    data = _closed_mapping(payload, _PROOF_KEYS, code="invalid_serialization")
    window = _deserialize_window(data["window"])
    if window is None:
        raise FleetError("invalid_reconciliation", "Fenêtre de preuve absente")
    return ReconciliationProof(
        window=window,
        source_count=data["source_count"],
        target_count=data["target_count"],
        missing=data["missing"],
        extra=data["extra"],
        duplicates=data["duplicates"],
        source_hash=data["source_hash"],
        target_hash=data["target_hash"],
        destination_freshness_seconds=data["destination_freshness_seconds"],
        freshness_slo_seconds=data["freshness_slo_seconds"],
        latency_seconds=data["latency_seconds"],
        throughput_rows_per_second=data["throughput_rows_per_second"],
        cost_units=data["cost_units"],
    )
