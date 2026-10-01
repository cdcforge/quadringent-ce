"""Adaptateur de coûts réel — réutilise ``costs.project_costs`` v1 tel quel
(``v2/services/costs_projection.py``, chantier observabilité v2 suite).

``cost_snapshot_from_pipeline`` est testée directement avec une
``ObservabilityProjection`` construite comme le fait déjà
``tests/test_costs.py`` (même helper ``observation()``) — c'est la façon
supportée de construire ce type dans cette suite, sans réinventer
l'enveloppe JSON complète de preuve d'observabilité (déjà couverte par
``tests/test_observability_snapshot.py``). ``CostsV1ProjectionAdapter``
(la résolution id v2 -> document réel) est testée séparément avec un vrai
document console minimal (observabilité absente = état réel, pas simulé).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

from site_fixture import build_test_site

from quadringent_control_plane.model import ObservabilityProjection, PipelineProjection, SloCheckProjection, SourceDescriptor
from quadringent_control_plane.projection import project_console_document
from quadringent_control_plane.v2.services.costs_projection import (
    CostsV1ProjectionAdapter,
    NO_MAPPING_REASON,
    OUT_OF_SITE_REASON,
    TABLE_SCOPE_UNSUPPORTED_REASON,
    cost_snapshot_from_pipeline,
)

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
END = int(NOW.timestamp()) - 6 * 3600


def observability(value=2.5, *, freshness="fresh", kind="live", reason=None) -> ObservabilityProjection:
    return ObservabilityProjection(
        "pass",
        {"freshness": freshness, "evidence_kind": kind},
        NOW.isoformat(),
        "within_policy",
        (
            SloCheckProjection(
                "snowflake_credits",
                "cost",
                "pass",
                value,
                10,
                "warehousecredits/delayed24h",
                reason or f"metering_window_{END - 86400}_{END}_24",
            ),
        ),
        (),
    )


def _minimal_pipeline(observability_value: ObservabilityProjection) -> PipelineProjection:
    document = {
        "format_version": "as400-console-v1",
        "generated_at": NOW.isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 1}},
    }
    descriptor = SourceDescriptor("acme", "live", "test", "file:///doc.json")
    pipeline = project_console_document(document, descriptor, now=NOW)
    return replace(pipeline, observability=observability_value)


def test_cost_snapshot_from_pipeline_converts_measured_credits() -> None:
    site = build_test_site(snowflake_credit_price="3.10", cost_currency="EUR")
    descriptor = SourceDescriptor(site.pipeline_id, "live", site.environment, "file:///example.json")
    pipeline = _minimal_pipeline(observability())
    snapshot = cost_snapshot_from_pipeline(
        pipeline, descriptor, site, scope="connection", id_="conn1", window="24h", now=NOW
    )
    assert snapshot.status == "measured"
    assert snapshot.amount == 7.75
    assert snapshot.currency == "EUR"
    assert "warehouse:" in snapshot.basis


def test_cost_snapshot_from_pipeline_absent_without_price() -> None:
    site = build_test_site()
    descriptor = SourceDescriptor(site.pipeline_id, "live", site.environment, "file:///example.json")
    pipeline = _minimal_pipeline(observability())
    snapshot = cost_snapshot_from_pipeline(
        pipeline, descriptor, site, scope="connection", id_="conn1", window=None, now=NOW
    )
    assert snapshot.status == "absent"
    assert snapshot.amount is None
    assert snapshot.reason


def test_cost_snapshot_from_pipeline_out_of_site_scope() -> None:
    site = build_test_site(snowflake_credit_price="2", cost_currency="EUR")
    other_descriptor = SourceDescriptor("other-site", "live", site.environment, "file:///other.json")
    pipeline = _minimal_pipeline(observability())
    snapshot = cost_snapshot_from_pipeline(
        pipeline, other_descriptor, site, scope="connection", id_="conn1", window=None, now=NOW
    )
    assert snapshot.status == "absent"
    assert snapshot.reason == OUT_OF_SITE_REASON


# --- CostsV1ProjectionAdapter (résolution id v2 -> document réel) ---------


def test_adapter_rejects_table_scope_explicitly() -> None:
    adapter = CostsV1ProjectionAdapter(lambda id_: None)
    snapshot = adapter.get("table", "tbl1", window=None)
    assert snapshot.status == "absent"
    assert snapshot.reason == TABLE_SCOPE_UNSUPPORTED_REASON


def test_adapter_unmapped_connection_returns_absent() -> None:
    adapter = CostsV1ProjectionAdapter(lambda id_: None)
    snapshot = adapter.get("connection", "conn1", window=None)
    assert snapshot.status == "absent"
    assert snapshot.reason == NO_MAPPING_REASON


def test_adapter_reads_a_real_document_without_observability_attached(tmp_path) -> None:
    document = {
        "format_version": "as400-console-v1",
        "generated_at": NOW.isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 1}},
    }
    path = tmp_path / "console.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    origin = f"file://{path}"
    adapter = CostsV1ProjectionAdapter(lambda id_: f"live:acme:{origin}")
    snapshot = adapter.get("connection", "conn1", window=None)
    # Aucune observabilité attachée dans le document -> aucun crédit mesuré,
    # jamais un montant inventé.
    assert snapshot.status == "absent"
    assert snapshot.amount is None
