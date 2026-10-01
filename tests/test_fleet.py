from __future__ import annotations

import site_fixture

import json
from pathlib import Path
import unittest

SITE = site_fixture.build_test_site()

from quadringent_control_plane.fleet import (
    DESTINATION_NAMESPACE,
    ENVIRONMENT,
    FORMAT_VERSION,
    MANIFEST,
    TABLE_COUNT,
    BlockedReason,
    FleetError,
    FleetRun,
    JournalCheckpoint,
    Phase,
    ProofWindow,
    ReceiverChain,
    ReceiverSpan,
    ReconciliationProof,
    SafeAction,
    admit_next,
    admission_candidates,
    begin_reconciliation,
    block_table,
    certify_table,
    create_fleet,
    deserialize_fleet,
    get_table,
    pause_table,
    prepare_table,
    record_actual_cost,
    record_history_progress,
    record_journal_evidence,
    resume_table,
    serialize_fleet,
    summarize,
)


ROOT = Path(__file__).resolve().parents[1]
FLEET_SOURCE = ROOT / "src" / "quadringent_control_plane" / "fleet.py"
FLEET_TESTS = Path(__file__).resolve()
WINDOW = ProofWindow("2026-09-13T10:00:00Z", "2026-09-13T11:00:00Z")
DEFAULT_ESTIMATE = 1.25
DEFAULT_ACTUAL = 1.25


def checkpoint(sequence: int = 10, receiver: str = "DEMOJRN3776") -> JournalCheckpoint:
    return JournalCheckpoint(receiver, sequence)


def chain(*spans: tuple[str, int, int]) -> ReceiverChain:
    if not spans:
        spans = (("DEMOJRN3776", 1, 200),)
    return ReceiverChain(tuple(ReceiverSpan(*span) for span in spans))


def proof(**overrides: object) -> ReconciliationProof:
    payload = {
        "window": WINDOW,
        "source_count": 10,
        "target_count": 10,
        "missing": 0,
        "extra": 0,
        "duplicates": 0,
        "source_hash": "sha256:window-a",
        "target_hash": "sha256:window-a",
        "destination_freshness_seconds": 2.0,
        "freshness_slo_seconds": 30.0,
        "latency_seconds": 1.5,
        "throughput_rows_per_second": 100.0,
        "cost_units": DEFAULT_ACTUAL,
    }
    payload.update(overrides)
    return ReconciliationProof(**payload)  # type: ignore[arg-type]


def prepared_fleet(*, max_concurrency: int = 2, credit_budget: float = 100.0) -> FleetRun:
    fleet = create_fleet(max_concurrency=max_concurrency, credit_budget=credit_budget)
    for name in MANIFEST:
        fleet = prepare_table(fleet, name, checkpoint())
    return fleet


def journal_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "current_checkpoint": None,
        "journal_tail": None,
        "receiver_chain": chain(),
        "continuity_proven": True,
        "gap": False,
    }
    payload.update(overrides)
    return payload


def enter_historical(fleet: FleetRun | None = None, *, estimated_credits: float = DEFAULT_ESTIMATE) -> FleetRun:
    current = prepared_fleet() if fleet is None else fleet
    return admit_next(current, estimated_credits=estimated_credits)


def enter_catching_up(fleet: FleetRun | None = None) -> FleetRun:
    current = enter_historical(fleet)
    current = record_history_progress(current, "ADDRS1", copied_rows=100, total_rows=100)
    return record_journal_evidence(current, "ADDRS1", **journal_kwargs())  # type: ignore[arg-type]


def enter_live(fleet: FleetRun | None = None, *, actual_credits: float = DEFAULT_ACTUAL) -> FleetRun:
    current = enter_catching_up(fleet)
    current = record_actual_cost(current, "ADDRS1", actual_credits)
    tail = checkpoint(50)
    return record_journal_evidence(
        current,
        "ADDRS1",
        **journal_kwargs(current_checkpoint=tail, journal_tail=tail),  # type: ignore[arg-type]
    )


def enter_reconciling(fleet: FleetRun | None = None) -> FleetRun:
    return begin_reconciliation(enter_live(fleet), "ADDRS1", WINDOW)


def advance_table_to_live(fleet: FleetRun, name: str, *, estimated_credits: float = DEFAULT_ESTIMATE) -> FleetRun:
    fleet = admit_next(fleet, estimated_credits=estimated_credits)
    fleet = record_history_progress(fleet, name, copied_rows=10, total_rows=10)
    fleet = record_journal_evidence(fleet, name, **journal_kwargs())  # type: ignore[arg-type]
    fleet = record_actual_cost(fleet, name, estimated_credits)
    tail = checkpoint(50)
    return record_journal_evidence(
        fleet,
        name,
        **journal_kwargs(current_checkpoint=tail, journal_tail=tail),  # type: ignore[arg-type]
    )


def assert_json_safe(test: unittest.TestCase, value: object) -> None:
    if value is None or type(value) in (str, int, float, bool):
        return
    if type(value) is list:
        for item in value:
            assert_json_safe(test, item)
        return
    if type(value) is dict:
        for key, item in value.items():
            test.assertIs(type(key), str)
            assert_json_safe(test, item)
        return
    test.fail(f"type JSON non autorisé: {type(value)!r}")


