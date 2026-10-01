"""Tests du rapprochement trois voies (reconcile.py)."""

from __future__ import annotations

import pytest

from quadringent.contract import JournalPosition
from quadringent_qualification.reconcile import (
    JournalEvent,
    boundary,
    diff,
    journal_continuity,
    materialize,
    reconcile,
    replay_check,
)
from quadringent_qualification.schema import Column, TableSchema

SCHEMA = TableSchema(
    "QUALIF_LIB.QUALIF_ORDERS",
    primary_key="ORDER_ID",
    columns=(
        Column("ORDER_ID", "integer"),
        Column("LABEL", "varchar", length=40),
    ),
)


def row(order_id: int, label: str | None) -> dict:
    return {"ORDER_ID": order_id, "LABEL": label}


# --- diff() ---------------------------------------------------------------------

def test_diff_equal_when_identical():
    expected = {1: {"ORDER_ID": 1, "LABEL": "a"}}
    result = diff(expected, expected, ("ORDER_ID", "LABEL"))
    assert result.equal
    assert result.compared_keys == 1


def test_diff_detects_missing_key():
    expected = {1: row(1, "a"), 2: row(2, "b")}
    actual = {1: row(1, "a")}
    result = diff(expected, actual, ("ORDER_ID", "LABEL"))
    assert result.missing_keys == (2,)
    assert not result.equal


def test_diff_detects_extra_key():
    expected = {1: row(1, "a")}
    actual = {1: row(1, "a"), 2: row(2, "b")}
    result = diff(expected, actual, ("ORDER_ID", "LABEL"))
    assert result.extra_keys == (2,)


def test_diff_detects_value_difference_and_lists_it():
    expected = {1: row(1, "a")}
    actual = {1: row(1, "b")}
    result = diff(expected, actual, ("ORDER_ID", "LABEL"))
    assert not result.equal
    assert len(result.value_differences) == 1
    d = result.value_differences[0].as_dict()
    assert d == {"key": 1, "field": "LABEL", "expected": "a", "actual": "b"}


def test_diff_as_dict_shape():
    result = diff({}, {}, ("ORDER_ID",))
    assert result.as_dict() == {
        "missing_keys": [], "extra_keys": [], "value_differences": [], "compared_keys": 0, "equal": True,
    }


# --- journal_continuity() --------------------------------------------------------

def test_journal_continuity_equal_when_sequences_match():
    result = journal_continuity([1, 2, 3], [1, 2, 3], ["R1"])
    assert result.equal
    assert result.duplicate_positions == 0


def test_journal_continuity_detects_missing_sequence():
    result = journal_continuity([1, 3], [1, 2, 3], ["R1"])
    assert result.missing_positions == (JournalPosition("R1", 2),)
    assert not result.equal


def test_journal_continuity_detects_unexpected_sequence():
    result = journal_continuity([1, 2, 3, 99], [1, 2, 3], ["R1"])
    assert result.unexpected_positions == (JournalPosition("R1", 99),)
    assert not result.equal


def test_journal_continuity_detects_duplicate_sequence():
    result = journal_continuity([1, 2, 2, 3], [1, 2, 3], ["R1"])
    assert result.duplicate_positions == 1
    assert not result.equal


# --- materialize() : snapshot puis journal, images avant --------------------------

def test_materialize_snapshot_only():
    events = [JournalEvent("SNAPSHOT:1", 1, "c", {"after": row(1, "a")}, is_snapshot=True)]
    state, mismatches = materialize(events, SCHEMA)
    assert state == {1: {"ORDER_ID": 1, "LABEL": "a"}}
    assert mismatches == []


def test_materialize_applies_insert_update_delete_in_sequence_order():
    events = [
        JournalEvent("R1", 3, "d", {"before": row(1, "a")}),
        JournalEvent("R1", 1, "c", {"after": row(1, "a")}),
        JournalEvent("R1", 2, "u_after", {"before": row(1, "a"), "after": row(1, "b")}),
    ]
    state, mismatches = materialize(events, SCHEMA)
    assert state == {}
    assert mismatches == []


def test_materialize_detects_before_image_mismatch():
    events = [
        JournalEvent("R1", 1, "c", {"after": row(1, "a")}),
        JournalEvent("R1", 2, "u_before", {"before": row(1, "WRONG")}),
        JournalEvent("R1", 3, "u_after", {"after": row(1, "c")}),
    ]
    state, mismatches = materialize(events, SCHEMA)
    assert len(mismatches) == 1
    assert mismatches[0].sequence == 2
    assert mismatches[0].key == 1


