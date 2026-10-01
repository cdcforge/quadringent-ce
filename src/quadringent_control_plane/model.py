from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping

from quadringent.site_config import current as _current_site


class ProjectionError(ValueError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class SourceDescriptor:
    id: str
    evidence_kind: Literal["live", "historical", "simulation"]
    environment: str
    origin: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ProjectionError("invalid_source_id", "Identifiant de source invalide")
        if (
            not isinstance(self.evidence_kind, str)
            or self.evidence_kind not in {"live", "historical", "simulation"}
        ):
            raise ProjectionError("invalid_evidence_kind", "Nature de preuve invalide")
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise ProjectionError("invalid_environment", "Environnement de source invalide")
        if not isinstance(self.origin, str) or not self.origin.strip():
            raise ProjectionError("invalid_origin", "Origine de source invalide")


@dataclass(frozen=True)
class StageProjection:
    id: Literal["source", "capture", "raw", "load", "destination"]
    status: Literal["healthy", "degraded", "incident", "unknown", "planned_stop", "awaiting_resume"]
    observed_at: str | None
    headline: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "observed_at": self.observed_at,
            "headline": self.headline,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class JournalCheckpoint:
    receiver: str
    sequence: int


@dataclass(frozen=True)
class DestinationTargetProof:
    kind: Literal["snowflake"]
    destination_id: str
    environment: str


@dataclass(frozen=True)
class DestinationActivationProof:
    state: str
    observed_at: datetime
    checks: Mapping[str, str]
    blocker_code: str | None


@dataclass(frozen=True)
class DestinationLoadProof:
    state: str
    observed_at: datetime | None
    checkpoint: JournalCheckpoint | None
    batch_count: int | None
    event_count: int | None
    failed_event_count: int | None
    incident_code: str | None


@dataclass(frozen=True)
class DestinationApplyProof:
    state: str
    observed_at: datetime | None
    apply_checkpoint: JournalCheckpoint | None
    failed_mutation_count: int | None
    incident_code: str | None


@dataclass(frozen=True)
class ReconciliationWindow:
    from_exclusive: JournalCheckpoint | None
    to_inclusive: JournalCheckpoint


@dataclass(frozen=True)
class DestinationReconciliationProof:
    state: str
    observed_at: datetime | None
    window: ReconciliationWindow | None
    captured_event_count: int | None
    loaded_event_count: int | None
    ledger_event_count: int | None
    distinct_event_count: int | None
    duplicate_event_count: int | None
    missing_event_count: int | None
    unexpected_event_count: int | None
    failed_mutation_count: int | None


@dataclass(frozen=True)
class DestinationProofInput:
    observed_at: datetime
    source_checkpoint: JournalCheckpoint
    target: DestinationTargetProof
    activation: DestinationActivationProof
    load: DestinationLoadProof
    destination: DestinationApplyProof
    reconciliation: DestinationReconciliationProof


@dataclass(frozen=True)
class LagBucketProjection:
    start_s: float
    end_s: float
    minimum: int | None
    maximum: int | None
    last: int | None
    samples: int
    unknown_samples: int
    coverage: Literal["complete", "gap"]
    kind: Literal["observed", "temporal_gap"]

    def to_dict(self) -> dict[str, object]:
        return {
            "start_s": self.start_s,
            "end_s": self.end_s,
            "min": self.minimum,
            "max": self.maximum,
            "last": self.last,
            "samples": self.samples,
            "unknown_samples": self.unknown_samples,
            "coverage": self.coverage,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class LagSeriesProjection:
    resolution_s: float
    sample_count: int
    unknown_sample_count: int
    buckets: tuple[LagBucketProjection, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "resolution_s": self.resolution_s,
            "sample_count": self.sample_count,
            "unknown_sample_count": self.unknown_sample_count,
            "buckets": [bucket.to_dict() for bucket in self.buckets],
        }


SloValue = int | float | str | tuple[str, ...] | None


def _slo_value_to_dict(value: SloValue) -> object:
    return list(value) if isinstance(value, tuple) else value


@dataclass(frozen=True)
class SloCheckProjection:
    id: str
    stage: str
    status: Literal["pass", "breach", "unobserved"]
    observed: SloValue
    threshold: SloValue
    unit: str | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "stage": self.stage,
            "status": self.status,
            "observed": _slo_value_to_dict(self.observed),
            "threshold": _slo_value_to_dict(self.threshold),
            "unit": self.unit,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SloAlertProjection:
    fingerprint: str
    check_id: str
    stage: str
    lifecycle_state: Literal["firing", "resolved"]
    signal_status: Literal["pass", "breach", "unobserved"]
    severity: Literal["none", "warning", "critical"]
    reason: str
    observed: SloValue
    threshold: SloValue
    unit: str | None
    first_fired_at: str
    firing_since: str
    last_observed_at: str
    resolved_at: str | None
    occurrence_count: int
    evaluation_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "check_id": self.check_id,
            "stage": self.stage,
            "lifecycle_state": self.lifecycle_state,
            "signal_status": self.signal_status,
            "severity": self.severity,
            "reason": self.reason,
            "observed": _slo_value_to_dict(self.observed),
            "threshold": _slo_value_to_dict(self.threshold),
            "unit": self.unit,
            "first_fired_at": self.first_fired_at,
            "firing_since": self.firing_since,
            "last_observed_at": self.last_observed_at,
            "resolved_at": self.resolved_at,
            "occurrence_count": self.occurrence_count,
            "evaluation_count": self.evaluation_count,
        }


@dataclass(frozen=True)
class ObservabilityProjection:
    status: Literal["pass", "breach", "unobserved", "unavailable"]
    quality: Mapping[str, str]
    observed_at: str | None
    reason: str
    checks: tuple[SloCheckProjection, ...]
    alerts: tuple[SloAlertProjection, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "quality": dict(self.quality),
            "observed_at": self.observed_at,
            "reason": self.reason,
            "checks": [check.to_dict() for check in self.checks],
            "alerts": [alert.to_dict() for alert in self.alerts],
        }


# Identité de flotte du site déclaré — résolue à l'accès via ``__getattr__``,
# jamais figée à une installation.
FLEET_ID: str


def __getattr__(name: str) -> object:
    if name == "FLEET_ID":
        return _current_site().fleet_id
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True)
class FleetCapabilityProjection:
    state: Literal["available", "unavailable"]
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state, "reason": self.reason}


