"""Démarrage historique d'un orchestrateur unique, indépendant du fournisseur.

Le START n'admet que l'état PREPARED, validé par PrepareRuntime et des
fournisseurs sentinelles jamais appelés. STARTING est persisté atomiquement
avant tout lancement. Un seul launcher.launch(request) porte toutes les
voies et la borne de concurrence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Protocol
import string
import threading
import uuid

from quadringent_control_plane import fleet as _fleet
from quadringent_control_plane.fleet import (
    MAX_CONCURRENCY,
    MIN_CONCURRENCY,
    FleetError,
    JournalCheckpoint,
)
from quadringent_control_plane.fleet_plan import (
    FleetPlan,
    HistoricalLane,
    lane_composition,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    PHASE_PREPARED,
    PrepareRuntime,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStoreError


HISTORY_FORMAT_VERSION = "quadringent-fleet-history-v1"
PHASE_STARTING = "STARTING"
PHASE_HISTORICAL = "HISTORICAL"
PHASE_HISTORY_FAILED = "HISTORY_FAILED"
RECEIPT_RUNNING = "RUNNING"

CODE_INVALID_PLAN = "invalid_plan"
CODE_INVALID_ENVIRONMENT = "invalid_environment"
CODE_INVALID_CONCURRENCY = "invalid_concurrency"
CODE_NOT_PREPARED = "not_prepared"
CODE_LAUNCH_FAILED = "launch_failed"
CODE_RECEIPT_MISMATCH = "receipt_mismatch"
CODE_UNSAFE_ORCHESTRATOR_ID = "unsafe_orchestrator_id"
CODE_UNSAFE_READER_ID = "unsafe_reader_id"
CODE_NEEDS_RECOVERY = "needs_recovery"
CODE_HISTORY_FAILED = "history_failed"
CODE_INVALID_RUNTIME_STATE = "invalid_runtime_state"

_SENSITIVE_TOKENS = ("host", "user", "password", "secret", "token", "credential")
_READER_ID_CHARS = frozenset(string.ascii_letters + string.digits + "._-")
_ORCHESTRATOR_ID_CHARS = frozenset(string.ascii_letters + string.digits + "._-")
_HISTORY_LOCK = threading.Lock()
_STATE_KEYS = (
    "format_version",
    "environment",
    "phase",
    "intent_id",
    "prepare_intent_id",
    "reader_id",
    "checkpoint",
    "manifest",
    "lanes",
    "max_concurrency",
    "receipt",
    "error_code",
    "needs_recovery",
)
_RECEIPT_KEYS = (
    "status",
    "intent_id",
    "prepare_intent_id",
    "reader_id",
    "checkpoint",
    "manifest",
    "lanes",
    "max_concurrency",
    "orchestrator_id",
)
_LANE_KEYS = ("slot", "tables", "row_count", "data_size")
_CHECKPOINT_KEYS = ("receiver", "sequence")
_ALLOWED_ERROR_CODES = frozenset(
    {
        CODE_INVALID_PLAN,
        CODE_INVALID_ENVIRONMENT,
        CODE_INVALID_CONCURRENCY,
        CODE_NOT_PREPARED,
        CODE_LAUNCH_FAILED,
        CODE_RECEIPT_MISMATCH,
        CODE_UNSAFE_ORCHESTRATOR_ID,
        CODE_UNSAFE_READER_ID,
        CODE_NEEDS_RECOVERY,
        CODE_HISTORY_FAILED,
        CODE_INVALID_RUNTIME_STATE,
    }
)
_SAFE_MESSAGES = {
    CODE_INVALID_PLAN: "Plan de flotte invalide",
    CODE_INVALID_ENVIRONMENT: "Environnement hors DEV",
    CODE_INVALID_CONCURRENCY: "Concurrence hors borne 1..4",
    CODE_NOT_PREPARED: "Préparation absente ou invalide",
    CODE_LAUNCH_FAILED: "Lancement de l'orchestrateur impossible",
    CODE_RECEIPT_MISMATCH: "Reçu de lancement non conforme",
    CODE_UNSAFE_ORCHESTRATOR_ID: "Identifiant d'orchestrateur non sûr",
    CODE_UNSAFE_READER_ID: "Identifiant de lecteur non sûr",
    CODE_NEEDS_RECOVERY: "Démarrage historique interrompu, reprise manuelle requise",
    CODE_HISTORY_FAILED: "Échec du démarrage historique",
    CODE_INVALID_RUNTIME_STATE: "État runtime illisible",
}


class RuntimeStore(Protocol):
    def load(self) -> dict[str, Any] | None:
        """Charge l'objet d'état ou None s'il est absent."""

    def save(self, mapping: Mapping[str, Any]) -> None:
        """Persiste atomiquement un objet JSON."""


