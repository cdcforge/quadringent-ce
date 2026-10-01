"""Transport borné et fail-open des alertes Quadringent DEV.

``slo_alerts.py`` produit un état durable et des transitions ; ce module porte
la seule responsabilité de les émettre vers l'extérieur, quand un opérateur le
demande. Deux destinations, jamais imposées :

- ``QUADRINGENT_ALERT_WEBHOOK_URL`` : POST JSON vers un webhook (Slack, PagerDuty
  Events ou équivalent) ;
- ``QUADRINGENT_ALERT_SNS_TOPIC`` : publication SNS vers un ARN de sujet.

Le payload ne contient que des métadonnées d'alerte : identifiants de check,
états, seuils, horodatages et digests. Aucune donnée métier, aucun secret —
les raisons d'alerte sont déjà des jetons stables (``freshness_exceeded``,
``count_mismatch``…).

Le transport est borné : charge utile plafonnée à ``MAX_NOTIFICATION_BYTES``,
événements tronqués à ``MAX_EVENTS`` (le décompte exact reste présent), délai
réseau plafonné. Et il est **fail-open** : ``deliver`` ne lève jamais pour une
erreur d'émission, il rend un statut ``failed`` par destination ; c'est
l'appelant qui consigne l'échec. Une alerte non émise ne doit jamais bloquer
ni masquer la capture.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Sequence
import urllib.request

from .site_config import SiteConfig


NOTIFICATION_SCHEMA = "quadringent-alert-notification-v1"
DEADMAN_SCHEMA = "quadringent-deadman-v1"
DEADMAN_CHECK_ID = "observability_freshness"
MAX_NOTIFICATION_BYTES = 60 * 1024
MAX_EVENTS = 64
DEFAULT_TIMEOUT_SECONDS = 5.0


def _site(site: SiteConfig) -> SiteConfig:
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    return site


def sns_subject(site: SiteConfig) -> str:
    """Sujet SNS borné, dérivé de l'environnement déclaré."""

    return f"Quadringent {_site(site).environment.upper()} SLO"

_EVENT_FIELDS = (
    "event_id",
    "fingerprint",
    "check_id",
    "stage",
    "kind",
    "transition",
    "signal_status",
    "severity",
    "reason",
    "observed",
    "threshold",
    "unit",
    "first_fired_at",
    "firing_since",
    "resolved_at",
    "occurrence_count",
    "observed_at",
)
_ACTIVE_FIELDS = (
    "fingerprint",
    "check_id",
    "stage",
    "severity",
    "reason",
    "firing_since",
    "occurrence_count",
)


def notification_payload(
    batch: Mapping[str, object], *, site: SiteConfig
) -> dict[str, object]:
    """Projete un batch d'alertes validé en notification bornée.

    Seuls les champs explicitement listés traversent : une extension future du
    batch ne fuira pas dans le webhook. La troncature des événements est
    signalée par ``events_truncated`` — jamais silencieuse.
    """

    if not isinstance(batch, Mapping):
        raise ValueError("alert batch must be an object")
    site = _site(site)
    if batch.get("schema_version") not in (
        "quadringent-alert-batch-v1", "cdcforge-alert-batch-v1"
    ):
        raise ValueError("unsupported alert batch schema")
    if (
        batch.get("environment") != site.environment
        or batch.get("pipeline_id") != site.pipeline_id
    ):
        raise ValueError("alert batch escaped the declared pipeline")
    observed_at = batch.get("observed_at")
    if not isinstance(observed_at, str):
        raise ValueError("alert batch observed_at is missing")
    source = batch.get("source_report")
    if not isinstance(source, Mapping) or not isinstance(source.get("digest"), str):
        raise ValueError("alert batch source report digest is missing")
    state = batch.get("state")
    if not isinstance(state, Mapping) or not isinstance(state.get("state_digest"), str):
        raise ValueError("alert batch state digest is missing")

    events_value = batch.get("events")
    if not isinstance(events_value, list):
        raise ValueError("alert batch events must be an array")
    events: list[dict[str, object]] = []
    for value in events_value[:MAX_EVENTS]:
        if not isinstance(value, Mapping):
            raise ValueError("alert event must be an object")
        events.append({name: value.get(name) for name in _EVENT_FIELDS})

    alerts_value = state.get("alerts")
    if not isinstance(alerts_value, list):
        raise ValueError("alert batch state alerts must be an array")
    active: list[dict[str, object]] = []
    for record in alerts_value:
        if not isinstance(record, Mapping) or record.get("lifecycle_state") != "firing":
            continue
        active.append({name: record.get(name) for name in _ACTIVE_FIELDS})

    payload: dict[str, object] = {
        "schema_version": NOTIFICATION_SCHEMA,
        "environment": site.environment,
        "pipeline_id": site.pipeline_id,
        "observed_at": observed_at,
        "source_report_digest": source["digest"],
        "state_digest": state["state_digest"],
        "event_count": len(events_value),
        "events_truncated": len(events_value) > len(events),
        "events": events,
        "active_alerts": active[:MAX_EVENTS],
        "active_alert_count": len(active),
    }
    encoded = _encode(payload)
    if len(encoded) > MAX_NOTIFICATION_BYTES:
        # Dernier recours borné : on garde les transitions, on abandonne le
        # détail des alertes actives (leur décompte reste exact).
        payload["active_alerts"] = []
        encoded = _encode(payload)
        if len(encoded) > MAX_NOTIFICATION_BYTES:
            payload["events"] = []
            payload["events_truncated"] = True
            encoded = _encode(payload)
            if len(encoded) > MAX_NOTIFICATION_BYTES:
                raise ValueError("alert notification exceeds the bounded size")
    return payload


