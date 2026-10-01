"""Composition sûre d'une preuve capture avec une réconciliation Snowflake."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Mapping

from .site_config import SiteConfig
from .snowflake_loader import assert_declared_destination


_RUN_TAG = re.compile(r"^[A-Z0-9_]{1,64}$")


def attach_snowflake_proof(
    capture_document: Mapping[str, object],
    replay_metrics: Mapping[str, object],
    *,
    run_tag: str,
    observed_at: datetime,
    site: SiteConfig,
) -> dict[str, object]:
    """Retourne un nouveau snapshot uniquement si le replay est réconcilié.

    La fonction ne fait confiance ni au statut seul, ni au nom des objets. Elle
    recalcule les invariants qui permettent à la console de présenter une
    livraison comme prouvée.
    """

    if capture_document.get("format_version") != "as400-console-v1":
        raise ValueError("capture snapshot format is incompatible")
    if not _RUN_TAG.fullmatch(run_tag):
        raise ValueError("run_tag is invalid")
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")

    database = _required_text(replay_metrics, "database")
    schema = _required_text(replay_metrics, "schema")
    stage = _required_text(replay_metrics, "stage")
    raw_table = _required_text(replay_metrics, "raw_table")
    canonical_table = _required_text(replay_metrics, "canonical_table")
    assert_declared_destination(
        site.snowflake_scope,
        database,
        schema,
        stage,
        raw_table,
        canonical_table,
    )
    if replay_metrics.get("status") != "PASS":
        raise ValueError("Snowflake replay did not pass")

    checkpoint = _checkpoint(capture_document)
    source_events = _known_capture_events(capture_document)
    raw_rows = _non_negative_int(replay_metrics, "raw_rows_after_second")
    distinct_event_ids = _non_negative_int(
        replay_metrics, "distinct_event_ids_after_second"
    )
    canonical_rows = _non_negative_int(
        replay_metrics, "canonical_rows_after_second"
    )
    duplicates = raw_rows - distinct_event_ids
    if (
        source_events <= 0
        or raw_rows != source_events
        or distinct_event_ids != source_events
        or canonical_rows != source_events
        or duplicates != 0
    ):
        raise ValueError("Snowflake replay is not reconciled")

    observed = observed_at.isoformat()
    combined = deepcopy(dict(capture_document))
    combined["capture_observed_at"] = capture_document.get(
        "capture_observed_at", capture_document.get("generated_at")
    )
    # Le document combiné est un nouveau snapshot, horodaté à la réconciliation.
    combined["generated_at"] = observed
    combined["destination"] = {
        "kind": "snowflake",
        "database": database,
        "schema": schema,
        "stage": stage,
        "raw_table": raw_table,
        "canonical_table": canonical_table,
        "run_tag": run_tag,
        "observed_at": observed,
        "load_checkpoint": deepcopy(checkpoint),
        "apply_checkpoint": deepcopy(checkpoint),
        "source_events": source_events,
        "raw_rows": raw_rows,
        "canonical_rows": canonical_rows,
        "duplicates": duplicates,
    }
    combined["destination_proof"] = _destination_proof_v1(
        checkpoint=checkpoint,
        observed_at=observed,
        destination_id=site.destination_id,
        environment=site.environment,
        source_events=source_events,
        raw_rows=raw_rows,
        canonical_rows=canonical_rows,
        distinct_event_ids=distinct_event_ids,
        duplicates=duplicates,
        batch_count=_batch_count(capture_document),
    )
    return combined


def _destination_proof_v1(
    *,
    checkpoint: Mapping[str, object],
    observed_at: str,
    destination_id: str,
    environment: str,
    source_events: int,
    raw_rows: int,
    canonical_rows: int,
    distinct_event_ids: int,
    duplicates: int,
    batch_count: int,
) -> dict[str, object]:
    """Contrat control-plane destination-proof-v1, sans inférer une fenêtre journal.

    Les checks d'activation enregistrent ce que le replay PASS a effectivement
    exercé (config isolée RD, credential, connectivité, COPY, contrat de comptes).
    `from_exclusive` reste null : le loader ne prouve pas le début du journal.
    """

    source_checkpoint = deepcopy(dict(checkpoint))
    return {
        "schema_version": "destination-proof-v1",
        "observed_at": observed_at,
        "source_checkpoint": source_checkpoint,
        "target": {
            "kind": "snowflake",
            "destination_id": destination_id,
            "environment": environment,
        },
        "activation": {
            "state": "active",
            "observed_at": observed_at,
            "checks": {
                "configuration": "valid",
                "credential": "available",
                "connectivity": "reachable",
                "authorization": "allowed",
                "contract": "compatible",
            },
            "blocker_code": None,
        },
        "load": {
            "state": "succeeded",
            "observed_at": observed_at,
            "checkpoint": deepcopy(source_checkpoint),
            "batch_count": batch_count,
            "event_count": source_events,
            "failed_event_count": 0,
            "incident_code": None,
        },
        "destination": {
            "state": "applied",
            "observed_at": observed_at,
            "apply_checkpoint": deepcopy(source_checkpoint),
            "failed_mutation_count": 0,
            "incident_code": None,
        },
        "reconciliation": {
            "state": "matched",
            "observed_at": observed_at,
            "window": {
                "from_exclusive": None,
                "to_inclusive": deepcopy(source_checkpoint),
            },
            "captured_event_count": source_events,
            "loaded_event_count": raw_rows,
            "ledger_event_count": canonical_rows,
            "distinct_event_count": distinct_event_ids,
            "duplicate_event_count": duplicates,
            "missing_event_count": 0,
            "unexpected_event_count": 0,
            "failed_mutation_count": 0,
        },
    }


def _batch_count(document: Mapping[str, object]) -> int:
    counters = document.get("counters")
    wrapper = counters.get("windows_published") if isinstance(counters, Mapping) else None
    value = wrapper.get("value") if isinstance(wrapper, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return value


def _required_text(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    return value


def _non_negative_int(values: Mapping[str, object], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _checkpoint(document: Mapping[str, object]) -> dict[str, object]:
    position = document.get("position")
    checkpoint = position.get("checkpoint") if isinstance(position, Mapping) else None
    if not isinstance(checkpoint, Mapping):
        raise ValueError("capture checkpoint is required")
    receiver = checkpoint.get("receiver")
    sequence = checkpoint.get("sequence")
    if (
        not isinstance(receiver, str)
        or not receiver
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
    ):
        raise ValueError("capture checkpoint is invalid")
    return {"receiver": receiver, "sequence": sequence}


def _known_capture_events(document: Mapping[str, object]) -> int:
    counters = document.get("counters")
    wrapper = counters.get("events_published") if isinstance(counters, Mapping) else None
    value = wrapper.get("value") if isinstance(wrapper, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("capture event count is unknown")
    return value