def test_materialize_detects_delete_of_absent_key():
    events = [JournalEvent("R1", 1, "d", {"before": row(5, "x")})]
    state, mismatches = materialize(events, SCHEMA)
    assert state == {}
    assert len(mismatches) == 1
    assert mismatches[0].delete_of_absent is True


def test_materialize_unknown_operation_raises():
    events = [JournalEvent("R1", 1, "bogus", {"after": row(1, "a")})]
    try:
        materialize(events, SCHEMA)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- boundary() -------------------------------------------------------------------

def test_boundary_equal_when_no_journal_event_precedes_bootstrap():
    events = [JournalEvent("R1", 105, "c", {"after": row(1, "a")})]
    result = boundary(events, bootstrap_sequence=105)
    assert result.equal
    assert result.events_before_bootstrap == 0


def test_boundary_as_dict_includes_equal_flag():
    events = [JournalEvent("R1", 105, "c", {"after": row(1, "a")})]
    result = boundary(events, bootstrap_sequence=105)
    assert result.as_dict()["equal"] is True


def test_boundary_detects_events_before_bootstrap():
    events = [JournalEvent("R1", 104, "c", {"after": row(1, "a")}), JournalEvent("R1", 106, "c", {"after": row(2, "b")})]
    result = boundary(events, bootstrap_sequence=105)
    assert not result.equal
    assert result.events_before_bootstrap == 1


# --- replay_check() -----------------------------------------------------------------

def test_replay_check_equal_when_no_divergence():
    result = replay_check(raw_rows=203, raw_distinct_events=154, replayed_identical=49, replayed_divergent=0)
    assert result.equal
    assert result.raw_attempt_rows == 49


def test_replay_check_as_dict_includes_equal_flag():
    result = replay_check(raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0)
    assert result.as_dict()["equal"] is True


def test_replay_check_detects_divergent_replay():
    result = replay_check(raw_rows=203, raw_distinct_events=154, replayed_identical=40, replayed_divergent=9)
    assert not result.equal


# --- reconcile() : rapport complet -------------------------------------------------

def _full_scenario():
    oracle = {1: row(1, "a"), 2: row(2, "b")}
    source = {1: row(1, "a"), 2: row(2, "b")}
    events = [
        JournalEvent("SNAPSHOT:1", 1, "c", {"after": row(1, "a")}, is_snapshot=True),
        JournalEvent("R1", 105, "c", {"after": row(2, "b")}),
    ]
    return oracle, source, events


def test_reconcile_pass_when_everything_matches():
    oracle, source, events = _full_scenario()
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[105], raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0,
        deleted_keys=(99,),
        mirror_rows=list(source.values()),
        history_event_ids=("observed-snapshot-id", "observed-journal-id"),
    )
    assert report.status == "PASS"
    assert report.as_dict()["status"] == "PASS"


def test_reconcile_fail_on_missing_destination_key():
    oracle, source, events = _full_scenario()
    events = events[:1]  # ne matérialise que la clé 1 : la clé 2 manque à la destination
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[], raw_rows=1, raw_distinct_events=1, replayed_identical=0, replayed_divergent=0,
    )
    assert report.status == "FAIL"
    assert report.oracle_vs_destination.missing_keys == (2,)


def test_reconcile_fail_on_deleted_key_still_present():
    oracle, source, events = _full_scenario()
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[105], raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0,
        deleted_keys=(1,),  # la clé 1 est bien présente : la suppression attendue a échoué
    )
    assert report.status == "FAIL"
    assert report.deleted_keys_absent is False


def test_reconcile_fail_on_divergent_replay():
    oracle, source, events = _full_scenario()
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[105], raw_rows=3, raw_distinct_events=2, replayed_identical=0, replayed_divergent=1,
    )
    assert report.status == "FAIL"


