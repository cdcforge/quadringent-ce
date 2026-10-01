"""Deterministic lifecycle projection for Quadringent SLO alerts.

The SLO evaluator describes the current observation.  This module turns those
instantaneous signals into an idempotent operator contract: a stable alert
identity, explicit firing/resolved transitions and a bounded persistent state.
It deliberately delivers no notification itself. The environment and pipeline
identities stamped on states and batches always come from the declared site
configuration — never from a literal.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Mapping

from .site_config import SiteConfig


ALERT_BATCH_SCHEMA = "quadringent-alert-batch-v1"
ALERT_STATE_SCHEMA = "quadringent-alert-state-v1"
SLO_REPORT_SCHEMA = "quadringent-slo-v1"
# Schémas persistés avant le renommage produit : acceptés en lecture seule.
# Leur contenu reste intègre — digests et empreintes portent sur les champs,
# pas sur le nom de version. Toute nouvelle écriture porte les schémas ci-dessus.
LEGACY_ALERT_STATE_SCHEMAS = frozenset({"cdcforge-alert-state-v1"})
LEGACY_SLO_REPORT_SCHEMAS = frozenset({"cdcforge-slo-v1"})
_SIGNAL_STATUSES = {"pass", "breach", "unobserved"}


def _site(site: SiteConfig) -> SiteConfig:
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    return site


def reconcile_alerts(
    report: Mapping[str, object],
    previous: Mapping[str, object] | None = None,
    *,
    site: SiteConfig,
) -> dict[str, object]:
    """Project one validated SLO report into an idempotent alert lifecycle.

    A missing check never resolves an existing alert.  Only an explicit
    ``pass`` for the same check can emit ``resolved``.
    """

    site = _site(site)
    parsed = _validate_report(report)
    observed_at = parsed["observed_at"]
    report_digest = parsed["digest"]
    previous_state = _validate_previous(previous, site)

    if previous_state is not None:
        previous_at = _timestamp(previous_state["updated_at"], "state.updated_at")
        if observed_at < previous_at:
            raise ValueError("SLO report is older than alert state")
        if observed_at == previous_at:
            if previous_state["source_report_digest"] != report_digest:
                raise ValueError("conflicting report at the same observed_at")
            return _batch(report, report_digest, [], deepcopy(previous_state), site)

    previous_records = {
        record["check_id"]: deepcopy(record)
        for record in (previous_state or {}).get("alerts", [])
    }
    current_checks = parsed["checks"]
    next_records = dict(previous_records)
    events: list[dict[str, object]] = []

    for check_id, check in current_checks.items():
        prior = previous_records.get(check_id)
        if check["status"] == "pass":
            if prior is not None and prior["lifecycle_state"] == "firing":
                record = _resolve(prior, check, parsed["observed_at_text"])
                next_records[check_id] = record
                events.append(_event(record, "resolved", "resolved"))
            continue

        if prior is None:
            record = _open(check, parsed["observed_at_text"], site)
            transition = "opened"
        elif prior["lifecycle_state"] == "resolved":
            record = _reopen(prior, check, parsed["observed_at_text"])
            transition = "reopened"
        else:
            changed = _signal_signature(prior) != _signal_signature(check)
            record = _refresh(prior, check, parsed["observed_at_text"])
            transition = "changed" if changed else ""
        next_records[check_id] = record
        if transition:
            events.append(_event(record, "firing", transition))

    state = _seal_state({
        "schema_version": ALERT_STATE_SCHEMA,
        "environment": site.environment,
        "pipeline_id": site.pipeline_id,
        "updated_at": parsed["observed_at_text"],
        "source_report_digest": report_digest,
        "alerts": sorted(next_records.values(), key=lambda item: item["check_id"]),
    })
    return _batch(report, report_digest, events, state, site)


def alert_fingerprint(check_id: str, *, site: SiteConfig) -> str:
    if not isinstance(check_id, str) or not check_id:
        raise ValueError("check_id must be a non-empty string")
    site = _site(site)
    identity = {
        "environment": site.environment,
        "pipeline_id": site.pipeline_id,
        "check_id": check_id,
    }
    return _digest(identity)


def slo_report_digest(report: Mapping[str, object]) -> str:
    """Return the order-independent digest of one validated v1 SLO report."""

    return str(_validate_report(report)["digest"])


def validate_alert_state(
    previous: Mapping[str, object], *, site: SiteConfig
) -> dict[str, object]:
    """Validate and detach the persistent state from a state or batch document."""

    state = _validate_previous(previous, _site(site))
    if state is None:
        raise ValueError("alert state is required")
    return state


def _batch(
    report: Mapping[str, object],
    report_digest: str,
    events: list[dict[str, object]],
    state: Mapping[str, object],
    site: SiteConfig,
) -> dict[str, object]:
    return {
        "schema_version": ALERT_BATCH_SCHEMA,
        "environment": site.environment,
        "pipeline_id": site.pipeline_id,
        "observed_at": report["observed_at"],
        "source_report": {
            "schema_version": report["schema_version"],
            "digest": report_digest,
            "status": report["status"],
        },
        "events": events,
        "state": state,
    }


def _open(
    check: Mapping[str, object], observed_at: str, site: SiteConfig
) -> dict[str, object]:
    return {
        "fingerprint": alert_fingerprint(str(check["id"]), site=site),
        "check_id": check["id"],
        "stage": check["stage"],
        "lifecycle_state": "firing",
        "signal_status": check["status"],
        "severity": _severity(str(check["status"])),
        "reason": check["reason"],
        "observed": check.get("observed"),
        "threshold": check.get("threshold"),
        "unit": check.get("unit"),
        "first_fired_at": observed_at,
        "firing_since": observed_at,
        "last_observed_at": observed_at,
        "resolved_at": None,
        "occurrence_count": 1,
        "evaluation_count": 1,
    }


def _refresh(
    prior: Mapping[str, object], check: Mapping[str, object], observed_at: str
) -> dict[str, object]:
    record = dict(prior)
    record.update(
        {
            "stage": check["stage"],
            "signal_status": check["status"],
            "severity": _severity(str(check["status"])),
            "reason": check["reason"],
            "observed": check.get("observed"),
            "threshold": check.get("threshold"),
            "unit": check.get("unit"),
            "last_observed_at": observed_at,
            "evaluation_count": int(prior["evaluation_count"]) + 1,
        }
    )
    return record


def _resolve(
    prior: Mapping[str, object], check: Mapping[str, object], observed_at: str
) -> dict[str, object]:
    record = dict(prior)
    record.update(
        {
            "stage": check["stage"],
            "lifecycle_state": "resolved",
            "signal_status": "pass",
            "severity": "none",
            "reason": check["reason"],
            "observed": check.get("observed"),
            "threshold": check.get("threshold"),
            "unit": check.get("unit"),
            "last_observed_at": observed_at,
            "resolved_at": observed_at,
            "evaluation_count": int(prior["evaluation_count"]) + 1,
        }
    )
    return record


def _reopen(
    prior: Mapping[str, object], check: Mapping[str, object], observed_at: str
) -> dict[str, object]:
    record = dict(prior)
    record.update(
        {
            "stage": check["stage"],
            "lifecycle_state": "firing",
            "signal_status": check["status"],
            "severity": _severity(str(check["status"])),
            "reason": check["reason"],
            "observed": check.get("observed"),
            "threshold": check.get("threshold"),
            "unit": check.get("unit"),
            "firing_since": observed_at,
            "last_observed_at": observed_at,
            "resolved_at": None,
            "occurrence_count": int(prior["occurrence_count"]) + 1,
            "evaluation_count": int(prior["evaluation_count"]) + 1,
        }
    )
    return record


def _event(
    record: Mapping[str, object], kind: str, transition: str
) -> dict[str, object]:
    event = {
        "fingerprint": record["fingerprint"],
        "check_id": record["check_id"],
        "stage": record["stage"],
        "kind": kind,
        "transition": transition,
        "signal_status": record["signal_status"],
        "severity": "info" if kind == "resolved" else record["severity"],
        "reason": record["reason"],
        "observed": record["observed"],
        "threshold": record["threshold"],
        "unit": record["unit"],
        "first_fired_at": record["first_fired_at"],
        "firing_since": record["firing_since"],
        "resolved_at": record["resolved_at"],
        "occurrence_count": record["occurrence_count"],
        "observed_at": record["last_observed_at"],
    }
    event["event_id"] = _digest(event)
    return event


def _validate_report(report: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(report, Mapping):
        raise ValueError("SLO report must be an object")
    if report.get("schema_version") not in (
        {SLO_REPORT_SCHEMA} | LEGACY_SLO_REPORT_SCHEMAS
    ):
        raise ValueError("unsupported SLO report schema")
    if set(report) != {"schema_version", "observed_at", "status", "checks", "alerts"}:
        raise ValueError("SLO report must contain exactly the v1 fields")
    observed_at_text = report.get("observed_at")
    observed_at = _timestamp(observed_at_text, "report.observed_at")
    checks_value = report.get("checks")
    alerts_value = report.get("alerts")
    if not isinstance(checks_value, list) or not checks_value:
        raise ValueError("SLO report checks must be a non-empty array")
    if not isinstance(alerts_value, list):
        raise ValueError("SLO report alerts must be an array")

    checks: dict[str, Mapping[str, object]] = {}
    for value in checks_value:
        if not isinstance(value, Mapping):
            raise ValueError("SLO report check must be an object")
        if set(value) != {"id", "stage", "status", "observed", "threshold", "unit", "reason"}:
            raise ValueError("SLO report check must contain exactly the v1 fields")
        check_id = value.get("id")
        stage = value.get("stage")
        status = value.get("status")
        reason = value.get("reason")
        if not isinstance(check_id, str) or not check_id or check_id in checks:
            raise ValueError("SLO report check ids must be unique non-empty strings")
        if not isinstance(stage, str) or not stage:
            raise ValueError("SLO report check stage must be a non-empty string")
        if status not in _SIGNAL_STATUSES:
            raise ValueError("SLO report check status is unsupported")
        if not isinstance(reason, str) or not reason:
            raise ValueError("SLO report check reason must be a non-empty string")
        checks[check_id] = value

    expected_status = _overall_status(checks.values())
    if report.get("status") != expected_status:
        raise ValueError("SLO report status does not match checks")

    alert_ids: set[str] = set()
    for value in alerts_value:
        if not isinstance(value, Mapping):
            raise ValueError("SLO report alert must be an object")
        if set(value) != {"check_id", "stage", "status", "severity", "reason"}:
            raise ValueError("SLO report alert must contain exactly the v1 fields")
        check_id = value.get("check_id")
        if not isinstance(check_id, str) or check_id in alert_ids:
            raise ValueError("SLO report alert ids must be unique strings")
        check = checks.get(check_id)
        if check is None or check["status"] == "pass":
            raise ValueError("SLO report alerts do not match non-passing checks")
        expected = {
            "stage": check["stage"],
            "status": check["status"],
            "severity": _severity(str(check["status"])),
            "reason": check["reason"],
        }
        if any(value.get(name) != expected_value for name, expected_value in expected.items()):
            raise ValueError("SLO report alerts do not match non-passing checks")
        alert_ids.add(check_id)
    expected_alert_ids = {
        check_id for check_id, check in checks.items() if check["status"] != "pass"
    }
    if alert_ids != expected_alert_ids:
        raise ValueError("SLO report alerts do not match non-passing checks")

    digest = _semantic_report_digest(report)
    return {
        "observed_at": observed_at,
        "observed_at_text": observed_at_text,
        "checks": checks,
        "digest": digest,
    }


def _validate_previous(
    previous: Mapping[str, object] | None,
    site: SiteConfig,
) -> dict[str, object] | None:
    if previous is None:
        return None
    if not isinstance(previous, Mapping):
        raise ValueError("alert state must be an object")
    candidate: object = previous.get("state", previous)
    if not isinstance(candidate, Mapping):
        raise ValueError("alert state must be an object")
    if candidate.get("schema_version") not in (
        {ALERT_STATE_SCHEMA} | LEGACY_ALERT_STATE_SCHEMAS
    ):
        raise ValueError("unsupported alert state schema")
    if set(candidate) != {
        "schema_version",
        "environment",
        "pipeline_id",
        "updated_at",
        "source_report_digest",
        "alerts",
        "state_digest",
    }:
        raise ValueError("alert state must contain exactly the v1 fields")
    if (
        candidate.get("environment") != site.environment
        or candidate.get("pipeline_id") != site.pipeline_id
    ):
        raise ValueError("alert state escaped the declared pipeline")
    state_updated_at = _timestamp(candidate.get("updated_at"), "state.updated_at")
    digest = candidate.get("source_report_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("alert state source report digest is invalid")
    records = candidate.get("alerts")
    if not isinstance(records, list):
        raise ValueError("alert state alerts must be an array")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("alert state record must be an object")
        if set(record) != {
            "fingerprint",
            "check_id",
            "stage",
            "lifecycle_state",
            "signal_status",
            "severity",
            "reason",
            "observed",
            "threshold",
            "unit",
            "first_fired_at",
            "firing_since",
            "last_observed_at",
            "resolved_at",
            "occurrence_count",
            "evaluation_count",
        }:
            raise ValueError("alert state record must contain exactly the v1 fields")
        check_id = record.get("check_id")
        if not isinstance(check_id, str) or not check_id or check_id in seen:
            raise ValueError("alert state check ids must be unique strings")
        if record.get("lifecycle_state") not in {"firing", "resolved"}:
            raise ValueError("alert lifecycle state is invalid")
        if record.get("signal_status") not in _SIGNAL_STATUSES:
            raise ValueError("alert signal status is invalid")
        if record.get("fingerprint") != alert_fingerprint(check_id, site=site):
            raise ValueError("alert fingerprint is invalid")
        if not isinstance(record.get("stage"), str) or not record["stage"]:
            raise ValueError("alert stage is invalid")
        if not isinstance(record.get("reason"), str) or not record["reason"]:
            raise ValueError("alert reason is invalid")
        for count_name in ("occurrence_count", "evaluation_count"):
            count = record.get(count_name)
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError(f"alert {count_name} is invalid")
        if int(record["occurrence_count"]) > int(record["evaluation_count"]):
            raise ValueError("alert counters are inconsistent")
        times = {
            time_name: _timestamp(record.get(time_name), f"alert.{time_name}")
            for time_name in ("first_fired_at", "firing_since", "last_observed_at")
        }
        if not (
            times["first_fired_at"]
            <= times["firing_since"]
            <= times["last_observed_at"]
        ):
            raise ValueError("alert timestamps are not monotonic")
        if times["last_observed_at"] > state_updated_at:
            raise ValueError("alert observation is newer than state")
        resolved_at = record.get("resolved_at")
        if record["lifecycle_state"] == "firing":
            if record["signal_status"] == "pass" or resolved_at is not None:
                raise ValueError("firing alert state is inconsistent")
            if record.get("severity") != _severity(str(record["signal_status"])):
                raise ValueError("firing alert severity is inconsistent")
        else:
            if record["signal_status"] != "pass" or record.get("severity") != "none":
                raise ValueError("resolved alert state is inconsistent")
            resolved = _timestamp(resolved_at, "alert.resolved_at")
            if resolved != times["last_observed_at"]:
                raise ValueError("resolved alert timestamp is inconsistent")
        seen.add(check_id)
    expected_digest = candidate.get("state_digest")
    state_without_digest = dict(candidate)
    state_without_digest.pop("state_digest")
    if expected_digest != _digest(state_without_digest):
        raise ValueError("alert state integrity digest is invalid")
    return deepcopy(dict(candidate))


def _seal_state(state: dict[str, object]) -> dict[str, object]:
    sealed = dict(state)
    sealed["state_digest"] = _digest(state)
    return sealed


def _semantic_report_digest(report: Mapping[str, object]) -> str:
    checks = report["checks"]
    alerts = report["alerts"]
    assert isinstance(checks, list)
    assert isinstance(alerts, list)
    normalized = {
        "schema_version": report["schema_version"],
        "observed_at": report["observed_at"],
        "status": report["status"],
        "checks": sorted(
            (dict(check) for check in checks),
            key=lambda check: str(check["id"]),
        ),
        "alerts": sorted(
            (dict(alert) for alert in alerts),
            key=lambda alert: str(alert["check_id"]),
        ),
    }
    return _digest(normalized)


def _overall_status(checks: object) -> str:
    statuses = {str(check["status"]) for check in checks}  # type: ignore[index]
    if "breach" in statuses:
        return "breach"
    if "unobserved" in statuses:
        return "unobserved"
    return "pass"


def _signal_signature(value: Mapping[str, object]) -> tuple[object, ...]:
    return (
        value.get("stage"),
        value.get("signal_status", value.get("status")),
        value.get("severity", _severity(str(value.get("status")))),
        value.get("reason"),
        _canonical(value.get("threshold")),
        value.get("unit"),
    )


def _severity(status: str) -> str:
    return "critical" if status == "breach" else "warning"


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("alert input must contain JSON-compatible values") from error


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
