"""``CompositeObservationProvider`` (``v2/services/observation_composite.py``).

Vérifie la répartition stricte des champs entre les deux adaptateurs réels
(projection v1 pour état/retard/lignes, stockage objet pour débit/dernière
arrivée) avec de vrais fichiers/checkpoints, pas des doublures qui
réimplémentent le calcul."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent_control_plane.v2.services.observation_composite import CompositeObservationProvider
from quadringent_control_plane.v2.services.observation_projection import ProjectionRepositoryObservationAdapter
from quadringent_control_plane.v2.services.observation_storage import StorageBackendObservationAdapter

NOW = datetime.now(timezone.utc)


class _FileStorageBackend:
    def __init__(self, root) -> None:
        self.root = root

    def checkpoint_store(self, stream_key: str) -> JsonCheckpointStore:
        return JsonCheckpointStore(self.root / f"{stream_key}.json")


def _write_console_document(tmp_path) -> str:
    document = {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=5)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 100}, "events_in_target": {"value": 95}},
    }
    path = tmp_path / "console.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return f"file://{path}"


def test_composite_merges_state_from_projection_and_rate_from_storage(tmp_path) -> None:
    origin = _write_console_document(tmp_path)
    projection = ProjectionRepositoryObservationAdapter(lambda pid: f"live:src1:{origin}")
    backend = _FileStorageBackend(tmp_path)
    store = backend.checkpoint_store("stream1")
    store.commit(JournalPosition("REC1", 10))
    storage = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    composite = CompositeObservationProvider(projection, storage)

    composite.observe("ppl1")  # premier échantillon de débit
    store.commit(JournalPosition("REC1", 20))
    time.sleep(0.01)
    observation = composite.observe("ppl1")

    assert observation.observed_state is not None
    assert observation.rows_source == 100
    assert observation.rows_destination == 95
    assert observation.throughput_rows_per_second is not None
    assert observation.throughput_rows_per_second > 0
    assert observation.last_arrival_at is not None


def test_composite_keeps_per_field_reasons_when_both_sides_absent() -> None:
    projection = ProjectionRepositoryObservationAdapter(lambda pid: None)
    storage = StorageBackendObservationAdapter(object(), pipeline_stream_key=lambda pid: None)
    composite = CompositeObservationProvider(projection, storage)
    observation = composite.observe("ppl1")
    assert observation.observed_state is None
    assert observation.throughput_rows_per_second is None
    assert observation.absent_reasons["observed_state"]
    assert observation.absent_reasons["throughput_rows_per_second"]


def test_composite_metrics_delegates_to_projection_adapter(tmp_path) -> None:
    origin = _write_console_document(tmp_path)
    projection = ProjectionRepositoryObservationAdapter(lambda pid: f"live:src1:{origin}")
    storage = StorageBackendObservationAdapter(_FileStorageBackend(tmp_path), pipeline_stream_key=lambda pid: None)
    composite = CompositeObservationProvider(projection, storage)
    series = composite.metrics("ppl1", "1h")
    assert series.provenance in {"absent", "lag_series_projection"}
