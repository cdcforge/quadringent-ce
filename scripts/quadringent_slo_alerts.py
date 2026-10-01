#!/usr/bin/env python3
"""Persist deterministic firing/resolved events from one Quadringent SLO report.

Notification optionnelle et bornée : quand ``QUADRINGENT_ALERT_WEBHOOK_URL``
ou ``QUADRINGENT_ALERT_SNS_TOPIC`` est défini, chaque transition (opened,
changed, reopened, resolved) est émise vers la destination configurée.
L'émission est fail-open : un échec de livraison est consigné en clair sur
stderr et ne change ni l'état persisté ni le code de sortie.

Dead man's switch : ``--observability-snapshot`` pointe vers la dernière
preuve cockpit combinée. Si son bloc observabilité dépasse
``--deadman-max-age-seconds`` — ou n'existe pas — la sonde elle-même est
suspecte et une notification autonome part par le même transport.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC in sys.path:
    sys.path.remove(SRC)
sys.path.insert(0, SRC)

from quadringent.alert_transport import (
    assess_observability_freshness,
    deadman_payload,
    deliver,
    notification_payload,
)
from quadringent.console_snapshot import FileSnapshotSink
from quadringent.site_config import current as current_site
from quadringent.slo_alerts import reconcile_alerts

DEFAULT_DEADMAN_MAX_AGE_SECONDS = 3600.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument(
        "--state",
        required=True,
        help="Atomic alert batch/state file; may be reused as input on the next run",
    )
    parser.add_argument(
        "--observability-snapshot",
        default=None,
        help="Dernière preuve cockpit combinée ; son absence de fraîcheur "
        "déclenche l'alerte dead man's switch",
    )
    parser.add_argument(
        "--deadman-max-age-seconds",
        type=float,
        default=DEFAULT_DEADMAN_MAX_AGE_SECONDS,
        help="Seuil de silence de la sonde d'observabilité (défaut : 3600 s)",
    )
    parser.add_argument(
        "--deadman-now",
        default=None,
        help="Horodatage ISO imposé pour l'évaluation deadman (tests, rejeu)",
    )
    args = parser.parse_args(argv)

    try:
        site = current_site()
        report = _object(Path(args.report), "report")
        state_path = Path(args.state)
        previous = _object(state_path, "state") if state_path.exists() else None
        result = reconcile_alerts(report, previous, site=site)
        payload = (
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        FileSnapshotSink(state_path).write(payload)
    except Exception as error:
        print(
            json.dumps(
                {"status": "invalid", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    webhook_url = os.environ.get("QUADRINGENT_ALERT_WEBHOOK_URL") or None
    sns_topic = os.environ.get("QUADRINGENT_ALERT_SNS_TOPIC") or None
    deliveries: list[dict[str, object]] = []
    if result["events"] and (webhook_url or sns_topic):
        deliveries += _emit(
            lambda: notification_payload(result, site=site),
            webhook_url=webhook_url,
            sns_topic=sns_topic,
            site=site,
        )

    deadman = _deadman(args, webhook_url=webhook_url, sns_topic=sns_topic, site=site)
    deliveries += deadman.pop("deliveries", [])

    active = [
        record
        for record in result["state"]["alerts"]
        if record["lifecycle_state"] == "firing"
    ]
    critical = sum(record["severity"] == "critical" for record in active)
    warning = sum(record["severity"] == "warning" for record in active)
    status = "critical" if critical else "warning" if warning else "clear"
    print(
        json.dumps(
            {
                "status": status,
                "phase": "slo_alerts",
                "event_count": len(result["events"]),
                "active_critical_count": critical,
                "active_warning_count": warning,
                "deadman": deadman,
            },
            sort_keys=True,
        )
    )
    deadman_breach = deadman.get("status") in ("stale", "missing")
    return 1 if critical else 2 if warning or deadman_breach else 0


def _emit(
    build_payload,
    *,
    webhook_url: str | None,
    sns_topic: str | None,
    site,
) -> list[dict[str, object]]:
    """Construit puis livre une notification ; l'échec d'émission est consigné."""

    try:
        payload = build_payload()
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "alert_payload_failed",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return []
    results = deliver(payload, webhook_url=webhook_url, sns_topic=sns_topic, site=site)
    for item in results:
        if item["status"] != "delivered":
            print(
                json.dumps(
                    {"event": "alert_delivery_failed", **item},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
    return results


def _deadman(
    args: argparse.Namespace,
    *,
    webhook_url: str | None,
    sns_topic: str | None,
    site,
) -> dict[str, object]:
    """Évalue la fraîcheur de la sonde ; n'alerte que sur silence constaté."""

    if not args.observability_snapshot:
        return {"status": "disabled"}
    snapshot_path = Path(args.observability_snapshot)
    try:
        document = _object(snapshot_path, "observability snapshot")
    except Exception:
        document = None
    try:
        now = (
            _timestamp(args.deadman_now)
            if args.deadman_now
            else datetime.now(timezone.utc)
        )
        assessment = assess_observability_freshness(
            document, now=now, max_age_seconds=args.deadman_max_age_seconds
        )
    except Exception as error:
        return {"status": "error", "error_type": type(error).__name__}
    deliveries: list[dict[str, object]] = []
    if assessment["status"] in ("stale", "missing") and (webhook_url or sns_topic):
        deliveries += _emit(
            lambda: deadman_payload(assessment, detected_at=now.isoformat(), site=site),
            webhook_url=webhook_url,
            sns_topic=sns_topic,
            site=site,
        )
    return {**assessment, "deliveries": deliveries}


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("deadman-now must be timezone-aware")
    return parsed


def _object(path: Path, label: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
