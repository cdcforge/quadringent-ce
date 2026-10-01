"""Compose a bounded SLO/alert proof into the autonomous cockpit snapshot."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re
from typing import Any, Mapping, Sequence

from .site_config import SiteConfig
from .slo_alerts import reconcile_alerts, slo_report_digest, validate_alert_state
from .slo import SloPolicy, evaluate_slo
from .slo_telemetry import collect_slo_telemetry


OBSERVABILITY_SCHEMA = "quadringent-observability-v1"
# Enveloppes persistées avant le renommage produit : acceptées en lecture
# seule. Toute nouvelle écriture porte OBSERVABILITY_SCHEMA.
LEGACY_OBSERVABILITY_SCHEMAS = frozenset({"cdcforge-observability-v1"})
ACCEPTED_OBSERVABILITY_SCHEMAS = frozenset(
    {OBSERVABILITY_SCHEMA} | LEGACY_OBSERVABILITY_SCHEMAS
)


def read_previous_observability(
    client: Any, *, site: SiteConfig
) -> tuple[dict[str, object] | None, str | None]:
    """Read only the declared proof key; lost or corrupt history is never reset.

    The ETag must be retained for conditional publication. A legacy proof with
    no observability is a valid bootstrap, but an explicitly malformed one is not.
    """
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    from .snowflake_autonomous import autonomous_proof_s3_target

    bucket, key = autonomous_proof_s3_target(
        site.autonomous_proof_s3_uri, expected=site.autonomous_proof_s3_uri
    )
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
            return None, None
        raise
    body = response["Body"]
    try:
        size = response.get("ContentLength")
        if type(size) is not int or not 0 < size <= 1048576:
            raise ValueError("previous proof size is invalid")
        payload = body.read(1048577)
        if len(payload) != size:
            raise ValueError("previous proof length mismatch")
        document = json.loads(payload)
    finally:
        body.close()
    etag = response.get("ETag")
    if not isinstance(etag, str) or not etag.strip():
        raise ValueError("previous proof publication version is missing")
    if not isinstance(document, dict) or document.get("format_version") != "as400-console-v1":
        raise ValueError("previous proof format is invalid")
    flux = document.get("flux")
    flux_id = flux.get("id") if isinstance(flux, dict) else None
    if not isinstance(flux_id, str) or not re.fullmatch(
        re.escape(site.stream_prefix) + r"(?:/runs/[a-z0-9][a-z0-9-]{0,79})?", flux_id
    ):
        raise ValueError("previous proof escaped the declared stream scope")
    if "observability" not in document:
        return None, etag
    observation = document["observability"]
    if not isinstance(observation, dict) or (
        observation.get("schema_version") not in ACCEPTED_OBSERVABILITY_SCHEMAS
        or observation.get("environment") != site.environment
        or observation.get("pipeline_id") != site.pipeline_id
    ):
        raise ValueError("previous observability scope is invalid")
    validated = attach_observability_snapshot(
        document, observation.get("slo_report"), observation.get("alert_state"),
        site=site,
    )
    return validated["observability"]["alert_state"], etag


def collect_and_attach_observability(
    proof: Mapping[str, object], s3_client: Any, cloudwatch_client: Any,
    snowflake_cursor: Any, *, object_keys: Sequence[str], policy: SloPolicy,
    now: datetime, site: SiteConfig,
    previous_alert_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Collect the reconciled population and attach an honest SLO verdict."""
    run = proof.get("run")
    destination = proof.get("destination_proof")
    if not isinstance(run, Mapping) or not isinstance(destination, Mapping):
        raise ValueError("proof window is missing")
    reconciliation = destination.get("reconciliation")
    if not isinstance(reconciliation, Mapping) or reconciliation.get("state") != "matched":
        raise ValueError("observability requires a reconciled population")
    start, end = run.get("started_at"), destination.get("observed_at")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("proof window timestamps are missing")
    expected = reconciliation.get("ledger_event_count")
    if type(expected) is not int or expected <= 0:
        raise ValueError("reconciled population is missing")
    telemetry = collect_slo_telemetry(
        s3_client, cloudwatch_client, snowflake_cursor, now=now,
        window_started_at=datetime.fromisoformat(start.replace("Z", "+00:00")),
        window_ended_at=datetime.fromisoformat(end.replace("Z", "+00:00")),
        site=site, object_keys=object_keys, expected_event_count=expected,
    )
    report = evaluate_slo(proof, telemetry, policy, now=now, site=site)
    alerts = reconcile_alerts(report, previous_alert_state, site=site)
    return attach_observability_snapshot(proof, report, alerts, site=site)


def attach_observability_snapshot(
    proof: Mapping[str, object],
    report: Mapping[str, object],
    alerts: Mapping[str, object],
    *,
    site: SiteConfig,
) -> dict[str, object]:
    """Return a new combined snapshot after strict cross-document validation."""

    if not isinstance(proof, Mapping) or proof.get("format_version") != "as400-console-v1":
        raise ValueError("autonomous proof format is incompatible")
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    report_digest = slo_report_digest(report)
    state = validate_alert_state(alerts, site=site)
    if (
        state.get("environment") != site.environment
        or state.get("pipeline_id") != site.pipeline_id
    ):
        raise ValueError("alert state escaped the declared pipeline")
    if state.get("updated_at") != report.get("observed_at"):
        raise ValueError("SLO report and alert state timestamps do not match")
    if state.get("source_report_digest") != report_digest:
        raise ValueError("SLO report and alert state digests do not match")

    combined = deepcopy(dict(proof))
    combined["observability"] = {
        "schema_version": OBSERVABILITY_SCHEMA,
        "pipeline_id": state["pipeline_id"],
        "environment": state["environment"],
        "slo_report": deepcopy(dict(report)),
        "alert_state": deepcopy(state),
    }
    return combined