class ManifestAndIsolationTests(unittest.TestCase):
    def test_manifest_is_exactly_the_thirteen_dev_tables_in_order(self) -> None:
        self.assertEqual(
            MANIFEST,
            (
                "ADDRS1",
                "CAL001",
                "COST1",
                "CUSTOM1",
                "ORDER",
                "EXPENS",
                "DATE01",
                "SALE",
                "PLACE01",
                "PLACES",
                "CNTR",
                "PRODUCT",
                "HOLIDAYS",
            ),
        )
        fleet = create_fleet(max_concurrency=1, credit_budget=13)
        self.assertEqual(tuple(table.name for table in fleet.tables), MANIFEST)
        self.assertEqual(len(fleet.tables), TABLE_COUNT)
        self.assertEqual(TABLE_COUNT, 13)
        self.assertEqual(fleet.environment, ENVIRONMENT)
        self.assertEqual(ENVIRONMENT, SITE.fleet_environment)
        self.assertEqual(fleet.destination_namespace, DESTINATION_NAMESPACE)
        self.assertEqual(DESTINATION_NAMESPACE, SITE.destination_namespace)
        self.assertTrue(all(table.phase is Phase.NOT_PREPARED for table in fleet.tables))
        self.assertEqual(fleet.consumed_credits, 0.0)
        self.assertEqual(fleet.reserved_credits, 0.0)

    def test_create_fleet_rejects_concurrency_and_budget_outside_bounds(self) -> None:
        with self.assertRaises(FleetError) as zero:
            create_fleet(max_concurrency=0, credit_budget=13)
        self.assertEqual(zero.exception.code, "invalid_concurrency")
        with self.assertRaises(FleetError):
            create_fleet(max_concurrency=5, credit_budget=13)
        with self.assertRaises(FleetError):
            create_fleet(max_concurrency=True, credit_budget=13)  # type: ignore[arg-type]
        with self.assertRaises(FleetError) as negative:
            create_fleet(max_concurrency=2, credit_budget=-1)
        self.assertEqual(negative.exception.code, "invalid_credit_budget")
        with self.assertRaises(FleetError):
            create_fleet(max_concurrency=1, credit_budget=float("inf"))
        zero_budget = create_fleet(max_concurrency=1, credit_budget=0)
        self.assertEqual(zero_budget.credit_budget, 0.0)
        self.assertEqual(zero_budget.consumed_credits, 0.0)
        self.assertEqual(zero_budget.reserved_credits, 0.0)

    def test_non_dev_environment_and_foreign_namespace_fail_closed(self) -> None:
        fleet = create_fleet(max_concurrency=1, credit_budget=13)
        payload = serialize_fleet(fleet)
        payload["environment"] = "PROD"
        with self.assertRaises(FleetError) as env:
            deserialize_fleet(payload)
        self.assertEqual(env.exception.code, "invalid_environment")
        payload = serialize_fleet(fleet)
        payload["environment"] = "INT"
        with self.assertRaises(FleetError):
            deserialize_fleet(payload)
        payload = serialize_fleet(fleet)
        payload["destination_namespace"] = "DEV_RAW.OTHER_SCHEMA"
        with self.assertRaises(FleetError) as destination:
            deserialize_fleet(payload)
        self.assertEqual(destination.exception.code, "invalid_destination")


class PrepareAndAdmitTests(unittest.TestCase):
    def test_prepare_records_start_checkpoint_before_ready(self) -> None:
        fleet = create_fleet(max_concurrency=1, credit_budget=13)
        start = checkpoint(42)
        fleet = prepare_table(fleet, "ADDRS1", start)
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.phase, Phase.READY)
        self.assertEqual(table.start_checkpoint, start)
        with self.assertRaises(FleetError):
            prepare_table(fleet, "ADDRS1", checkpoint(43))
        with self.assertRaises(FleetError):
            prepare_table(fleet, "UNKNOWN", start)
        with self.assertRaises(FleetError):
            prepare_table(fleet, "addrs1", start)

    def test_historical_work_is_refused_until_every_table_is_prepared(self) -> None:
        fleet = create_fleet(max_concurrency=2, credit_budget=13)
        fleet = prepare_table(fleet, "ADDRS1", checkpoint())
        self.assertEqual(admission_candidates(fleet), ())
        with self.assertRaises(FleetError) as refused:
            admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(refused.exception.code, "admission_refused")
        for name in MANIFEST[1:]:
            fleet = prepare_table(fleet, name, checkpoint())
        self.assertEqual(admission_candidates(fleet), ("ADDRS1", "CAL001"))

    def test_admission_is_deterministic_in_manifest_order(self) -> None:
        fleet = prepared_fleet(max_concurrency=2, credit_budget=13)
        self.assertEqual(admission_candidates(fleet), ("ADDRS1", "CAL001"))
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        self.assertTrue(get_table(fleet, "ADDRS1").admitted)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.READY)
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.HISTORICAL)
        self.assertEqual(
            [table.name for table in fleet.tables if table.phase is Phase.HISTORICAL],
            ["ADDRS1", "CAL001"],
        )

    def test_concurrency_bounds_only_historical_and_catching_up(self) -> None:
        fleet = prepared_fleet(max_concurrency=2, credit_budget=100.0)
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(admission_candidates(fleet), ())
        with self.assertRaises(FleetError):
            admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        fleet = pause_table(fleet, "ADDRS1")
        self.assertEqual(admission_candidates(fleet), ("COST1",))
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(get_table(fleet, "COST1").phase, Phase.HISTORICAL)
        self.assertEqual(fleet.reserved_credits, 3 * DEFAULT_ESTIMATE)


class HistoryProgressTests(unittest.TestCase):
    def test_unknown_history_is_not_treated_as_zero_or_complete(self) -> None:
        fleet = enter_historical()
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=None, total_rows=None)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        fleet = record_journal_evidence(fleet, "ADDRS1", **journal_kwargs())  # type: ignore[arg-type]
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=None, total_rows=0)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        summary = summarize(fleet)
        self.assertIsNone(summary.known_copied_rows)
        self.assertEqual(summary.known_total_rows, 0)
        self.assertNotEqual(summary.known_copied_rows, 0)

    def test_incomplete_known_history_does_not_leave_historical(self) -> None:
        fleet = enter_historical()
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=40, total_rows=100)
        fleet = record_journal_evidence(fleet, "ADDRS1", **journal_kwargs())  # type: ignore[arg-type]
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        with self.assertRaises(FleetError):
            record_history_progress(fleet, "ADDRS1", copied_rows=101, total_rows=100)
        with self.assertRaises(FleetError):
            record_history_progress(fleet, "ADDRS1", copied_rows=10, total_rows=100)
        with self.assertRaises(FleetError):
            record_history_progress(fleet, "ADDRS1", copied_rows=True, total_rows=100)  # type: ignore[arg-type]


