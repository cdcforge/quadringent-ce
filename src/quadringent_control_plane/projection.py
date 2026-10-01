from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re
from typing import Mapping, Sequence

from quadringent.site_config import SiteConfig, current as _current_site
from quadringent.slo import REQUIRED_SLO_CHECK_IDS
from quadringent.observability_snapshot import ACCEPTED_OBSERVABILITY_SCHEMAS
from quadringent.slo_alerts import slo_report_digest, validate_alert_state

from . import model as _model
from .costs import project_costs
from .fleet import deserialize_fleet, serialize_fleet, summarize
from .model import (
    FleetCapabilityProjection,
    FleetProjection,
    DestinationActivationProof,
    DestinationApplyProof,
    DestinationLoadProof,
    DestinationProofInput,
    DestinationReconciliationProof,
    DestinationTargetProof,
    JournalCheckpoint,
    LagBucketProjection,
    LagSeriesProjection,
    ObservabilityProjection,
    PipelineProjection,
    ProjectionError,
    ReconciliationWindow,
    SourceDescriptor,
    SloAlertProjection,
    SloCheckProjection,
    StageProjection,
)


FORMAT_VERSION = "as400-console-v1"
FRESH_FOR = timedelta(minutes=5)
CLOCK_SKEW_TOLERANCE = timedelta(minutes=1)
MAX_PUBLIC_COUNT = 9_223_372_036_854_775_807


def _site() -> SiteConfig:
    return _current_site()
PUBLIC_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PUBLIC_SLO_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PUBLIC_SLO_VALUE_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PUBLIC_SLO_UNIT = re.compile(r"^[a-z][a-z0-9/]{0,31}$")
PUBLIC_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_CAPTURE_INCIDENT_TYPES = {
    "JdbcFailure": "capture_connection_failure",
    "SqlWindowTimeout": "capture_timeout",
    "SourceAuthenticationBlockedError": "capture_auth_blocked",
    "IbmiUserDisabledError": "capture_auth_blocked",
    "SourceConfigurationBlockedError": "capture_config_blocked",
    "ReceiverPlanningError": "capture_position_review",
}
ACTIVATION_STATES = {
    "not_configured",
    "validating",
    "active",
    "blocked",
    "disabled",
    "unknown",
}
ACTIVATION_CHECK_VALUES = {
    "configuration": {"valid", "missing", "invalid", "unknown"},
    "credential": {"available", "missing", "denied", "invalid", "unknown"},
    "connectivity": {"reachable", "unreachable", "unknown"},
    "authorization": {"allowed", "denied", "unknown"},
    "contract": {"compatible", "incompatible", "unknown"},
}
POSITIVE_ACTIVATION_CHECKS = {
    "configuration": "valid",
    "credential": "available",
    "connectivity": "reachable",
    "authorization": "allowed",
    "contract": "compatible",
}
ACTIVATION_BLOCKERS = {
    "destination_configuration_missing": ("configuration", "missing"),
    "destination_configuration_invalid": ("configuration", "invalid"),
    "destination_credential_missing": ("credential", "missing"),
    "destination_credential_denied": ("credential", "denied"),
    "destination_credential_invalid": ("credential", "invalid"),
    "destination_unreachable": ("connectivity", "unreachable"),
    "destination_authorization_denied": ("authorization", "denied"),
    "destination_contract_incompatible": ("contract", "incompatible"),
}
LOAD_STATES = {"not_started", "running", "succeeded", "failed", "unknown", "planned_stop"}
LOAD_INCIDENT_CODES = {
    "destination_load_failed",
    "destination_load_timeout",
    "destination_load_contract_invalid",
    "destination_load_checkpoint_conflict",
}
DESTINATION_STATES = {"not_started", "applying", "applied", "failed", "unknown", "planned_stop"}
DESTINATION_INCIDENT_CODES = {
    "destination_apply_failed",
    "destination_apply_timeout",
    "destination_apply_contract_invalid",
    "destination_apply_checkpoint_conflict",
}
RECONCILIATION_STATES = {"not_run", "running", "matched", "mismatch", "failed", "unknown"}
PUBLIC_COUNTERS = frozenset(
    {
        "polls",
        "events_published",
        "payload_bytes_published",
        "errors",
        "empty_scans",
        "idle_polls",
        "windows_published",
        "receiver_rotations",
        "run_duration_s",
        "cpu_ms_per_event",
        "mean_mcpu",
        "events_in_target",
        "duplicates_in_target",
    }
)
FLEET_CAPABILITY_IDS = ("refresh", "prepare", "start", "pause", "resume")
LAG_VERDICTS = {"STABLE", "BOUNDED", "CATCHING_UP", "DIVERGING"}
_SENSITIVE_MARKERS = ("host", "user", "password", "secret", "token", "credential")
_RUN_TAG = re.compile(r"^[A-Z0-9_]{1,64}$")


def project_console_document(
    document: Mapping[str, object], source: SourceDescriptor, now: datetime
) -> PipelineProjection:
    _require(document.get("format_version") == FORMAT_VERSION, "incompatible_format", "Format de snapshot incompatible")
    observed = _parse_timestamp(document.get("generated_at"))
    capture_observed = _parse_timestamp(document.get("capture_observed_at", document.get("generated_at")))
    _require(now.tzinfo is not None, "invalid_now", "Horodatage serveur invalide")
    observed_at = capture_observed.isoformat()
    freshness = _freshness(now.astimezone(timezone.utc) - capture_observed)
    document_freshness = _freshness(now.astimezone(timezone.utc) - observed)
    if document_freshness != "fresh":
        freshness = document_freshness
    flux = _mapping(document.get("flux"), "invalid_flux", "Identité du flux absente")
    pipeline_id = flux.get("id")
    _require(isinstance(pipeline_id, str) and bool(pipeline_id), "invalid_flux", "Identité du flux absente")
    run = _mapping(document.get("run"), "invalid_run", "État de capture absent")
    position = _mapping(document.get("position"), "invalid_position", "Position de capture absente")
    lag = _mapping(document.get("lag"), "invalid_lag", "Retard de capture absent")
    counters = _mapping(document.get("counters"), "invalid_counters", "Compteurs de capture absents")
    public_counters = _safe_counters(counters)

    source_stage = _source_stage(position, observed_at)
    capture_stage, incident = _capture_stage(run, observed_at)
    raw_stage = _raw_stage(counters, observed_at)
    lag_sequences, lag_state = _lag_state(lag)
    lag_series = _lag_series(lag.get("series"))
    lag_verdict, lag_verdict_reason = _lag_verdict_field(lag)
    destination_field, destination_reason = _destination_field(document.get("destination"))
    raw_destination_proof = document.get("destination_proof")
    if "destination_proof" not in document:
        stages = (
            source_stage,
            capture_stage,
            raw_stage,
            StageProjection("load", "unknown", None, "Chargement non observé", "Aucune preuve de chargement Snowflake n'est fournie par le snapshot v1"),
            StageProjection("destination", "unknown", None, "Destination non observée", "Aucune preuve d'application ou de visibilité Snowflake n'est fournie par le snapshot v1"),
        )
        status = _pipeline_status(
            freshness=freshness,
            stage_statuses=(source_stage.status, capture_stage.status, raw_stage.status),
            lag_state=lag_state,
            evidence_kind=source.evidence_kind,
        )
        coverage = "partial"
        summary = _summary(status, freshness, source.evidence_kind, incident)
    else:
        destination_proof = _parse_destination_proof(raw_destination_proof)
        load_stage, destination_stage, destination_incident, complete = _destination_stages(
            destination_proof,
            source=source,
            now=now.astimezone(timezone.utc),
            generated_at=observed,
            source_position=_existing_checkpoint(position.get("checkpoint")),
            source_status=source_stage.status,
        )
        stages = (source_stage, capture_stage, raw_stage, load_stage, destination_stage)
        if load_stage.status == "healthy" and destination_stage.status == "healthy":
            # Capture cannot query the target. Use only the validated destination
            # window, never unchecked counts from a stale or contradictory proof.
            public_counters["events_in_target"] = destination_proof.reconciliation.ledger_event_count
            public_counters["duplicates_in_target"] = destination_proof.reconciliation.duplicate_event_count
        if incident is None and destination_incident is not None:
            incident = {"code": destination_incident, "type": "destination"}
        status = _pipeline_status_e2e(
            freshness=freshness,
            stage_statuses=tuple(stage.status for stage in stages),
            lag_state=lag_state,
            lag_verdict=lag_verdict,
            evidence_kind=source.evidence_kind,
        )
        coverage = "complete" if complete else "partial"
        summary = _destination_summary(
            status, freshness, source.evidence_kind, incident
        )
    quality = {
        "coverage": coverage,
        "freshness": freshness,
        "evidence_kind": source.evidence_kind,
    }
    observability = _observability_projection(
        document.get("observability"), source=source, now=now
    )
    return PipelineProjection(
        id=source.id,
        environment=source.environment,
        status=status,
        quality=quality,
        summary=summary,
        observed_at=observed_at,
        stages=stages,
        lag_sequences=lag_sequences,
        lag_seconds=None,
        lag_series=lag_series,
        counters=public_counters,
        incident=incident,
        observability=observability,
        costs=project_costs(observability, source, now, site=_site()),
        window_delivery=_window_delivery(document.get('window_destination_proof'),flux,source,now),
        fleet=_fleet(document.get("fleet"), source),
        position=_position_field(position),
        flux=_flux_field(flux),
        run=_run_field(run),
        lag_verdict=lag_verdict,
        lag_verdict_reason=lag_verdict_reason,
        destination=destination_field,
        destination_reason=destination_reason,
    )


