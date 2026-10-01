"""Suspension et reprise coopératives des Jobs de flotte.

Suspendre un Job Kubernetes arrête son pod ; le curseur durable déjà commité
reste la seule position de reprise. Rien n'est perdu, rien n'est deviné : un
redémarrage repart de la dernière fenêtre certifiée.

Ce module n'agit que sur `spec.suspend`. Il refuse d'agir si l'intention
durable ne désigne pas le Job, si le Job a disparu, ou si l'effet demandé ne
peut pas être constaté par une relecture. Une suspension déjà en place est
idempotente ; une reprise sans suspension enregistrée est refusée.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import string
import threading

from . import fleet as _fleet
from .fleet import FleetError
from .fleet_history_runtime import PHASE_HISTORICAL
from .fleet_job_launcher import KIND_HISTORY, KIND_READER, LABEL_INTENT, LABEL_KIND
from .fleet_prepare_runtime import PHASE_PREPARED
from .k8s_jobs import JobsApiError, KubernetesJobsClient


PAUSE_FORMAT_VERSION = "quadringent-fleet-pause-v1"
PHASE_PAUSED = "PAUSED"
PHASE_RESUMED = "RESUMED"

CODE_NOT_PREPARED = "not_prepared"
CODE_ALREADY_PAUSED = "already_paused"
CODE_NOT_PAUSED = "not_paused"
CODE_NEEDS_RECOVERY = "needs_recovery"
CODE_JOB_UNAVAILABLE = "job_unavailable"
CODE_SUSPEND_FAILED = "suspend_failed"
CODE_INVALID_RUNTIME_STATE = "invalid_runtime_state"

_SAFE_MESSAGES = {
    CODE_NOT_PREPARED: "Aucune préparation à suspendre",
    CODE_ALREADY_PAUSED: "Flotte déjà suspendue",
    CODE_NOT_PAUSED: "Flotte non suspendue",
    CODE_NEEDS_RECOVERY: "Suspension incohérente, reprise manuelle requise",
    CODE_JOB_UNAVAILABLE: "Job de flotte introuvable",
    CODE_SUSPEND_FAILED: "Effet de suspension non observé",
    CODE_INVALID_RUNTIME_STATE: "État de suspension illisible",
}

_STATE_KEYS = (
    "format_version",
    "environment",
    "phase",
    "intent_id",
    "reader_id",
    "history_intent_id",
    "history_id",
    "checkpoint",
)
_CHECKPOINT_KEYS = ("receiver", "sequence")
_IDENTIFIER_CHARS = frozenset(string.ascii_letters + string.digits + "._-")
_PAUSE_LOCK = threading.Lock()

JobObserver = KubernetesJobsClient


@dataclass(frozen=True)
class PauseOutcome:
    phase: str
    suspended: tuple[str, ...]
    already_suspended: tuple[str, ...]
    reader_id: str
    history_id: str | None
    error_code: str | None
    needs_recovery: bool


class _Refusal(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FleetPauseRuntime:
    """Suspend ou reprend les Jobs d'une intention durable unique."""

    def __init__(
        self,
        prepare_store: object,
        history_store: object,
        pause_store: object,
        jobs: JobObserver,
    ) -> None:
        for store in (prepare_store, history_store, pause_store):
            if not callable(getattr(store, "load", None)) or not callable(
                getattr(store, "save", None)
            ):
                raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
        if not isinstance(jobs, KubernetesJobsClient):
            raise FleetError(CODE_INVALID_RUNTIME_STATE, _SAFE_MESSAGES[CODE_INVALID_RUNTIME_STATE])
        self._prepare_store = prepare_store
        self._history_store = history_store
        self._pause_store = pause_store
        self._jobs = jobs

    def pause(self) -> PauseOutcome:
        with _PAUSE_LOCK:
            return self._guarded(self._pause_locked)

    def resume(self) -> PauseOutcome:
        with _PAUSE_LOCK:
            return self._guarded(self._resume_locked)

    def _guarded(self, operation: Any) -> PauseOutcome:
        try:
            return operation()
        except _Refusal as refusal:
            return PauseOutcome(
                phase="",
                suspended=(),
                already_suspended=(),
                reader_id="",
                history_id=None,
                error_code=refusal.code,
                needs_recovery=refusal.code in {CODE_NEEDS_RECOVERY, CODE_INVALID_RUNTIME_STATE},
            )
        except FleetError as error:
            code = error.code if error.code in _SAFE_MESSAGES else CODE_INVALID_RUNTIME_STATE
            return PauseOutcome(
                phase="",
                suspended=(),
                already_suspended=(),
                reader_id="",
                history_id=None,
                error_code=code,
                needs_recovery=code in {CODE_NEEDS_RECOVERY, CODE_INVALID_RUNTIME_STATE},
            )
        except Exception:
            return PauseOutcome(
                phase="",
                suspended=(),
                already_suspended=(),
                reader_id="",
                history_id=None,
                error_code=CODE_INVALID_RUNTIME_STATE,
                needs_recovery=True,
            )

    def _pause_locked(self) -> PauseOutcome:
        reader_id, history_id, intent_id, history_intent_id, checkpoint = self._intents()
        existing = self._load_pause_state()
        if existing is not None:
            self._assert_state_matches(existing, intent_id, history_intent_id)
            if existing.get("phase") == PHASE_PAUSED:
                # Idempotent : la suspension déjà enregistrée est revérifiée.
                self._assert_suspended(reader_id, KIND_READER, intent_id)
                if history_id is not None:
                    self._assert_suspended(history_id, KIND_HISTORY, history_intent_id)
                return PauseOutcome(
                    phase=PHASE_PAUSED,
                    suspended=(),
                    already_suspended=tuple(
                        job for job in (reader_id, history_id) if job is not None
                    ),
                    reader_id=reader_id,
                    history_id=history_id,
                    error_code=None,
                    needs_recovery=False,
                )
        suspended = [self._suspend(reader_id, KIND_READER, intent_id)]
        if history_id is not None:
            suspended.append(self._suspend(history_id, KIND_HISTORY, history_intent_id))
        self._save_state(
            phase=PHASE_PAUSED,
            intent_id=intent_id,
            reader_id=reader_id,
            history_intent_id=history_intent_id,
            history_id=history_id,
            checkpoint=checkpoint,
        )
        return PauseOutcome(
            phase=PHASE_PAUSED,
            suspended=tuple(suspended),
            already_suspended=(),
            reader_id=reader_id,
            history_id=history_id,
            error_code=None,
            needs_recovery=False,
        )

    def _resume_locked(self) -> PauseOutcome:
        state = self._load_pause_state()
        if state is None or state.get("phase") != PHASE_PAUSED:
            raise _Refusal(CODE_NOT_PAUSED)
        reader_id, history_id, intent_id, history_intent_id, checkpoint = self._intents()
        self._assert_state_matches(state, intent_id, history_intent_id)
        recorded_reader = state.get("reader_id")
        recorded_history = state.get("history_id")
        if recorded_reader != reader_id or recorded_history != history_id:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        resumed = [self._unsuspend(reader_id, KIND_READER, intent_id)]
        if history_id is not None:
            resumed.append(self._unsuspend(history_id, KIND_HISTORY, history_intent_id))
        self._save_state(
            phase=PHASE_RESUMED,
            intent_id=intent_id,
            reader_id=reader_id,
            history_intent_id=history_intent_id,
            history_id=history_id,
            checkpoint=checkpoint,
        )
        return PauseOutcome(
            phase=PHASE_RESUMED,
            suspended=(),
            already_suspended=(),
            reader_id=reader_id,
            history_id=history_id,
            error_code=None,
            needs_recovery=False,
        )

    def is_paused(self) -> tuple[bool, str | None]:
        """État de suspension lisible, sans appel fournisseur."""

        try:
            state = self._load_pause_state()
        except Exception:
            return False, CODE_INVALID_RUNTIME_STATE
        if state is None:
            return False, None
        return state.get("phase") == PHASE_PAUSED, None

    def _intents(self) -> tuple[str, str | None, str, str | None, dict[str, object]]:
        prepare = self._load_store(self._prepare_store)
        if prepare is None or prepare.get("phase") != PHASE_PREPARED:
            raise _Refusal(CODE_NOT_PREPARED)
        if prepare.get("environment") != _fleet.ENVIRONMENT:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        intent_id = _require_identifier(prepare.get("intent_id"))
        checkpoint = _require_checkpoint(prepare.get("checkpoint"))
        reader_id = _receipt_identifier(prepare.get("receipt"), "reader_id")
        history = self._load_store(self._history_store)
        if history is None:
            return reader_id, None, intent_id, None, checkpoint
        if history.get("phase") != PHASE_HISTORICAL:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        if history.get("environment") != _fleet.ENVIRONMENT:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        history_intent_id = _require_identifier(history.get("intent_id"))
        if history.get("prepare_intent_id") != intent_id:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        if _require_checkpoint(history.get("checkpoint")) != checkpoint:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        history_id = _receipt_identifier(history.get("receipt"), "orchestrator_id")
        return reader_id, history_id, intent_id, history_intent_id, checkpoint

    def _load_store(self, store: object) -> dict[str, Any] | None:
        try:
            raw = store.load()
        except Exception:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE) from None
        if raw is None:
            return None
        if type(raw) is not dict:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        return raw

    def _load_pause_state(self) -> dict[str, Any] | None:
        raw = self._load_store(self._pause_store)
        if raw is None:
            return None
        if type(raw) is not dict or set(raw) != set(_STATE_KEYS):
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        if raw.get("format_version") != PAUSE_FORMAT_VERSION:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        if raw.get("environment") != _fleet.ENVIRONMENT:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        if raw.get("phase") not in {PHASE_PAUSED, PHASE_RESUMED}:
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        _require_identifier(raw.get("intent_id"))
        _require_identifier(raw.get("reader_id"))
        _require_checkpoint(raw.get("checkpoint"))
        history_id = raw.get("history_id")
        history_intent_id = raw.get("history_intent_id")
        if (history_id is None) != (history_intent_id is None):
            raise _Refusal(CODE_INVALID_RUNTIME_STATE)
        if history_id is not None:
            _require_identifier(history_id)
            _require_identifier(history_intent_id)
        return raw

    def _assert_state_matches(
        self,
        state: Mapping[str, object],
        intent_id: str,
        history_intent_id: str | None,
    ) -> None:
        if state.get("intent_id") != intent_id:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        if state.get("history_intent_id") != history_intent_id:
            raise _Refusal(CODE_NEEDS_RECOVERY)

    def _save_state(
        self,
        *,
        phase: str,
        intent_id: str,
        reader_id: str,
        history_intent_id: str | None,
        history_id: str | None,
        checkpoint: Mapping[str, object],
    ) -> None:
        try:
            self._pause_store.save(
                {
                    "format_version": PAUSE_FORMAT_VERSION,
                    "environment": _fleet.ENVIRONMENT,
                    "phase": phase,
                    "intent_id": intent_id,
                    "reader_id": reader_id,
                    "history_intent_id": history_intent_id,
                    "history_id": history_id,
                    "checkpoint": dict(checkpoint),
                }
            )
        except Exception:
            raise _Refusal(CODE_NEEDS_RECOVERY) from None

    def _read_job(self, name: str, kind: str, intent: str | None) -> Mapping[str, object]:
        if intent is None:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        try:
            job = self._jobs.read_job(name)
        except JobsApiError:
            raise _Refusal(CODE_NEEDS_RECOVERY) from None
        if job is None:
            raise _Refusal(CODE_JOB_UNAVAILABLE)
        metadata = job.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, Mapping) else None
        if not isinstance(labels, Mapping):
            raise _Refusal(CODE_NEEDS_RECOVERY)
        if labels.get(LABEL_KIND) != kind or labels.get(LABEL_INTENT) != intent:
            raise _Refusal(CODE_NEEDS_RECOVERY)
        return job

    def _suspend(self, name: str, kind: str, intent: str | None) -> str:
        job = self._read_job(name, kind, intent)
        if _is_suspended(job):
            return name
        self._patch(name, True)
        if not _is_suspended(self._read_job(name, kind, intent)):
            raise _Refusal(CODE_SUSPEND_FAILED)
        return name

    def _assert_suspended(self, name: str, kind: str, intent: str | None) -> None:
        if not _is_suspended(self._read_job(name, kind, intent)):
            raise _Refusal(CODE_NEEDS_RECOVERY)

    def _unsuspend(self, name: str, kind: str, intent: str | None) -> str:
        job = self._read_job(name, kind, intent)
        if _is_suspended(job):
            self._patch(name, False)
            if _is_suspended(self._read_job(name, kind, intent)):
                raise _Refusal(CODE_SUSPEND_FAILED)
        return name

    def _patch(self, name: str, suspend: bool) -> None:
        try:
            self._jobs.set_job_suspend(name, suspend)
        except JobsApiError:
            raise _Refusal(CODE_SUSPEND_FAILED) from None


def _is_suspended(job: Mapping[str, object]) -> bool:
    spec = job.get("spec")
    return isinstance(spec, Mapping) and spec.get("suspend") is True


def _require_identifier(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or any(character not in _IDENTIFIER_CHARS for character in value)
    ):
        raise _Refusal(CODE_INVALID_RUNTIME_STATE)
    return value


def _receipt_identifier(receipt: object, key: str) -> str:
    if type(receipt) is not dict:
        raise _Refusal(CODE_INVALID_RUNTIME_STATE)
    return _require_identifier(receipt.get(key))


def _require_checkpoint(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != set(_CHECKPOINT_KEYS):
        raise _Refusal(CODE_INVALID_RUNTIME_STATE)
    receiver = value.get("receiver")
    sequence = value.get("sequence")
    if type(receiver) is not str or not receiver or len(receiver) > 128:
        raise _Refusal(CODE_INVALID_RUNTIME_STATE)
    if isinstance(sequence, bool) or type(sequence) is not int or sequence < 0:
        raise _Refusal(CODE_INVALID_RUNTIME_STATE)
    return {"receiver": receiver, "sequence": sequence}