class PhaseGateTests(unittest.TestCase):
    def test_historical_becomes_catching_up_only_with_complete_copy_and_continuity(self) -> None:
        fleet = enter_historical()
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=100, total_rows=100)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(continuity_proven=None, gap=None),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        fleet = record_journal_evidence(fleet, "ADDRS1", **journal_kwargs())  # type: ignore[arg-type]
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)

    def test_catching_up_becomes_live_only_at_observed_tail_without_gap(self) -> None:
        fleet = enter_catching_up()
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)
        behind = checkpoint(40)
        tail = checkpoint(50)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=behind, journal_tail=tail),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=tail, journal_tail=tail, gap=None),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)
        fleet = record_actual_cost(fleet, "ADDRS1", DEFAULT_ACTUAL)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=tail, journal_tail=tail, gap=False),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.LIVE)
        self.assertEqual(get_table(fleet, "ADDRS1").reserved_credits, 0.0)
        self.assertEqual(fleet.consumed_credits, DEFAULT_ACTUAL)

    def test_live_is_refused_while_actual_cost_is_unknown(self) -> None:
        fleet = enter_catching_up()
        tail = checkpoint(50)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=tail, journal_tail=tail, gap=False),  # type: ignore[arg-type]
        )
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.phase, Phase.CATCHING_UP)
        self.assertIsNone(table.actual_credits)
        self.assertEqual(table.reserved_credits, DEFAULT_ESTIMATE)

    def test_receiver_discontinuity_blocks_instead_of_advancing(self) -> None:
        fleet = enter_historical()
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=100, total_rows=100)
        other = chain(("DEMOJRN3777", 1, 50))
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(receiver_chain=other, continuity_proven=None),  # type: ignore[arg-type]
        )
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.phase, Phase.BLOCKED)
        self.assertEqual(table.blocked_reason, BlockedReason.RECEIVER_DISCONTINUITY.value)

    def test_explicit_gap_blocks_catching_up(self) -> None:
        fleet = enter_catching_up()
        tail = checkpoint(50)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(
                current_checkpoint=tail,
                journal_tail=tail,
                continuity_proven=None,
                gap=True,
            ),  # type: ignore[arg-type]
        )
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.phase, Phase.BLOCKED)
        self.assertEqual(table.blocked_reason, BlockedReason.SEQUENCE_GAP.value)

    def test_explicit_discontinuity_flag_blocks(self) -> None:
        fleet = enter_catching_up()
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(continuity_proven=False, gap=False),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.BLOCKED)
        self.assertEqual(
            get_table(fleet, "ADDRS1").blocked_reason,
            BlockedReason.UNPROVEN_CONTINUITY.value,
        )

    def test_live_requires_closed_aligned_utc_window_to_reconcile(self) -> None:
        fleet = enter_live()
        with self.assertRaises(FleetError):
            begin_reconciliation(enter_catching_up(), "ADDRS1", WINDOW)
        with self.assertRaises(FleetError):
            ProofWindow("2026-09-13T10:00:00", "2026-09-13T11:00:00")
        with self.assertRaises(FleetError):
            ProofWindow("2026-09-13T10:00:00+02:00", "2026-09-13T11:00:00+02:00")
        with self.assertRaises(FleetError):
            ProofWindow("2026-09-13T11:00:00Z", "2026-09-13T10:00:00Z")
        with self.assertRaises(FleetError):
            ProofWindow("2026-09-13T10:00:00Z", "2026-09-13T10:00:00Z")
        with self.assertRaises(FleetError) as unaligned:
            ProofWindow("2026-09-13T10:00:00.500000Z", "2026-09-13T11:00:00Z")
        self.assertEqual(unaligned.exception.code, "unaligned_window")
        fleet = begin_reconciliation(fleet, "ADDRS1", WINDOW)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.RECONCILING)
        self.assertEqual(get_table(fleet, "ADDRS1").proof_window, WINDOW)
        self.assertIsNone(get_table(fleet, "ADDRS1").reconciliation_proof)

    def test_certified_requires_same_window_integrity_freshness_and_measurements(self) -> None:
        fleet = enter_reconciling()
        certified_proof = proof()
        certified = certify_table(fleet, "ADDRS1", certified_proof)
        self.assertEqual(get_table(certified, "ADDRS1").phase, Phase.CERTIFIED)
        self.assertEqual(get_table(certified, "ADDRS1").reconciliation_proof, certified_proof)
        self.assertEqual(get_table(certified, "ADDRS1").proof_window, WINDOW)
        with self.assertRaises(FleetError):
            certify_table(enter_live(), "ADDRS1", proof())
        other_window = ProofWindow("2026-09-13T12:00:00Z", "2026-09-13T13:00:00Z")
        with self.assertRaises(FleetError) as window:
            certify_table(fleet, "ADDRS1", proof(window=other_window))
        self.assertEqual(window.exception.code, "window_mismatch")
        with self.assertRaises(FleetError) as counts:
            certify_table(fleet, "ADDRS1", proof(source_count=10, target_count=9))
        self.assertEqual(counts.exception.code, "count_mismatch")
        with self.assertRaises(FleetError) as missing:
            certify_table(fleet, "ADDRS1", proof(missing=1))
        self.assertEqual(missing.exception.code, "missing_rows")
        with self.assertRaises(FleetError) as extra:
            certify_table(fleet, "ADDRS1", proof(extra=1))
        self.assertEqual(extra.exception.code, "extra_rows")
        with self.assertRaises(FleetError) as duplicates:
            certify_table(fleet, "ADDRS1", proof(duplicates=1))
        self.assertEqual(duplicates.exception.code, "duplicate_rows")
        with self.assertRaises(FleetError) as hashes:
            certify_table(fleet, "ADDRS1", proof(target_hash="sha256:other"))
        self.assertEqual(hashes.exception.code, "hash_mismatch")
        with self.assertRaises(FleetError):
            proof(source_hash="")
        with self.assertRaises(FleetError):
            proof(target_hash="   ")
        with self.assertRaises(FleetError) as freshness:
            certify_table(
                fleet,
                "ADDRS1",
                proof(destination_freshness_seconds=45.0, freshness_slo_seconds=30.0),
            )
        self.assertEqual(freshness.exception.code, "freshness_slo_breached")
        with self.assertRaises(FleetError):
            proof(freshness_slo_seconds=0)
        with self.assertRaises(FleetError):
            proof(latency_seconds=None)  # type: ignore[arg-type]
        with self.assertRaises(FleetError):
            proof(throughput_rows_per_second=None)  # type: ignore[arg-type]
        with self.assertRaises(FleetError):
            proof(cost_units=None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ReconciliationProof(  # type: ignore[call-arg]
                window=WINDOW,
                source_count=10,
                target_count=10,
                missing=0,
                extra=0,
                duplicates=0,
                source_hash="sha256:window-a",
                target_hash="sha256:window-a",
                destination_freshness_seconds=2.0,
                freshness_slo_seconds=30.0,
                latency_seconds=1.5,
                throughput_rows_per_second=100.0,
            )

    def test_proof_cost_mismatch_cannot_certify(self) -> None:
        fleet = enter_reconciling()
        with self.assertRaises(FleetError) as mismatch:
            certify_table(fleet, "ADDRS1", proof(cost_units=0.5))
        self.assertEqual(mismatch.exception.code, "cost_mismatch")
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.RECONCILING)

    def test_phase_functions_fail_closed_on_skipped_gates(self) -> None:
        fleet = prepared_fleet()
        with self.assertRaises(FleetError):
            record_history_progress(fleet, "ADDRS1", copied_rows=1, total_rows=1)
        with self.assertRaises(FleetError):
            record_journal_evidence(fleet, "ADDRS1", **journal_kwargs())  # type: ignore[arg-type]
        with self.assertRaises(FleetError):
            begin_reconciliation(fleet, "ADDRS1", WINDOW)
        with self.assertRaises(FleetError):
            certify_table(fleet, "ADDRS1", proof())