def _window_delivery(value, flux, source, now):
    """Separate historical-window evidence; never overwrite process state/counters."""
    if value is None:
        return None
    try:
        def require(condition):
            if not condition:
                raise ValueError('invalid window delivery evidence')
        require(isinstance(value,Mapping) and value.get('format_version')=='quadringent-window-destination-v1')
        site=_site()
        require(source.environment==site.environment and flux.get('journal')==site.journal_name
                and flux.get('objects')==[f'{site.source_schema}.{site.proof_table}'])
        require(value.get('storage_backend') in ('local','s3') and value.get('process_state')=='not_observed')
        for key in ('archive_run_id','window_id'):
            require(isinstance(value.get(key),str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',value[key]) is not None)
        window=value['window']
        intent=window['intent']
        require(window['format_version']=='quadringent-closed-window-v2' and window['window_id']==value['window_id'])
        require(intent.get('format_version')=='quadringent-window-intent-v2' and intent.get('window_id')==value['window_id'])
        stream_id=intent.get('stream_id')
        require(isinstance(stream_id,str) and 1<=len(stream_id)<=512
                and re.fullmatch(r'[a-z0-9][a-z0-9._/-]*',stream_id) is not None
                and all(part not in ('','.','..') for part in stream_id.split('/'))
                and stream_id==flux['id'])
        started,closed,sealed=(_parse_timestamp(v) for v in (intent['started_at'],window['closed_at'],window['sealed_at']))
        duration,grace=intent['duration_seconds'],intent['closure_grace_seconds']
        require(type(duration) is int and 600<=duration<=3600 and type(grace) is int and 0<=grace<=120)
        require(started+timedelta(seconds=duration)<=closed<=sealed<=started+timedelta(seconds=duration+grace))
        require(_existing_checkpoint(intent['previous']) is not None and _existing_checkpoint(window['end']) is not None)
        count=window['event_count']
        require(type(count) is int and 0<=count<=MAX_PUBLIC_COUNT)
        destination=value['destination']
        observed=_parse_timestamp(destination['observed_at'])
        require(sealed<=observed<=now and destination['event_count']==count and type(destination['event_count']) is int)
        state=destination['state']
        require(state in ('matched','not_tested'))
        if state=='matched':
            require(count>0 and isinstance(destination.get('event_ids_sha256'),str) and re.fullmatch(r'[a-f0-9]{64}',destination['event_ids_sha256']) is not None)
            metrics=destination['metrics']
            for key,expected in {'status':'PASS','database':site.destination_database,'schema':site.destination_schema,'stage':site.proof_stage,'raw_table':site.proof_raw_table,'canonical_table':site.proof_canonical_table}.items():
                require(metrics.get(key)==expected)
            for key in ('raw_rows_after_second','distinct_event_ids_after_second','canonical_rows_after_second'):
                require(type(metrics.get(key)) is int and metrics[key]==count)
        else:
            require(count==0 and destination.get('reason')=='no_events')
        evidence_kind='simulation' if value['storage_backend']=='local' else source.evidence_kind
        return {'state':state,'archive_run_id':value['archive_run_id'],'window_id':value['window_id'],
                'started_at':started.isoformat(),'closed_at':closed.isoformat(),'destination_observed_at':observed.isoformat(),
                'event_count':count,'quality':{'freshness':_freshness(now-closed),'evidence_kind':evidence_kind},
                'scope':'closed_window_only'}
    except (ValueError,TypeError,KeyError,AttributeError):
        return {'state':'invalid','reason':'window_delivery_invalid'}


def _fleet(value: object, source: SourceDescriptor) -> FleetProjection | None:
    """Restate a closed autonomous fleet. Never read a secret or a raw table."""
    if value is None:
        return None
    try:
        if not isinstance(value, Mapping):
            raise ValueError("invalid fleet")
        fleet = deserialize_fleet(value)
        serialized = serialize_fleet(fleet)
        summary = summarize(fleet).to_dict()
        live = source.evidence_kind == "live"
        capabilities = {
            name: FleetCapabilityProjection(
                "available" if live else "unavailable",
                None if live else "not_live",
            )
            for name in FLEET_CAPABILITY_IDS
        }
        return FleetProjection(
            fleet_id=_model.FLEET_ID,
            payload=serialized,
            summary=summary,
            capabilities=capabilities,
        )
    except (KeyError, TypeError, ValueError, ProjectionError):
        raise ProjectionError("invalid_fleet", "Flotte invalide") from None


def _observability_projection(
    value: object,
    *,
    source: SourceDescriptor,
    now: datetime,
) -> ObservabilityProjection:
    if value is None:
        return ObservabilityProjection(
            status="unavailable",
            quality={
                "coverage": "none",
                "freshness": "unavailable",
                "evidence_kind": source.evidence_kind,
            },
            observed_at=None,
            reason="observability_not_attached",
            checks=(),
            alerts=(),
        )
    try:
        envelope = _closed_observability_mapping(
            value,
            {
                "schema_version",
                "pipeline_id",
                "environment",
                "slo_report",
                "alert_state",
            },
        )
        if envelope.get("schema_version") not in ACCEPTED_OBSERVABILITY_SCHEMAS:
            raise ValueError("schema")
        if envelope.get("pipeline_id") != source.id:
            raise ValueError("pipeline")
        if envelope.get("environment") != source.environment:
            raise ValueError("environment")
        report = _closed_observability_mapping(
            envelope.get("slo_report"),
            {"schema_version", "observed_at", "status", "checks", "alerts"},
        )
        report_digest = slo_report_digest(report)
        state = validate_alert_state(
            _closed_observability_mapping(
                envelope.get("alert_state"),
                {
                    "schema_version",
                    "environment",
                    "pipeline_id",
                    "updated_at",
                    "source_report_digest",
                    "alerts",
                    "state_digest",
                },
            ),
            site=_site(),
        )
        if state.get("pipeline_id") != source.id:
            raise ValueError("state pipeline")
        if state.get("environment") != source.environment:
            raise ValueError("state environment")
        if state.get("source_report_digest") != report_digest:
            raise ValueError("digest")
        if state.get("updated_at") != report.get("observed_at"):
            raise ValueError("timestamp")
        observed = _observability_timestamp(report.get("observed_at"))
        if now.tzinfo is None:
            raise ValueError("now")
        checks = tuple(
            _slo_check_projection(check)
            for check in sorted(
                report["checks"], key=lambda item: str(item.get("id"))
            )
        )
        alerts = tuple(
            _slo_alert_projection(alert)
            for alert in sorted(
                state["alerts"], key=lambda item: str(item.get("check_id"))
            )
        )
        check_ids = {check.id for check in checks}
        active_unknown_check = any(
            alert.lifecycle_state == "firing" and alert.check_id not in check_ids
            for alert in alerts
        )
        coverage = (
            "complete"
            if check_ids == REQUIRED_SLO_CHECK_IDS and not active_unknown_check
            else "partial"
        )
        status = report.get("status")
        if status not in {"pass", "breach", "unobserved"}:
            raise ValueError("status")
        return ObservabilityProjection(
            status=status,
            quality={
                "coverage": coverage,
                "freshness": _freshness(now.astimezone(timezone.utc) - observed),
                "evidence_kind": source.evidence_kind,
            },
            observed_at=observed.isoformat(),
            reason={
                "pass": "within_policy",
                "breach": "threshold_breach",
                "unobserved": "measurement_gap",
            }[status],
            checks=checks,
            alerts=alerts,
        )
    except (KeyError, TypeError, ValueError, ProjectionError):
        raise ProjectionError(
            "invalid_observability", "Preuve d'observabilité invalide"
        ) from None


def _slo_check_projection(value: object) -> SloCheckProjection:
    check = _closed_observability_mapping(
        value, {"id", "stage", "status", "observed", "threshold", "unit", "reason"}
    )
    check_id = _slo_token(check.get("id"), "check id")
    stage = _slo_token(check.get("stage"), "check stage")
    status = check.get("status")
    if status not in {"pass", "breach", "unobserved"}:
        raise ValueError("check status")
    return SloCheckProjection(
        id=check_id,
        stage=stage,
        status=status,
        observed=_slo_value(check.get("observed")),
        threshold=_slo_value(check.get("threshold")),
        unit=_slo_unit(check.get("unit")),
        reason=_slo_token(check.get("reason"), "check reason"),
    )


def _slo_alert_projection(value: object) -> SloAlertProjection:
    alert = _closed_observability_mapping(
        value,
        {
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
        },
    )
    fingerprint = alert.get("fingerprint")
    if not isinstance(fingerprint, str) or not PUBLIC_SHA256.fullmatch(fingerprint):
        raise ValueError("alert fingerprint")
    lifecycle = alert.get("lifecycle_state")
    signal = alert.get("signal_status")
    severity = alert.get("severity")
    if lifecycle not in {"firing", "resolved"}:
        raise ValueError("alert lifecycle")
    if signal not in {"pass", "breach", "unobserved"}:
        raise ValueError("alert signal")
    if severity not in {"none", "warning", "critical"}:
        raise ValueError("alert severity")
    resolved = alert.get("resolved_at")
    return SloAlertProjection(
        fingerprint=fingerprint,
        check_id=_slo_token(alert.get("check_id"), "alert check"),
        stage=_slo_token(alert.get("stage"), "alert stage"),
        lifecycle_state=lifecycle,
        signal_status=signal,
        severity=severity,
        reason=_slo_token(alert.get("reason"), "alert reason"),
        observed=_slo_value(alert.get("observed")),
        threshold=_slo_value(alert.get("threshold")),
        unit=_slo_unit(alert.get("unit")),
        first_fired_at=_observability_timestamp(alert.get("first_fired_at")).isoformat(),
        firing_since=_observability_timestamp(alert.get("firing_since")).isoformat(),
        last_observed_at=_observability_timestamp(alert.get("last_observed_at")).isoformat(),
        resolved_at=None if resolved is None else _observability_timestamp(resolved).isoformat(),
        occurrence_count=_slo_count(alert.get("occurrence_count")),
        evaluation_count=_slo_count(alert.get("evaluation_count")),
    )


def _closed_observability_mapping(
    value: object, expected: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("observability shape")
    return value


def _observability_timestamp(value: object) -> datetime:
    try:
        return _parse_timestamp(value)
    except ProjectionError:
        raise ValueError("observability timestamp") from None


def _slo_token(value: object, field: str) -> str:
    if not isinstance(value, str) or not PUBLIC_SLO_TOKEN.fullmatch(value):
        raise ValueError(field)
    return value


def _slo_unit(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not PUBLIC_SLO_UNIT.fullmatch(value):
        raise ValueError("SLO unit")
    return value


def _slo_value(value: object) -> int | float | str | tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("SLO value")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise ValueError("SLO value")
        return value
    if isinstance(value, str):
        if not PUBLIC_SLO_VALUE_TOKEN.fullmatch(value):
            raise ValueError("SLO value")
        return value
    if isinstance(value, list) and len(value) <= 16:
        if any(
            not isinstance(item, str) or not PUBLIC_SLO_VALUE_TOKEN.fullmatch(item)
            for item in value
        ):
            raise ValueError("SLO value")
        return tuple(value)
    raise ValueError("SLO value")


def _slo_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("SLO count")
    return value


def build_overview(
    pipelines: Sequence[PipelineProjection],
    revision: int,
    generated_at: datetime,
    *,
    source_environments: Sequence[str] = (),
) -> dict[str, object]:
    _require(
        isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0,
        "invalid_revision",
        "Révision invalide",
    )
    _require(
        isinstance(generated_at, datetime) and generated_at.tzinfo is not None,
        "invalid_generated_at",
        "Horodatage de synthèse invalide",
    )
    ids = [pipeline.id for pipeline in pipelines]
    _require(len(ids) == len(set(ids)), "duplicate_pipeline_id", "Identifiants de pipeline dupliqués")
    environments = sorted(
        {
            environment.strip().lower()
            for environment in (
                *(pipeline.environment for pipeline in pipelines),
                *source_environments,
            )
            if isinstance(environment, str) and environment.strip()
        }
    )
    scope_kind = (
        "unavailable"
        if not environments
        else "single"
        if len(environments) == 1
        else "mixed"
    )
    return {
        "revision": revision,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "scope": {"kind": scope_kind, "environments": environments},
        "pipelines": [pipeline.to_dict() for pipeline in pipelines],
    }


def _source_stage(position: Mapping[str, object], observed_at: str) -> StageProjection:
    checkpoint = position.get("checkpoint")
    tail = position.get("source_tail")
    if not isinstance(checkpoint, Mapping) or not isinstance(tail, Mapping):
        return StageProjection("source", "unknown", observed_at, "Position source inconnue", "Le checkpoint ou le tail source est absent")
    checkpoint_receiver = checkpoint.get("receiver")
    tail_receiver = tail.get("receiver")
    checkpoint_sequence = checkpoint.get("sequence")
    tail_sequence = tail.get("sequence")
    if (
        not isinstance(checkpoint_receiver, str)
        or not checkpoint_receiver
        or not isinstance(tail_receiver, str)
        or not tail_receiver
        or not _valid_sequence(checkpoint_sequence)
        or not _valid_sequence(tail_sequence)
    ):
        return StageProjection("source", "unknown", observed_at, "Position source inconnue", "Le checkpoint ou le tail source est incompatible")
    if checkpoint_receiver != tail_receiver or checkpoint_sequence > tail_sequence:
        return StageProjection("source", "unknown", observed_at, "Position source ambiguë", "L'ordre checkpoint/tail ne peut pas être vérifié")
    if not _valid_receiver_window(position, tail_sequence):
        return StageProjection("source", "unknown", observed_at, "Position source ambiguë", "La fenêtre de receiver est incompatible")
    return StageProjection("source", "healthy", observed_at, "Source observée", "Checkpoint et tail source sont présents")


def _capture_stage(run: Mapping[str, object], observed_at: str) -> tuple[StageProjection, Mapping[str, object] | None]:
    state = run.get("state")
    if state == "STOPPED_FAIL_CLOSED":
        last_error = run.get("last_error")
        raw_type = (
            last_error.get("type")
            if isinstance(last_error, Mapping) and isinstance(last_error.get("type"), str)
            else None
        )
        return (
            StageProjection("capture", "incident", observed_at, "Capture arrêtée en sécurité", "La capture s'est arrêtée selon la politique fail-closed"),
            {
                "code": "capture_stopped_fail_closed",
                "type": SAFE_CAPTURE_INCIDENT_TYPES.get(raw_type, "capture_stopped"),
            },
        )
    if state == "STOPPED_AUTH_BLOCKED":
        return (
            StageProjection("capture", "incident", observed_at, "Authentification source bloquée", "La source a refusé la connexion ; aucun nouvel essai n'est tenté avant intervention"),
            {"code": "capture_stopped_fail_closed", "type": "capture_auth_blocked"},
        )
    if state == "PAUSED_SOURCE":
        return StageProjection("capture", "planned_stop", observed_at, "Source en pause", "La source ne répond pas ; aucune tentative avant l'échéance, reprise automatique"), None
    if state == "STOPPED_PROOF_CHAIN":
        return StageProjection("capture", "planned_stop", observed_at, "Fenêtres de capture clôturées", "La clôture de capture ne prouve pas la livraison Snowflake"), None
    if state == "STOPPED_BUDGET":
        return StageProjection("capture", "planned_stop", observed_at, "Capture arrêtée par budget", "L'arrêt observé est déclaré par le worker"), None
    if state == "RUNNING":
        return StageProjection("capture", "healthy", observed_at, "Capture active", "Le worker déclare un run en cours"), None
    return StageProjection("capture", "unknown", observed_at, "État de capture inconnu", "Le worker ne fournit pas un état de run compatible"), None


def _raw_stage(counters: Mapping[str, object], observed_at: str) -> StageProjection:
    events = _known_number(counters.get("events_published"), integer=True)
    if events is None:
        return StageProjection("raw", "unknown", observed_at, "Raw non observé", "Le nombre d'événements publiés est inconnu")
    return StageProjection("raw", "healthy", observed_at, "Raw publié", "Le worker confirme une publication raw")


def _parse_destination_proof(value: object) -> DestinationProofInput:
    proof = _closed_destination_mapping(
        value,
        {
            "schema_version",
            "observed_at",
            "source_checkpoint",
            "target",
            "activation",
            "load",
            "destination",
            "reconciliation",
        },
    )
    _destination_require(proof.get("schema_version") == "destination-proof-v1")
    return DestinationProofInput(
        observed_at=_destination_timestamp(proof.get("observed_at")),
        source_checkpoint=_destination_checkpoint(proof.get("source_checkpoint")),
        target=_parse_destination_target(proof.get("target")),
        activation=_parse_destination_activation(proof.get("activation")),
        load=_parse_destination_load(proof.get("load")),
        destination=_parse_destination_apply(proof.get("destination")),
        reconciliation=_parse_destination_reconciliation(proof.get("reconciliation")),
    )


def _parse_destination_target(value: object) -> DestinationTargetProof:
    target = _closed_destination_mapping(
        value, {"kind", "destination_id", "environment"}
    )
    kind = target.get("kind")
    destination_id = target.get("destination_id")
    environment = target.get("environment")
    _destination_require(kind == "snowflake")
    _destination_require(
        isinstance(destination_id, str) and bool(PUBLIC_IDENTIFIER.fullmatch(destination_id))
    )
    _destination_require(
        isinstance(environment, str) and bool(PUBLIC_IDENTIFIER.fullmatch(environment))
    )
    return DestinationTargetProof("snowflake", destination_id, environment)


def _parse_destination_activation(value: object) -> DestinationActivationProof:
    activation = _closed_destination_mapping(
        value, {"state", "observed_at", "checks", "blocker_code"}
    )
    state = activation.get("state")
    _destination_require(isinstance(state, str) and state in ACTIVATION_STATES)
    checks = _closed_destination_mapping(
        activation.get("checks"), set(ACTIVATION_CHECK_VALUES)
    )
    parsed_checks: dict[str, str] = {}
    for name, allowed in ACTIVATION_CHECK_VALUES.items():
        check = checks.get(name)
        _destination_require(isinstance(check, str) and check in allowed)
        parsed_checks[name] = check
    blocker_code = activation.get("blocker_code")
    _destination_require(
        blocker_code is None
        or (isinstance(blocker_code, str) and blocker_code in ACTIVATION_BLOCKERS)
    )
    return DestinationActivationProof(
        state=state,
        observed_at=_destination_timestamp(activation.get("observed_at")),
        checks=parsed_checks,
        blocker_code=blocker_code,
    )


def _parse_destination_load(value: object) -> DestinationLoadProof:
    load = _closed_destination_mapping(
        value,
        {
            "state",
            "observed_at",
            "checkpoint",
            "batch_count",
            "event_count",
            "failed_event_count",
            "incident_code",
        },
        required={"state", "incident_code"},
    )
    state = load.get("state")
    _destination_require(isinstance(state, str) and state in LOAD_STATES)
    observed_at = _optional_destination_timestamp(load, "observed_at")
    checkpoint = _optional_destination_checkpoint(load, "checkpoint")
    batch_count = _optional_destination_count(load, "batch_count")
    event_count = _optional_destination_count(load, "event_count")
    failed_event_count = _optional_destination_count(load, "failed_event_count")
    incident_code = load.get("incident_code")
    _destination_require(
        incident_code is None
        or (isinstance(incident_code, str) and incident_code in LOAD_INCIDENT_CODES)
    )
    if state in {"running", "succeeded", "planned_stop"}:
        _destination_require(observed_at is not None and checkpoint is not None)
    if state == "succeeded":
        _destination_require(event_count is not None and failed_event_count is not None)
    if state == "failed":
        _destination_require(
            observed_at is not None
            and failed_event_count is not None
            and incident_code is not None
        )
    else:
        _destination_require(incident_code is None)
    return DestinationLoadProof(
        state=state,
        observed_at=observed_at,
        checkpoint=checkpoint,
        batch_count=batch_count,
        event_count=event_count,
        failed_event_count=failed_event_count,
        incident_code=incident_code,
    )


def _parse_destination_apply(value: object) -> DestinationApplyProof:
    destination = _closed_destination_mapping(
        value,
        {
            "state",
            "observed_at",
            "apply_checkpoint",
            "failed_mutation_count",
            "incident_code",
        },
        required={"state", "incident_code"},
    )
    state = destination.get("state")
    _destination_require(isinstance(state, str) and state in DESTINATION_STATES)
    observed_at = _optional_destination_timestamp(destination, "observed_at")
    checkpoint = _optional_destination_checkpoint(destination, "apply_checkpoint")
    failed_mutation_count = _optional_destination_count(
        destination, "failed_mutation_count"
    )
    incident_code = destination.get("incident_code")
    _destination_require(
        incident_code is None
        or (
            isinstance(incident_code, str)
            and incident_code in DESTINATION_INCIDENT_CODES
        )
    )
    if state in {"applying", "applied", "planned_stop"}:
        _destination_require(observed_at is not None and checkpoint is not None)
    if state == "applied":
        _destination_require(failed_mutation_count is not None)
    if state == "failed":
        _destination_require(
            observed_at is not None
            and failed_mutation_count is not None
            and incident_code is not None
        )
    else:
        _destination_require(incident_code is None)
    return DestinationApplyProof(
        state=state,
        observed_at=observed_at,
        apply_checkpoint=checkpoint,
        failed_mutation_count=failed_mutation_count,
        incident_code=incident_code,
    )


def _parse_destination_reconciliation(
    value: object,
) -> DestinationReconciliationProof:
    counter_names = {
        "captured_event_count",
        "loaded_event_count",
        "ledger_event_count",
        "distinct_event_count",
        "duplicate_event_count",
        "missing_event_count",
        "unexpected_event_count",
        "failed_mutation_count",
    }
    reconciliation = _closed_destination_mapping(
        value,
        {"state", "observed_at", "window", *counter_names},
        required={"state"},
    )
    state = reconciliation.get("state")
    _destination_require(
        isinstance(state, str) and state in RECONCILIATION_STATES
    )
    observed_at = _optional_destination_timestamp(reconciliation, "observed_at")
    window = (
        _parse_reconciliation_window(reconciliation.get("window"))
        if "window" in reconciliation
        else None
    )
    counters = {
        name: _optional_destination_count(reconciliation, name)
        for name in counter_names
    }
    if state in {"running", "matched", "mismatch", "failed"}:
        _destination_require(observed_at is not None)
    if state in {"matched", "mismatch"}:
        _destination_require(
            window is not None and all(value is not None for value in counters.values())
        )
    return DestinationReconciliationProof(
        state=state,
        observed_at=observed_at,
        window=window,
        captured_event_count=counters["captured_event_count"],
        loaded_event_count=counters["loaded_event_count"],
        ledger_event_count=counters["ledger_event_count"],
        distinct_event_count=counters["distinct_event_count"],
        duplicate_event_count=counters["duplicate_event_count"],
        missing_event_count=counters["missing_event_count"],
        unexpected_event_count=counters["unexpected_event_count"],
        failed_mutation_count=counters["failed_mutation_count"],
    )


def _parse_reconciliation_window(value: object) -> ReconciliationWindow:
    window = _closed_destination_mapping(
        value, {"from_exclusive", "to_inclusive"}
    )
    raw_from = window.get("from_exclusive")
    return ReconciliationWindow(
        from_exclusive=(
            None if raw_from is None else _destination_checkpoint(raw_from)
        ),
        to_inclusive=_destination_checkpoint(window.get("to_inclusive")),
    )


def _closed_destination_mapping(
    value: object,
    allowed: set[str],
    *,
    required: set[str] | None = None,
) -> Mapping[str, object]:
    _destination_require(isinstance(value, Mapping))
    mapping = value
    keys = set(mapping)
    _destination_require(all(isinstance(key, str) for key in keys))
    _destination_require(keys <= allowed)
    _destination_require((required if required is not None else allowed) <= keys)
    return mapping


def _destination_timestamp(value: object) -> datetime:
    _destination_require(isinstance(value, str))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ProjectionError(
            "invalid_destination_proof", "Preuve de destination invalide"
        ) from None
    _destination_require(parsed.tzinfo is not None)
    return parsed.astimezone(timezone.utc)


def _optional_destination_timestamp(
    mapping: Mapping[str, object], name: str
) -> datetime | None:
    return _destination_timestamp(mapping[name]) if name in mapping else None


def _destination_checkpoint(value: object) -> JournalCheckpoint:
    checkpoint = _closed_destination_mapping(value, {"receiver", "sequence"})
    receiver = checkpoint.get("receiver")
    sequence = checkpoint.get("sequence")
    _destination_require(
        isinstance(receiver, str)
        and 1 <= len(receiver) <= 128
        and not any(character.isspace() for character in receiver)
    )
    _destination_require(_valid_sequence(sequence) and sequence <= MAX_PUBLIC_COUNT)
    return JournalCheckpoint(receiver, sequence)


def _optional_destination_checkpoint(
    mapping: Mapping[str, object], name: str
) -> JournalCheckpoint | None:
    return _destination_checkpoint(mapping[name]) if name in mapping else None


def _optional_destination_count(
    mapping: Mapping[str, object], name: str
) -> int | None:
    if name not in mapping:
        return None
    value = mapping[name]
    _destination_require(
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_PUBLIC_COUNT
    )
    return value


def _destination_require(condition: bool) -> None:
    if not condition:
        raise ProjectionError(
            "invalid_destination_proof", "Preuve de destination invalide"
        )


def _existing_checkpoint(value: object) -> JournalCheckpoint | None:
    if not isinstance(value, Mapping):
        return None
    receiver = value.get("receiver")
    sequence = value.get("sequence")
    if (
        not isinstance(receiver, str)
        or not receiver
        or not _valid_sequence(sequence)
    ):
        return None
    return JournalCheckpoint(receiver, sequence)


def _destination_stages(
    proof: DestinationProofInput,
    *,
    source: SourceDescriptor,
    now: datetime,
    generated_at: datetime,
    source_position: JournalCheckpoint | None,
    source_status: str,
) -> tuple[StageProjection, StageProjection, str | None, bool]:
    complete = _destination_coverage_complete(proof, source_position)

    def both_incident(code: str) -> tuple[StageProjection, StageProjection, str, bool]:
        return (
            _load_projection("incident", proof.load),
            _destination_projection("incident", proof.destination),
            code,
            complete,
        )

    if proof.target.environment.lower() != source.environment.strip().lower():
        return both_incident("destination_environment_mismatch")
    if not _activation_consistent(proof.activation):
        return both_incident("destination_activation_inconsistent")
    if proof.activation.state == "blocked":
        return both_incident(
            proof.activation.blocker_code or "destination_activation_inconsistent"
        )
    if source_position is None:
        return (
            _load_projection("unknown", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if proof.source_checkpoint != source_position:
        return both_incident("destination_source_checkpoint_mismatch")
    if proof.load.state == "failed":
        return both_incident(proof.load.incident_code or "destination_load_failed")
    if proof.destination.state == "failed":
        return both_incident(
            proof.destination.incident_code or "destination_apply_failed"
        )
    reconciliation_incident = _reconciliation_incident(proof.reconciliation)
    if reconciliation_incident is not None:
        return both_incident(reconciliation_incident)

    load_relation = _checkpoint_relation(
        proof.load.checkpoint, proof.source_checkpoint
    )
    if load_relation == "ahead":
        return both_incident("destination_load_ahead_of_source")
    apply_relation = _checkpoint_relation(
        proof.destination.apply_checkpoint, proof.load.checkpoint
    )
    if apply_relation == "ahead":
        return both_incident("destination_apply_ahead_of_load")
    if (
        proof.reconciliation.state == "matched"
        and proof.load.event_count != proof.reconciliation.loaded_event_count
    ):
        return both_incident("destination_reconciliation_inconsistent")
    if (
        proof.reconciliation.state == "matched"
        and proof.destination.failed_mutation_count
        != proof.reconciliation.failed_mutation_count
    ):
        return both_incident("destination_reconciliation_inconsistent")
    if proof.load.state == "succeeded" and proof.load.failed_event_count != 0:
        return both_incident("destination_load_failed")
    if (
        proof.destination.state == "applied"
        and proof.destination.failed_mutation_count != 0
    ):
        return both_incident("destination_apply_failed")

    if source_status != "healthy":
        return (
            _load_projection("unknown", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if not _destination_proof_is_fresh(proof, generated_at, now):
        return (
            _load_projection("unknown", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if proof.activation.state != "active":
        return (
            _load_projection("unknown", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if load_relation == "unknown" or apply_relation == "unknown":
        return (
            _load_projection("unknown", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if proof.load.state in {"not_started", "unknown"}:
        return (
            _load_projection("unknown", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if proof.destination.state in {"not_started", "unknown"}:
        return (
            _load_projection("degraded", proof.load),
            _destination_projection("unknown", proof.destination),
            None,
            complete,
        )
    if proof.load.state == "planned_stop" or proof.destination.state == "planned_stop":
        return (
            _load_projection("planned_stop", proof.load),
            _destination_projection("planned_stop", proof.destination),
            None,
            complete,
        )
    if (
        proof.load.state == "running"
        or proof.destination.state == "applying"
        or proof.reconciliation.state in {"not_run", "running", "unknown"}
        or load_relation == "behind"
        or apply_relation == "behind"
        or not complete
    ):
        return (
            _load_projection("degraded", proof.load),
            _destination_projection("degraded", proof.destination),
            None,
            complete,
        )
    if (
        proof.load.state == "succeeded"
        and proof.destination.state == "applied"
        and proof.reconciliation.state == "matched"
        and complete
    ):
        return (
            _load_projection("healthy", proof.load),
            _destination_projection("healthy", proof.destination),
            None,
            complete,
        )
    return (
        _load_projection("unknown", proof.load),
        _destination_projection("unknown", proof.destination),
        None,
        complete,
    )


def _load_projection(status: str, proof: DestinationLoadProof) -> StageProjection:
    messages = {
        "healthy": ("Chargement vérifié", "Le checkpoint chargé est réconcilié avec la source"),
        "degraded": ("Chargement partiel", "Le chargement est observé mais ne couvre pas encore la source"),
        "incident": ("Incident de chargement", "Le chargement a produit une preuve contradictoire ou un échec"),
        "planned_stop": ("Chargement arrêté comme prévu", "L'arrêt de chargement est déclaré par l'adapter"),
        "unknown": ("Chargement non établi", "La preuve de chargement est absente, périmée ou non comparable"),
    }
    headline, detail = messages[status]
    return StageProjection(
        "load",
        status,
        proof.observed_at.isoformat() if proof.observed_at is not None else None,
        headline,
        detail,
    )


def _destination_projection(
    status: str, proof: DestinationApplyProof
) -> StageProjection:
    messages = {
        "healthy": ("Destination vérifiée", "L'application Snowflake est réconciliée avec la source"),
        "degraded": ("Destination partielle", "L'application est observée mais ne couvre pas encore le chargement"),
        "incident": ("Incident de destination", "L'application a produit une preuve contradictoire ou un échec"),
        "planned_stop": ("Destination arrêtée comme prévu", "L'arrêt d'application est déclaré par l'adapter"),
        "unknown": ("Destination non établie", "La preuve d'application est absente, périmée ou non comparable"),
    }
    headline, detail = messages[status]
    return StageProjection(
        "destination",
        status,
        proof.observed_at.isoformat() if proof.observed_at is not None else None,
        headline,
        detail,
    )


def _activation_consistent(activation: DestinationActivationProof) -> bool:
    if activation.state == "active":
        return (
            activation.blocker_code is None
            and all(
                activation.checks.get(name) == expected
                for name, expected in POSITIVE_ACTIVATION_CHECKS.items()
            )
        )
    if activation.state == "blocked":
        expected = (
            ACTIVATION_BLOCKERS.get(activation.blocker_code)
            if activation.blocker_code is not None
            else None
        )
        return (
            expected is not None
            and activation.checks.get(expected[0]) == expected[1]
        )
    if activation.blocker_code is not None:
        return False
    if activation.state == "not_configured":
        return activation.checks.get("configuration") == "missing"
    return True


def _reconciliation_incident(
    reconciliation: DestinationReconciliationProof,
) -> str | None:
    if reconciliation.state == "mismatch":
        return "destination_reconciliation_mismatch"
    if reconciliation.state == "failed":
        return "destination_reconciliation_failed"
    if reconciliation.state == "matched" and not _reconciliation_consistent(
        reconciliation
    ):
        return "destination_reconciliation_inconsistent"
    return None


def _reconciliation_consistent(
    reconciliation: DestinationReconciliationProof,
) -> bool:
    window = reconciliation.window
    if window is None:
        return False
    if (
        window.from_exclusive is not None
        and (
            window.from_exclusive.receiver != window.to_inclusive.receiver
            or window.from_exclusive.sequence > window.to_inclusive.sequence
        )
    ):
        return False
    counts = (
        reconciliation.captured_event_count,
        reconciliation.loaded_event_count,
        reconciliation.ledger_event_count,
        reconciliation.distinct_event_count,
    )
    return (
        None not in counts
        and len(set(counts)) == 1
        and reconciliation.duplicate_event_count == 0
        and reconciliation.missing_event_count == 0
        and reconciliation.unexpected_event_count == 0
        and reconciliation.failed_mutation_count == 0
    )


def _destination_coverage_complete(
    proof: DestinationProofInput, source_position: JournalCheckpoint | None
) -> bool:
    reconciliation = proof.reconciliation
    return (
        source_position is not None
        and proof.source_checkpoint == source_position
        and proof.load.checkpoint == proof.source_checkpoint
        and proof.destination.apply_checkpoint == proof.source_checkpoint
        and reconciliation.state == "matched"
        and _reconciliation_consistent(reconciliation)
        and reconciliation.window is not None
        and reconciliation.window.to_inclusive == proof.source_checkpoint
    )


def _checkpoint_relation(
    downstream: JournalCheckpoint | None, upstream: JournalCheckpoint | None
) -> str:
    if downstream is None or upstream is None or downstream.receiver != upstream.receiver:
        return "unknown"
    if downstream.sequence == upstream.sequence:
        return "equal"
    return "behind" if downstream.sequence < upstream.sequence else "ahead"


def _destination_proof_is_fresh(
    proof: DestinationProofInput, generated_at: datetime, now: datetime
) -> bool:
    timestamps = [
        proof.observed_at,
        proof.activation.observed_at,
        proof.load.observed_at,
        proof.destination.observed_at,
        proof.reconciliation.observed_at,
    ]
    present = [timestamp for timestamp in timestamps if timestamp is not None]
    if proof.observed_at > generated_at + CLOCK_SKEW_TOLERANCE:
        return False
    if any(timestamp > proof.observed_at + CLOCK_SKEW_TOLERANCE for timestamp in present):
        return False
    return all(_freshness(now - timestamp) == "fresh" for timestamp in present)


def _pipeline_status_e2e(
    *,
    freshness: str,
    stage_statuses: Sequence[str],
    lag_state: str,
    lag_verdict: str | None,
    evidence_kind: str,
) -> str:
    if "incident" in stage_statuses or lag_state == "incident":
        return "incident"
    if freshness != "fresh" or "unknown" in stage_statuses or lag_state == "unknown":
        return "unknown"
    if (
        "degraded" in stage_statuses
        or lag_verdict == "CATCHING_UP"
        or evidence_kind != "live"
    ):
        return "degraded"
    if "planned_stop" in stage_statuses:
        return "planned_stop"
    return "healthy"


def _incident_summary_locus(incident: Mapping[str, object]) -> str:
    return "Incident de destination à traiter" if incident.get("type") == "destination" else "Incident de capture à traiter"


def _destination_summary(
    status: str,
    freshness: str,
    evidence_kind: str,
    incident: Mapping[str, object] | None,
) -> str:
    if status == "incident":
        return _incident_summary_locus(incident) if incident else "Retard de capture divergent"
    if freshness == "clock_untrusted":
        return "Horloge de capture non fiable"
    if freshness != "fresh":
        return "Snapshot de capture périmé"
    if status == "unknown":
        return "Preuve de livraison Snowflake incomplète ou périmée"
    if status == "planned_stop":
        return "Capture arrêtée comme prévu"
    if evidence_kind == "simulation":
        return "Simulation : preuve E2E non live"
    if evidence_kind == "historical":
        return "Preuve historique E2E : état courant non prouvé"
    if status == "healthy":
        return "Livraison Snowflake vérifiée de bout en bout"
    return "Livraison Snowflake partielle ou en rattrapage"


def _pipeline_status(*, freshness: str, stage_statuses: Sequence[str], lag_state: str, evidence_kind: str) -> str:
    if "incident" in stage_statuses or lag_state == "incident":
        return "incident"
    if freshness != "fresh" or "unknown" in stage_statuses or lag_state == "unknown":
        return "unknown"
    if "degraded" in stage_statuses or evidence_kind != "live":
        return "degraded"
    if "planned_stop" in stage_statuses:
        return "planned_stop"
    return "degraded"


def _freshness(age: timedelta) -> str:
    if age < -CLOCK_SKEW_TOLERANCE:
        return "clock_untrusted"
    if age <= FRESH_FOR:
        return "fresh"
    return "stale"


def _summary(status: str, freshness: str, evidence_kind: str, incident: Mapping[str, object] | None) -> str:
    if status == "incident":
        return _incident_summary_locus(incident) if incident else "Retard de capture divergent"
    if freshness == "clock_untrusted":
        return "Horloge de capture non fiable"
    if freshness != "fresh":
        return "Snapshot de capture périmé"
    if status == "unknown":
        return "État opérateur incomplet ou incompatible"
    if status == "planned_stop":
        return "Capture arrêtée comme prévu"
    if evidence_kind == "simulation":
        return "Simulation : livraison Snowflake non prouvée"
    if evidence_kind == "historical":
        return "Preuve historique : livraison Snowflake non prouvée"
    return "Capture saine, livraison Snowflake non prouvée"


def _parse_timestamp(value: object) -> datetime:
    _require(isinstance(value, str), "invalid_generated_at", "Horodatage de snapshot invalide")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProjectionError("invalid_generated_at", "Horodatage de snapshot invalide") from error
    _require(parsed.tzinfo is not None, "invalid_generated_at", "Horodatage de snapshot invalide")
    return parsed.astimezone(timezone.utc)


def _mapping(value: object, code: str, message: str) -> Mapping[str, object]:
    _require(isinstance(value, Mapping), code, message)
    return value


def _known_number(value: object, *, integer: bool = False) -> int | float | None:
    if not isinstance(value, Mapping):
        return None
    number = value.get("value")
    if number is None:
        return None
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
        or (integer and not isinstance(number, int))
    ):
        raise ProjectionError("invalid_number", "Valeur numérique invalide")
    return number


def _known_text(value: object) -> str | None:
    return value.get("value") if isinstance(value, Mapping) and isinstance(value.get("value"), str) else None


def _safe_counters(counters: Mapping[str, object]) -> dict[str, int | float | None]:
    return {
        name: _known_number(counters[name])
        for name in sorted(PUBLIC_COUNTERS)
        if name in counters
    }


def _lag_state(lag: Mapping[str, object]) -> tuple[int | None, str]:
    current = _known_number(lag.get("current"), integer=True)
    verdict = _known_text(lag.get("verdict"))
    if current is None or verdict not in LAG_VERDICTS:
        return None, "unknown"
    if current < 0 or (verdict == "DIVERGING" and current == 0):
        return None, "unknown"
    return current, "incident" if verdict == "DIVERGING" else "known"


def _lag_series(value: object) -> LagSeriesProjection | None:
    if value is None:
        return None
    series = _mapping(
        value,
        "invalid_lag_series",
        "Série de retard invalide",
    )
    resolution_s = _series_number(series.get("resolution_s"), integer=False)
    sample_count = _series_number(series.get("sample_count"), integer=True)
    unknown_sample_count = _series_number(
        series.get("unknown_sample_count"), integer=True
    )
    _require(resolution_s > 0, "invalid_lag_series", "Série de retard invalide")
    _require(
        unknown_sample_count <= sample_count,
        "invalid_lag_series",
        "Série de retard invalide",
    )
    raw_buckets = series.get("buckets")
    _require(
        isinstance(raw_buckets, list),
        "invalid_lag_series",
        "Série de retard invalide",
    )
    buckets: list[LagBucketProjection] = []
    previous_end: float | None = None
    previous_start: float | None = None
    for raw_bucket in raw_buckets:
        bucket = _mapping(
            raw_bucket,
            "invalid_lag_series",
            "Série de retard invalide",
        )
        start_s = _series_number(bucket.get("start_s"), integer=False)
        end_s = _series_number(bucket.get("end_s"), integer=False)
        samples = _series_number(bucket.get("samples"), integer=True)
        unknown_samples = _series_number(
            bucket.get("unknown_samples"), integer=True
        )
        _require(
            end_s >= start_s
            and samples > 0
            and unknown_samples <= samples
            and (previous_end is None or start_s >= previous_end),
            "invalid_lag_series",
            "Série de retard invalide",
        )
        minimum = _series_optional_integer(bucket.get("min"))
        maximum = _series_optional_integer(bucket.get("max"))
        last = _series_optional_integer(bucket.get("last"))
        known_samples = samples - unknown_samples
        if known_samples == 0:
            _require(
                minimum is None and maximum is None and last is None,
                "invalid_lag_series",
                "Série de retard invalide",
            )
        else:
            _require(
                minimum is not None
                and maximum is not None
                and last is not None
                and minimum <= last <= maximum,
                "invalid_lag_series",
                "Série de retard invalide",
            )
        coverage = "gap" if unknown_samples else "complete"
        # Un trou significatif = au moins une cellule nominale sautée. Son
        # intervalle honnête est [fin du seau précédent, début du suivant) :
        # un seau fusionné peut déborder sa cellule nominale (end_s réel),
        # partir de start+resolution recouvrirait des échantillons observés.
        if (
            previous_end is not None
            and previous_start is not None
            and start_s > previous_start + resolution_s
            and not math.isclose(start_s, previous_start + resolution_s)
            and start_s > previous_end
        ):
            buckets.append(
                LagBucketProjection(
                    start_s=previous_end,
                    end_s=start_s,
                    minimum=None,
                    maximum=None,
                    last=None,
                    samples=0,
                    unknown_samples=0,
                    coverage="gap",
                    kind="temporal_gap",
                )
            )
        buckets.append(
            LagBucketProjection(
                start_s=start_s,
                end_s=end_s,
                minimum=minimum,
                maximum=maximum,
                last=last if coverage == "complete" else None,
                samples=samples,
                unknown_samples=unknown_samples,
                coverage=coverage,
                kind="observed",
            )
        )
        previous_end = end_s
        previous_start = start_s
    _require(
        sum(bucket.samples for bucket in buckets) == sample_count
        and sum(bucket.unknown_samples for bucket in buckets)
        == unknown_sample_count,
        "invalid_lag_series",
        "Série de retard invalide",
    )
    return LagSeriesProjection(
        resolution_s=resolution_s,
        sample_count=sample_count,
        unknown_sample_count=unknown_sample_count,
        buckets=tuple(buckets),
    )


def _series_number(value: object, *, integer: bool) -> int | float:
    _require(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
        and (not integer or isinstance(value, int)),
        "invalid_lag_series",
        "Série de retard invalide",
    )
    return value


def _series_optional_integer(value: object) -> int | None:
    if value is None:
        return None
    result = _series_number(value, integer=True)
    return result if isinstance(result, int) else None


def _valid_sequence(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _public_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    lowered = value.lower()
    if "://" in value or any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return None
    return value


def _checkpoint_field(value: object) -> dict[str, object] | None:
    checkpoint = _existing_checkpoint(value)
    if checkpoint is None:
        return None
    return {"receiver": checkpoint.receiver, "sequence": checkpoint.sequence}


def _count_field(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_PUBLIC_COUNT
    ):
        return None
    return value


def _duration_field(value: object) -> int | float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return value


def _timestamp_field(value: object) -> str | None:
    try:
        return _parse_timestamp(value).isoformat()
    except ProjectionError:
        return None


def _position_field(position: Mapping[str, object]) -> dict[str, object]:
    first = position.get("receiver_first_sequence")
    last = position.get("receiver_last_sequence")
    return {
        "checkpoint": _checkpoint_field(position.get("checkpoint")),
        "source_tail": _checkpoint_field(position.get("source_tail")),
        "receiver_first_sequence": first if _valid_sequence(first) else None,
        "receiver_last_sequence": last if _valid_sequence(last) else None,
    }


def _objects_field(value: object) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 128:
        return None
    items = [_public_text(item, 256) for item in value]
    if None in items:
        return None
    return items


def _flux_field(flux: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": _public_text(flux.get("id"), 512),
        "label": _public_text(flux.get("label"), 256),
        "journal": _public_text(flux.get("journal"), 128),
        "journal_library": _public_text(flux.get("journal_library"), 128),
        "objects": _objects_field(flux.get("objects")),
        "reader_path": _public_text(flux.get("reader_path"), 256),
        "target": _public_text(flux.get("target"), 256),
        "job": _public_text(flux.get("job"), 128),
    }


def _diagnostic_field(value: object) -> dict[str, object] | None:
    """Reformule ``run.last_error`` en diagnostic sain : type en allowlist,
    head borné et déjà expurgé par le worker — jamais le payload brut."""
    if not isinstance(value, Mapping):
        return None
    error_type = value.get("type")
    return {
        "type": error_type
        if isinstance(error_type, str) and error_type in SAFE_CAPTURE_INCIDENT_TYPES
        else None,
        "head": _public_text(value.get("head"), 160),
        "at": _timestamp_field(value.get("at")),
    }


def _source_pause_field(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "retry_after": _timestamp_field(value.get("retry_after")),
        "reason_code": _public_text(value.get("reason_code"), 64),
    }


def _run_field(run: Mapping[str, object]) -> dict[str, object]:
    return {
        "state": _public_text(run.get("state"), 64),
        "started_at": _timestamp_field(run.get("started_at")),
        "elapsed_s": _duration_field(run.get("elapsed_s")),
        "stopped_because": _public_text(run.get("stopped_because"), 160),
        "diagnostic": _diagnostic_field(run.get("last_error")),
        "source_pause": _source_pause_field(run.get("source_pause")),
    }


def _lag_verdict_field(lag: Mapping[str, object]) -> tuple[str | None, str | None]:
    envelope = lag.get("verdict")
    verdict = _known_text(envelope)
    if verdict in LAG_VERDICTS:
        return verdict, None
    declared = _public_text(
        envelope.get("unknown") if isinstance(envelope, Mapping) else None, 200
    )
    if declared is not None:
        return None, declared
    return None, "verdict_not_declared" if verdict is None else "verdict_out_of_contract"


def _destination_field(value: object) -> tuple[dict[str, object] | None, str | None]:
    if value is None:
        return None, "destination_not_attached"
    if not isinstance(value, Mapping):
        return None, "destination_invalid"
    run_tag = value.get("run_tag")
    return (
        {
            "kind": _public_text(value.get("kind"), 32),
            "database": _public_text(value.get("database"), 128),
            "schema": _public_text(value.get("schema"), 128),
            "stage": _public_text(value.get("stage"), 128),
            "raw_table": _public_text(value.get("raw_table"), 128),
            "canonical_table": _public_text(value.get("canonical_table"), 128),
            "run_tag": run_tag
            if isinstance(run_tag, str) and _RUN_TAG.fullmatch(run_tag)
            else None,
            "observed_at": _timestamp_field(value.get("observed_at")),
            "load_checkpoint": _checkpoint_field(value.get("load_checkpoint")),
            "apply_checkpoint": _checkpoint_field(value.get("apply_checkpoint")),
            "source_events": _count_field(value.get("source_events")),
            "raw_rows": _count_field(value.get("raw_rows")),
            "canonical_rows": _count_field(value.get("canonical_rows")),
            "duplicates": _count_field(value.get("duplicates")),
        },
        None,
    )


def _valid_receiver_window(position: Mapping[str, object], tail_sequence: int) -> bool:
    first = position.get("receiver_first_sequence")
    last = position.get("receiver_last_sequence")
    if first is None and last is None:
        return True
    if not _valid_sequence(first) or not _valid_sequence(last):
        return False
    return first <= last and first <= tail_sequence <= last


def _require(condition: bool, code: str, safe_message: str) -> None:
    if not condition:
        raise ProjectionError(code, safe_message)
