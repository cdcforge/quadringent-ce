"""Les exemples publics ne transportent aucune observation de site privé."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from quadringent.site_config import current, from_environment, install
from quadringent_control_plane.fleet_plan import parse_fleet_catalog
from quadringent_control_plane.fleet_sidecar import generate_fleet_ui_sidecar, parse_fleet_ui_sidecar
from site_fixture import TEST_SITE_ENV


def test_public_catalog_and_sidecar_have_only_synthetic_unqualified_data() -> None:
    root = Path(__file__).resolve().parents[1] / "infra-values"
    catalog_payload = json.loads((root / "fleet-catalog-int.json").read_text())
    sidecar_payload = json.loads((root / "fleet-sidecar-int.json").read_text())
    env = dict(TEST_SITE_ENV)
    env.update(
        QUADRINGENT_ENVIRONMENT="dev",
        QUADRINGENT_SITE_ID="example-corp",
        QUADRINGENT_SOURCE_SCHEMA="SALES",
        QUADRINGENT_JOURNAL_NAME="DEMOJRN",
        QUADRINGENT_FLEET_TABLES=",".join(t["name"] for t in catalog_payload["tables"]),
        QUADRINGENT_KEYED_TABLES="SALE",
        QUADRINGENT_PROVISIONED_STAGES="SALE",
        QUADRINGENT_PROOF_KEY_COLUMNS="ID",
        QUADRINGENT_DESTINATION_DATABASE="EXAMPLE_RAW",
        QUADRINGENT_DESTINATION_SCHEMA="IBMI_DEV",
    )
    previous = current()
    try:
        install(from_environment(env))
        catalog = parse_fleet_catalog(catalog_payload)
        sidecar = parse_fleet_ui_sidecar(sidecar_payload)
        generated = generate_fleet_ui_sidecar(
            catalog_payload, generated_at=datetime(2000, 1, 2, tzinfo=timezone.utc)
        )
        # La fixture ajoute seulement la provenance de simulation aux champs
        # déjà acceptés ; toutes les données restent celles du générateur natif.
        generated_pipeline = generated["overview"]["pipelines"][0]
        generated_pipeline["quality"]["evidence_kind"] = "simulation"
        generated_pipeline["summary"] = (
            "Simulation synthétique vide : aucune observation de client, capture ou destination qualifiée. "
            "Les enveloppes historical et admissions de plan sont techniques, sans preuve runtime."
        )
        generated_pipeline["stages"][0].update(
            status="unknown",
            headline="Catalogue synthétique de démonstration",
            detail="Volumes nuls et date 2000 de scénario ; aucune source réelle observée. "
            "historical désigne uniquement le format metadata-only, pas une observation client.",
        )
        assert sidecar == parse_fleet_ui_sidecar(generated)
        assert generated == sidecar_payload
    finally:
        install(previous)
    assert catalog.observed_at.startswith("2000-")
    assert catalog.observed_row_count == 0
    assert all(table.data_size == 0 for table in catalog.tables)
    assert sidecar["generated_at"].startswith("2000-")
    assert sidecar["fleet"]["observed_totals"]["row_count"] == 0
    assert sidecar["fleet"]["observed_totals"]["data_size"] == 0
    assert sidecar["fleet"]["continuity"] == "uncertain"
    assert sidecar["fleet"]["certification_blocked"] is True
    assert sidecar["fleet"]["cost"]["status"] == "unknown"
    assert all(table["copied_rows"] is None for table in sidecar["fleet"]["tables"])

    pipeline = sidecar["overview"]["pipelines"][0]
    assert pipeline["quality"]["evidence_kind"] == "simulation"
    assert pipeline["stages"][0]["status"] == "unknown"
    assert "aucune observation de client" in pipeline["summary"]