class ReceiverChainCatalogTests(unittest.TestCase):
    RESET_CHAIN = (
        ("DEMOJRN4042", 900009227, 901994124),
        ("DEMOJRN4043", 1, 787461),
    )

    def test_ipl_sequence_reset_is_valid_and_orders_by_receiver_index(self) -> None:
        reset = chain(*self.RESET_CHAIN)
        end_previous = JournalCheckpoint("DEMOJRN4042", 901994124)
        after_ipl = JournalCheckpoint("DEMOJRN4043", 1)
        self.assertEqual(reset.rank(end_previous), (0, 901994124))
        self.assertEqual(reset.rank(after_ipl), (1, 1))
        self.assertLess(reset.rank(end_previous), reset.rank(after_ipl))
        self.assertGreater(end_previous.sequence, after_ipl.sequence)
        with self.assertRaises(FleetError) as missing:
            reset.rank(JournalCheckpoint("DEMOJRN4043", 787462))
        self.assertEqual(missing.exception.code, "receiver_discontinuity")

    def test_duplicate_receiver_in_chain_is_refused(self) -> None:
        with self.assertRaises(FleetError) as duplicated:
            chain(("DEMOJRN4042", 900009227, 901994124), ("DEMOJRN4042", 1, 787461))
        self.assertEqual(duplicated.exception.code, "invalid_receiver_chain")
        with self.assertRaises(FleetError):
            ReceiverSpan("DEMOJRN4043", 787461, 1)
        with self.assertRaises(FleetError):
            ReceiverChain(tuple())

    def test_unproven_continuity_blocks_transitions_on_reset_chain(self) -> None:
        start = JournalCheckpoint("DEMOJRN4042", 900009227)
        tail = JournalCheckpoint("DEMOJRN4043", 787461)
        reset = chain(*self.RESET_CHAIN)
        fleet = create_fleet(max_concurrency=2, credit_budget=100.0)
        for name in MANIFEST:
            fleet = prepare_table(fleet, name, start)
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=100, total_rows=100)

        unknown = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=start, journal_tail=tail,
                             receiver_chain=reset, continuity_proven=None, gap=False),
        )
        self.assertEqual(get_table(unknown, "ADDRS1").phase, Phase.HISTORICAL)
        self.assertEqual(summarize(unknown).next_action, SafeAction.PROVE_CONTINUITY.value)

        denied = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=start, journal_tail=tail,
                             receiver_chain=reset, continuity_proven=False, gap=False),
        )
        self.assertEqual(get_table(denied, "ADDRS1").phase, Phase.BLOCKED)
        self.assertEqual(get_table(denied, "ADDRS1").blocked_reason,
                         BlockedReason.UNPROVEN_CONTINUITY.value)

        proven = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=start, journal_tail=tail,
                             receiver_chain=reset, continuity_proven=True, gap=False),
        )
        self.assertEqual(get_table(proven, "ADDRS1").phase, Phase.CATCHING_UP)


