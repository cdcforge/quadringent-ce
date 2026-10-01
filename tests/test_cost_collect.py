"""La collecte reste bornée, en lecture seule, avec des pannes indépendantes."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import boto3
import pytest

from quadringent import cost_collect
from test_infrastructure_costs import SITE, prices


@pytest.mark.parametrize("storage_failure", [False, True])
def test_la_collecte_date_les_fenetres_et_isole_les_pannes(tmp_path, monkeypatch, storage_failure):
    calls = []

    class CloudWatch:
        def get_metric_statistics(self, **kwargs):
            calls.append(kwargs)
            if storage_failure:
                raise RuntimeError("détail privé à ne pas publier")
            return {"Datapoints": [{"Timestamp": datetime.now(timezone.utc)-timedelta(minutes=1), "Average": 1024**3, "Unit": "Bytes"}]}

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["region_name"] == SITE.aws_region

        def client(self, name, **kwargs):
            assert name == "cloudwatch"
            return CloudWatch()

    def fetch(url):
        if url.startswith("https://pricing.us-east-1.amazonaws.com/"):
            return prices()
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        start, end = query["window"][0].split(",")
        assert start.endswith("Z") and end.endswith("Z")
        row = {
            "properties": {"cluster": "cluster-test", "namespace": SITE.cost_namespace},
            "window": {"start": start, "end": end},
            "totalCost": 0.25,
        }
        if parsed.path == "/allocation":
            assert query["aggregate"] == ["cluster,namespace"]
            assert query["shareIdle"] == ["false"]
            return {"code": 200, "data": [{"namespace": row}]}
        assert parsed.path == "/assets"
        return {"code": 200, "data": {"node": {**row, "totalCost": 3}}}

    monkeypatch.setattr(boto3, "Session", Session)
    monkeypatch.setattr(cost_collect, "fetch", fetch)
    output = tmp_path / "private" / "costs.json"
    result = cost_collect.main(
        [
            "--site-id",
            SITE.site_id,
            "--environment",
            SITE.environment,
            "--namespace",
            SITE.cost_namespace,
            "--bucket",
            SITE.raw_bucket,
            "--region",
            SITE.aws_region,
            "--opencost-url",
            "http://localhost:19003",
            "--cluster-id",
            "cluster-test",
            "--cluster-currency",
            "EUR",
            "--output",
            str(output),
        ]
    )
    proof = json.loads(output.read_text())
    assert result == int(storage_failure)
    assert proof["storage"]["status"] == ("unavailable" if storage_failure else "measured")
    assert proof["cluster"]["allocated_amount"] == "0.25"
    assert proof["cluster"]["currency"] == "EUR"
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert calls[0]["Namespace"] == "AWS/S3"
    assert calls[0]["StartTime"].time().isoformat() == "00:00:00"
    assert calls[0]["Dimensions"] == [
        {"Name": "BucketName", "Value": SITE.raw_bucket},
        {"Name": "StorageType", "Value": "StandardStorage"},
    ]


def test_la_production_est_refusee_avant_tout_acces(tmp_path, monkeypatch):
    monkeypatch.setattr(cost_collect, "fetch", lambda _: pytest.fail("accès réseau interdit"))
    with pytest.raises(SystemExit) as error:
        cost_collect.main(
            [
                "--site-id",
                SITE.site_id,
                "--environment",
                "prod",
                "--namespace",
                SITE.cost_namespace,
                "--bucket",
                SITE.raw_bucket,
                "--region",
                SITE.aws_region,
                "--output",
                str(tmp_path / "costs.json"),
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "costs.json").exists()


def test_une_redirection_http_n_est_pas_suivie():
    with pytest.raises(ValueError, match="redirection refusée"):
        cost_collect.NoRedirect().redirect_request(None, None, 302, "", None, "https://other.invalid/")
