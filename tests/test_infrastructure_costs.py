"""Les coûts restent attribués et sourcés, jamais une facture inventée."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from dataclasses import replace

from quadringent.infrastructure_costs import storage_cost, cluster_cost, project_infrastructure_costs
from site_fixture import build_test_site

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
START = NOW.replace(hour=0) - timedelta(days=1)
END = START + timedelta(days=1)
SITE = replace(build_test_site(), cost_namespace="quadringent-test")


def prices():
    return {
        "products": {
            "sku": {
                "productFamily": "Storage",
                "attributes": {
                    "regionCode": SITE.aws_region,
                    "volumeType": "Standard",
                    "usagetype": "TimedStorage-ByteHrs",
                },
            }
        },
        "terms": {
            "OnDemand": {
                "sku": {
                    "term": {
                        "priceDimensions": {
                            "rate": {
                                "unit": "GB-Mo",
                                "beginRange": "0",
                                "endRange": "51200",
                                "pricePerUnit": {"USD": "0.037"},
                            }
                        }
                    }
                }
            }
        },
    }


def record(amount, **props):
    return {
        "properties": {"cluster": "cluster-test", **props},
        "window": {"start": START.isoformat(), "end": END.isoformat()},
        "totalCost": amount,
    }


def test_le_stockage_utilise_le_tarif_releve_et_ne_transforme_pas_absence_en_zero():
    points = [{"Timestamp": START, "Average": 10 * 1024**3, "Unit": "Bytes"}]
    result = storage_cost(points, prices(), region=SITE.aws_region, now=NOW)
    assert Decimal(result["monthly_run_rate"]) == Decimal("0.37")
    assert result["price_per_gib_month"] == "0.037"
    assert storage_cost([], prices(), region=SITE.aws_region, now=NOW)["status"] == "unavailable"
    points[0]["Average"] = 0
    assert storage_cost(points, prices(), region=SITE.aws_region, now=NOW)["monthly_run_rate"] == "0.000"


@pytest.mark.parametrize(
    "patch",
    [
        {"Average": float("nan")},
        {"Average": -1},
        {"Timestamp": NOW + timedelta(seconds=1)},
        {"Timestamp": NOW - timedelta(days=5)},
    ],
)
def test_le_stockage_refuse_une_mesure_invalide_ancienne_ou_future(patch):
    point = {"Timestamp": START, "Average": 10, "Unit": "Bytes", **patch}
    assert storage_cost([point], prices(), region=SITE.aws_region, now=NOW)["status"] == "unavailable"


def test_le_tarif_manquant_ne_prend_jamais_une_constante_de_repli():
    point = {"Timestamp": START, "Average": 10, "Unit": "Bytes"}
    assert (
        storage_cost([point], {"products": {}, "terms": {}}, region=SITE.aws_region, now=NOW)["status"] == "unavailable"
    )


def test_le_cluster_distingue_namespace_actifs_et_inutilise():
    allocation = {
        "code": 200,
        "data": [{"example": record(0.25, namespace=SITE.cost_namespace), "__idle__": record(2)}],
    }
    assets = {"code": 200, "data": {"node": record(3), "other": record(99, cluster="another")}}
    result = cluster_cost(
        allocation,
        assets,
        namespace=SITE.cost_namespace,
        cluster_id="cluster-test",
        currency="USD",
        start=START,
        end=END,
    )
    assert result["allocated_amount"] == "0.25"
    assert result["cluster_amount"] == "3"
    assert result["idle_amount"] == "2"
    assert (
        cluster_cost(
            {"code": 200, "data": [{}]},
            assets,
            namespace=SITE.cost_namespace,
            cluster_id="cluster-test",
            currency="USD",
            start=START,
            end=END,
        )["status"]
        == "unavailable"
    )
    allocation["data"][0]["example"]["window"]["end"] = NOW.isoformat()
    assert (
        cluster_cost(
            allocation,
            assets,
            namespace=SITE.cost_namespace,
            cluster_id="cluster-test",
            currency="USD",
            start=START,
            end=END,
        )["status"]
        == "unavailable"
    )


def test_la_projection_refuse_le_mauvais_site_et_les_preuves_simulees():
    proof = {
        "format_version": "quadringent-infrastructure-costs-v1",
        "site_id": SITE.site_id,
        "environment": SITE.environment,
        "namespace": SITE.cost_namespace,
        "bucket": SITE.raw_bucket,
        "region": SITE.aws_region,
        "evidence_kind": "live",
        "collected_at": NOW.isoformat(),
        "storage": {"status": "unavailable"},
        "cluster": {"status": "unavailable"},
    }
    assert project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")["status"] == "available"
    assert (
        project_infrastructure_costs({**proof, "site_id": "other"}, site=SITE, now=NOW, evidence_kind="live")["status"]
        == "unavailable"
    )
    assert (
        project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="simulation")["status"] == "unavailable"
    )


def test_un_montant_forge_n_est_pas_presente_comme_mesure():
    storage = storage_cost(
        [{"Timestamp": START, "Average": 1024**3, "Unit": "Bytes"}], prices(), region=SITE.aws_region, now=NOW
    )
    proof = {
        "format_version": "quadringent-infrastructure-costs-v1",
        "site_id": SITE.site_id,
        "environment": SITE.environment,
        "namespace": SITE.cost_namespace,
        "bucket": SITE.raw_bucket,
        "region": SITE.aws_region,
        "evidence_kind": "live",
        "collected_at": NOW.isoformat(),
        "storage": storage,
        "cluster": {"status": "unavailable"},
    }
    assert (
        project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")["storage"]["monthly_run_rate"]
        == "0.037"
    )
    storage["monthly_run_rate"] = "999"
    assert (
        project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")["storage"]["status"]
        == "unavailable"
    )


def measured_proof():
    return {
        "format_version": "quadringent-infrastructure-costs-v1",
        "site_id": SITE.site_id,
        "environment": SITE.environment,
        "namespace": SITE.cost_namespace,
        "bucket": SITE.raw_bucket,
        "region": SITE.aws_region,
        "evidence_kind": "live",
        "collected_at": NOW.isoformat(),
        "storage": storage_cost(
            [{"Timestamp": START, "Average": 1024**3, "Unit": "Bytes"}], prices(), region=SITE.aws_region, now=NOW
        ),
        "cluster": cluster_cost(
            {"code": 200, "data": [{"namespace": record(0.25, namespace=SITE.cost_namespace)}]},
            {"code": 200, "data": {"node": record(3)}},
            namespace=SITE.cost_namespace,
            cluster_id="cluster-test",
            currency="EUR",
            start=START,
            end=END,
        ),
    }


@pytest.mark.parametrize("field", ["site_id", "environment", "namespace", "bucket", "region"])
def test_la_preuve_d_un_autre_perimetre_ne_chiffre_pas_ce_site(field):
    proof = measured_proof()
    proof[field] = "another"
    assert project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")["status"] == "unavailable"


@pytest.mark.parametrize("kind", ["simulation", "historical"])
def test_les_preuves_non_live_ne_portent_pas_de_cout_actuel(kind):
    proof = measured_proof()
    assert project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind=kind)["status"] == "unavailable"
    proof["evidence_kind"] = kind
    assert project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")["status"] == "unavailable"


def test_une_mesure_invalide_ne_masque_pas_l_autre_source_valide():
    proof = measured_proof()
    proof["storage"]["observed_at"] = (NOW - timedelta(days=4)).isoformat()
    projected = project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")
    assert projected["storage"]["status"] == "unavailable"
    assert projected["cluster"]["allocated_amount"] == "0.25"
    assert projected["cluster"]["idle_amount"] is None
    assert projected["cluster"]["currency"] == "EUR"
    proof = measured_proof()
    proof["cluster"]["end"] = NOW.isoformat()
    projected = project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")
    assert projected["cluster"]["status"] == "unavailable"
    assert projected["storage"]["status"] == "measured"


@pytest.mark.parametrize("offset", [timedelta(seconds=1), -timedelta(hours=27)])
def test_la_collecte_future_ou_ancienne_n_est_pas_actuelle(offset):
    proof = measured_proof()
    proof["collected_at"] = (NOW + offset).isoformat()
    assert project_infrastructure_costs(proof, site=SITE, now=NOW, evidence_kind="live")["status"] == "unavailable"


def test_le_releve_local_est_branche_et_son_absence_ne_casse_pas_la_capture(tmp_path):
    import json
    from unittest.mock import patch
    from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
    from test_control_plane_repository import document

    source = tmp_path / "source.json"
    source.write_text(json.dumps(document()))
    proof = tmp_path / "costs.json"
    now = datetime.now(timezone.utc)
    proof.write_text(
        json.dumps(
            {
                "format_version": "quadringent-infrastructure-costs-v1",
                "site_id": SITE.site_id,
                "environment": SITE.environment,
                "namespace": SITE.cost_namespace,
                "bucket": SITE.raw_bucket,
                "region": SITE.aws_region,
                "evidence_kind": "live",
                "collected_at": now.isoformat(),
                "storage": {"status": "unavailable"},
                "cluster": {"status": "unavailable"},
            }
        )
    )
    repo = ProjectionRepository(
        [parse_source_spec(f"live:{SITE.site_id}:{source.as_uri()}", environment=SITE.environment)],
        infrastructure_costs_source=proof.as_uri(),
    )
    with patch("quadringent_control_plane.repository._site", return_value=SITE):
        first = repo.refresh()
        assert first.pipelines[0].to_dict()["infrastructure_costs"]["status"] == "available"
        proof.unlink()
        second = repo.refresh()
        assert second.pipelines[0].infrastructure_costs["status"] == "unavailable"
        assert second.sources[0].status == "available"