class HistoryLauncher(Protocol):
    def launch(self, request: HistoryLaunchRequest) -> HistoryReceipt:
        """Lance exactement un orchestrateur historique."""


@dataclass(frozen=True)
class HistoryLaunchRequest:
    intent_id: str
    prepare_intent_id: str
    reader_id: str
    checkpoint: JournalCheckpoint
    manifest: tuple[str, ...]
    lanes: tuple[HistoricalLane, ...]
    max_concurrency: int


@dataclass(frozen=True)
class HistoryReceipt:
    status: str
    intent_id: str
    prepare_intent_id: str
    reader_id: str
    checkpoint: JournalCheckpoint
    manifest: tuple[str, ...]
    lanes: tuple[HistoricalLane, ...]
    max_concurrency: int
    orchestrator_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "intent_id": self.intent_id,
            "prepare_intent_id": self.prepare_intent_id,
            "reader_id": self.reader_id,
            "checkpoint": self.checkpoint.to_dict(),
            "manifest": list(self.manifest),
            "lanes": [lane.to_dict() for lane in self.lanes],
            "max_concurrency": self.max_concurrency,
            "orchestrator_id": self.orchestrator_id,
        }


@dataclass(frozen=True)
class HistoryOutcome:
    phase: str
    intent_id: str
    prepare_intent_id: str
    reader_id: str
    checkpoint: JournalCheckpoint
    manifest: tuple[str, ...]
    lanes: tuple[HistoricalLane, ...]
    max_concurrency: int
    receipt: HistoryReceipt
    needs_recovery: bool
    error_code: str | None


class _SentinelCheckpointProvider:
    def current(self, plan: FleetPlan) -> JournalCheckpoint:
        raise RuntimeError("sentinel checkpoint provider must never be called")


class _SentinelReaderLauncher:
    def launch(self, request: object) -> object:
        raise RuntimeError("sentinel reader launcher must never be called")


