from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import math
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .site_config import SiteConfig


REQUIRED_SLO_CHECK_IDS = frozenset(
    {
        "capture_freshness",
        "capture_state",
        "capture_errors",
        "checkpoint_lag",
        "s3_freshness",
        "s3_requests",
        "snowpipe_queue",
        "canonical_freshness",
        "delivery_latency_p95",
        "delivery_latency_p99",
        "observability_freshness",
        "reconciliation",
        "snowflake_credits",
    }
)


@dataclass(frozen=True)
class SloPolicy:
    """Explicit site thresholds used by the bounded SLO evaluator."""

    capture_freshness_seconds: float
    s3_freshness_seconds: float
    destination_freshness_seconds: float
    checkpoint_lag_sequences: int
    delivery_latency_p95_seconds: float
    delivery_latency_p99_seconds: float
    s3_requests_24h: int
    snowpipe_pending_files: int
    snowflake_credits_24h: float
    observability_freshness_seconds: float

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _number(value, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SloPolicy":
        if not isinstance(value, Mapping):
            raise ValueError("SLO policy must be an object")
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise ValueError("SLO policy must contain exactly the supported thresholds")
        return cls(**{name: value[name] for name in expected})  # type: ignore[arg-type]

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


def evaluate_slo(
    proof: Mapping[str, object],
    telemetry: Mapping[str, object],
    policy: SloPolicy,
    *,
    now: datetime,
    site: "SiteConfig",
) -> dict[str, object]:
    """Evaluate every required stage without allowing missing data to pass."""

    if not isinstance(proof, Mapping) or not isinstance(telemetry, Mapping):
        raise ValueError("proof and telemetry must be objects")
    if site is None:
        raise ValueError("the declared site configuration is required")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    # Le check dead-man mesure l'âge de la sonde contre l'horloge réelle de
    # l'évaluation, jamais contre l'instant substitué utilisé pour les autres
    # mesures : rejouer un fichier ancien doit révéler que l'observabilité
    # s'est tue.
    evaluation_now = now
    collected_at = telemetry.get("collected_at")
    if collected_at is None:
        # Legacy/undated values cannot establish an external measurement.
        telemetry = {}
    else:
        if not isinstance(collected_at, str):
            raise ValueError("collection time must be an ISO timestamp")
        collected = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        if collected.tzinfo is None or collected > now:
            raise ValueError("collection time is naive or in the future")
        # A report describes the collection instant, not the replay instant.
        # Consumers age observed_at against their own clock. Replaying a stored
        # file must neither refresh it nor generate new alert transitions.
        now = collected

    checks = [
        _age_check(
            "capture_freshness",
            "capture",
            proof.get("capture_observed_at", proof.get("generated_at")),
            policy.capture_freshness_seconds,
            now,
        ),
        _capture_state_check(proof),
        _maximum_check(
            "capture_errors",
            "capture",
            _nested_optional(proof, "counters", "errors", "value"),
            0,
            "errors",
        ),
        _checkpoint_lag_check(proof, policy.checkpoint_lag_sequences),
        _age_check(
            "s3_freshness",
            "s3",
            telemetry.get("s3_last_object_at"),
            policy.s3_freshness_seconds,
            now,
        ),
        _maximum_check(
            "s3_requests",
            "cost",
            telemetry.get("s3_requests_24h"),
            policy.s3_requests_24h,
            "requests/24h",
        ),
        _maximum_check(
            "snowpipe_queue",
            "snowpipe",
            telemetry.get("snowpipe_pending_files"),
            policy.snowpipe_pending_files,
            "files",
        ),
        _age_check(
            "canonical_freshness",
            "canonical",
            _nested_optional(proof, "destination_proof", "observed_at"),
            policy.destination_freshness_seconds,
            now,
        ),
        _maximum_check(
            "delivery_latency_p95",
            "delivery",
            telemetry.get("delivery_latency_p95_seconds"),
            policy.delivery_latency_p95_seconds,
            "seconds",
        ),
        _maximum_check(
            "delivery_latency_p99",
            "delivery",
            telemetry.get("delivery_latency_p99_seconds"),
            policy.delivery_latency_p99_seconds,
            "seconds",
        ),
        _reconciliation_check(proof),
        _maximum_check(
            "snowflake_credits",
            "cost",
            telemetry.get("snowflake_credits_24h"),
            policy.snowflake_credits_24h,
            "warehousecredits/delayed24h",
        ),
        _age_check(
            "observability_freshness",
            "observability",
            collected_at,
            policy.observability_freshness_seconds,
            evaluation_now,
        ),
    ]
    credits_check = next(
        check for check in checks if check["id"] == "snowflake_credits"
    )
    metering = telemetry.get("snowflake_credits_window")
    if metering is None and telemetry.get("snowflake_credits_24h") is not None:
        checks[checks.index(credits_check)] = _unobserved(
            "snowflake_credits", "cost", "measurement_missing"
        )
    if metering is not None:
        if (
            not isinstance(metering, Mapping)
            or metering.get("scope") != site.warehouse_name
            or metering.get("status") != "delayed_metering"
        ):
            raise ValueError("invalid warehouse metering provenance")
        start = datetime.fromisoformat(str(metering.get("from_inclusive")))
        end = datetime.fromisoformat(str(metering.get("to_exclusive")))
        rows = metering.get("reported_rows")
        if (
            start.tzinfo is None or end.tzinfo is None
            or end - start != timedelta(hours=24)
            or end > now - timedelta(hours=6)
            or type(rows) is not int or not 0 < rows <= 1000
        ):
            raise ValueError("invalid warehouse metering window")
        # Preserve bounded, machine-readable provenance inside the existing v1
        # public reason token; its digest is included in proof and alert state.
        credits_check["reason"] = (
            f"metering_window_{int(start.timestamp())}_{int(end.timestamp())}_{rows}"
        )
    non_passing = [check for check in checks if check["status"] != "pass"]
    if any(check["status"] == "breach" for check in non_passing):
        status = "breach"
    elif non_passing:
        status = "unobserved"
    else:
        status = "pass"
    alerts = [
        {
            "check_id": check["id"],
            "stage": check["stage"],
            "status": check["status"],
            "severity": "critical" if check["status"] == "breach" else "warning",
            "reason": check["reason"],
        }
        for check in non_passing
    ]
    return {
        "schema_version": "quadringent-slo-v1",
        "observed_at": now.isoformat(),
        "status": status,
        "checks": checks,
        "alerts": alerts,
    }


def _capture_state_check(proof: Mapping[str, object]) -> dict[str, object]:
    run = proof.get("run")
    if not isinstance(run, Mapping):
        return _unobserved("capture_state", "capture", "run_missing")
    state = run.get("state")
    if state == "RUNNING":
        status, reason = "pass", "running"
    elif state == "STOPPED_BUDGET" and run.get("last_error") is None:
        status, reason = "pass", "planned_stop"
    elif state == "STOPPED_FAIL_CLOSED":
        status, reason = "breach", "capture_fail_closed"
    else:
        status, reason = "unobserved", "run_state_unknown"
    return {
        "id": "capture_state",
        "stage": "capture",
        "status": status,
        "observed": state if isinstance(state, str) else None,
        "threshold": ["RUNNING", "STOPPED_BUDGET"],
        "unit": "state",
        "reason": reason,
    }


def _checkpoint_lag_check(
    proof: Mapping[str, object], threshold: int
) -> dict[str, object]:
    checkpoint = _nested_optional(proof, "position", "checkpoint")
    tail = _nested_optional(proof, "position", "source_tail")
    if not isinstance(checkpoint, Mapping) or not isinstance(tail, Mapping):
        return _unobserved("checkpoint_lag", "checkpoint", "position_missing")
    checkpoint_receiver = checkpoint.get("receiver")
    tail_receiver = tail.get("receiver")
    if (
        not isinstance(checkpoint_receiver, str)
        or not checkpoint_receiver
        or not isinstance(tail_receiver, str)
        or not tail_receiver
    ):
        return _unobserved("checkpoint_lag", "checkpoint", "position_missing")
    if checkpoint_receiver != tail_receiver:
        return _unobserved(
            "checkpoint_lag", "checkpoint", "receiver_chain_required"
        )
    return _maximum_check(
        "checkpoint_lag",
        "checkpoint",
        _nested_optional(proof, "lag", "current", "value"),
        threshold,
        "sequences",
    )


def _reconciliation_check(proof: Mapping[str, object]) -> dict[str, object]:
    reconciliation = _nested_optional(proof, "destination_proof", "reconciliation")
    if not isinstance(reconciliation, Mapping):
        return _unobserved("reconciliation", "canonical", "proof_missing")
    names = (
        "captured_event_count",
        "loaded_event_count",
        "ledger_event_count",
        "distinct_event_count",
        "duplicate_event_count",
        "missing_event_count",
        "unexpected_event_count",
        "failed_mutation_count",
    )
    if any(name not in reconciliation for name in names):
        return _unobserved("reconciliation", "canonical", "counts_missing")
    counts = {name: _number(reconciliation[name], name) for name in names}
    if any(value < 0 for value in counts.values()):
        raise ValueError("reconciliation counts must be non-negative")
    expected = counts["captured_event_count"]
    delta = max(
        abs(expected - counts["loaded_event_count"]),
        abs(expected - counts["ledger_event_count"]),
        abs(expected - counts["distinct_event_count"]),
        counts["duplicate_event_count"],
        counts["missing_event_count"],
        counts["unexpected_event_count"],
        counts["failed_mutation_count"],
    )
    passed = reconciliation.get("state") == "matched" and delta == 0
    return _check(
        "reconciliation",
        "canonical",
        "pass" if passed else "breach",
        delta,
        0,
        "events",
        "within_threshold" if passed else "count_mismatch",
    )


def _age_check(
    check_id: str,
    stage: str,
    value: object,
    threshold: float,
    now: datetime,
) -> dict[str, object]:
    if value is None:
        return _unobserved(check_id, stage, "measurement_missing")
    if not isinstance(value, str):
        raise ValueError(f"{check_id} timestamp must be a string")
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{check_id} timestamp is invalid") from error
    if observed_at.tzinfo is None:
        raise ValueError(f"{check_id} timestamp must be timezone-aware")
    age = (now - observed_at).total_seconds()
    if age < 0:
        return _unobserved(check_id, stage, "clock_untrusted")
    passed = age <= threshold
    return _check(
        check_id,
        stage,
        "pass" if passed else "breach",
        round(age, 3),
        threshold,
        "seconds",
        "within_threshold" if passed else "freshness_exceeded",
    )


def _maximum_check(
    check_id: str,
    stage: str,
    value: object,
    threshold: int | float,
    unit: str,
) -> dict[str, object]:
    if value is None:
        return _unobserved(check_id, stage, "measurement_missing")
    measured = _number(value, _telemetry_field(check_id))
    if measured < 0:
        raise ValueError(f"{_telemetry_field(check_id)} must be non-negative")
    passed = measured <= threshold
    return _check(
        check_id,
        stage,
        "pass" if passed else "breach",
        measured,
        threshold,
        unit,
        "within_threshold" if passed else "threshold_exceeded",
    )


def _check(
    check_id: str,
    stage: str,
    status: str,
    observed: int | float,
    threshold: int | float,
    unit: str,
    reason: str,
) -> dict[str, object]:
    return {
        "id": check_id,
        "stage": stage,
        "status": status,
        "observed": observed,
        "threshold": threshold,
        "unit": unit,
        "reason": reason,
    }


def _unobserved(check_id: str, stage: str, reason: str) -> dict[str, object]:
    return {
        "id": check_id,
        "stage": stage,
        "status": "unobserved",
        "observed": None,
        "threshold": None,
        "unit": None,
        "reason": reason,
    }


def _optional(value: Mapping[str, object], name: str) -> object:
    return value.get(name)


def _nested_optional(value: Mapping[str, object], *path: str) -> object:
    current: object = value
    for name in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(name)
    return current


def _number(value: object, name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _telemetry_field(check_id: str) -> str:
    return {
        "capture_errors": "capture_errors",
        "checkpoint_lag": "checkpoint_lag_sequences",
        "s3_requests": "s3_requests_24h",
        "snowpipe_queue": "snowpipe_pending_files",
        "delivery_latency_p95": "delivery_latency_p95_seconds",
        "delivery_latency_p99": "delivery_latency_p99_seconds",
        "snowflake_credits": "snowflake_credits_24h",
    }.get(check_id, check_id)