@dataclass(frozen=True)
class FleetProjection:
    fleet_id: str
    payload: Mapping[str, object]
    summary: Mapping[str, object]
    capabilities: Mapping[str, FleetCapabilityProjection]

    def to_dict(self) -> dict[str, object]:
        return {
            **dict(self.payload),
            "fleet_id": self.fleet_id,
            "summary": dict(self.summary),
            "capabilities": {
                name: capability.to_dict() for name, capability in self.capabilities.items()
            },
        }


# Champs top-level additifs du contrat pipeline : toujours émis par to_dict,
# admis en option par les schémas fermés qui relisent une projection sérialisée.
PASS_THROUGH_FIELDS = (
    "position",
    "flux",
    "run",
    "lag_verdict",
    "lag_verdict_reason",
    "destination",
    "destination_reason",
    "resume",
)


@dataclass(frozen=True)
class PipelineProjection:
    id: str
    environment: str
    status: str
    quality: Mapping[str, str]
    summary: str
    observed_at: str
    stages: tuple[StageProjection, ...]
    lag_sequences: int | None
    lag_seconds: float | None
    lag_series: LagSeriesProjection | None
    counters: Mapping[str, int | float | None]
    incident: Mapping[str, object] | None
    observability: ObservabilityProjection
    window_delivery: Mapping[str, object] | None = None
    fleet: FleetProjection | None = None
    fleet_plan: Mapping[str, object] | None = None
    position: Mapping[str, object] | None = None
    flux: Mapping[str, object] | None = None
    run: Mapping[str, object] | None = None
    lag_verdict: str | None = None
    lag_verdict_reason: str | None = None
    destination: Mapping[str, object] | None = None
    destination_reason: str | None = None
    resume: Mapping[str, object] | None = None
    costs: Mapping[str, object] | None = None
    infrastructure_costs: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "environment": self.environment,
            "status": self.status,
            "quality": dict(self.quality),
            "summary": self.summary,
            "observed_at": self.observed_at,
            "stages": [stage.to_dict() for stage in self.stages],
            "lag_sequences": self.lag_sequences,
            "lag_seconds": self.lag_seconds,
            "lag_series": self.lag_series.to_dict() if self.lag_series is not None else None,
            "counters": dict(self.counters),
            "incident": dict(self.incident) if self.incident is not None else None,
            "observability": self.observability.to_dict(),
            "position": dict(self.position) if self.position is not None else None,
            "flux": dict(self.flux) if self.flux is not None else None,
            "run": dict(self.run) if self.run is not None else None,
            "lag_verdict": self.lag_verdict,
            "lag_verdict_reason": self.lag_verdict_reason,
            "destination": dict(self.destination) if self.destination is not None else None,
            "destination_reason": self.destination_reason,
            "resume": dict(self.resume) if self.resume is not None else None,
            **({"costs": dict(self.costs)} if self.costs is not None else {}),
            **({"infrastructure_costs": dict(self.infrastructure_costs)} if self.infrastructure_costs is not None else {}),
            **({"window_delivery": dict(self.window_delivery)} if self.window_delivery is not None else {}),
            **({"fleet": self.fleet.to_dict()} if self.fleet is not None else {}),
            **({"fleet_plan": dict(self.fleet_plan)} if self.fleet_plan is not None else {}),
        }