class HistoryRuntime:
    def __init__(
        self,
        plan: FleetPlan,
        prepare_store: RuntimeStore,
        history_store: RuntimeStore,
        launcher: HistoryLauncher,
    ) -> None:
        self._plan = plan
        self._prepare_store = prepare_store
        self._store = history_store
        self._launcher = launcher

    def start(self) -> HistoryOutcome:
        with _HISTORY_LOCK:
            return self._start_locked()

    def _start_locked(self) -> HistoryOutcome:
        try:
            raw = self._store.load()
        except AtomicJsonStateStoreError:
            raise _safe_error(CODE_INVALID_RUNTIME_STATE) from None
        except Exception:
            raise _safe_error(CODE_INVALID_RUNTIME_STATE) from None

        if raw is not None:
            return self._resume_existing(raw)

        if type(self._plan) is not FleetPlan:
            raise _safe_error(CODE_INVALID_PLAN) from None
        if self._plan.environment != _fleet.ENVIRONMENT:
            raise _safe_error(CODE_INVALID_ENVIRONMENT) from None
        plan_code: str | None = None
        lanes: tuple[HistoricalLane, ...] | None = None
        max_concurrency: int | None = None
        try:
            lanes, max_concurrency = self._require_history_plan()
        except FleetError as error:
            plan_code = error.code if error.code in _ALLOWED_ERROR_CODES else CODE_INVALID_PLAN
        if plan_code is not None:
            self._abort(plan_code, needs_recovery=False)
        if lanes is None or max_concurrency is None:
            self._abort(CODE_INVALID_PLAN, needs_recovery=False)
        prepared = self._require_prepared()
        intent_id = uuid.uuid4().hex
        self._save_phase(
            PHASE_STARTING,
            intent_id=intent_id,
            prepare_intent_id=prepared.intent_id,
            reader_id=prepared.receipt.reader_id,
            checkpoint=prepared.checkpoint,
            lanes=lanes,
            max_concurrency=max_concurrency,
            receipt=None,
            error_code=None,
            needs_recovery=False,
        )
        receipt = self._launch_once(
            intent_id=intent_id,
            prepare_intent_id=prepared.intent_id,
            reader_id=prepared.receipt.reader_id,
            checkpoint=prepared.checkpoint,
            lanes=lanes,
            max_concurrency=max_concurrency,
        )
        self._save_phase(
            PHASE_HISTORICAL,
            intent_id=intent_id,
            prepare_intent_id=prepared.intent_id,
            reader_id=prepared.receipt.reader_id,
            checkpoint=prepared.checkpoint,
            lanes=lanes,
            max_concurrency=max_concurrency,
            receipt=receipt,
            error_code=None,
            needs_recovery=False,
        )
        return HistoryOutcome(
            phase=PHASE_HISTORICAL,
            intent_id=intent_id,
            prepare_intent_id=prepared.intent_id,
            reader_id=prepared.receipt.reader_id,
            checkpoint=prepared.checkpoint,
            manifest=_fleet.MANIFEST,
            lanes=lanes,
            max_concurrency=max_concurrency,
            receipt=receipt,
            needs_recovery=False,
            error_code=None,
        )

    def _resume_existing(self, raw: object) -> HistoryOutcome:
        if type(raw) is not dict:
            self._abort(CODE_INVALID_RUNTIME_STATE, needs_recovery=True)
        phase = raw.get("phase")
        if phase == PHASE_HISTORICAL:
            return self._idempotent_historical(raw)
        if phase == PHASE_STARTING:
            self._abort_from_previous(raw, CODE_NEEDS_RECOVERY, needs_recovery=True)
        if phase == PHASE_HISTORY_FAILED:
            code = _persisted_error_code(raw.get("error_code"))
            needs_recovery = raw.get("needs_recovery") is True
            raise _safe_error(CODE_NEEDS_RECOVERY if needs_recovery else code) from None
        self._abort_from_previous(raw, CODE_NEEDS_RECOVERY, needs_recovery=True)

    def _idempotent_historical(self, raw: dict[str, object]) -> HistoryOutcome:
        try:
            state = _closed_mapping(raw, _STATE_KEYS)
            if state["format_version"] != HISTORY_FORMAT_VERSION:
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
            if state["environment"] != _fleet.ENVIRONMENT:
                raise FleetError(CODE_INVALID_ENVIRONMENT, _SAFE_MESSAGES[CODE_INVALID_ENVIRONMENT])
            if state["phase"] != PHASE_HISTORICAL:
                raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
            intent_id = _require_token(state["intent_id"], CODE_INVALID_RUNTIME_STATE)
            prepare_intent_id = _require_token(state["prepare_intent_id"], CODE_INVALID_RUNTIME_STATE)
            reader_id = _require_safe_reader_id(state["reader_id"])
            checkpoint = _parse_checkpoint(state["checkpoint"])
            manifest = _parse_manifest(state["manifest"])
            lanes = _parse_lanes(state["lanes"], code=CODE_INVALID_RUNTIME_STATE)
            max_concurrency = _parse_concurrency(state["max_concurrency"], code=CODE_INVALID_RUNTIME_STATE)
            if manifest != _fleet.MANIFEST:
                raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
            receipt = _parse_receipt(state["receipt"])
            _assert_receipt_matches(
                receipt,
                intent_id=intent_id,
                prepare_intent_id=prepare_intent_id,
                reader_id=reader_id,
                checkpoint=checkpoint,
                manifest=manifest,
                lanes=lanes,
                max_concurrency=max_concurrency,
            )
            if state["needs_recovery"] is not False or state["error_code"] is not None:
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
            plan_lanes, plan_concurrency = self._require_history_plan()
            if (
                lane_composition(lanes) != lane_composition(plan_lanes)
                or max_concurrency != plan_concurrency
            ):
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
        except FleetError as error:
            code = error.code if error.code in _ALLOWED_ERROR_CODES else CODE_INVALID_RUNTIME_STATE
        else:
            code = None
        if code is not None:
            self._abort_from_previous(raw, code, needs_recovery=True)
        prepare_code: str | None = None
        prepared: Any = None
        try:
            prepared = self._load_prepared()
        except FleetError as error:
            prepare_code = error.code if error.code in _ALLOWED_ERROR_CODES else CODE_NOT_PREPARED
        except Exception:
            prepare_code = CODE_NOT_PREPARED
        if prepare_code is not None:
            self._abort_from_previous(raw, prepare_code, needs_recovery=True)
        if (
            prepared is None
            or prepared.intent_id != prepare_intent_id
            or prepared.receipt.reader_id != reader_id
            or prepared.checkpoint != checkpoint
        ):
            self._abort_from_previous(raw, CODE_INVALID_RUNTIME_STATE, needs_recovery=True)
        return HistoryOutcome(
            phase=PHASE_HISTORICAL,
            intent_id=intent_id,
            prepare_intent_id=prepare_intent_id,
            reader_id=reader_id,
            checkpoint=checkpoint,
            manifest=manifest,
            lanes=lanes,
            max_concurrency=max_concurrency,
            receipt=receipt,
            needs_recovery=False,
            error_code=None,
        )

    def _require_history_plan(self) -> tuple[tuple[HistoricalLane, ...], int]:
        plan = self._plan
        if type(plan) is not FleetPlan:
            raise _safe_error(CODE_INVALID_PLAN) from None
        if plan.environment != _fleet.ENVIRONMENT:
            raise _safe_error(CODE_INVALID_ENVIRONMENT) from None
        if _fleet.TABLE_COUNT != len(_fleet.MANIFEST):
            raise _safe_error(CODE_INVALID_PLAN) from None
        max_concurrency = plan.max_concurrency
        if type(max_concurrency) is not int or max_concurrency < MIN_CONCURRENCY or max_concurrency > MAX_CONCURRENCY:
            raise _safe_error(CODE_INVALID_CONCURRENCY) from None
        lanes = plan.historical_lanes
        if type(lanes) is not tuple or not lanes:
            raise _safe_error(CODE_INVALID_PLAN) from None
        if len(lanes) > max_concurrency:
            raise _safe_error(CODE_INVALID_CONCURRENCY) from None
        assigned: list[str] = []
        for lane in lanes:
            if type(lane) is not HistoricalLane:
                raise _safe_error(CODE_INVALID_PLAN) from None
            assigned.extend(lane.tables)
        if len(assigned) != len(set(assigned)):
            raise _safe_error(CODE_INVALID_PLAN) from None
        if len(assigned) != _fleet.TABLE_COUNT or set(assigned) != set(_fleet.MANIFEST):
            raise _safe_error(CODE_INVALID_PLAN) from None
        return lanes, max_concurrency

    def _require_prepared(self) -> Any:
        prepare_code: str | None = None
        outcome: Any = None
        try:
            outcome = self._load_prepared()
        except FleetError as error:
            prepare_code = error.code if error.code in _ALLOWED_ERROR_CODES else CODE_NOT_PREPARED
        except Exception:
            prepare_code = CODE_NOT_PREPARED
        if prepare_code is not None:
            self._abort(prepare_code, needs_recovery=False)
        return outcome

    def _load_prepared(self) -> Any:
        try:
            raw = self._prepare_store.load()
        except AtomicJsonStateStoreError:
            raise _safe_error(CODE_INVALID_RUNTIME_STATE) from None
        except Exception:
            raise _safe_error(CODE_INVALID_RUNTIME_STATE) from None
        if raw is None or type(raw) is not dict or raw.get("phase") != PHASE_PREPARED:
            raise _safe_error(CODE_NOT_PREPARED) from None
        prepare_code: str | None = None
        outcome: Any = None
        try:
            outcome = PrepareRuntime(
                self._plan,
                self._prepare_store,
                _SentinelCheckpointProvider(),
                _SentinelReaderLauncher(),
            ).prepare()
        except FleetError as error:
            prepare_code = error.code if error.code in _ALLOWED_ERROR_CODES else CODE_NOT_PREPARED
        except Exception:
            prepare_code = CODE_NOT_PREPARED
        if prepare_code is not None:
            raise _safe_error(prepare_code) from None
        if (
            outcome is None
            or outcome.phase != PHASE_PREPARED
            or outcome.needs_recovery is not False
            or outcome.error_code is not None
            or outcome.manifest != _fleet.MANIFEST
            or outcome.receipt.status != RECEIPT_RUNNING
        ):
            raise _safe_error(CODE_NOT_PREPARED) from None
        return outcome

    def _launch_once(
        self,
        *,
        intent_id: str,
        prepare_intent_id: str,
        reader_id: str,
        checkpoint: JournalCheckpoint,
        lanes: tuple[HistoricalLane, ...],
        max_concurrency: int,
    ) -> HistoryReceipt:
        request = HistoryLaunchRequest(
            intent_id=intent_id,
            prepare_intent_id=prepare_intent_id,
            reader_id=reader_id,
            checkpoint=checkpoint,
            manifest=_fleet.MANIFEST,
            lanes=lanes,
            max_concurrency=max_concurrency,
        )
        raw_receipt: object = None
        launch_failed = False
        try:
            raw_receipt = self._launcher.launch(request)
        except Exception:
            launch_failed = True
        if launch_failed:
            self._abort(
                CODE_LAUNCH_FAILED,
                intent_id=intent_id,
                prepare_intent_id=prepare_intent_id,
                reader_id=reader_id,
                checkpoint=checkpoint,
                lanes=lanes,
                max_concurrency=max_concurrency,
                needs_recovery=True,
            )
        receipt_code: str | None = None
        receipt: HistoryReceipt | None = None
        try:
            receipt = _parse_receipt(raw_receipt)
            _assert_receipt_matches(
                receipt,
                intent_id=intent_id,
                prepare_intent_id=prepare_intent_id,
                reader_id=reader_id,
                checkpoint=checkpoint,
                manifest=_fleet.MANIFEST,
                lanes=lanes,
                max_concurrency=max_concurrency,
            )
        except FleetError as error:
            receipt_code = (
                error.code
                if error.code in {CODE_RECEIPT_MISMATCH, CODE_UNSAFE_ORCHESTRATOR_ID, CODE_UNSAFE_READER_ID}
                else CODE_RECEIPT_MISMATCH
            )
        if receipt_code is not None:
            self._abort(
                receipt_code,
                intent_id=intent_id,
                prepare_intent_id=prepare_intent_id,
                reader_id=reader_id,
                checkpoint=checkpoint,
                lanes=lanes,
                max_concurrency=max_concurrency,
                needs_recovery=True,
            )
        if type(receipt) is not HistoryReceipt:
            self._abort(
                CODE_RECEIPT_MISMATCH,
                intent_id=intent_id,
                prepare_intent_id=prepare_intent_id,
                reader_id=reader_id,
                checkpoint=checkpoint,
                lanes=lanes,
                max_concurrency=max_concurrency,
                needs_recovery=True,
            )
        return receipt

    def _save_phase(
        self,
        phase: str,
        *,
        intent_id: str,
        prepare_intent_id: str,
        reader_id: str,
        checkpoint: JournalCheckpoint,
        lanes: tuple[HistoricalLane, ...],
        max_concurrency: int,
        receipt: HistoryReceipt | None,
        error_code: str | None,
        needs_recovery: bool,
    ) -> None:
        payload = _state_payload(
            phase=phase,
            intent_id=intent_id,
            prepare_intent_id=prepare_intent_id,
            reader_id=reader_id,
            checkpoint=checkpoint,
            lanes=lanes,
            max_concurrency=max_concurrency,
            receipt=receipt,
            error_code=error_code,
            needs_recovery=needs_recovery,
        )
        try:
            self._store.save(payload)
        except Exception:
            raise _safe_error(CODE_HISTORY_FAILED) from None

    def _abort(
        self,
        code: str,
        *,
        intent_id: str | None = None,
        prepare_intent_id: str | None = None,
        reader_id: str | None = None,
        checkpoint: JournalCheckpoint | None = None,
        lanes: tuple[HistoricalLane, ...] | None = None,
        max_concurrency: int | None = None,
        needs_recovery: bool,
    ) -> NoReturn:
        safe_code = _persisted_error_code(code)
        payload = _state_payload(
            phase=PHASE_HISTORY_FAILED,
            intent_id=intent_id,
            prepare_intent_id=prepare_intent_id,
            reader_id=reader_id,
            checkpoint=checkpoint,
            lanes=lanes,
            max_concurrency=max_concurrency,
            receipt=None,
            error_code=safe_code,
            needs_recovery=needs_recovery,
        )
        try:
            self._store.save(payload)
        except Exception:
            raise _safe_error(CODE_HISTORY_FAILED) from None
        raise _safe_error(safe_code) from None

    def _abort_from_previous(
        self,
        previous: Mapping[str, object],
        code: str,
        *,
        needs_recovery: bool,
    ) -> NoReturn:
        intent_id = _optional_safe_token(previous.get("intent_id"))
        prepare_intent_id = _optional_safe_token(previous.get("prepare_intent_id"))
        reader_id = _optional_safe_reader_id(previous.get("reader_id"))
        checkpoint = _optional_checkpoint(previous.get("checkpoint"))
        lanes = _optional_lanes(previous.get("lanes"))
        max_concurrency = _optional_concurrency(previous.get("max_concurrency"))
        payload = {
            "format_version": HISTORY_FORMAT_VERSION,
            "environment": _fleet.ENVIRONMENT,
            "phase": PHASE_HISTORY_FAILED,
            "intent_id": intent_id,
            "prepare_intent_id": prepare_intent_id,
            "reader_id": reader_id,
            "checkpoint": None if checkpoint is None else checkpoint.to_dict(),
            "manifest": list(_fleet.MANIFEST),
            "lanes": None if lanes is None else [lane.to_dict() for lane in lanes],
            "max_concurrency": max_concurrency,
            "receipt": None,
            "error_code": _persisted_error_code(code),
            "needs_recovery": needs_recovery,
        }
        try:
            self._store.save(payload)
        except Exception:
            raise _safe_error(CODE_HISTORY_FAILED) from None
        raise _safe_error(_persisted_error_code(code)) from None


