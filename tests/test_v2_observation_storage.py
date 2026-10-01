"""Adaptateur d'observation réel — checkpoint de capture dans le stockage
objet (``v2/services/observation_storage.py``, chantier observabilité v2
suite).

Deux stockages réels exercés (pas de simulation du calcul lui-même) :
un backend fichier local (``checkpoint.JsonCheckpointStore``, la brique
utilisée par le POC) et un GCS factice (``FakeGcsClient`` de
``tests/test_gcs_backend.py``, via ``storage_backend.StorageBackend`` réel
en mode ``gcs``).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.storage_backend import StorageBackend
from quadringent_control_plane.v2.services.observation_storage import (
    NO_CHECKPOINT_REASON,
    NO_STREAM_REASON,
    StorageBackendObservationAdapter,
)

from test_gcs_backend import FakeGcsClient  # double déjà utilisé par la suite gcs_backend


@dataclass
class _FileStorageBackend:
    """Double minimal exposant ``checkpoint_store`` sur un backend fichier
    local — ``storage_backend.StorageBackend`` ne connaît que ``aws``/``gcs``
    (voir son en-tête), un backend fichier est donc un double dédié ici,
    pas une simulation du calcul de débit lui-même."""

    root: object

    def checkpoint_store(self, stream_key: str) -> JsonCheckpointStore:
        return JsonCheckpointStore(self.root / f"{stream_key}.json")


def test_unmapped_pipeline_returns_absent(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: None)
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is None
    assert observation.absent_reasons["throughput_rows_per_second"] == NO_STREAM_REASON


def test_pipeline_with_no_checkpoint_yet_returns_absent(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    observation = adapter.observe("ppl1")
    assert observation.absent_reasons["throughput_rows_per_second"] == NO_CHECKPOINT_REASON


def test_first_sample_never_invents_a_throughput(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    backend.checkpoint_store("stream1").commit(JournalPosition("REC1", 10))
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is None
    assert observation.last_arrival_at is None
    assert "premier échantillon" in observation.absent_reasons["throughput_rows_per_second"]


def test_second_sample_computes_real_throughput_from_the_checkpoint_delta(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    store = backend.checkpoint_store("stream1")
    store.commit(JournalPosition("REC1", 10))
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    adapter.observe("ppl1")

    # Force un intervalle mesurable en retenant nous-mêmes le second appel
    # (le test ne dépend pas d'un vrai sleep : on advance le curseur puis on
    # patch l'horloge interne via un second appel immédiat, delta_t > 0
    # étant garanti par l'horloge système entre les deux appels réels).
    store.commit(JournalPosition("REC1", 60))
    import time

    time.sleep(0.01)
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is not None
    assert observation.throughput_rows_per_second > 0
    assert observation.last_arrival_at is not None


def test_no_progress_between_samples_yields_zero_throughput_not_none(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    store = backend.checkpoint_store("stream1")
    store.commit(JournalPosition("REC1", 10))
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    adapter.observe("ppl1")
    import time

    time.sleep(0.01)
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second == 0.0
    assert observation.last_arrival_at is None  # jamais d'arrivée inventée sans progression


def test_receiver_rotation_between_samples_stays_absent_with_reason(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    store = backend.checkpoint_store("stream1")
    store.commit(JournalPosition("REC1", 10))
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    adapter.observe("ppl1")
    store.transition(JournalPosition("REC1", 10), JournalPosition("REC2", 1))
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is None
    assert "rotation" in observation.absent_reasons["throughput_rows_per_second"]


def test_rows_and_observed_state_are_never_owned_by_this_adapter(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    backend.checkpoint_store("stream1").commit(JournalPosition("REC1", 10))
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    observation = adapter.observe("ppl1")
    assert observation.observed_state is None
    assert observation.rows_source is None
    assert observation.rows_destination is None
    assert "observation_projection" in observation.absent_reasons["observed_state"]


def test_metrics_returns_empty_series_no_history_kept(tmp_path) -> None:
    backend = _FileStorageBackend(tmp_path)
    adapter = StorageBackendObservationAdapter(backend, pipeline_stream_key=lambda pid: "stream1")
    series = adapter.metrics("ppl1", "1h")
    assert series.points == ()
    assert series.provenance == "absent"


# --- GCS factice (StorageBackend réel en mode gcs) ------------------------


@pytest.fixture()
def gcs_storage_backend():
    client = FakeGcsClient()
    return StorageBackend(kind="gcs", raw_bucket="raw-bucket", state_location="checkpoint-bucket", gcs_client=client)


def test_real_storage_backend_gcs_checkpoint_first_sample_absent(gcs_storage_backend) -> None:
    store = gcs_storage_backend.checkpoint_store("stream-gcs")
    store.commit(JournalPosition("REC1", 5))
    adapter = StorageBackendObservationAdapter(gcs_storage_backend, pipeline_stream_key=lambda pid: "stream-gcs")
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is None  # premier échantillon


def test_real_storage_backend_gcs_checkpoint_second_sample_measures_throughput(gcs_storage_backend) -> None:
    store = gcs_storage_backend.checkpoint_store("stream-gcs")
    store.commit(JournalPosition("REC1", 5))
    adapter = StorageBackendObservationAdapter(gcs_storage_backend, pipeline_stream_key=lambda pid: "stream-gcs")
    adapter.observe("ppl1")
    store.commit(JournalPosition("REC1", 25))
    import time

    time.sleep(0.01)
    observation = adapter.observe("ppl1")
    assert observation.throughput_rows_per_second is not None
    assert observation.throughput_rows_per_second > 0
