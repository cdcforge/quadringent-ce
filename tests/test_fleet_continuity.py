"""Continuité d'un run : chaîne de reçus, contiguïté, unicité des événements."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "quadringent_fleet_continuity", ROOT / "scripts" / "quadringent_fleet_continuity.py"
)
continuity = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(continuity)


def receipt(start: int, end: int, count: int, previous=None) -> dict:
    return {
        "start": {"receiver": "R1", "sequence": start},
        "end": {"receiver": "R1", "sequence": end},
        "event_count": count,
        "previous": previous,
    }


def events(identifiers: list[str]) -> list[dict]:
    return [{"event_id": identifier} for identifier in identifiers]


def test_a_contiguous_chain_is_continuous() -> None:
    chain = [receipt(100, 199, 2), receipt(200, 299, 3, {"sequence": 199})]
    report = continuity.check(chain, events(["a", "b", "c", "d", "e"]))
    assert report["verdict"] == "CONTINUOUS"
    assert report["chained_from"] == 100
    assert report["chained_to"] == 299
    assert report["continuity_gaps"] == 0
    assert report["duplicate_events"] == 0


def test_a_missing_interval_is_reported_as_a_gap() -> None:
    chain = [receipt(100, 199, 1), receipt(250, 299, 1, {"sequence": 199})]
    report = continuity.check(chain, events(["a", "b"]))
    assert report["continuity_gaps"] == 1
    assert report["gap_samples"][0]["expected_sequence"] == 200
    assert report["verdict"] == "BROKEN_OR_INCOMPLETE"


def test_a_receiver_change_without_continuity_is_a_gap() -> None:
    first = receipt(100, 199, 1)
    second = receipt(200, 299, 1, {"sequence": 199})
    second["end"] = {"receiver": "R2", "sequence": 299}
    second["start"] = {"receiver": "R2", "sequence": 200}
    report = continuity.check([first, second], events(["a", "b"]))
    assert report["continuity_gaps"] == 1


def test_a_duplicated_event_identifier_breaks_the_verdict() -> None:
    chain = [receipt(100, 199, 2), receipt(200, 299, 2, {"sequence": 199})]
    report = continuity.check(chain, events(["a", "b", "a", "c"]))
    assert report["duplicate_events"] == 1
    assert report["unique_events"] == 3
    assert report["verdict"] == "BROKEN_OR_INCOMPLETE"


def test_receipt_counts_must_match_the_events_read() -> None:
    chain = [receipt(100, 199, 5)]
    report = continuity.check(chain, events(["a", "b"]))
    assert report["receipt_counts_match_events"] is False
    assert report["verdict"] == "BROKEN_OR_INCOMPLETE"


def test_an_empty_run_is_not_declared_continuous() -> None:
    report = continuity.check([], [])
    assert report["verdict"] == "BROKEN_OR_INCOMPLETE"
    assert report["receipts"] == 0
