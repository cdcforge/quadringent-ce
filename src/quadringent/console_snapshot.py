"""Document lisible par la console de pilotage, et son émission.

Le worker n'exposait rien : ``CaptureMetrics.snapshot()`` partait sur stdout,
poll par poll, et personne ne le ramassait. Savoir si un flux avançait
demandait donc d'ouvrir les logs d'un pod — exactement ce que la console existe
pour supprimer.

Ce module construit un document unique, versionné, qui répond à lui seul à
« est-ce que ça avance ? », et le pose là où une interface peut le lire.

Trois règles tiennent le format.

**Un instant, un document.** Toutes les grandeurs d'un document sont observées
au même ``generated_at``. C'est ce qui autorise l'écran à afficher un âge
unique plutôt qu'un âge par chiffre.

**Ce qui n'est pas su est nul, et dit pourquoi.** Un compteur absent vaut
``null`` avec sa raison dans ``unknown``. Jamais zéro : un zéro se lirait comme
une mesure, et un retard à zéro se lirait « au tail ».

**Ce que le worker ignore, il le déclare.** Le nombre de lignes réellement
arrivées en cible, par exemple, n'est pas connu ici : le document le dit au
lieu de le taire, et l'écran affiche « inconnu » plutôt qu'un blanc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Protocol

from .continuous import lag_trend
from .lag_history import LagHistory

FORMAT_VERSION = "as400-console-v1"

# RUNNING | PAUSED_SOURCE | STOPPED_FAIL_CLOSED | STOPPED_AUTH_BLOCKED |
# STOPPED_CONFIG_BLOCKED | STOPPED_BUDGET | STOPPED_PROOF_CHAIN | UNKNOWN
RunState = str


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _known(value: object) -> dict[str, object]:
    return {"value": value}


def _unknown(reason: str) -> dict[str, object]:
    return {"value": None, "unknown": reason}


@dataclass(frozen=True)
class FluxIdentity:
    """Ce qui identifie un flux, et rien d'autre.

    Un flux est un couple journal × objets, lu par un chemin donné, posé dans
    une cible donnée. Tenir cette identité en un seul objet est délibéré :
    éclater la configuration entre source, modèle, souscription et cible est
    précisément ce qui empêche de savoir qui porte l'état réel.
    """

    id: str
    label: str
    journal: str
    journal_library: str
    objects: tuple[str, ...]
    reader_path: str
    target: str
    job: str

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "journal": self.journal,
            "journal_library": self.journal_library,
            "objects": list(self.objects),
            "reader_path": self.reader_path,
            "target": self.target,
            "job": self.job,
        }


@dataclass
class ConsoleSnapshotBuilder:
    """Accumule l'état d'un run et rend le document à la demande.

    ``observe`` a la signature du ``on_result`` de ``CaptureRunner.run`` : le
    branchement dans la boucle de capture tient en une ligne.
    """

    identity: FluxIdentity
    started_at_iso: str = field(default_factory=_now_iso)
    history: LagHistory = field(default_factory=LagHistory)
    clock: Any = time.monotonic
    _started_monotonic: float = field(default_factory=time.monotonic)
    _run_state: RunState = "RUNNING"
    _stopped_because: str | None = None
    _metrics: dict[str, object] = field(default_factory=dict)
    _last_error: dict[str, object] | None = None
    _cpu_seconds: float | None = None
    _source_pause: dict[str, object] | None = None

    # -- alimentation -----------------------------------------------------

    def observe(self, result: Any, metrics: dict[str, object]) -> None:
        self._metrics = dict(metrics)
        lag = metrics.get("last_lag_sequences")
        self.history.observe(
            elapsed_s=max(0.0, self.clock() - self._started_monotonic),
            lag=lag if isinstance(lag, int) else None,
        )
        pause = metrics.get("source_pause")
        self._source_pause = pause if isinstance(pause, dict) else None
        error_type = metrics.get("last_error_type")
        if error_type:
            self._last_error = {
                "type": error_type,
                "head": metrics.get("last_error_head"),
                "at": _now_iso(),
            }

    def mark_stopped(self, state: RunState, because: str) -> None:
        self._run_state = state
        self._stopped_because = because

    def observe_cpu_seconds(self, cpu_seconds: float) -> None:
        self._cpu_seconds = cpu_seconds

    # -- lecture ----------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        return max(0.0, self.clock() - self._started_monotonic)

    def document(self) -> dict[str, object]:
        metrics = self._metrics
        lag = metrics.get("last_lag_sequences")
        lag_known = isinstance(lag, int)

        series = self.history.known_series()
        trend = lag_trend(series) if len(series) >= 2 else None
        floor_first, floor_last = self.history.floor_thirds()
        # Le pic se lit sur les maxima des seaux, jamais sur la série décimée :
        # celle-ci ne garde qu'un échantillon par seau et perd les pointes.
        peak = self.history.peak()

        events = _int_or_none(metrics.get("events_published"))
        elapsed = self.elapsed_s

        # Une pause source est un état de run, pas un arrêt : la capture attend
        # l'échéance sans aucun sign-on, et reprend seule au retour du serveur.
        run_state = self._run_state
        if run_state == "RUNNING" and self._source_pause is not None:
            run_state = "PAUSED_SOURCE"
        return {
            "format_version": FORMAT_VERSION,
            "generated_at": _now_iso(),
            "flux": self.identity.payload(),
            "run": {
                "state": run_state,
                "stopped_because": self._stopped_because,
                "started_at": self.started_at_iso,
                "elapsed_s": round(elapsed, 3),
                "last_error": self._last_error,
                "source_pause": self._source_pause,
            },
            "position": {
                "checkpoint": metrics.get("last_watermark"),
                "source_tail": metrics.get("last_source_tail"),
                "receiver_first_sequence": metrics.get("last_receiver_first_sequence"),
                "receiver_last_sequence": metrics.get("last_receiver_last_sequence"),
            },
            "lag": {
                "current": _known(lag)
                if lag_known
                else _unknown(
                    "le curseur et le tail sont sur des receivers disjoints, ou "
                    "aucun poll n'a encore abouti"
                ),
                "verdict": _known(trend["verdict"])
                if trend
                else _unknown("moins de deux échantillons connus"),
                "floor_first_third": _known(floor_first)
                if floor_first is not None
                else _unknown("pas encore assez de seaux pour un plancher"),
                "floor_last_third": _known(floor_last)
                if floor_last is not None
                else _unknown("pas encore assez de seaux pour un plancher"),
                "max": _known(peak)
                if peak is not None
                else _unknown("aucun retard connu n'a encore été observé"),
                "series": self.history.payload(),
            },
            "counters": {
                "polls": _known(_int_or_none(metrics.get("polls"))),
                "idle_polls": _known(_int_or_none(metrics.get("idle_polls"))),
                "empty_scans": _known(_int_or_none(metrics.get("empty_scans"))),
                "errors": _known(_int_or_none(metrics.get("errors"))),
                "windows_published": _known(_int_or_none(metrics.get("batches_published"))),
                "events_published": _known(events),
                "receiver_rotations": _known(_int_or_none(metrics.get("receiver_rotations"))),
                "payload_bytes_published": _known(
                    _int_or_none(metrics.get("payload_bytes_published"))
                ),
                # Le worker écrit le raw ; il ne relit pas la cible. Prétendre
                # savoir ce qui y est arrivé serait une affirmation sans mesure.
                "events_in_target": _unknown(
                    "le worker n'interroge pas la cible ; ce compte demande une "
                    "lecture Snowflake que rien ne fait aujourd'hui"
                ),
                "duplicates_in_target": _unknown(
                    "aucune réconciliation raw / cible n'existe"
                ),
                "run_duration_s": _known(round(elapsed, 3)),
                **_cpu_counters(self._cpu_seconds, events, elapsed),
            },
        }

    def encode(self) -> bytes:
        return json.dumps(self.document(), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        ) + b"\n"


def _cpu_counters(
    cpu_seconds: float | None, events: int | None, elapsed_s: float
) -> dict[str, object]:
    if cpu_seconds is None:
        reason = "le CPU n'est relevé qu'en fin de run par le script pilote"
        return {"mean_mcpu": _unknown(reason), "cpu_ms_per_event": _unknown(reason)}
    mean_mcpu = round(cpu_seconds / elapsed_s * 1000, 3) if elapsed_s > 0 else None
    per_event = round(cpu_seconds * 1000.0 / events, 4) if events else None
    return {
        "mean_mcpu": _known(mean_mcpu)
        if mean_mcpu is not None
        else _unknown("durée de run nulle"),
        "cpu_ms_per_event": _known(per_event)
        if per_event is not None
        else _unknown("aucun événement publié : la division n'a pas de sens"),
    }


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


# ---------------------------------------------------------------------------
# Émission
# ---------------------------------------------------------------------------


class SnapshotSink(Protocol):
    """Où le document est posé. Écrasé à chaque fois, contrairement au raw.

    Le raw est immuable et passe par ``put_once`` : un rejeu ne réécrit pas. Le
    document de console est l'inverse — un seul emplacement, toujours le
    dernier état. Les deux ne doivent pas partager de code, sous peine qu'une
    correction de l'un casse l'invariant de l'autre.
    """

    def write(self, payload: bytes) -> None: ...


@dataclass(frozen=True)
class FileSnapshotSink:
    """Écriture atomique sur disque : un lecteur ne voit jamais un demi-JSON."""

    path: Path

    def write(self, payload: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with open(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class S3SnapshotSink:
    """Une clé unique, réécrite. Voir ``ThrottledSink`` pour le coût."""

    bucket: str
    key: str
    client: Any

    def write(self, payload: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=payload,
            ContentType="application/json",
            CacheControl="no-store",
        )


@dataclass
class ThrottledSink:
    """Borne le nombre d'écritures par seconde.

    Un PUT par poll à un poll par seconde ferait 86 400 PUT par jour.
    Le nombre de requêtes peut alors dépasser celui du flux de lots. L'intervalle
    est donc explicite, et le dernier document est toujours écrit à la
    fermeture, quoi qu'il arrive.
    """

    sink: SnapshotSink
    interval_s: float = 10.0
    clock: Any = time.monotonic
    _last_write: float | None = None
    writes: int = 0
    skipped: int = 0

    def write(self, payload: bytes) -> None:
        now = self.clock()
        if self._last_write is not None and now - self._last_write < self.interval_s:
            self.skipped += 1
            return
        self._last_write = now
        self.writes += 1
        self.sink.write(payload)

    def flush(self, payload: bytes) -> None:
        """Écrit sans regarder l'intervalle. Le dernier état n'est jamais perdu."""

        self._last_write = self.clock()
        self.writes += 1
        self.sink.write(payload)
