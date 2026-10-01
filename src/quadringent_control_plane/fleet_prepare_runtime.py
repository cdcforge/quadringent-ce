"""Préparation d'un lecteur journal unique, indépendante du fournisseur.

Le checkpoint de cutover du plan n'est jamais lu. Seul un checkpoint frais
relevé au moment de l'appel est admissible. L'état PREPARING est persisté
atomiquement avant tout lancement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Protocol
import string
import threading
import uuid

from quadringent_control_plane import fleet as _fleet
from quadringent_control_plane.fleet import (
    FleetError,
    JournalCheckpoint,
)
from quadringent_control_plane.fleet_plan import READER_KIND, FleetPlan, JournalGroupPlan
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStoreError


PREPARE_FORMAT_VERSION = "quadringent-fleet-prepare-v1"
PHASE_PREPARING = "PREPARING"
PHASE_PREPARED = "PREPARED"
PHASE_PREPARE_FAILED = "PREPARE_FAILED"
RECEIPT_RUNNING = "RUNNING"
READER_COUNT = 1

CODE_INVALID_PLAN = "invalid_plan"
CODE_INVALID_ENVIRONMENT = "invalid_environment"
CODE_CHECKPOINT_UNAVAILABLE = "checkpoint_unavailable"
CODE_LAUNCH_FAILED = "launch_failed"
CODE_RECEIPT_MISMATCH = "receipt_mismatch"
CODE_UNSAFE_READER_ID = "unsafe_reader_id"
CODE_NEEDS_RECOVERY = "needs_recovery"
CODE_PREPARE_FAILED = "prepare_failed"
CODE_INVALID_RUNTIME_STATE = "invalid_runtime_state"

_SENSITIVE_TOKENS = ("host", "user", "password", "secret", "token", "credential")
_READER_ID_CHARS = frozenset(string.ascii_letters + string.digits + "._-")
_PREPARE_LOCK = threading.Lock()
_STATE_KEYS = (
    "format_version",
    "environment",
    "phase",
    "intent_id",
    "manifest",
    "checkpoint",
    "journal_library",
    "journal_name",
    "reader_kind",
    "reader_count",
    "receipt",
    "error_code",
    "needs_recovery",
)
_RECEIPT_KEYS = ("status", "intent_id", "checkpoint", "manifest", "reader_id")
_CHECKPOINT_KEYS = ("receiver", "sequence")
_ALLOWED_ERROR_CODES = frozenset(
    {
        CODE_INVALID_PLAN,
        CODE_INVALID_ENVIRONMENT,
        CODE_CHECKPOINT_UNAVAILABLE,
        CODE_LAUNCH_FAILED,
        CODE_RECEIPT_MISMATCH,
        CODE_UNSAFE_READER_ID,
        CODE_NEEDS_RECOVERY,
        CODE_PREPARE_FAILED,
        CODE_INVALID_RUNTIME_STATE,
    }
)
_SAFE_MESSAGES = {
    CODE_INVALID_PLAN: "Plan de flotte invalide",
    CODE_INVALID_ENVIRONMENT: "Environnement hors DEV",
    CODE_CHECKPOINT_UNAVAILABLE: "Checkpoint journal indisponible",
    CODE_LAUNCH_FAILED: "Lancement du lecteur impossible",
    CODE_RECEIPT_MISMATCH: "Reçu de lancement non conforme",
    CODE_UNSAFE_READER_ID: "Identifiant de lecteur non sûr",
    CODE_NEEDS_RECOVERY: "Préparation interrompue, reprise manuelle requise",
    CODE_PREPARE_FAILED: "Échec de préparation",
    CODE_INVALID_RUNTIME_STATE: "État runtime illisible",
}


class RuntimeStore(Protocol):
    def load(self) -> dict[str, Any] | None:
        """Charge l'objet d'état ou None s'il est absent."""

    def save(self, mapping: Mapping[str, Any]) -> None:
        """Persiste atomiquement un objet JSON."""


class CheckpointProvider(Protocol):
    def current(self, plan: FleetPlan) -> JournalCheckpoint:
        """Relève un checkpoint frais au moment de l'appel."""


class ReaderLauncher(Protocol):
    def launch(self, request: ReaderLaunchRequest) -> ReaderReceipt:
        """Lance exactement un lecteur multi-objets."""


@dataclass(frozen=True)
class ReaderLaunchRequest:
    intent_id: str
    reader_kind: str
    reader_count: int
    journal_library: str
    journal_name: str
    manifest: tuple[str, ...]
    checkpoint: JournalCheckpoint