def _state_payload(
    *,
    phase: str,
    intent_id: str | None,
    prepare_intent_id: str | None,
    reader_id: str | None,
    checkpoint: JournalCheckpoint | None,
    lanes: tuple[HistoricalLane, ...] | None,
    max_concurrency: int | None,
    receipt: HistoryReceipt | None,
    error_code: str | None,
    needs_recovery: bool,
) -> dict[str, object]:
    return {
        "format_version": HISTORY_FORMAT_VERSION,
        "environment": _fleet.ENVIRONMENT,
        "phase": phase,
        "intent_id": intent_id,
        "prepare_intent_id": prepare_intent_id,
        "reader_id": reader_id,
        "checkpoint": None if checkpoint is None else checkpoint.to_dict(),
        "manifest": list(_fleet.MANIFEST),
        "lanes": None if lanes is None else [lane.to_dict() for lane in lanes],
        "max_concurrency": max_concurrency,
        "receipt": None if receipt is None else receipt.to_dict(),
        "error_code": error_code,
        "needs_recovery": needs_recovery,
    }


def _assert_receipt_matches(
    receipt: HistoryReceipt,
    *,
    intent_id: str,
    prepare_intent_id: str,
    reader_id: str,
    checkpoint: JournalCheckpoint,
    manifest: tuple[str, ...],
    lanes: tuple[HistoricalLane, ...],
    max_concurrency: int,
) -> None:
    if receipt.status != RECEIPT_RUNNING:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.intent_id != intent_id:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.prepare_intent_id != prepare_intent_id:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.reader_id != reader_id:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    _require_safe_reader_id(receipt.reader_id)
    _require_safe_reader_id(reader_id)
    if receipt.checkpoint != checkpoint:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.manifest != manifest:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.lanes != lanes:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.max_concurrency != max_concurrency:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    _require_safe_orchestrator_id(receipt.orchestrator_id)


