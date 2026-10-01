"""Le cockpit relit les faits du chargeur, sans mélanger deux tables."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import create_engine

from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.services.loader_telemetry import (
    KubernetesLoaderTelemetry,
    LoaderIdentity,
    resolve_loader_identity,
)


class FakePods:
    def __init__(self, text: str) -> None:
        self.text = text
        self.selectors: list[str] = []

    def list_pod_names(self, *, label_selector: str, limit: int) -> tuple[str, ...]:
        self.selectors.append(label_selector)
        return ("loader-1",)

    def read_pod_log(self, name: str, *, since_time: str | None, tail_lines: int) -> str:
        assert name == "loader-1"
        assert tail_lines <= 5000
        return self.text


def _line(at: str, message: str) -> str:
    return f"{at} 2026-09-28 21:00:00,000 INFO {message}\n"


def test_loader_telemetry_is_per_table_and_uses_only_measured_delivery() -> None:
    log = "".join(
        (
            _line("2026-09-28T21:01:00Z", "retard mesuré table=QDC_TAIL_HISTORY historique=12.0s miroir=13.0s"),
            _line("2026-09-28T21:02:00Z", "livraison table=QDC_TAIL événements_nouveaux=2 miroir_secondes=11.2"),
            _line("2026-09-28T21:03:00Z", "livraison table=QDC_ORDERS événements_nouveaux=300 miroir_secondes=99.0"),
            _line("2026-09-28T21:04:00Z", "livraison table=QDC_TAIL événements_nouveaux=3 miroir_secondes=8.1"),
            _line("2026-09-28T21:05:00Z", "password=should-not-leak table=QDC_TAIL"),
        )
    )
    pods = FakePods(log)
    telemetry = KubernetesLoaderTelemetry(
        pods,
        resolve=lambda pipeline_id: LoaderIdentity("dst1", "QDC_TAIL") if pipeline_id == "p1" else None,
        now=lambda: datetime(2026, 9, 28, 21, 10, tzinfo=timezone.utc),
    )

    series = telemetry.metrics("p1", "1h")
    assert [point.lag_seconds for point in series.points] == [11.2, 8.1]
    assert series.points[0].throughput_rows_per_second is None
    assert series.points[1].throughput_rows_per_second == 3 / 120
    assert series.provenance == "journal_chargeur_kubernetes"
    assert pods.selectors == ["quadringent.io/component=destination-loader,quadringent.io/destination-id=dst1"]

    entries = telemetry.fetch("p1", since=None)
    assert len(entries) == 3
    assert all("QDC_ORDERS" not in entry.message for entry in entries)
    assert all("should-not-leak" not in entry.message for entry in entries)

    observation = telemetry.observe("p1")
    assert observation.lag_seconds is None  # pas de mesure du retard source -> miroir au repos
    assert observation.history_lag_seconds == 12.0
    assert observation.mirror_lag_seconds == 13.0
    assert observation.throughput_rows_per_second == 3 / 120
    assert observation.last_arrival_at == "2026-09-28T21:04:00Z"


def test_loader_telemetry_refuses_unknown_mapping_and_stale_delivery() -> None:
    pods = FakePods(_line("2026-09-27T20:00:00Z", "livraison table=QDC_TAIL événements_nouveaux=2 miroir_secondes=9.0"))
    telemetry = KubernetesLoaderTelemetry(
        pods,
        resolve=lambda pipeline_id: None if pipeline_id == "unknown" else LoaderIdentity("dst1", "QDC_TAIL"),
        now=lambda: datetime(2026, 9, 28, 21, 10, tzinfo=timezone.utc),
    )
    assert telemetry.metrics("unknown", "1h").points == ()
    assert telemetry.fetch("unknown", since=None) == ()
    assert telemetry.metrics("p1", "24h").points == ()
    assert telemetry.observe("p1").last_arrival_at is None


def test_loader_identity_comes_from_the_pipeline_table_and_destination(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'telemetry.sqlite3'}")
    schema.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "org1", "name": "Site"})
        connection.execute(schema.sources.insert(), {
            "id": "src1", "org_id": "org1", "display_name": "Source",
            "ibmi_host": "as400.example.test", "ibmi_user": "TEST", "secret_ciphertext": "ciphertext",
        })
        connection.execute(schema.destinations.insert(), {
            "id": "dst1", "org_id": "org1", "snowflake_account": "example",
            "key_pair_ciphertext": "ciphertext", "setup_script": "-- test",
        })
        connection.execute(schema.tables.insert(), {
            "id": "tbl1", "source_id": "src1", "schema_name": "TESTLIB", "table_name": "QDC_TAIL",
        })
        connection.execute(schema.pipelines.insert(), {
            "id": "p1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": "live",
        })
    assert resolve_loader_identity(engine, "p1") == LoaderIdentity("dst1", "QDC_TAIL", "tbl1")
    assert resolve_loader_identity(engine, "absent") is None
    engine.dispose()