@dataclass(frozen=True)
class ReaderReceipt:
    status: str
    intent_id: str
    checkpoint: JournalCheckpoint
    manifest: tuple[str, ...]
    reader_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "intent_id": self.intent_id,
            "checkpoint": self.checkpoint.to_dict(),
            "manifest": list(self.manifest),
            "reader_id": self.reader_id,
        }


@dataclass(frozen=True)
class PrepareOutcome:
    phase: str
    intent_id: str
    checkpoint: JournalCheckpoint
    manifest: tuple[str, ...]
    receipt: ReaderReceipt
    needs_recovery: bool
    error_code: str | None


class PrepareRuntime:
    def __init__(
        self,
        plan: FleetPlan,
        store: RuntimeStore,
        provider: CheckpointProvider,
        launcher: ReaderLauncher,
    ) -> None:
        self._plan = plan
        self._store = store
        self._provider = provider
        self._launcher = launcher

    def prepare(self) -> PrepareOutcome:
        with _PREPARE_LOCK:
            return self._prepare_locked()

    def _prepare_locked(self) -> PrepareOutcome:
        try:
            raw = self._store.load()
        except AtomicJsonStateStoreError:
            raise _safe_error(CODE_INVALID_RUNTIME_STATE) from None
        except Exception:
            raise _safe_error(CODE_INVALID_RUNTIME_STATE) from None

        if raw is not None:
            return self._resume_existing(raw)

        group = self._require_dev_plan()
        intent_id = uuid.uuid4().hex
        checkpoint = self._fresh_checkpoint(intent_id, group)
        self._save_phase(
            PHASE_PREPARING,
            intent_id=intent_id,
            checkpoint=checkpoint,
            group=group,
            receipt=None,
            error_code=None,
            needs_recovery=False,
        )
        receipt = self._launch_once(intent_id, checkpoint, group)
        self._save_phase(
            PHASE_PREPARED,
            intent_id=intent_id,
            checkpoint=checkpoint,
            group=group,
            receipt=receipt,
            error_code=None,
            needs_recovery=False,
        )
        return PrepareOutcome(
            phase=PHASE_PREPARED,
            intent_id=intent_id,
            checkpoint=checkpoint,
            manifest=_fleet.MANIFEST,
            receipt=receipt,
            needs_recovery=False,
            error_code=None,
        )

    def _resume_existing(self, raw: object) -> PrepareOutcome:
        if type(raw) is not dict:
            self._abort(CODE_INVALID_RUNTIME_STATE, needs_recovery=True)
        phase = raw.get("phase")
        if phase == PHASE_PREPARED:
            return self._idempotent_prepared(raw)
        if phase == PHASE_PREPARING:
            self._abort_from_previous(raw, CODE_NEEDS_RECOVERY, needs_recovery=True)
        if phase == PHASE_PREPARE_FAILED:
            code = _persisted_error_code(raw.get("error_code"))
            needs_recovery = raw.get("needs_recovery") is True
            raise _safe_error(CODE_NEEDS_RECOVERY if needs_recovery else code) from None
        self._abort_from_previous(raw, CODE_NEEDS_RECOVERY, needs_recovery=True)

    def _idempotent_prepared(self, raw: dict[str, object]) -> PrepareOutcome:
        try:
            state = _closed_mapping(raw, _STATE_KEYS)
            if state["format_version"] != PREPARE_FORMAT_VERSION:
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
            if state["environment"] != _fleet.ENVIRONMENT:
                raise FleetError(CODE_INVALID_ENVIRONMENT, _SAFE_MESSAGES[CODE_INVALID_ENVIRONMENT])
            if state["phase"] != PHASE_PREPARED:
                raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
            intent_id = _require_token(state["intent_id"], CODE_INVALID_RUNTIME_STATE)
            checkpoint = _parse_checkpoint(state["checkpoint"])
            manifest = _parse_manifest(state["manifest"])
            if manifest != _fleet.MANIFEST:
                raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
            receipt = _parse_receipt(state["receipt"])
            _assert_receipt_matches(receipt, intent_id, checkpoint, manifest)
            if state["needs_recovery"] is not False or state["error_code"] is not None:
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
            group = self._require_dev_plan()
            if (
                state["journal_library"] != group.library
                or state["journal_name"] != group.name
                or state["reader_kind"] != group.reader_kind
                or state["reader_count"] != group.reader_count
            ):
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
        except FleetError as error:
            code = error.code if error.code in _ALLOWED_ERROR_CODES else CODE_INVALID_RUNTIME_STATE
        else:
            code = None
        if code is not None:
            self._abort_from_previous(raw, code, needs_recovery=True)
        return PrepareOutcome(
            phase=PHASE_PREPARED,
            intent_id=intent_id,
            checkpoint=checkpoint,
            manifest=manifest,
            receipt=receipt,
            needs_recovery=False,
            error_code=None,
        )

    def _require_dev_plan(self) -> JournalGroupPlan:
        plan = self._plan
        if type(plan) is not FleetPlan:
            raise _safe_error(CODE_INVALID_PLAN) from None
        if plan.environment != _fleet.ENVIRONMENT:
            raise _safe_error(CODE_INVALID_ENVIRONMENT) from None
        if _fleet.TABLE_COUNT != len(_fleet.MANIFEST):
            raise _safe_error(CODE_INVALID_PLAN) from None
        groups = plan.journal_groups
        if type(groups) is not tuple or len(groups) != 1:
            raise _safe_error(CODE_INVALID_PLAN) from None
        group = groups[0]
        if type(group) is not JournalGroupPlan:
            raise _safe_error(CODE_INVALID_PLAN) from None
        if group.reader_kind != READER_KIND or group.reader_count != READER_COUNT:
            raise _safe_error(CODE_INVALID_PLAN) from None
        if group.table_names != _fleet.MANIFEST:
            raise _safe_error(CODE_INVALID_PLAN) from None
        return group

    def _fresh_checkpoint(self, intent_id: str, group: JournalGroupPlan) -> JournalCheckpoint:
        checkpoint: object = None
        try:
            checkpoint = self._provider.current(self._plan)
        except Exception:
            checkpoint = None
        if type(checkpoint) is not JournalCheckpoint:
            self._abort(
                CODE_CHECKPOINT_UNAVAILABLE,
                intent_id=intent_id,
                group=group,
                needs_recovery=False,
            )
        return checkpoint

    def _launch_once(
        self,
        intent_id: str,
        checkpoint: JournalCheckpoint,
        group: JournalGroupPlan,
    ) -> ReaderReceipt:
        request = ReaderLaunchRequest(
            intent_id=intent_id,
            reader_kind=READER_KIND,
            reader_count=READER_COUNT,
            journal_library=group.library,
            journal_name=group.name,
            manifest=_fleet.MANIFEST,
            checkpoint=checkpoint,
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
                checkpoint=checkpoint,
                group=group,
                needs_recovery=True,
            )
        receipt_code: str | None = None
        receipt: ReaderReceipt | None = None
        try:
            receipt = _parse_receipt(raw_receipt)
            _assert_receipt_matches(receipt, intent_id, checkpoint, _fleet.MANIFEST)
        except FleetError as error:
            receipt_code = (
                error.code
                if error.code in {CODE_RECEIPT_MISMATCH, CODE_UNSAFE_READER_ID}
                else CODE_RECEIPT_MISMATCH
            )
        if receipt_code is not None:
            self._abort(
                receipt_code,
                intent_id=intent_id,
                checkpoint=checkpoint,
                group=group,
                needs_recovery=True,
            )
        if type(receipt) is not ReaderReceipt:
            self._abort(
                CODE_RECEIPT_MISMATCH,
                intent_id=intent_id,
                checkpoint=checkpoint,
                group=group,
                needs_recovery=True,
            )
        return receipt

    def _save_phase(
        self,
        phase: str,
        *,
        intent_id: str,
        checkpoint: JournalCheckpoint,
        group: JournalGroupPlan,
        receipt: ReaderReceipt | None,
        error_code: str | None,
        needs_recovery: bool,
    ) -> None:
        payload = _state_payload(
            phase=phase,
            intent_id=intent_id,
            checkpoint=checkpoint,
            group=group,
            receipt=receipt,
            error_code=error_code,
            needs_recovery=needs_recovery,
        )
        try:
            self._store.save(payload)
        except Exception:
            raise _safe_error(CODE_PREPARE_FAILED) from None

    def _abort(
        self,
        code: str,
        *,
        intent_id: str | None = None,
        checkpoint: JournalCheckpoint | None = None,
        group: JournalGroupPlan | None = None,
        needs_recovery: bool,
    ) -> NoReturn:
        safe_code = _persisted_error_code(code)
        payload = _state_payload(
            phase=PHASE_PREPARE_FAILED,
            intent_id=intent_id,
            checkpoint=checkpoint,
            group=group,
            receipt=None,
            error_code=safe_code,
            needs_recovery=needs_recovery,
        )
        try:
            self._store.save(payload)
        except Exception:
            raise _safe_error(CODE_PREPARE_FAILED) from None
        raise _safe_error(safe_code) from None

    def _abort_from_previous(
        self,
        previous: Mapping[str, object],
        code: str,
        *,
        needs_recovery: bool,
    ) -> NoReturn:
        intent_id = _optional_safe_token(previous.get("intent_id"))
        checkpoint = _optional_checkpoint(previous.get("checkpoint"))
        library = _optional_safe_token(previous.get("journal_library"))
        name = _optional_safe_token(previous.get("journal_name"))
        payload = {
            "format_version": PREPARE_FORMAT_VERSION,
            "environment": _fleet.ENVIRONMENT,
            "phase": PHASE_PREPARE_FAILED,
            "intent_id": intent_id,
            "manifest": list(_fleet.MANIFEST),
            "checkpoint": None if checkpoint is None else checkpoint.to_dict(),
            "journal_library": library,
            "journal_name": name,
            "reader_kind": READER_KIND,
            "reader_count": READER_COUNT,
            "receipt": None,
            "error_code": _persisted_error_code(code),
            "needs_recovery": needs_recovery,
        }
        try:
            self._store.save(payload)
        except Exception:
            raise _safe_error(CODE_PREPARE_FAILED) from None
        raise _safe_error(_persisted_error_code(code)) from None