def _parse_receipt(value: object) -> HistoryReceipt:
    if type(value) is HistoryReceipt:
        _require_safe_orchestrator_id(value.orchestrator_id)
        if value.manifest != tuple(value.manifest) or value.lanes != tuple(value.lanes):
            raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
        return HistoryReceipt(
            status=_require_str(value.status, CODE_RECEIPT_MISMATCH),
            intent_id=_require_token(value.intent_id, CODE_RECEIPT_MISMATCH),
            prepare_intent_id=_require_token(value.prepare_intent_id, CODE_RECEIPT_MISMATCH),
            reader_id=_require_safe_reader_id(value.reader_id),
            checkpoint=_require_checkpoint(value.checkpoint),
            manifest=_parse_manifest(list(value.manifest)),
            lanes=_parse_lanes(list(value.lanes), code=CODE_RECEIPT_MISMATCH),
            max_concurrency=_parse_concurrency(value.max_concurrency, code=CODE_RECEIPT_MISMATCH),
            orchestrator_id=_require_safe_orchestrator_id(value.orchestrator_id),
        )
    data = _closed_mapping(value, _RECEIPT_KEYS)
    return HistoryReceipt(
        status=_require_str(data["status"], CODE_RECEIPT_MISMATCH),
        intent_id=_require_token(data["intent_id"], CODE_RECEIPT_MISMATCH),
        prepare_intent_id=_require_token(data["prepare_intent_id"], CODE_RECEIPT_MISMATCH),
        reader_id=_require_safe_reader_id(data["reader_id"]),
        checkpoint=_parse_checkpoint(data["checkpoint"]),
        manifest=_parse_manifest(data["manifest"]),
        lanes=_parse_lanes(data["lanes"], code=CODE_RECEIPT_MISMATCH),
        max_concurrency=_parse_concurrency(data["max_concurrency"], code=CODE_RECEIPT_MISMATCH),
        orchestrator_id=_require_safe_orchestrator_id(data["orchestrator_id"]),
    )