def test_receiver_rotation_with_restarted_sequence_keeps_source_order():
    events = [
        JournalEvent("SNAPSHOT:qual", 1, "c", {"after": row(1, "a")}, is_snapshot=True),
        JournalEvent("R2", 1, "u_after", {"after": row(1, "c")}),
        JournalEvent("R1", 100, "u_after", {"after": row(1, "b")}),
    ]
    positions = [JournalPosition("R1", 100), JournalPosition("R2", 1)]
    report = reconcile(
        oracle={1: row(1, "c")}, source={1: row(1, "c")}, events=events,
        schema=SCHEMA, bootstrap_position=JournalPosition("R1", 100),
        source_positions=positions, receiver_order=("R1", "R2"),
        raw_rows=3, raw_distinct_events=3, replayed_identical=0, replayed_divergent=0,
        mirror_rows=[row(1, "c")],
        history_event_ids=("observed-snapshot-id", "observed-r2-id", "observed-r1-id"),
    )
    assert report.status == "PASS"
    assert report.journal_continuity.duplicate_positions == 0
    assert report.boundary.events_before_bootstrap == 0


def test_reconcile_cannot_pass_without_an_observed_mirror():
    oracle, source, events = _full_scenario()
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[105], raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0,
    )
    assert report.status == "FAIL"
    assert report.as_dict()["mirror"] is None


def test_unmeasured_replays_cannot_appear_equal():
    assert not replay_check(3, 2, 0, 0).equal


def test_historical_pure_call_without_history_ids_does_not_invent_uniqueness():
    oracle, source, events = _full_scenario()
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[105], raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0,
        mirror_rows=list(source.values()),
    )
    assert report.status == "FAIL"
    assert report.oracle_vs_destination.equal and report.oracle_vs_mirror.equal
    assert report.as_dict()["history"] is None
    assert report.counts["history_rows"] == 2
    assert report.counts["history_distinct_events"] is None


@pytest.mark.parametrize("history_ids", [(), ("observed-id",), ("observed-id", None),
                                        ("observed-id", " "), ("observed-id", 1)])
def test_history_ids_must_describe_every_physical_row(history_ids):
    oracle, source, events = _full_scenario()
    with pytest.raises(ValueError, match="HISTORY"):
        reconcile(
            oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
            source_sequences=[105], raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0,
            mirror_rows=list(source.values()), history_event_ids=history_ids,
        )


def test_history_identity_check_is_independent_of_native_journal_positions():
    oracle, source, events = _full_scenario()
    report = reconcile(
        oracle=oracle, source=source, events=events, schema=SCHEMA, bootstrap_sequence=105,
        source_sequences=[105], raw_rows=2, raw_distinct_events=2, replayed_identical=0, replayed_divergent=0,
        mirror_rows=list(source.values()), history_event_ids=("observed-id", "observed-id"),
    )
    assert report.status == "FAIL"
    assert report.journal_continuity.equal
    assert report.history.duplicate_event_ids == ("observed-id",)
    assert report.history.duplicate_rows == 1


def test_journal_continuity_compares_receiver_and_sequence_pairs():
    result = journal_continuity(
        [JournalPosition("R1", 1), JournalPosition("R2", 1)],
        [JournalPosition("R1", 1), JournalPosition("R2", 1)],
        receiver_order=("R1", "R2"),
    )
    assert result.equal
    assert result.duplicate_positions == 0


def test_journal_continuity_rejects_an_unordered_source_oracle():
    with pytest.raises(ValueError, match="ordre"):
        journal_continuity(
            [JournalPosition("R1", 100), JournalPosition("R2", 1)],
            [JournalPosition("R2", 1), JournalPosition("R1", 100)],
            receiver_order=("R1", "R2"),
        )


def test_equal_sequence_in_different_receivers_is_a_missing_and_unexpected_position():
    result = journal_continuity(
        [JournalPosition("R1", 1)], [JournalPosition("R2", 1)],
        receiver_order=("R1", "R2"),
    )
    assert not result.equal
    assert result.as_dict()["missing_positions"] == [{"receiver": "R2", "sequence": 1}]
    assert result.as_dict()["unexpected_positions"] == [{"receiver": "R1", "sequence": 1}]


def test_before_image_mismatch_reports_receiver_when_sequences_can_repeat():
    events = [
        JournalEvent("R1", 1, "c", {"after": row(1, "a")}),
        JournalEvent("R2", 1, "u_before", {"before": row(1, "wrong")}),
    ]
    _, mismatches = materialize(events, SCHEMA, receiver_order=("R1", "R2"))
    assert mismatches[0].as_dict()["receiver"] == "R2"
