"""Orchestrateur de run : exécute les étapes configurées via les adaptateurs.

Ne fait aucune hypothèse sur la nature réelle des adaptateurs : ils sont reçus
tout faits (``SourceDriver``, ``CaptureRunner``, ``StorageBackend``,
``WarehouseLoader``), ce qui permet de tester tout l'enchaînement avec des
« fakes » en mémoire, sans jamais toucher un système réel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import math
import time
from typing import Callable, Sequence

from quadringent.contract import JournalPosition

from .adapters import CaptureBoundary, CaptureRunner, SourceDriver, StorageBackend, WarehouseLoader, RawReplayEvidence
from .config import RunConfig
from .generator import DML_STEP_NAMES, LABEL, Oracle, freshness_markers, step_plan
from .latency import LatencySummary, observed_mirror_latency, summarize
from .published_probes import find_receipted_probes
from .reconcile import JournalEvent, ReconciliationReport, reconcile
from .schema import canonical, update_sql

_RECEIVER_NAME = re.compile(r"[A-Za-z0-9_$#@]{1,128}\Z")


def _source_receiver(entry: dict[str, object]) -> str:
    name = entry["JOURNAL_RECEIVER_NAME"]
    if not isinstance(name, str) or _RECEIVER_NAME.fullmatch(name) is None:
        raise ValueError("nom de receiver source invalide")
    return name


def _source_sequence(entry: dict[str, object], field: str = "SEQUENCE_NUMBER") -> int:
    value = entry[field]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("séquence source invalide")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("séquence source invalide")
    sequence = int(value)
    if sequence < 0:
        raise ValueError("séquence source invalide")
    return sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SourceReadError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class StepResult:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIPPED"
    details: dict[str, object] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""


@dataclass
class RunReport:
    run_id: str
    steps: list[StepResult] = field(default_factory=list)
    reconciliation: ReconciliationReport | None = None
    freshness: LatencySummary | None = None
    execution_mode: str = "unknown"
    freshness_raw: LatencySummary | None = None

    @property
    def status(self) -> str:
        if not self.steps or any(s.status != "PASS" for s in self.steps):
            return "FAIL"
        if self.reconciliation is not None and self.reconciliation.status == "FAIL":
            return "FAIL"
        return "PASS"

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "execution_mode": self.execution_mode,
            "steps": [
                {"name": s.name, "status": s.status, "details": s.details,
                 "started_at": s.started_at, "finished_at": s.finished_at}
                for s in self.steps
            ],
            "reconciliation": self.reconciliation.as_dict() if self.reconciliation else None,
            "freshness": self.freshness.as_dict() if self.freshness else None,
            "freshness_raw": self.freshness_raw.as_dict() if self.freshness_raw else None,
        }


class Orchestrator:
    """Exécute une suite d'étapes de qualification pour une ``RunConfig``.

    L'état accumulé entre étapes (oracle en mémoire, évènements de journal vus)
    vit sur l'instance : un objet neuf par run.
    """

    def __init__(self, config: RunConfig, *, source: SourceDriver, capture: CaptureRunner,
                 storage: StorageBackend, warehouse: WarehouseLoader,
                 clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 execution_mode: str = "unknown") -> None:
        if execution_mode not in {"unknown", "real", "offline_fake"}:
            raise ValueError("mode de qualification invalide")
        self.config = config
        self.source = source
        self.capture = capture
        self.storage = storage
        self.warehouse = warehouse
        self.execution_mode = execution_mode
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self.oracle: Oracle = {}
        self.events: list[JournalEvent] = []
        self._capture_label_counter = 0
        self._last_reconciliation: ReconciliationReport | None = None
        self._snapshot_bootstrap: CaptureBoundary | None = None
        self._capture_bootstrap: CaptureBoundary | None = None
        self._last_freshness: LatencySummary | None = None
        self._last_freshness_raw: LatencySummary | None = None

    def run(self, steps: Sequence[str]) -> RunReport:
        report = RunReport(run_id=self.config.run_id, execution_mode=self.execution_mode)
        for name in steps:
            started = _now()
            try:
                result = self._run_step(name)
            except Exception:
                # Un pilote externe peut inclure une référence de secret dans
                # son exception. La rapporter serait une fuite via l'artefact
                # nightly ; seule une raison constante sort du processus.
                result = StepResult(
                    name=name, status="FAIL",
                    details={"reason": "qualification_runtime_error"},
                    started_at=started, finished_at=_now(),
                )
            report.steps.append(result)
            if result.status == "FAIL":
                break
        report.reconciliation = self._last_reconciliation
        if "freshness" in steps:
            report.freshness = self._last_freshness
            report.freshness_raw = self._last_freshness_raw
        return report

    def _run_step(self, name: str) -> StepResult:
        started = _now()
        if name in DML_STEP_NAMES:
            details = self._run_dml(name)
        elif name == "snapshot":
            details = self._run_snapshot()
        elif name == "capture":
            details = self._run_capture()
        elif name == "rotate":
            details = self._run_rotate()
        elif name == "reconcile":
            details = self._run_reconcile()
        elif name == "freshness":
            details = self._run_freshness()
        else:
            raise ValueError(f"étape non prise en charge par l'orchestrateur : {name!r}")
        status = "FAIL" if details.get("_failed") else "PASS"
        details.pop("_failed", None)
        return StepResult(name=name, status=status, details=details, started_at=started, finished_at=_now())

    def _run_dml(self, name: str) -> dict[str, object]:
        plan = step_plan(name, self.config.table)
        result = self.source.execute(plan.statements)
        ok = result.exit_code == 0
        if ok:
            plan.apply(self.oracle)
        return {"statements": len(plan.statements), "exit_code": result.exit_code, "_failed": not ok}

    def _run_snapshot(self) -> dict[str, object]:
        try:
            boundary = self._tail_bootstrap()
        except _SourceReadError as error:
            return {"reason": error.reason, "_failed": True}
        result = self.capture.run(label="snapshot", max_seconds=self.config.capture.max_seconds,
                                   bootstrap=boundary, env={})
        if result.exit_code != 0:
            return {"exit_code": result.exit_code, "rows": result.event_count, "_failed": True}
        # Le scénario garde la source immobile pendant le snapshot. La
        # première capture doit reprendre à cette frontière, même si le
        # receiver change après les mutations du scénario.
        self._snapshot_bootstrap = boundary
        for event in result.events:
            self.events.append(JournalEvent(
                receiver=f"SNAPSHOT:{self.config.run_id}", sequence=int(event.get("sequence", 0)),
                operation="c", payload=event, is_snapshot=True,
            ))
        return {"exit_code": result.exit_code, "rows": result.event_count, "_failed": False}

    def _tail_bootstrap(self) -> CaptureBoundary:
        tail = self.source.tail()
        if tail.exit_code != 0:
            raise _SourceReadError("source_tail_failed")
        try:
            positions = tail.parsed("SRC_TAIL")
            if not positions:
                raise ValueError("no attached receiver")
            last = positions[-1]
            receiver_library = last["JOURNAL_RECEIVER_LIBRARY"]
            if (not isinstance(receiver_library, str)
                    or _RECEIVER_NAME.fullmatch(receiver_library) is None):
                raise ValueError("receiver library is invalid")
            return CaptureBoundary(
                receiver_library=receiver_library,
                receiver_name=_source_receiver(last),
                next_sequence=_source_sequence(last, "LAST_SEQUENCE_NUMBER") + 1,
                observed_at=datetime.now(timezone.utc),
            )
        except (KeyError, TypeError, ValueError):
            raise _SourceReadError("source_tail_invalid") from None

    def _run_capture(self) -> dict[str, object]:
        if self._capture_bootstrap is None:
            if self._snapshot_bootstrap is not None:
                self._capture_bootstrap = self._snapshot_bootstrap
            else:
                try:
                    self._capture_bootstrap = self._tail_bootstrap()
                except _SourceReadError as error:
                    return {"reason": error.reason, "_failed": True}
        # Le lecteur du produit privilégie son checkpoint s'il existe. Cette
        # frontière reste nécessaire si le premier passage n'a rien publié.
        bootstrap = self._capture_bootstrap
        self._capture_label_counter += 1
        result = self.capture.run(label=f"capture-{self._capture_label_counter}",
                                   max_seconds=self.config.capture.max_seconds, bootstrap=bootstrap, env={})
        for event in result.events:
            self.events.append(JournalEvent(
                receiver=str(event.get("receiver", "")), sequence=int(event.get("sequence", 0)),
                operation=str(event.get("operation", "")), payload=event.get("payload", {}),
            ))
        return {"exit_code": result.exit_code, "events": result.event_count,
                "bootstrap": bootstrap.capture_start,
                "_failed": result.exit_code != 0}

    def _run_rotate(self) -> dict[str, object]:
        result = self.source.rotate()
        return {"exit_code": result.exit_code, "_failed": result.exit_code != 0}

    def _run_reconcile(self) -> dict[str, object]:
        self._last_reconciliation = None
        try:
            raw = self.warehouse.fetch_raw_evidence(schema=self.config.warehouse.schema_name)
        except Exception:
            return {"reason": "raw_replay_read_failed", "_failed": True}
        details = {"raw_replay": raw.as_dict()}
        try:
            self.warehouse.load(raw_prefix=self.config.storage.raw_prefix)
        except Exception:
            # Les exceptions du client Snowflake peuvent contenir des détails
            # de connexion. Le rapport ne reprend jamais leur message.
            return {**details, "reason": "warehouse_load_failed", "_failed": True}
        try:
            self._last_reconciliation = self._reconcile(raw_evidence=raw)
        except _SourceReadError as error:
            return {**details, "reason": error.reason, "_failed": True}
        except Exception:
            return {**details, "reason": "reconciliation_read_failed", "_failed": True}
        return {
            **details,
            "reconciliation_status": self._last_reconciliation.status,
            "_failed": self._last_reconciliation.status != "PASS",
        }

    def _reconcile(self, *, raw_evidence: RawReplayEvidence | None = None) -> ReconciliationReport:
        """Rapproche l'oracle, la source relue et l'état chargé dans l'entrepôt.

        Les évènements matérialisés viennent du chargeur d'entrepôt
        (``warehouse.fetch_events``), pas de ceux vus pendant la capture : le
        rapprochement doit porter sur ce qui a réellement été chargé, pas sur
        ce que le lecteur a émis.
        """
        schema = self.config.table
        boundary = self._snapshot_bootstrap or self._capture_bootstrap
        if boundary is None:
            if self.config.bootstrap_receiver is None or self.config.bootstrap_sequence is None:
                raise _SourceReadError("source_bootstrap_missing")
            bootstrap = (self.config.bootstrap_receiver, self.config.bootstrap_sequence)
        else:
            bootstrap = boundary.capture_start
        bootstrap_position = JournalPosition(*bootstrap)
        oracle = {key: canonical(row, schema) for key, row in self.oracle.items()}
        source_dump = self.source.dump()
        if source_dump.exit_code != 0:
            raise _SourceReadError("source_dump_failed")
        source_rows_raw = source_dump.parsed("SRC_ROW")
        source = {int(r[schema.primary_key]): canonical(r, schema) for r in source_rows_raw}
        row_positions = self.source.row_positions(bootstrap)
        if row_positions.exit_code != 0:
            raise _SourceReadError("source_row_positions_failed")
        try:
            receiver_entries = row_positions.parsed("SRC_RECEIVER")
            receiver_order = tuple(_source_receiver(entry) for entry in receiver_entries)
            if not receiver_order or receiver_order[0] != bootstrap_position.receiver:
                raise ValueError("receiver de bootstrap absent")
            if len(set(receiver_order)) != len(receiver_order) or any(not name for name in receiver_order):
                raise ValueError("ordre des receivers ambigu")
            position_entries = row_positions.parsed("SRC_ROWPOS")
            source_positions = tuple(
                JournalPosition(_source_receiver(entry), _source_sequence(entry))
                for entry in position_entries
            )
            rank = {receiver: index for index, receiver in enumerate(receiver_order)}
            if any(position.receiver not in rank for position in source_positions):
                raise ValueError("receiver de position absent")
            keys = [(rank[position.receiver], position.sequence) for position in source_positions]
            if keys != sorted(set(keys)) or any(
                rank[position.receiver] == 0 and position.sequence < bootstrap_position.sequence
                for position in source_positions
            ):
                raise ValueError("positions source non ordonnées ou avant la frontière")
        except (KeyError, TypeError, ValueError):
            raise _SourceReadError("source_row_positions_invalid") from None
        raw = raw_evidence or self.warehouse.fetch_raw_evidence(schema=self.config.warehouse.schema_name)
        mirror_rows = self.warehouse.fetch_mirror_rows(schema=self.config.warehouse.schema_name)
        loaded = self.warehouse.fetch_events(schema=self.config.warehouse.schema_name)
        events = [
            JournalEvent(
                receiver=str(e["receiver"]), sequence=int(e["sequence"]), operation=str(e["operation"]),
                payload=e["payload"], is_snapshot=bool(e.get("is_snapshot", False)),
            )
            for e in loaded
        ]
        return reconcile(
            oracle=oracle, source=source, events=events, schema=schema,
            bootstrap_position=bootstrap_position, source_positions=source_positions,
            receiver_order=receiver_order,
            raw_rows=raw.raw_rows, raw_distinct_events=raw.raw_distinct_events,
            replayed_identical=raw.replayed_identical, replayed_divergent=raw.replayed_divergent,
            identical_event_ids=raw.identical_event_ids, divergent_event_ids=raw.divergent_event_ids,
            mirror_rows=mirror_rows,
            history_event_ids=[e.get("event_id") for e in loaded],
        )

    def _run_freshness(self) -> dict[str, object]:
        budget = self.config.freshness
        values: list[float] = []
        raw_values: list[float] = []
        probes: list[dict[str, object]] = []
        self._last_freshness = summarize([])
        self._last_freshness_raw = summarize([])

        def evidence(accepted: bool) -> dict[str, object]:
            return {
                "target": "snowflake_mirror", "metric": "write_to_mirror_observed_upper_bound",
                "scope": "sql_loader_bounded_docker_capture", "steady_state_streaming": False,
                "count": len(values), "p95_seconds": self._last_freshness.p95,
                "max_seconds": self._last_freshness.maximum, "slo_seconds": budget.max_seconds,
                "accepted": accepted, "status": "PASS" if accepted else "FAIL", "probes": probes,
            }

        def fail(reason: str, **details: object) -> dict[str, object]:
            self._last_reconciliation = None
            return {**details, "reason": reason, "mirror_measurement": evidence(False), "_failed": True}

        if (self._last_reconciliation is None or self._last_reconciliation.status != "PASS"
                or self._capture_bootstrap is None or 1 not in self.oracle):
            return fail("freshness_not_measured")
        schema = self.config.table
        markers = freshness_markers(self.config.run_id)
        label = schema.column(LABEL)
        if (schema.primary_key != "ORDER_ID" or label.kind != "varchar"
                or label.length is not None and any(len(marker) > label.length for marker in markers)):
            return fail("freshness_schema_unsupported")
        self._last_reconciliation = None
        raw = None
        event_ids = []
        for marker in markers:
            try:
                before = self._clock()
                start = self._monotonic()
                if not math.isfinite(start):
                    return fail("freshness_clock_invalid")
            except Exception:
                return fail("freshness_clock_invalid")
            latest = before
            latest_monotonic = start

            def observe() -> tuple[datetime, float]:
                nonlocal latest, latest_monotonic
                now = self._clock()
                monotonic = self._monotonic()
                if (not isinstance(before, datetime) or before.tzinfo is None or before.utcoffset() is None
                        or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                        or now < latest or not math.isfinite(monotonic) or monotonic < latest_monotonic
                        or abs((now - before).total_seconds() - (monotonic - start)) > 1):
                    raise ValueError("horloge de fraîcheur invalide")
                latest, latest_monotonic = now, monotonic
                return now, monotonic - start

            try:
                observe()
            except Exception:
                return fail("freshness_clock_invalid")
            try:
                result = self.source.execute((update_sql(schema, 1, {LABEL: marker}),))
            except Exception:
                return fail("freshness_write_failed")
            try:
                after, _ = observe()
            except Exception:
                return fail("freshness_clock_invalid")
            if result.exit_code != 0:
                return fail("freshness_write_failed")
            self.oracle[1] = {**self.oracle[1], LABEL: marker}
            try:
                capture = self._run_capture()
            except Exception:
                return fail("freshness_capture_failed")
            if capture.get("_failed"):
                return fail("freshness_capture_failed")
            try:
                published = find_receipted_probes(
                    self.storage, raw_prefix=self.config.storage.raw_prefix,
                    schema=schema, row_key=1, markers=(marker,),
                )
            except Exception:
                return fail("freshness_publication_invalid")
            if set(published) != {marker}:
                return fail("freshness_unpublished")
            try:
                now, _ = observe()
                timestamp = published[marker].created_at
                if (not isinstance(timestamp, datetime) or timestamp.tzinfo is None
                        or timestamp.utcoffset() is None or timestamp < after or timestamp > now):
                    return fail("freshness_clock_invalid")
            except ValueError:
                return fail("freshness_clock_invalid")
            raw_values.append((timestamp - after).total_seconds())
            self._last_freshness_raw = summarize(raw_values)
            event_ids.append(published[marker].event_id)
            try:
                raw = self.warehouse.fetch_raw_evidence(schema=self.config.warehouse.schema_name)
                self.warehouse.load(raw_prefix=self.config.storage.raw_prefix)
            except Exception:
                return fail("freshness_destination_failed", **({"raw_replay": raw.as_dict()} if raw else {}))
            for poll in range(1, budget.max_polls + 1):
                try:
                    _, elapsed = observe()
                except ValueError:
                    return fail("freshness_clock_invalid")
                remaining = budget.max_seconds - elapsed
                try:
                    value = self.warehouse.fetch_mirror_value(
                        schema=self.config.warehouse.schema_name, row_key=1, column=LABEL,
                        timeout_seconds=max(1, min(10, math.ceil(remaining))),
                    )
                except Exception:
                    return fail("freshness_mirror_read_failed")
                try:
                    observed, elapsed = observe()
                except ValueError:
                    return fail("freshness_clock_invalid")
                if value == marker:
                    try:
                        measured = observed_mirror_latency(before, observed, elapsed)
                    except ValueError:
                        return fail("freshness_clock_invalid")
                    values.append(measured)
                    probes.append({"marker": marker, "observed_upper_bound_seconds": measured, "poll_count": poll,
                                   "written_before": before.isoformat(), "written_after": after.isoformat(),
                                   "mirror_read_at": observed.isoformat(), "read_value": value})
                    self._last_freshness = summarize(values)
                    if measured > budget.max_seconds:
                        return fail("freshness_mirror_slo_exceeded")
                    break
                if elapsed >= budget.max_seconds or poll == budget.max_polls:
                    return fail("freshness_mirror_missing")
                try:
                    self._sleep(min(budget.poll_interval_seconds, budget.max_seconds - elapsed))
                except Exception:
                    return fail("freshness_poll_wait_failed")
        try:
            self._last_reconciliation = self._reconcile(raw_evidence=raw)
        except Exception:
            return fail("freshness_destination_failed")
        if self._last_reconciliation.status != "PASS":
            return fail("freshness_destination_differs")
        accepted = len(values) == 3 and all(0 < value <= budget.max_seconds for value in values)
        return {
            "samples": len(values), "p95_seconds": self._last_freshness.p95,
            "max_seconds": self._last_freshness.maximum, "event_ids": event_ids,
            "raw_replay": raw.as_dict(), "mirror_measurement": evidence(accepted),
            "raw_latency": {"target": "durable_raw_object", "metric": "write_to_raw_object_lower_bound",
                            "count": len(raw_values), "p95_seconds": self._last_freshness_raw.p95,
                            "max_seconds": self._last_freshness_raw.maximum},
            "_failed": not accepted,
        }


def measure_freshness(writes: Sequence[tuple[datetime, datetime]], published_at: Sequence[datetime | None]) -> LatencySummary:
    """Résume la latence écriture -> objet brut durable.

    ``writes`` est une paire ``(avant, après)`` par écriture isolée (bornes
    basse/haute de l'horloge de l'appelant) ; ``published_at`` est
    l'horodatage de création de l'objet observé correspondant, ou ``None`` si
    jamais vu. Utilise la borne basse (après le retour de l'écriture).
    """
    values = [
        (published - after).total_seconds()
        for (_, after), published in zip(writes, published_at)
        if published is not None
    ]
    return summarize(values)