class PauseResumeBlockTests(unittest.TestCase):
    def test_pause_and_resume_restore_the_pre_pause_phase(self) -> None:
        fleet = enter_catching_up()
        paused = pause_table(fleet, "ADDRS1")
        table = get_table(paused, "ADDRS1")
        self.assertEqual(table.phase, Phase.PAUSED)
        self.assertEqual(table.paused_from, Phase.CATCHING_UP)
        resumed = resume_table(paused, "ADDRS1")
        restored = get_table(resumed, "ADDRS1")
        self.assertEqual(restored.phase, Phase.CATCHING_UP)
        self.assertIsNone(restored.paused_from)
        with self.assertRaises(FleetError):
            pause_table(paused, "ADDRS1")
        with self.assertRaises(FleetError):
            resume_table(fleet, "ADDRS1")
        with self.assertRaises(FleetError):
            pause_table(create_fleet(max_concurrency=1, credit_budget=1), "ADDRS1")

    def test_resume_of_a_running_phase_respects_concurrency(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=13)
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        fleet = pause_table(fleet, "ADDRS1")
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.HISTORICAL)
        with self.assertRaises(FleetError) as saturated:
            resume_table(fleet, "ADDRS1")
        self.assertEqual(saturated.exception.code, "concurrency_exceeded")
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.PAUSED)
        self.assertEqual(get_table(fleet, "ADDRS1").paused_from, Phase.HISTORICAL)

    def test_blocked_requires_an_allowlisted_machine_readable_reason(self) -> None:
        fleet = enter_historical()
        blocked = block_table(fleet, "ADDRS1", BlockedReason.OPERATOR_STOP)
        table = get_table(blocked, "ADDRS1")
        self.assertEqual(table.phase, Phase.BLOCKED)
        self.assertEqual(table.blocked_reason, "operator_stop")
        with self.assertRaises(FleetError):
            block_table(fleet, "ADDRS1", "explode_cluster")
        with self.assertRaises(FleetError):
            block_table(blocked, "ADDRS1", BlockedReason.OPERATOR_STOP)
        certified = certify_table(enter_reconciling(), "ADDRS1", proof())
        with self.assertRaises(FleetError):
            block_table(certified, "ADDRS1", BlockedReason.OPERATOR_STOP)