def _parse_lanes(value: object, *, code: str) -> tuple[HistoricalLane, ...]:
    if type(value) is not list:
        raise FleetError(code, _SAFE_MESSAGES[code])
    lanes = tuple(_parse_lane(item, code=code) for item in value)
    assigned: list[str] = []
    for lane in lanes:
        assigned.extend(lane.tables)
    if len(assigned) != len(set(assigned)):
        raise FleetError(code, _SAFE_MESSAGES[code])
    if len(assigned) != _fleet.TABLE_COUNT or set(assigned) != set(_fleet.MANIFEST):
        raise FleetError(code, _SAFE_MESSAGES[code])
    return lanes


def _parse_lane(value: object, *, code: str) -> HistoricalLane:
    if type(value) is HistoricalLane:
        if value.tables != tuple(value.tables):
            raise FleetError(code, _SAFE_MESSAGES[code])
        return value
    data = _closed_mapping(value, _LANE_KEYS)
    names = data["tables"]
    if type(names) is not list:
        raise FleetError(code, _SAFE_MESSAGES[code])
    try:
        return HistoricalLane(
            slot=data["slot"],
            tables=tuple(_require_token(name, code) for name in names),
            row_count=data["row_count"],
            data_size=data["data_size"],
        )
    except FleetError as error:
        if error.code in _ALLOWED_ERROR_CODES:
            raise
        raise FleetError(code, _SAFE_MESSAGES[code]) from None


