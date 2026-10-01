"""Collecte du relevé du pilote de progression — des documents vers des mesures.

Le pilote (:mod:`fleet_progression`) ne connaît ni S3 ni fichiers : ce
module traduit les artefacts déjà produits — intentions prepare/history,
drapeau de suspension, catalogue de receivers, preuve destination mesurée —
en :class:`FleetEvidence`. Chaque champ absent ou illisible laisse la
mesure à ``None`` : le domaine n'avance jamais sur un silence.
"""

from __future__ import annotations

from typing import Any, Mapping

from quadringent.fleet_certify_probe import parse_certify_document

from . import fleet as _fleet
from .fleet import JournalCheckpoint, ProofWindow
from .fleet_history_runtime import PHASE_HISTORICAL
from .fleet_pause_runtime import PHASE_PAUSED
from .fleet_plan import FleetCatalog
from .fleet_prepare_runtime import PHASE_PREPARED
from .fleet_progression import (
    CertificationMeasure,
    FleetEvidence,
    TableMeasure,
    committed_tail,
    continuity_verdict,
    receiver_chain,
)


def collect_fleet_evidence(
    *,
    prepare_document: Mapping[str, object] | None,
    history_document: Mapping[str, object] | None,
    pause_document: Mapping[str, object] | None,
    catalog: FleetCatalog | None,
    snapshot_document: Mapping[str, object] | None = None,
    proof_document: Mapping[str, object] | None = None,
    certify_documents: Mapping[str, object] | None = None,
) -> FleetEvidence | None:
    """Assemble le relevé courant ; ``None`` quand rien n'a encore été préparé.

    ``prepare_document`` est le contenu de ``fleet-prepare.json`` — son
    absence signifie qu'aucune intention n'existe : la progression n'a
    rien à piloter. Les autres documents manquants dégradent les mesures
    correspondantes sans bloquer la collecte.
    """

    if not isinstance(prepare_document, Mapping):
        return None
    if prepare_document.get("phase") != PHASE_PREPARED:
        return None
    intent = prepare_document.get("intent_id")
    if not isinstance(intent, str) or not intent.strip():
        return None
    start = _checkpoint_of(prepare_document.get("checkpoint"))

    history_active = (
        isinstance(history_document, Mapping)
        and history_document.get("phase") == PHASE_HISTORICAL
    )
    paused = (
        isinstance(pause_document, Mapping)
        and pause_document.get("phase") == PHASE_PAUSED
    )

    chain = None
    tail = None
    proven: bool | None = None
    gap: bool | None = None
    estimates: dict[str, int] = {}
    if catalog is not None:
        journal = catalog.journals[0]
        try:
            chain = receiver_chain(journal.receivers)
        except _fleet.FleetError:
            # Une chaîne incohérente (receiver attaché non terminal) n'est
            # pas une preuve : la continuité reste non établie.
            chain = None
        tail = committed_tail(journal.receivers)
        proven, gap = continuity_verdict(journal.continuity)
        estimates = {table.name: table.row_count for table in catalog.tables}

    current = None
    measures: dict[str, TableMeasure] = {}
    if isinstance(snapshot_document, Mapping):
        current = _proof_checkpoint(snapshot_document)
    if isinstance(proof_document, Mapping):
        measures = _table_measures(proof_document)
    certifications = _certifications(certify_documents)

    tables: dict[str, TableMeasure] = {}
    for name in _fleet.MANIFEST:
        measure = measures.get(name) or TableMeasure()
        estimate = estimates.get(name)
        tables[name] = TableMeasure(
            estimated_rows=estimate if estimate is not None else measure.estimated_rows,
            snapshot_rows=measure.snapshot_rows,
            snapshot_published=measure.snapshot_published,
            loaded_rows=measure.loaded_rows,
            certification=certifications.get(name),
        )

    return FleetEvidence(
        prepare_intent_id=intent.strip(),
        start_checkpoint=start,
        history_active=history_active,
        paused=paused,
        current_checkpoint=current,
        committed_tail=tail,
        receiver_chain=chain,
        continuity_proven=proven,
        gap=gap,
        tables=tables,
    )


def _checkpoint_of(value: object) -> JournalCheckpoint | None:
    """Un checkpoint {receiver, sequence} typé, ou ``None`` — jamais partiel."""

    if not isinstance(value, Mapping):
        return None
    receiver = value.get("receiver")
    sequence = value.get("sequence")
    if not isinstance(receiver, str) or not receiver.strip():
        return None
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        return None
    try:
        return JournalCheckpoint(receiver.strip(), sequence)
    except Exception:
        return None


def _proof_checkpoint(proof: Mapping[str, object]) -> JournalCheckpoint | None:
    """Position courante du lecteur, lue dans le document de preuve."""

    position = proof.get("position")
    if isinstance(position, Mapping):
        return _checkpoint_of(position.get("checkpoint"))
    return None


def _table_measures(proof: Mapping[str, object]) -> dict[str, TableMeasure]:
    """Mesures par voie depuis ``destination.tables`` de la preuve console."""

    destination = proof.get("destination")
    tables = destination.get("tables") if isinstance(destination, Mapping) else None
    if not isinstance(tables, Mapping):
        return {}
    measures: dict[str, TableMeasure] = {}
    for name, entry in tables.items():
        if not isinstance(name, str) or name not in _fleet.MANIFEST:
            continue
        if not isinstance(entry, Mapping):
            measures[name] = TableMeasure()
            continue
        measures[name] = TableMeasure(
            snapshot_rows=_count(entry.get("snapshot_rows")),
            snapshot_published=_count(entry.get("snapshot_published")),
            loaded_rows=_count(entry.get("raw_rows")),
        )
    return measures


def _count(value: object) -> int | None:
    """Un compteur mesuré non négatif, ou ``None`` — jamais de cast."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _certifications(
    documents: Mapping[str, object] | None,
) -> dict[str, CertificationMeasure]:
    """Preuves de certification par voie — document hors contrat ignoré."""

    if not isinstance(documents, Mapping):
        return {}
    measures: dict[str, CertificationMeasure] = {}
    for name, document in documents.items():
        if name not in _fleet.MANIFEST:
            continue
        cert = parse_certify_document(document)
        if cert is None or cert.table != name:
            continue
        try:
            window = ProofWindow(
                start_utc=cert.window_start_utc,
                end_utc=cert.window_end_utc,
            )
        except _fleet.FleetError:
            continue
        measures[name] = CertificationMeasure(
            window=window,
            source_count=cert.source_count,
            target_count=cert.target_count,
            missing=cert.missing,
            extra=cert.extra,
            duplicates=cert.duplicates,
            source_hash=cert.source_hash,
            target_hash=cert.target_hash,
            freshness_seconds=cert.destination_freshness_seconds,
            latency_seconds=cert.latency_seconds,
            throughput_rows_per_second=cert.throughput_rows_per_second,
            measured_at=cert.measured_at,
        )
    return measures