class CostBudgetTests(unittest.TestCase):
    def test_live_does_not_block_the_next_historical_admission(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=100.0)
        fleet = enter_live(fleet)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.LIVE)
        self.assertEqual(summarize(fleet).running_count, 0)
        self.assertEqual(admission_candidates(fleet), ("CAL001",))
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.HISTORICAL)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.LIVE)

    def test_thirteen_tables_flow_sequentially_with_max_concurrency_one(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=20.0)
        for name in MANIFEST:
            fleet = advance_table_to_live(fleet, name)
            self.assertEqual(get_table(fleet, name).phase, Phase.LIVE)
            self.assertEqual(summarize(fleet).running_count, 0)
            self.assertEqual(get_table(fleet, name).reserved_credits, 0.0)
        summary = summarize(fleet)
        self.assertEqual(summary.admitted_count, 13)
        self.assertEqual(summary.consumed_credits, 13 * DEFAULT_ACTUAL)
        self.assertEqual(summary.reserved_credits, 0.0)
        self.assertTrue(all(get_table(fleet, name).phase is Phase.LIVE for name in MANIFEST))

    def test_estimate_over_budget_refuses_admission(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=1.0)
        with self.assertRaises(FleetError) as refused:
            admit_next(fleet, estimated_credits=1.5)
        self.assertEqual(refused.exception.code, "admission_refused")
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.READY)
        self.assertEqual(fleet.reserved_credits, 0.0)
        with self.assertRaises(FleetError):
            admit_next(fleet, estimated_credits=0)
        with self.assertRaises(FleetError):
            admit_next(fleet, estimated_credits=float("nan"))

    def test_reservation_prevents_over_admission(self) -> None:
        fleet = prepared_fleet(max_concurrency=2, credit_budget=2.0)
        fleet = admit_next(fleet, estimated_credits=1.5)
        self.assertEqual(fleet.reserved_credits, 1.5)
        self.assertEqual(fleet.consumed_credits, 0.0)
        with self.assertRaises(FleetError) as refused:
            admit_next(fleet, estimated_credits=0.75)
        self.assertEqual(refused.exception.code, "admission_refused")
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.READY)
        fleet = admit_next(fleet, estimated_credits=0.5)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.HISTORICAL)
        self.assertEqual(fleet.reserved_credits, 2.0)

    def test_actual_above_estimate_within_budget_is_kept(self) -> None:
        fleet = enter_catching_up()
        fleet = record_actual_cost(fleet, "ADDRS1", 9.75)
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.phase, Phase.CATCHING_UP)
        self.assertEqual(table.actual_credits, 9.75)
        self.assertEqual(table.reserved_credits, 9.75)
        self.assertEqual(fleet.reserved_credits, 9.75)
        self.assertEqual(fleet.consumed_credits, 0.0)
        self.assertFalse(summarize(fleet).over_budget)
        with self.assertRaises(FleetError) as receded:
            record_actual_cost(fleet, "ADDRS1", 1.0)
        self.assertEqual(receded.exception.code, "contradictory_cost")
        self.assertEqual(get_table(fleet, "ADDRS1").actual_credits, 9.75)
        self.assertEqual(get_table(fleet, "ADDRS1").reserved_credits, 9.75)

    def test_actual_overrun_persists_cost_and_blocks(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=2.0)
        fleet = enter_catching_up(fleet)
        fleet = record_actual_cost(fleet, "ADDRS1", 9.75)
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.phase, Phase.BLOCKED)
        self.assertEqual(table.blocked_reason, BlockedReason.COST_OVERRUN.value)
        self.assertEqual(table.actual_credits, 9.75)
        self.assertEqual(table.reserved_credits, 0.0)
        self.assertEqual(fleet.consumed_credits, 9.75)
        self.assertGreater(fleet.consumed_credits, fleet.credit_budget)
        self.assertEqual(admission_candidates(fleet), ())
        with self.assertRaises(FleetError) as refused:
            admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(refused.exception.code, "admission_refused")
        summary = summarize(fleet)
        self.assertTrue(summary.over_budget)
        self.assertEqual(summary.consumed_credits, 9.75)
        self.assertEqual(summary.next_action, SafeAction.INSPECT_BLOCKED.value)

    def test_concurrent_actual_updates_reservation_and_blocks_over_admission(self) -> None:
        fleet = prepared_fleet(max_concurrency=2, credit_budget=2.5)
        fleet = admit_next(fleet, estimated_credits=1.0)
        fleet = admit_next(fleet, estimated_credits=1.0)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.HISTORICAL)
        self.assertEqual(fleet.reserved_credits, 2.0)
        fleet = record_actual_cost(fleet, "ADDRS1", 1.5)
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.HISTORICAL)
        self.assertEqual(get_table(fleet, "ADDRS1").actual_credits, 1.5)
        self.assertEqual(get_table(fleet, "ADDRS1").reserved_credits, 1.5)
        self.assertEqual(get_table(fleet, "CAL001").reserved_credits, 1.0)
        self.assertEqual(fleet.reserved_credits, 2.5)
        self.assertEqual(fleet.consumed_credits, 0.0)
        fleet = pause_table(fleet, "CAL001")
        self.assertEqual(fleet.reserved_credits, 2.5)
        self.assertEqual(admission_candidates(fleet), ())
        with self.assertRaises(FleetError) as refused:
            admit_next(fleet, estimated_credits=0.5)
        self.assertEqual(refused.exception.code, "admission_refused")
        self.assertEqual(get_table(fleet, "COST1").phase, Phase.READY)
        self.assertEqual(get_table(fleet, "CAL001").reserved_credits, 1.0)

    def test_concurrent_actual_plus_other_reservation_overrun(self) -> None:
        fleet = prepared_fleet(max_concurrency=2, credit_budget=2.0)
        fleet = admit_next(fleet, estimated_credits=1.0)
        fleet = admit_next(fleet, estimated_credits=1.0)
        fleet = record_actual_cost(fleet, "ADDRS1", 1.5)
        table_a = get_table(fleet, "ADDRS1")
        table_b = get_table(fleet, "CAL001")
        self.assertEqual(table_a.phase, Phase.BLOCKED)
        self.assertEqual(table_a.blocked_reason, BlockedReason.COST_OVERRUN.value)
        self.assertEqual(table_a.actual_credits, 1.5)
        self.assertEqual(table_a.reserved_credits, 0.0)
        self.assertEqual(table_b.phase, Phase.HISTORICAL)
        self.assertEqual(table_b.reserved_credits, 1.0)
        self.assertIsNone(table_b.actual_credits)
        self.assertEqual(fleet.consumed_credits, 1.5)
        self.assertEqual(fleet.reserved_credits, 1.0)
        self.assertEqual(admission_candidates(fleet), ())
        payload = serialize_fleet(fleet)
        assert_json_safe(self, payload)
        restored = deserialize_fleet(json.loads(json.dumps(payload)))
        self.assertEqual(serialize_fleet(restored), payload)
        self.assertEqual(restored.consumed_credits, 1.5)
        self.assertEqual(restored.reserved_credits, 1.0)
        self.assertEqual(get_table(restored, "ADDRS1").phase, Phase.BLOCKED)
        self.assertEqual(get_table(restored, "ADDRS1").actual_credits, 1.5)
        self.assertEqual(get_table(restored, "CAL001").phase, Phase.HISTORICAL)
        self.assertEqual(get_table(restored, "CAL001").reserved_credits, 1.0)
        self.assertEqual(get_table(restored, "CAL001").actual_credits, None)
        self.assertEqual(admission_candidates(restored), ())

    def test_unknown_cost_is_not_zero(self) -> None:
        fleet = enter_catching_up()
        table = get_table(fleet, "ADDRS1")
        self.assertIsNone(table.actual_credits)
        self.assertNotEqual(table.actual_credits, 0)
        summary = summarize(fleet)
        self.assertEqual(summary.consumed_credits, 0.0)
        self.assertEqual(summary.reserved_credits, DEFAULT_ESTIMATE)
        self.assertIsNone(get_table(fleet, "CAL001").actual_credits)
        self.assertIsNone(get_table(fleet, "CAL001").estimated_credits)
        with self.assertRaises(FleetError):
            record_actual_cost(fleet, "ADDRS1", None)  # type: ignore[arg-type]
        tail = checkpoint(50)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=tail, journal_tail=tail),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)

    def test_certified_table_does_not_occupy_a_backfill_slot(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=100.0)
        fleet = certify_table(enter_reconciling(fleet), "ADDRS1", proof())
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CERTIFIED)
        self.assertEqual(summarize(fleet).running_count, 0)
        fleet = admit_next(fleet, estimated_credits=DEFAULT_ESTIMATE)
        self.assertEqual(get_table(fleet, "CAL001").phase, Phase.HISTORICAL)