def _state_payload(
    *,
    phase: str,
    intent_id: str | None,
    checkpoint: JournalCheckpoint | None,
    group: JournalGroupPlan | None,
    receipt: ReaderReceipt | None,
    error_code: str | None,
    needs_recovery: bool,
) -> dict[str, object]:
    return {
        "format_version": PREPARE_FORMAT_VERSION,
        "environment": _fleet.ENVIRONMENT,
        "phase": phase,
        "intent_id": intent_id,
        "manifest": list(_fleet.MANIFEST),
        "checkpoint": None if checkpoint is None else checkpoint.to_dict(),
        "journal_library": None if group is None else group.library,
        "journal_name": None if group is None else group.name,
        "reader_kind": READER_KIND,
        "reader_count": READER_COUNT,
        "receipt": None if receipt is None else receipt.to_dict(),
        "error_code": error_code,
        "needs_recovery": needs_recovery,
    }


def _assert_receipt_matches(
    receipt: ReaderReceipt,
    intent_id: str,
    checkpoint: JournalCheckpoint,
    manifest: tuple[str, ...],
) -> None:
    if receipt.status != RECEIPT_RUNNING:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.intent_id != intent_id:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.checkpoint != checkpoint:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    if receipt.manifest != manifest:
        raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
    _require_safe_reader_id(receipt.reader_id)