def deadman_payload(
    assessment: Mapping[str, object], *, detected_at: str, site: SiteConfig
) -> dict[str, object]:
    """Notification autonome quand la sonde d'observabilité elle-même se tait.

    Émise uniquement pour ``stale`` ou ``missing`` : une preuve qui vieillit en
    silence est le défaut que ce check existe pour révéler.
    """

    if not isinstance(assessment, Mapping):
        raise ValueError("observability assessment must be an object")
    status = assessment.get("status")
    if status not in ("stale", "missing"):
        raise ValueError("deadman notification requires a stale or missing assessment")
    if not isinstance(detected_at, str):
        raise ValueError("deadman detection time must be a timestamp string")
    site = _site(site)
    identity = {
        "environment": site.environment,
        "pipeline_id": site.pipeline_id,
        "check_id": DEADMAN_CHECK_ID,
    }
    return {
        "schema_version": DEADMAN_SCHEMA,
        "environment": site.environment,
        "pipeline_id": site.pipeline_id,
        "fingerprint": "sha256:"
        + hashlib.sha256(_encode(identity)).hexdigest(),
        "check_id": DEADMAN_CHECK_ID,
        "kind": "firing",
        "severity": "warning",
        "status": status,
        "observed_at": assessment.get("observed_at"),
        "age_seconds": assessment.get("age_seconds"),
        "threshold_seconds": assessment.get("threshold_seconds"),
        "detected_at": detected_at,
    }


def assess_observability_freshness(
    document: Mapping[str, object] | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, object]:
    """Dead man's switch : la sonde d'observabilité a-t-elle tourné récemment ?

    ``document`` est la preuve cockpit combinée (celle que le CronJob republie
    sous ``proofs/quadringent-autonomous-latest.json``). ``None`` ou un document
    sans bloc ``observability`` valide rend ``missing`` — la sonde morte et la
    sonde sans preuve sont le même symptôme : personne ne mesure plus rien.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")

    observed_at: str | None = None
    if isinstance(document, Mapping):
        observability = document.get("observability")
        if isinstance(observability, Mapping):
            state = observability.get("alert_state")
            report = observability.get("slo_report")
            for candidate in (
                state.get("updated_at") if isinstance(state, Mapping) else None,
                report.get("observed_at") if isinstance(report, Mapping) else None,
            ):
                if isinstance(candidate, str):
                    try:
                        parsed = _timestamp(candidate)
                    except ValueError:
                        continue
                    if parsed.tzinfo is not None:
                        observed_at = candidate
                        break
    if observed_at is None:
        return {
            "status": "missing",
            "observed_at": None,
            "age_seconds": None,
            "threshold_seconds": max_age_seconds,
        }
    age = (now - _timestamp(observed_at)).total_seconds()
    status = "stale" if age > max_age_seconds else "fresh"
    return {
        "status": status,
        "observed_at": observed_at,
        "age_seconds": round(max(age, 0.0), 3),
        "threshold_seconds": max_age_seconds,
    }


def deliver(
    payload: Mapping[str, object],
    *,
    site: SiteConfig,
    webhook_url: str | None = None,
    sns_topic: str | None = None,
    sns_client: Any | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    """Émet le payload vers chaque destination configurée. Jamais d'exception.

    Chaque résultat vaut ``delivered``, ``failed`` ou ``skipped`` (destination
    non configurée n'apparaît pas : aucune destination, aucune ligne).
    """

    site = _site(site)
    if not isinstance(payload, Mapping):
        raise ValueError("notification payload must be an object")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    body = _encode(payload)
    if len(body) > MAX_NOTIFICATION_BYTES:
        raise ValueError("notification payload exceeds the bounded size")

    results: list[dict[str, object]] = []
    if webhook_url:
        results.append(
            _attempt("webhook", lambda: _post_webhook(webhook_url, body, timeout_seconds))
        )
    if sns_topic:
        results.append(
            _attempt(
                "sns",
                lambda: _publish_sns(
                    sns_topic, body, sns_client=sns_client, subject=sns_subject(site)
                ),
            )
        )
    return results


def _attempt(destination: str, send: Any) -> dict[str, object]:
    try:
        send()
    except Exception as error:  # fail-open : l'échec est rendu, jamais levé
        return {
            "destination": destination,
            "status": "failed",
            "error_type": type(error).__name__,
        }
    return {"destination": destination, "status": "delivered"}


def _post_webhook(url: str, body: bytes, timeout_seconds: float) -> None:
    if not url.startswith(("https://", "http://")):
        raise ValueError("webhook url must be http(s)")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        status = getattr(response, "status", response.getcode())
        if not 200 <= status < 300:
            raise ValueError(f"webhook status {status}")


def _publish_sns(
    topic_arn: str, body: bytes, *, sns_client: Any | None, subject: str
) -> None:
    if not topic_arn.startswith("arn:aws:sns:"):
        raise ValueError("sns topic must be an ARN")
    client = sns_client
    if client is None:
        import boto3  # importé ici : la capture locale n'a pas besoin d'AWS

        client = boto3.client("sns")
    client.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=body.decode("utf-8"),
    )


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _encode(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
