"""Preuve durable de fin de copie initiale — `executor/evidence.py`.

Le control plane ne fait que lire cette preuve (jamais l'écrire) pour
autoriser `copying -> live`. Un objet absent est un signal normal (copie
encore en cours), pas une erreur ; un objet mal formé échoue fermé.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import (
    EvidenceError,
    EvidenceReader,
    InitialCopyEvidence,
    evidence_key,
)

BOUNDARY = JournalBoundary(
    receiver_library="QGPL",
    receiver_name="RCV0001",
    last_sequence=4200,
    observed_at=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
)


class _FakeObjectStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        if key not in self._objects:
            raise FileNotFoundError(key)
        return self._objects[key]


def test_evidence_key_is_namespaced_by_table_and_run() -> None:
    key = evidence_key("raw/example", "tbl-1", "run-1")
    assert key == "raw/example/tbl-1/evidence/run-1.json"


def test_missing_evidence_reads_as_none_not_an_error() -> None:
    reader = EvidenceReader(_FakeObjectStore({}))
    assert reader.read("raw/example/tbl-1/evidence/run-1.json") is None


def test_well_formed_evidence_round_trips() -> None:
    evidence = InitialCopyEvidence(
        pipeline_id="pipe-1",
        table_id="tbl-1",
        run_id="run-1",
        boundary=BOUNDARY,
        rows_copied=12345,
        completed_at=datetime(2026, 9, 23, 10, 5, tzinfo=timezone.utc),
    )
    key = "raw/example/tbl-1/evidence/run-1.json"
    store = _FakeObjectStore({key: json.dumps(evidence.to_dict()).encode("utf-8")})
    reader = EvidenceReader(store)
    restored = reader.read(key)
    assert restored == evidence


def test_malformed_evidence_fails_closed() -> None:
    key = "raw/example/tbl-1/evidence/run-1.json"
    store = _FakeObjectStore({key: b"{not json"})
    reader = EvidenceReader(store)
    with pytest.raises(EvidenceError):
        reader.read(key)


def test_evidence_missing_required_field_fails_closed() -> None:
    key = "raw/example/tbl-1/evidence/run-1.json"
    store = _FakeObjectStore({key: json.dumps({"pipeline_id": "pipe-1"}).encode("utf-8")})
    reader = EvidenceReader(store)
    with pytest.raises(EvidenceError):
        reader.read(key)