def _parse_manifest(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    names = tuple(_require_token(item, CODE_RECEIPT_MISMATCH) for item in value)
    if names != _fleet.MANIFEST:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    return names


def _parse_checkpoint(value: object) -> JournalCheckpoint:
    data = _closed_mapping(value, _CHECKPOINT_KEYS)
    return JournalCheckpoint(receiver=data["receiver"], sequence=data["sequence"])


def _parse_concurrency(value: object, *, code: str) -> int:
    if type(value) is not int or value < MIN_CONCURRENCY or value > MAX_CONCURRENCY:
        raise FleetError(code, _SAFE_MESSAGES[code])
    return value


def _require_checkpoint(value: object) -> JournalCheckpoint:
    if type(value) is not JournalCheckpoint:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    return value


def _optional_checkpoint(value: object) -> JournalCheckpoint | None:
    if value is None:
        return None
    try:
        return _parse_checkpoint(value)
    except (FleetError, TypeError, ValueError):
        return None


def _optional_lanes(value: object) -> tuple[HistoricalLane, ...] | None:
    if value is None:
        return None
    try:
        return _parse_lanes(value, code=CODE_INVALID_RUNTIME_STATE)
    except (FleetError, TypeError, ValueError):
        return None


def _optional_concurrency(value: object) -> int | None:
    if value is None:
        return None
    try:
        return _parse_concurrency(value, code=CODE_INVALID_RUNTIME_STATE)
    except FleetError:
        return None


def _optional_safe_token(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        return _require_token(value, CODE_INVALID_RUNTIME_STATE)
    except FleetError:
        return None


def _optional_safe_reader_id(value: object) -> str | None:
    try:
        return _require_safe_reader_id(value)
    except FleetError:
        return None


def _closed_mapping(value: object, keys: tuple[str, ...]) -> dict[str, object]:
    if type(value) is not dict:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if set(value) != set(keys):
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    for key in value:
        if type(key) is not str:
            raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
        lowered = key.lower()
        if any(token in lowered for token in _SENSITIVE_TOKENS):
            raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    return value


def _require_str(value: object, code: str) -> str:
    if type(value) is not str:
        raise FleetError(code, _SAFE_MESSAGES[code])
    return value


def _require_token(value: object, code: str) -> str:
    text = _require_str(value, code)
    if not text.strip() or text != text.strip():
        raise FleetError(code, _SAFE_MESSAGES[code])
    return text


def _require_safe_reader_id(value: object) -> str:
    if type(value) is not str:
        raise FleetError(CODE_UNSAFE_READER_ID, _SAFE_MESSAGES[CODE_UNSAFE_READER_ID])
    if not value.strip() or value != value.strip():
        raise FleetError(CODE_UNSAFE_READER_ID, _SAFE_MESSAGES[CODE_UNSAFE_READER_ID])
    lowered = value.lower()
    if any(token in lowered for token in _SENSITIVE_TOKENS):
        raise FleetError(CODE_UNSAFE_READER_ID, _SAFE_MESSAGES[CODE_UNSAFE_READER_ID])
    if any(char not in _READER_ID_CHARS for char in value):
        raise FleetError(CODE_UNSAFE_READER_ID, _SAFE_MESSAGES[CODE_UNSAFE_READER_ID])
    return value


def _require_safe_orchestrator_id(value: object) -> str:
    if type(value) is not str:
        raise FleetError(CODE_UNSAFE_ORCHESTRATOR_ID, _SAFE_MESSAGES[CODE_UNSAFE_ORCHESTRATOR_ID])
    if not value.strip() or value != value.strip():
        raise FleetError(CODE_UNSAFE_ORCHESTRATOR_ID, _SAFE_MESSAGES[CODE_UNSAFE_ORCHESTRATOR_ID])
    lowered = value.lower()
    if any(token in lowered for token in _SENSITIVE_TOKENS):
        raise FleetError(CODE_UNSAFE_ORCHESTRATOR_ID, _SAFE_MESSAGES[CODE_UNSAFE_ORCHESTRATOR_ID])
    if any(char not in _ORCHESTRATOR_ID_CHARS for char in value):
        raise FleetError(CODE_UNSAFE_ORCHESTRATOR_ID, _SAFE_MESSAGES[CODE_UNSAFE_ORCHESTRATOR_ID])
    return value


def _persisted_error_code(code: object) -> str:
    if type(code) is str and code in _ALLOWED_ERROR_CODES:
        return code
    return CODE_HISTORY_FAILED


def _safe_error(code: str) -> FleetError:
    safe_code = _persisted_error_code(code)
    error = FleetError(safe_code, _SAFE_MESSAGES[safe_code])
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    return error