class SummaryAndSerializationTests(unittest.TestCase):
    def test_summary_reports_certified_running_known_progress_and_next_action(self) -> None:
        empty = summarize(create_fleet(max_concurrency=1, credit_budget=13))
        self.assertEqual(empty.certified_count, 0)
        self.assertEqual(empty.table_count, 13)
        self.assertEqual(empty.running_count, 0)
        self.assertEqual(empty.admitted_count, 0)
        self.assertIsNone(empty.known_copied_rows)
        self.assertIsNone(empty.known_total_rows)
        self.assertEqual(empty.credit_budget, 13.0)
        self.assertEqual(empty.consumed_credits, 0.0)
        self.assertEqual(empty.reserved_credits, 0.0)
        self.assertFalse(empty.over_budget)
        self.assertEqual(empty.next_action, SafeAction.PREPARE.value)
        self.assertEqual(empty.next_table, "ADDRS1")
        self.assertEqual(empty.next_reason, "missing_start_checkpoint")

        fleet = enter_historical()
        fleet = record_history_progress(fleet, "ADDRS1", copied_rows=40, total_rows=100)
        summary = summarize(fleet)
        self.assertEqual(summary.running_count, 1)
        self.assertEqual(summary.admitted_count, 1)
        self.assertEqual(summary.known_copied_rows, 40)
        self.assertEqual(summary.known_total_rows, 100)
        self.assertEqual(summary.reserved_credits, DEFAULT_ESTIMATE)
        self.assertEqual(summary.consumed_credits, 0.0)
        self.assertEqual(summary.next_action, SafeAction.RECORD_HISTORY_PROGRESS.value)

        certified = certify_table(enter_reconciling(), "ADDRS1", proof())
        done = summarize(certified)
        self.assertEqual(done.certified_count, 1)
        self.assertEqual(done.table_count, 13)
        self.assertEqual(done.running_count, 0)
        self.assertEqual(done.admitted_count, 1)
        self.assertEqual(done.consumed_credits, DEFAULT_ACTUAL)
        self.assertEqual(done.reserved_credits, 0.0)
        self.assertFalse(done.over_budget)
        self.assertEqual(done.next_action, SafeAction.ADMIT_HISTORICAL.value)
        self.assertEqual(done.next_table, "CAL001")

    def test_serialization_is_closed_json_safe_and_rejects_extra_semantics(self) -> None:
        fleet = pause_table(enter_catching_up(), "ADDRS1")
        payload = serialize_fleet(fleet)
        assert_json_safe(self, payload)
        encoded = json.dumps(payload)
        restored = deserialize_fleet(json.loads(encoded))
        self.assertEqual(serialize_fleet(restored), payload)
        self.assertEqual(payload["format_version"], FORMAT_VERSION)
        self.assertEqual(set(payload), {
            "format_version",
            "environment",
            "destination_namespace",
            "max_concurrency",
            "credit_budget",
            "consumed_credits",
            "reserved_credits",
            "tables",
        })
        self.assertEqual(
            set(payload["tables"][0]),
            {
                "name",
                "phase",
                "start_checkpoint",
                "current_checkpoint",
                "journal_tail",
                "receiver_chain",
                "copied_rows",
                "total_rows",
                "continuity_proven",
                "gap",
                "proof_window",
                "paused_from",
                "blocked_reason",
                "admitted",
                "estimated_credits",
                "reserved_credits",
                "actual_credits",
                "reconciliation_proof",
            },
        )
        self.assertIsNone(payload["tables"][0]["reconciliation_proof"])
        extra = json.loads(encoded)
        extra["unexpected"] = True
        with self.assertRaises(FleetError):
            deserialize_fleet(extra)
        missing = json.loads(encoded)
        del missing["tables"]
        with self.assertRaises(FleetError):
            deserialize_fleet(missing)
        mutated = json.loads(encoded)
        mutated["tables"][0]["secret"] = "token"
        with self.assertRaises(FleetError):
            deserialize_fleet(mutated)
        mutated = json.loads(encoded)
        mutated["tables"][0]["start_checkpoint"]["extra"] = 1
        with self.assertRaises(FleetError):
            deserialize_fleet(mutated)
        mutated = json.loads(encoded)
        mutated["tables"] = list(reversed(mutated["tables"]))
        with self.assertRaises(FleetError):
            deserialize_fleet(mutated)
        mutated = json.loads(encoded)
        mutated["tables"][0]["phase"] = "FLYING"
        with self.assertRaises(FleetError):
            deserialize_fleet(mutated)

    def test_serialization_retains_decimal_cost_fields(self) -> None:
        fleet = enter_historical(prepared_fleet(credit_budget=10.75), estimated_credits=1.25)
        payload = serialize_fleet(fleet)
        self.assertEqual(payload["credit_budget"], 10.75)
        self.assertEqual(payload["consumed_credits"], 0.0)
        self.assertEqual(payload["reserved_credits"], 1.25)
        self.assertEqual(payload["tables"][0]["estimated_credits"], 1.25)
        self.assertEqual(payload["tables"][0]["reserved_credits"], 1.25)
        self.assertIsNone(payload["tables"][0]["actual_credits"])
        restored = deserialize_fleet(json.loads(json.dumps(payload)))
        self.assertEqual(restored.credit_budget, 10.75)
        self.assertEqual(restored.reserved_credits, 1.25)
        self.assertEqual(get_table(restored, "ADDRS1").estimated_credits, 1.25)
        live = enter_live(prepared_fleet(credit_budget=10.75), actual_credits=1.25)
        live_payload = serialize_fleet(live)
        self.assertEqual(live_payload["consumed_credits"], 1.25)
        self.assertEqual(live_payload["reserved_credits"], 0.0)
        self.assertEqual(live_payload["tables"][0]["actual_credits"], 1.25)
        self.assertEqual(json.loads(json.dumps(live_payload))["tables"][0]["actual_credits"], 1.25)

    def test_over_budget_serialization_and_summary(self) -> None:
        fleet = prepared_fleet(max_concurrency=1, credit_budget=2.0)
        fleet = enter_catching_up(fleet)
        fleet = record_actual_cost(fleet, "ADDRS1", 9.75)
        payload = serialize_fleet(fleet)
        assert_json_safe(self, payload)
        self.assertEqual(payload["consumed_credits"], 9.75)
        self.assertEqual(payload["credit_budget"], 2.0)
        self.assertGreater(payload["consumed_credits"], payload["credit_budget"])
        self.assertEqual(payload["tables"][0]["actual_credits"], 9.75)
        self.assertEqual(payload["tables"][0]["phase"], Phase.BLOCKED.value)
        self.assertEqual(payload["tables"][0]["blocked_reason"], BlockedReason.COST_OVERRUN.value)
        restored = deserialize_fleet(json.loads(json.dumps(payload)))
        self.assertEqual(serialize_fleet(restored), payload)
        self.assertGreater(restored.consumed_credits, restored.credit_budget)
        self.assertEqual(get_table(restored, "ADDRS1").actual_credits, 9.75)
        summary = summarize(restored)
        self.assertTrue(summary.over_budget)
        self.assertEqual(summary.to_dict()["over_budget"], True)
        self.assertEqual(summary.consumed_credits, 9.75)
        self.assertEqual(summary.next_action, SafeAction.INSPECT_BLOCKED.value)
        self.assertEqual(admission_candidates(restored), ())

    def test_reconciliation_proof_roundtrip_and_inconsistent_proof_rejected(self) -> None:
        certified_proof = proof()
        fleet = certify_table(enter_reconciling(), "ADDRS1", certified_proof)
        table = get_table(fleet, "ADDRS1")
        self.assertEqual(table.reconciliation_proof, certified_proof)
        self.assertEqual(table.proof_window, WINDOW)
        payload = serialize_fleet(fleet)
        assert_json_safe(self, payload)
        proof_payload = payload["tables"][0]["reconciliation_proof"]
        self.assertEqual(
            set(proof_payload),
            {
                "window",
                "source_count",
                "target_count",
                "missing",
                "extra",
                "duplicates",
                "source_hash",
                "target_hash",
                "destination_freshness_seconds",
                "freshness_slo_seconds",
                "latency_seconds",
                "throughput_rows_per_second",
                "cost_units",
            },
        )
        self.assertEqual(proof_payload["source_count"], 10)
        self.assertEqual(proof_payload["target_count"], 10)
        self.assertEqual(proof_payload["missing"], 0)
        self.assertEqual(proof_payload["extra"], 0)
        self.assertEqual(proof_payload["duplicates"], 0)
        self.assertEqual(proof_payload["source_hash"], "sha256:window-a")
        self.assertEqual(proof_payload["target_hash"], "sha256:window-a")
        self.assertEqual(proof_payload["destination_freshness_seconds"], 2.0)
        self.assertEqual(proof_payload["freshness_slo_seconds"], 30.0)
        self.assertEqual(proof_payload["latency_seconds"], 1.5)
        self.assertEqual(proof_payload["throughput_rows_per_second"], 100.0)
        self.assertEqual(proof_payload["cost_units"], DEFAULT_ACTUAL)
        self.assertEqual(proof_payload["window"], {"start_utc": WINDOW.start_utc, "end_utc": WINDOW.end_utc})
        restored = deserialize_fleet(json.loads(json.dumps(payload)))
        self.assertEqual(serialize_fleet(restored), payload)
        self.assertEqual(get_table(restored, "ADDRS1").reconciliation_proof, certified_proof)
        self.assertEqual(get_table(restored, "ADDRS1").phase, Phase.CERTIFIED)

        extra = json.loads(json.dumps(payload))
        extra["tables"][0]["reconciliation_proof"]["secret"] = "token"
        with self.assertRaises(FleetError):
            deserialize_fleet(extra)
        mismatched = json.loads(json.dumps(payload))
        mismatched["tables"][0]["reconciliation_proof"]["window"] = {
            "start_utc": "2026-09-13T12:00:00Z",
            "end_utc": "2026-09-13T13:00:00Z",
        }
        with self.assertRaises(FleetError) as window:
            deserialize_fleet(mismatched)
        self.assertEqual(window.exception.code, "window_mismatch")
        missing_proof = json.loads(json.dumps(payload))
        missing_proof["tables"][0]["reconciliation_proof"] = None
        with self.assertRaises(FleetError) as absent:
            deserialize_fleet(missing_proof)
        self.assertEqual(absent.exception.code, "invalid_reconciliation")
        reconciling = serialize_fleet(enter_reconciling())
        self.assertEqual(reconciling["tables"][0]["phase"], Phase.RECONCILING.value)
        self.assertIsNone(reconciling["tables"][0]["reconciliation_proof"])
        self.assertEqual(
            reconciling["tables"][0]["proof_window"],
            {"start_utc": WINDOW.start_utc, "end_utc": WINDOW.end_utc},
        )
        dropped_window = json.loads(json.dumps(reconciling))
        dropped_window["tables"][0]["proof_window"] = None
        with self.assertRaises(FleetError) as no_window:
            deserialize_fleet(dropped_window)
        self.assertEqual(no_window.exception.code, "invalid_proof_window")

    def test_next_action_does_not_ask_for_cost_before_tail(self) -> None:
        fleet = enter_catching_up()
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)
        self.assertIsNone(get_table(fleet, "ADDRS1").actual_credits)
        summary = summarize(fleet)
        self.assertEqual(summary.next_action, SafeAction.CATCH_UP_TO_TAIL.value)
        self.assertEqual(summary.next_reason, "journal_tail_not_reached")
        self.assertEqual(summary.next_table, "ADDRS1")
        behind = checkpoint(40)
        tail = checkpoint(50)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=behind, journal_tail=tail),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)
        self.assertEqual(summarize(fleet).next_action, SafeAction.CATCH_UP_TO_TAIL.value)
        fleet = record_journal_evidence(
            fleet,
            "ADDRS1",
            **journal_kwargs(current_checkpoint=tail, journal_tail=tail, gap=False),  # type: ignore[arg-type]
        )
        self.assertEqual(get_table(fleet, "ADDRS1").phase, Phase.CATCHING_UP)
        self.assertIsNone(get_table(fleet, "ADDRS1").actual_credits)
        reached = summarize(fleet)
        self.assertEqual(reached.next_action, SafeAction.RECORD_ACTUAL_COST.value)
        self.assertEqual(reached.next_reason, "unknown_cost")


class CompetitorAbsenceTests(unittest.TestCase):
    def test_owned_sources_do_not_contain_the_forbidden_competitor_name(self) -> None:
        forbidden = "Pop" + "sink"
        for path in (FLEET_SOURCE, FLEET_TESTS):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(forbidden, text)
            self.assertNotIn(forbidden.lower(), text)
            self.assertNotIn(forbidden.upper(), text)


if __name__ == "__main__":
    unittest.main()
