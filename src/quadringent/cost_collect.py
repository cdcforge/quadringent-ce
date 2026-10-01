#!/usr/bin/env python3
"""Collecte en lecture seule S3/CloudWatch et OpenCost ; écrit une preuve locale."""

from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse
from urllib.request import build_opener, HTTPRedirectHandler

from quadringent.infrastructure_costs import FORMAT, storage_cost, cluster_cost
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("redirection refusée")


def fetch(url):
    with build_opener(NoRedirect).open(url, timeout=10) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("réponse trop volumineuse")
    return json.loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("site-id", "environment", "namespace", "bucket", "region", "output"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--aws-profile")
    parser.add_argument("--opencost-url")
    parser.add_argument("--cluster-id")
    parser.add_argument("--cluster-currency")
    args = parser.parse_args(argv)
    if (
        args.environment not in {"dev", "int", "test", "staging"}
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.site_id)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.namespace)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", args.bucket)
        or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", args.region)
    ):
        parser.error("périmètre non productif explicite requis")
    if args.opencost_url:
        parsed = urlparse(args.opencost_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not args.cluster_id
            or not args.cluster_currency
            or not re.fullmatch(r"[A-Z]{3}", args.cluster_currency)
        ):
            parser.error("endpoint OpenCost, cluster et devise explicites requis")
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    proof = {
        "format_version": FORMAT,
        "site_id": args.site_id,
        "environment": args.environment,
        "namespace": args.namespace,
        "bucket": args.bucket,
        "region": args.region,
        "evidence_kind": "live",
        "collected_at": now.isoformat(),
        "storage": {"status": "unavailable"},
        "cluster": {"status": "unavailable"},
    }
    errors = []
    try:
        import boto3
        from botocore.config import Config

        session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
        cw = session.client(
            "cloudwatch", config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})
        )
        data = cw.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="BucketSizeBytes",
            Dimensions=[
                {"Name": "BucketName", "Value": args.bucket},
                {"Name": "StorageType", "Value": "StandardStorage"},
            ],
            # Une fenêtre glissante décalerait l'horodatage agrégé du relevé quotidien.
            StartTime=end - timedelta(days=4),
            EndTime=now,
            Period=86400,
            Statistics=["Average"],
        )
        prices = fetch(
            f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/{args.region}/index.json"
        )
        proof["storage"] = storage_cost(data.get("Datapoints", []), prices, region=args.region, now=now)
    except Exception:
        errors.append("storage_collection_unavailable")
    if args.opencost_url:
        try:
            window = start.strftime("%Y-%m-%dT%H:%M:%SZ") + "," + end.strftime("%Y-%m-%dT%H:%M:%SZ")
            base = args.opencost_url.rstrip("/")
            allocation = fetch(
                base
                + "/allocation?"
                + urlencode(
                    {
                        "window": window,
                        "aggregate": "cluster,namespace",
                        "accumulate": "true",
                        "includeIdle": "true",
                        "shareIdle": "false",
                    }
                )
            )
            assets = fetch(base + "/assets?" + urlencode({"window": window}))
            proof["cluster"] = cluster_cost(
                allocation,
                assets,
                namespace=args.namespace,
                cluster_id=args.cluster_id,
                currency=args.cluster_currency,
                start=start,
                end=end,
            )
        except Exception:
            errors.append("cluster_collection_unavailable")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    AtomicJsonStateStore(output).save(proof)
    print(json.dumps({"storage": proof["storage"]["status"], "cluster": proof["cluster"]["status"], "errors": errors}))
    requested = ("storage", "cluster") if args.opencost_url else ("storage",)
    return int(any(proof[k]["status"] != "measured" for k in requested))


if __name__ == "__main__":
    raise SystemExit(main())
