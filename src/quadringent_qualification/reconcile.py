"""Rapprochement trois voies : oracle synthétique, source relue, entrepôt cible.

Fonctions pures : elles reçoivent des données déjà extraites (dictionnaires
Python, listes d'événements) et ne font aucune I/O. C'est l'orchestrateur
(``orchestrator.py``) qui les alimente à partir des adaptateurs réels ou de
leurs équivalents de test (« fakes »).

Toutes les différences sont listées, jamais seulement comptées : un nombre de
lignes identique ne suffit pas à valider une étape de rapprochement.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from quadringent.contract import JournalPosition

from .schema import TableSchema


@dataclass(frozen=True)
class ValueDifference:
    key: object
    field: str
    expected: object
    actual: object

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "field": self.field, "expected": self.expected, "actual": self.actual}


@dataclass(frozen=True)
class SetDiff:
    """Différence entre deux jeux de lignes canoniques indexées par clé."""

    missing_keys: tuple[object, ...]
    extra_keys: tuple[object, ...]
    value_differences: tuple[ValueDifference, ...]
    compared_keys: int

    @property
    def equal(self) -> bool:
        return not self.missing_keys and not self.extra_keys and not self.value_differences

    def as_dict(self) -> dict[str, object]:
        return {
            "missing_keys": list(self.missing_keys),
            "extra_keys": list(self.extra_keys),
            "value_differences": [d.as_dict() for d in self.value_differences],
            "compared_keys": self.compared_keys,
            "equal": self.equal,
        }


def diff(expected: Mapping[object, Mapping[str, object]], actual: Mapping[object, Mapping[str, object]],
         fields: Sequence[str]) -> SetDiff:
    """Compare deux ensembles de lignes canoniques clé par clé, champ par champ."""
    missing = tuple(sorted(set(expected) - set(actual), key=str))
    extra = tuple(sorted(set(actual) - set(expected), key=str))
    common = sorted(set(expected) & set(actual), key=str)
    differences = []
    for key in common:
        for f in fields:
            e, a = expected[key].get(f), actual[key].get(f)
            if e != a:
                differences.append(ValueDifference(key, f, e, a))
    return SetDiff(missing, extra, tuple(differences), len(common))


# --- Journal : continuité des séquences et image "avant" -----------------------

@dataclass(frozen=True)
class JournalEvent:
    """Un évènement du journal, déjà décodé côté appelant.

    ``operation`` vaut ``"c"`` (create/insert), ``"u_before"``, ``"u_after"``
    ou ``"d"`` (delete). ``payload`` contient ``before``/``after`` (dict de
    colonnes brutes, pas encore canonicalisées) selon l'opération.
    """

    receiver: str
    sequence: int
    operation: str
    payload: Mapping[str, Any]
    is_snapshot: bool = False


@dataclass(frozen=True)
class JournalContinuity:
    captured_events: int
    duplicate_positions: int
    receivers: tuple[str, ...]
    missing_positions: tuple[JournalPosition, ...]
    unexpected_positions: tuple[JournalPosition, ...]

    @property
    def equal(self) -> bool:
        return (
            not self.missing_positions and not self.unexpected_positions
            and self.duplicate_positions == 0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "captured_events": self.captured_events,
            "receivers": list(self.receivers),
            "missing_positions": [_position_dict(p) for p in self.missing_positions],
            "unexpected_positions": [_position_dict(p) for p in self.unexpected_positions],
            "duplicate_positions": self.duplicate_positions,
            "equal": self.equal,
        }


def _position_dict(position: JournalPosition) -> dict[str, object]:
    return {"receiver": position.receiver, "sequence": position.sequence}


def _receiver_rank(receiver_order: Sequence[str]) -> dict[str, int]:
    if not receiver_order or len(set(receiver_order)) != len(receiver_order) or any(not r for r in receiver_order):
        raise ValueError("ordre des receivers absent ou ambigu")
    return {receiver: rank for rank, receiver in enumerate(receiver_order)}


def _position_key(position: JournalPosition, rank: Mapping[str, int]) -> tuple[int, int, str]:
    return (rank.get(position.receiver, len(rank)), position.sequence, position.receiver)


def journal_continuity(
    captured_sequences: Sequence[int] | Sequence[JournalPosition],
    source_sequences: Sequence[int] | Sequence[JournalPosition],
    receivers: Sequence[str] = (),
    *,
    receiver_order: Sequence[str] | None = None,
) -> JournalContinuity:
    """Compare les positions natives, sans confondre deux receivers qui redémarrent à 1.

    L'ancien appel avec des entiers reste limité à un seul receiver ; un
    pilote réel doit fournir la chaîne ordonnée et les couples natifs.
    """
    if receiver_order is None:
        unique = tuple(dict.fromkeys(receivers))
        if len(unique) != 1:
            raise ValueError("ordre des receivers requis pour comparer les positions")
        receiver_order = unique
        captured = tuple(JournalPosition(unique[0], int(value)) for value in captured_sequences)
        source = tuple(JournalPosition(unique[0], int(value)) for value in source_sequences)
    else:
        if any(not isinstance(value, JournalPosition) for value in (*captured_sequences, *source_sequences)):
            raise ValueError("couples receiver/séquence requis avec un ordre de receivers")
        captured = tuple(captured_sequences)
        source = tuple(source_sequences)
    rank = _receiver_rank(receiver_order)
    if any(position.receiver not in rank for position in source):
        raise ValueError("ordre des receivers incomplet pour l'oracle source")
    ordered_source = sorted(source, key=lambda position: _position_key(position, rank))
    if list(source) != ordered_source or len(set(source)) != len(source):
        raise ValueError("ordre des positions source invalide ou dupliqué")
    duplicate_count = len(captured) - len(set(captured))
    missing = tuple(sorted(set(source) - set(captured), key=lambda position: _position_key(position, rank)))
    unexpected = tuple(sorted(set(captured) - set(source), key=lambda position: _position_key(position, rank)))
    return JournalContinuity(
        len(captured), duplicate_count, tuple(receiver_order), missing, unexpected,
    )


@dataclass(frozen=True)
class BeforeImageMismatch:
    sequence: int
    key: object
    delete_of_absent: bool = False
    receiver: str = ""

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"receiver": self.receiver, "sequence": self.sequence, "key": self.key}
        if self.delete_of_absent:
            out["delete_of_absent"] = True
        return out


def materialize(events: Sequence[JournalEvent], schema: TableSchema, *,
                receiver_order: Sequence[str] | None = None) -> tuple[dict[object, dict[str, object]], list[BeforeImageMismatch]]:
    """Rejoue les évènements (snapshot d'abord, puis chaîne de receivers) et
    renvoie l'état matérialisé ainsi que les incohérences d'image "avant"
    détectées au passage (une image avant qui ne correspond pas à l'état
    courant, ou une suppression d'une clé déjà absente).
    """
    from .schema import canonical

    key_col = schema.primary_key
    snapshot = sorted((e for e in events if e.is_snapshot), key=lambda e: e.sequence)
    journal_rows = [e for e in events if not e.is_snapshot]
    if receiver_order is None:
        present = tuple(dict.fromkeys(e.receiver for e in journal_rows))
        if len(present) > 1:
            raise ValueError("ordre des receivers requis pour rejouer une rotation")
        rank = {present[0]: 0} if present else {}
    else:
        rank = _receiver_rank(receiver_order)
    journal = sorted(
        journal_rows,
        key=lambda e: _position_key(JournalPosition(e.receiver, e.sequence), rank),
    )

    state: dict[object, dict[str, object]] = {}
    mismatches: list[BeforeImageMismatch] = []
    for e in snapshot:
        row = canonical(e.payload["after"], schema)
        state[row[key_col]] = row
    for e in journal:
        p = e.payload
        if e.operation == "u_before":
            source_row = p.get("before") or p.get("after")
            before = canonical(source_row, schema)
            if state.get(before[key_col]) != before:
                mismatches.append(BeforeImageMismatch(e.sequence, before[key_col], receiver=e.receiver))
        elif e.operation in ("c", "u_after"):
            row = canonical(p["after"], schema)
            state[row[key_col]] = row
        elif e.operation == "d":
            raw_key = (p.get("before") or p.get("after"))[key_col]
            key = canonical({key_col: raw_key}, schema)[key_col]
            if key not in state:
                mismatches.append(BeforeImageMismatch(e.sequence, key, delete_of_absent=True, receiver=e.receiver))
            state.pop(key, None)
        else:
            raise ValueError(f"opération de journal inconnue : {e.operation!r}")
    return state, mismatches


@dataclass(frozen=True)
class Boundary:
    bootstrap_sequence: int
    snapshot_rows: int
    min_journal_sequence: int | None
    max_journal_sequence: int | None
    events_before_bootstrap: int
    bootstrap_receiver: str | None = None
    min_journal_position: JournalPosition | None = None
    max_journal_position: JournalPosition | None = None

    @property
    def equal(self) -> bool:
        return self.events_before_bootstrap == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "bootstrap_sequence": self.bootstrap_sequence,
            "snapshot_rows": self.snapshot_rows,
            "min_journal_sequence": self.min_journal_sequence,
            "max_journal_sequence": self.max_journal_sequence,
            "journal_events_before_bootstrap": self.events_before_bootstrap,
            "bootstrap_receiver": self.bootstrap_receiver,
            "min_journal_position": (
                _position_dict(self.min_journal_position) if self.min_journal_position else None
            ),
            "max_journal_position": (
                _position_dict(self.max_journal_position) if self.max_journal_position else None
            ),
            "equal": self.equal,
        }


def boundary(
    events: Sequence[JournalEvent], bootstrap_sequence: int | None = None, *,
    bootstrap_position: JournalPosition | None = None,
    receiver_order: Sequence[str] | None = None,
) -> Boundary:
    snapshot = [e for e in events if e.is_snapshot]
    journal = [e for e in events if not e.is_snapshot]
    if bootstrap_position is not None:
        if receiver_order is None:
            raise ValueError("ordre des receivers requis pour la frontière")
        rank = _receiver_rank(receiver_order)
        if bootstrap_position.receiver not in rank:
            raise ValueError("receiver de bootstrap absent de l'ordre source")
        positions = [JournalPosition(e.receiver, e.sequence) for e in journal]
        ordered = sorted(positions, key=lambda position: _position_key(position, rank))
        bootstrap_key = _position_key(bootstrap_position, rank)
        return Boundary(
            bootstrap_sequence=bootstrap_position.sequence,
            snapshot_rows=len(snapshot),
            min_journal_sequence=ordered[0].sequence if ordered else None,
            max_journal_sequence=ordered[-1].sequence if ordered else None,
            events_before_bootstrap=sum(
                _position_key(position, rank) < bootstrap_key for position in positions
            ),
            bootstrap_receiver=bootstrap_position.receiver,
            min_journal_position=ordered[0] if ordered else None,
            max_journal_position=ordered[-1] if ordered else None,
        )
    if bootstrap_sequence is None:
        raise ValueError("frontière de bootstrap absente")
    sequences = [e.sequence for e in journal]
    return Boundary(
        bootstrap_sequence=bootstrap_sequence,
        snapshot_rows=len(snapshot),
        min_journal_sequence=min(sequences) if sequences else None,
        max_journal_sequence=max(sequences) if sequences else None,
        events_before_bootstrap=sum(s < bootstrap_sequence for s in sequences),
    )


@dataclass(frozen=True)
class ReplayCheck:
    replayed_identical: int
    replayed_divergent: int
    raw_attempt_rows: int
    identical_event_ids: tuple[str, ...] = ()
    divergent_event_ids: tuple[str, ...] = ()

    @property
    def equal(self) -> bool:
        return (self.replayed_divergent == 0 and self.raw_attempt_rows >= 0
                and self.replayed_identical == self.raw_attempt_rows)

    def as_dict(self) -> dict[str, object]:
        return {
            "replayed_event_ids_identical": self.replayed_identical,
            "replayed_event_ids_divergent": self.replayed_divergent,
            "raw_attempt_rows": self.raw_attempt_rows,
            "identical_event_ids": list(self.identical_event_ids),
            "divergent_event_ids": list(self.divergent_event_ids),
            "equal": self.equal,
        }


def replay_check(raw_rows: int, raw_distinct_events: int, replayed_identical: int, replayed_divergent: int,
                 identical_event_ids: Sequence[str] = (), divergent_event_ids: Sequence[str] = ()) -> ReplayCheck:
    return ReplayCheck(replayed_identical, replayed_divergent, raw_rows - raw_distinct_events,
                       tuple(identical_event_ids), tuple(divergent_event_ids))


@dataclass(frozen=True)
class HistoryIdentityCheck:
    """Unicité physique des EVENT_ID relus dans HISTORY, snapshots compris."""

    rows: int
    distinct_event_ids: int
    duplicate_rows: int
    duplicate_event_ids: tuple[str, ...]

    @property
    def equal(self) -> bool:
        return self.duplicate_rows == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows, "distinct_event_ids": self.distinct_event_ids,
            "duplicate_rows": self.duplicate_rows, "duplicate_event_ids": list(self.duplicate_event_ids),
            "equal": self.equal,
        }


def _history_identity_check(event_ids: Sequence[str], rows: int) -> HistoryIdentityCheck:
    if len(event_ids) != rows or any(not isinstance(value, str) or not value.strip() for value in event_ids):
        raise ValueError("identifiants physiques HISTORY absents ou incohérents")
    occurrences = Counter(event_ids)
    return HistoryIdentityCheck(
        rows, len(occurrences), rows - len(occurrences),
        tuple(sorted(event_id for event_id, count in occurrences.items() if count > 1)),
    )


@dataclass(frozen=True)
class ReconciliationReport:
    counts: dict[str, int | None]
    boundary: Boundary
    journal_continuity: JournalContinuity
    replay: ReplayCheck
    before_image_mismatches: tuple[BeforeImageMismatch, ...]
    oracle_vs_destination: SetDiff
    oracle_vs_source: SetDiff
    source_vs_destination: SetDiff
    deleted_keys_absent: bool
    oracle_vs_mirror: SetDiff | None = None
    destination_vs_mirror: SetDiff | None = None
    duplicate_mirror_keys: tuple[object, ...] = ()
    history: HistoryIdentityCheck | None = None

    @property
    def status(self) -> str:
        ok = (
            self.oracle_vs_destination.equal
            and self.oracle_vs_source.equal
            and self.source_vs_destination.equal
            and not self.before_image_mismatches
            and self.journal_continuity.equal
            and self.boundary.equal
            and self.replay.equal
            and self.deleted_keys_absent
            and self.oracle_vs_mirror is not None and self.oracle_vs_mirror.equal
            and self.destination_vs_mirror is not None and self.destination_vs_mirror.equal
            and not self.duplicate_mirror_keys
            and self.history is not None and self.history.equal
        )
        return "PASS" if ok else "FAIL"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "counts": self.counts,
            "boundary": self.boundary.as_dict(),
            "journal_continuity": self.journal_continuity.as_dict(),
            "replay": self.replay.as_dict(),
            "before_image_mismatches": [m.as_dict() for m in self.before_image_mismatches],
            "oracle_vs_destination": self.oracle_vs_destination.as_dict(),
            "oracle_vs_source": self.oracle_vs_source.as_dict(),
            "source_vs_destination": self.source_vs_destination.as_dict(),
            "deleted_keys_absent": self.deleted_keys_absent,
            "history": self.history.as_dict() if self.history is not None else None,
            "mirror": None if self.oracle_vs_mirror is None else {
                "oracle_vs_mirror": self.oracle_vs_mirror.as_dict(),
                "destination_vs_mirror": self.destination_vs_mirror.as_dict(),
                "duplicate_keys": list(self.duplicate_mirror_keys),
            },
        }


def reconcile(
    *,
    oracle: Mapping[object, Mapping[str, object]],
    source: Mapping[object, Mapping[str, object]],
    events: Sequence[JournalEvent],
    schema: TableSchema,
    bootstrap_sequence: int | None = None,
    source_sequences: Sequence[int] | None = None,
    raw_rows: int,
    raw_distinct_events: int,
    replayed_identical: int,
    replayed_divergent: int,
    deleted_keys: Sequence[object] = (),
    bootstrap_position: JournalPosition | None = None,
    source_positions: Sequence[JournalPosition] | None = None,
    receiver_order: Sequence[str] | None = None,
    mirror_rows: Sequence[Mapping[str, object]] | None = None,
    identical_event_ids: Sequence[str] = (),
    divergent_event_ids: Sequence[str] = (),
    history_event_ids: Sequence[str] | None = None,
) -> ReconciliationReport:
    """Construit le rapport de rapprochement à trois voies (fonction pure).

    ``history_event_ids`` porte une identité par ligne physique relue dans
    HISTORY, pas les identités du brut. Les anciens appels sans cette liste
    restent acceptés, mais l'unicité de HISTORY reste absente et interdit PASS.
    """
    history = (_history_identity_check(history_event_ids, len(events))
               if history_event_ids is not None else None)
    journal = [e for e in events if not e.is_snapshot]
    position_mode = any(value is not None for value in (bootstrap_position, source_positions, receiver_order))
    if position_mode:
        if bootstrap_position is None or source_positions is None or receiver_order is None:
            raise ValueError("frontière, oracle et ordre des receivers requis ensemble")
        destination, mismatches = materialize(events, schema, receiver_order=receiver_order)
        journal_boundary = boundary(
            events, bootstrap_position=bootstrap_position, receiver_order=receiver_order,
        )
        continuity = journal_continuity(
            [JournalPosition(e.receiver, e.sequence) for e in journal],
            source_positions, receiver_order=receiver_order,
        )
    else:
        if bootstrap_sequence is None or source_sequences is None:
            raise ValueError("frontière et séquences source requises")
        destination, mismatches = materialize(events, schema)
        journal_boundary = boundary(events, bootstrap_sequence)
        continuity = journal_continuity(
            [e.sequence for e in journal], source_sequences,
            [e.receiver for e in journal] or ["UNKNOWN"],
        )
    fields = schema.column_names
    mirror: dict[object, dict[str, object]] = {}
    duplicate_keys: set[object] = set()
    if mirror_rows is not None:
        from .schema import canonical
        for raw in mirror_rows:
            row = canonical(raw, schema)
            key = row[schema.primary_key]
            if key is None:
                raise ValueError("clé du miroir absente")
            if key in mirror:
                duplicate_keys.add(key)
            mirror[key] = row
    return ReconciliationReport(
        counts={
            "oracle_keys": len(oracle), "source_keys": len(source), "destination_keys": len(destination),
            "snapshot_events": sum(1 for e in events if e.is_snapshot), "journal_events": len(journal),
            "raw_rows": raw_rows, "raw_distinct_events": raw_distinct_events,
            "mirror_rows": len(mirror_rows) if mirror_rows is not None else None,
            "history_rows": len(events),
            "history_distinct_events": history.distinct_event_ids if history is not None else None,
        },
        boundary=journal_boundary,
        journal_continuity=continuity,
        replay=replay_check(raw_rows, raw_distinct_events, replayed_identical, replayed_divergent,
                            identical_event_ids, divergent_event_ids),
        before_image_mismatches=tuple(mismatches),
        oracle_vs_destination=diff(oracle, destination, fields),
        oracle_vs_source=diff(oracle, source, fields),
        source_vs_destination=diff(source, destination, fields),
        deleted_keys_absent=all(k not in destination for k in deleted_keys),
        oracle_vs_mirror=diff(oracle, mirror, fields) if mirror_rows is not None else None,
        destination_vs_mirror=diff(destination, mirror, fields) if mirror_rows is not None else None,
        duplicate_mirror_keys=tuple(sorted(duplicate_keys, key=str)),
        history=history,
    )