def _parse_receipt(value: object) -> ReaderReceipt:
    if type(value) is ReaderReceipt:
        _require_safe_reader_id(value.reader_id)
        if value.manifest != tuple(value.manifest):
            raise FleetError(CODE_RECEIPT_MISMATCH, _SAFE_MESSAGES[CODE_RECEIPT_MISMATCH])
        return ReaderReceipt(
            status=_require_str(value.status, CODE_RECEIPT_MISMATCH),
            intent_id=_require_token(value.intent_id, CODE_RECEIPT_MISMATCH),
            checkpoint=_require_checkpoint(value.checkpoint),
            manifest=_parse_manifest(list(value.manifest)),
            reader_id=_require_safe_reader_id(value.reader_id),
        )
    data = _closed_mapping(value, _RECEIPT_KEYS)
    return ReaderReceipt(
        status=_require_str(data["status"], CODE_RECEIPT_MISMATCH),
        intent_id=_require_token(data["intent_id"], CODE_RECEIPT_MISMATCH),
        checkpoint=_parse_checkpoint(data["checkpoint"]),
        manifest=_parse_manifest(data["manifest"]),
        reader_id=_require_safe_reader_id(data["reader_id"]),
    )


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


def _optional_safe_token(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        return _require_token(value, CODE_INVALID_RUNTIME_STATE)
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


def _persisted_error_code(code: object) -> str:
    if type(code) is str and code in _ALLOWED_ERROR_CODES:
        return code
    return CODE_PREPARE_FAILED


def _safe_error(code: str) -> FleetError:
    safe_code = _persisted_error_code(code)
    error = FleetError(safe_code, _SAFE_MESSAGES[safe_code])
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    return error


def __getattr__(name: str):
    """Compatibilité paresseuse : le manifeste du site à l'appel."""

    if name == "MANIFEST":
        return _fleet.MANIFEST
    raise AttributeError(name)
