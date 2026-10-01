from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import (
    CaptureCircuitOpenError,
    CapturedWindow,
    ContinuousCaptureService,
    IbmiUserDisabledError,
    ReceiverPlanningError,
    ReceiverSnapshot,
    _is_ibmi_userid_disabled,
    finite_tail_bootstrap,
    plan_next_window,
)
from quadringent.source_gate import (
    SourceAuthenticationBlockedError,
    SourceUnavailablePausedError,
)
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.raw import RawBatchWriter
from quadringent.java_catalog import CachedReceiverCatalog


def event(receiver: str, sequence: int) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi",
        journal="TRNJRN",
        library="LEDGER",
        table="CNTR",
        operation="c",
        position=JournalPosition(receiver, sequence),
        commit_timestamp="2026-08-20T10:00:00Z",
        schema_version="sha256:test",
        before=None,
        after={"ID": sequence},
    )


def raw_bytes(
    root: Path,
    *,
    receiver: str,
    sequence: int,
    high_watermark: JournalPosition,
) -> tuple[bytes, bytes]:
    manifest = RawBatchWriter(root).write_batch(
        [event(receiver, sequence)],
        high_watermark=high_watermark,
    )
    payload_path = root / f"batch-{manifest.batch_id}.jsonl"
    manifest_path = root / f"batch-{manifest.batch_id}.manifest.json"
    return manifest_path.read_bytes(), payload_path.read_bytes()


class FakeCatalog:
    def __init__(self, snapshots: list[ReceiverSnapshot]) -> None:
        self.snapshots = snapshots
        self.required: list[str | None] = []

    def snapshot(self, required_receiver: str | None = None) -> list[ReceiverSnapshot]:
        self.required.append(required_receiver)
        return self.snapshots


class FakeRunner:
    def __init__(self, captured: CapturedWindow) -> None:
        self.captured = captured
        self.windows = []

    def capture(self, window):
        self.windows.append(window)
        return self.captured


class ContinuousCaptureTests(unittest.TestCase):
    def test_planner_uses_explicit_receiver_order_for_rotation(self) -> None:
        receivers = [
            ReceiverSnapshot("QGPL", "R2", 100, 110),
            ReceiverSnapshot("QGPL", "R10", 111, 130),
        ]

        plan = plan_next_window(
            JournalPosition("R2", 110),
            receivers,
            max_entries=10,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.rotated)
        self.assertEqual(plan.start, JournalPosition("R10", 111))
        self.assertEqual(plan.end, JournalPosition("R10", 120))
        self.assertEqual(plan.rotated_from, JournalPosition("R2", 110))

    def test_planner_rejects_a_receiver_sequence_gap(self) -> None:
        receivers = [
            ReceiverSnapshot("QGPL", "R2", 100, 110),
            ReceiverSnapshot("QGPL", "R10", 113, 130),
        ]

        with self.assertRaises(ReceiverPlanningError):
            plan_next_window(JournalPosition("R2", 110), receivers, max_entries=10)

    def test_planner_crosses_a_receiver_sequence_reset(self) -> None:
        """A journal sequence reset (weekend maintenance) restarts numbering
        inside the next receiver: the chain is intact, so the window crosses
        by attach order instead of treating it as a purged-receiver hole."""
        receivers = [
            ReceiverSnapshot("QGPL", "DEMOJRN4115", 349833721, 355014589),
            ReceiverSnapshot("QGPL", "DEMOJRN4116", 1, 3391667),
        ]

        plan = plan_next_window(
            JournalPosition("DEMOJRN4115", 355014589), receivers, max_entries=10
        )

        assert plan is not None
        self.assertEqual(plan.start, JournalPosition("DEMOJRN4116", 1))
        self.assertEqual(plan.end, JournalPosition("DEMOJRN4116", 10))
        self.assertEqual(plan.rotated_from, JournalPosition("DEMOJRN4115", 355014589))
        self.assertTrue(plan.sequence_reset)

    def test_tail_last_sequence_bootstraps_a_finite_lookback_not_a_one_seq_wait(self) -> None:
        # ``__TAIL__`` doit toujours calculer un recul borne : c'est
        # finite_tail_bootstrap qui produit ce recul, jamais plan_next_window
        # lui-meme (cf. test_explicit_bootstrap_is_never_moved_backwards).
        receivers = [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]
        bootstrap = finite_tail_bootstrap(receivers, max_entries=11)
        self.assertEqual(bootstrap, JournalPosition("R2", 100))
        self.assertNotEqual(bootstrap.sequence, 110)

        plan = plan_next_window(
            None,
            receivers,
            max_entries=11,
            bootstrap=bootstrap,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.start, JournalPosition("R2", 100))
        self.assertEqual(plan.end, JournalPosition("R2", 110))

        # Un bootstrap explicite egal a last_sequence sur un receiver ATTACHED
        # n'est plus recule : la regle de queue vivante a ete retiree
        # (docs/decisions/2026-09-23-queue-vivante.md), donc la fenetre lit
        # directement cette derniere entree au lieu d'attendre.
        explicit_plan = plan_next_window(
            None,
            receivers,
            max_entries=11,
            bootstrap=JournalPosition("R2", 110),
        )
        self.assertIsNotNone(explicit_plan)
        assert explicit_plan is not None
        self.assertEqual(explicit_plan.start, JournalPosition("R2", 110))
        self.assertEqual(explicit_plan.end, JournalPosition("R2", 110))

    def test_explicit_bootstrap_is_never_moved_backwards(self) -> None:
        cases: list[list[ReceiverSnapshot]] = [
            [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ONLINE")],
            [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")],
            [ReceiverSnapshot("QGPL", "R2", 100, 111, status="ATTACHED")],
        ]
        expected = [
            (JournalPosition("R2", 110), JournalPosition("R2", 110)),
            (JournalPosition("R2", 110), JournalPosition("R2", 110)),
            (JournalPosition("R2", 110), JournalPosition("R2", 111)),
        ]
        for receivers, expectation in zip(cases, expected, strict=True):
            with self.subTest(receivers=receivers):
                plan = plan_next_window(
                    None,
                    receivers,
                    max_entries=11,
                    bootstrap=JournalPosition("R2", 110),
                )
                if expectation is None:
                    self.assertIsNone(plan)
                else:
                    expected_start, expected_end = expectation
                    self.assertIsNotNone(plan)
                    assert plan is not None
                    self.assertEqual(plan.start, expected_start)
                    self.assertEqual(plan.end, expected_end)

    def test_attached_receiver_window_may_end_at_live_last_sequence(self) -> None:
        # La regle de queue vivante a ete retiree
        # (docs/decisions/2026-09-23-queue-vivante.md) : une fenetre peut
        # desormais se terminer exactement a last_sequence d'un receiver
        # ATTACHED, sur les deux chemins de lecture.
        receivers = [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]
        plan = plan_next_window(
            None,
            receivers,
            max_entries=50,
            bootstrap=JournalPosition("R2", 100),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.start, JournalPosition("R2", 100))
        self.assertEqual(plan.end, JournalPosition("R2", 110))

    def test_attached_without_first_sequence_may_reach_live_last(self) -> None:
        receivers = [ReceiverSnapshot("QGPL", "R2", None, 171253025, status="ATTACHED")]
        plan = plan_next_window(
            None,
            receivers,
            max_entries=1000,
            bootstrap=JournalPosition("R2", 171252026),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.end, JournalPosition("R2", 171253025))

    def test_attached_tail_is_idle_only_when_cursor_equals_live_last_sequence(self) -> None:
        # Le curseur est deja a last_sequence : rien de nouveau a lire, donc
        # inactif — a distinguer du cas juste avant (cf.
        # test_isolated_change_on_attached_receiver_is_planned) qui doit
        # desormais produire une fenetre.
        receivers = [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]
        plan = plan_next_window(
            JournalPosition("R2", 110),
            receivers,
            max_entries=50,
        )
        self.assertIsNone(plan)

    def test_isolated_change_on_attached_receiver_is_planned(self) -> None:
        # Regression : une modification isolee sur un receiver ATTACHED doit
        # etre capturee des le poll suivant, sans attendre l'ecriture suivante
        # ni une rotation (docs/decisions/2026-09-23-queue-vivante.md).
        receivers = [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]
        plan = plan_next_window(
            JournalPosition("R2", 109),
            receivers,
            max_entries=50,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.start, JournalPosition("R2", 110))
        self.assertEqual(plan.end, JournalPosition("R2", 110))

    def test_explicit_bootstrap_at_attached_last_sequence_reads_it(self) -> None:
        receivers = [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]
        plan = plan_next_window(
            None,
            receivers,
            max_entries=11,
            bootstrap=JournalPosition("R2", 110),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.start, JournalPosition("R2", 110))
        self.assertEqual(plan.end, JournalPosition("R2", 110))

    def test_transition_into_attached_receiver_reads_its_last_sequence(self) -> None:
        receivers = [
            ReceiverSnapshot("L", "R1", 1, 161, status="ONLINE"),
            ReceiverSnapshot("L", "R2", 162, 168, status="ONLINE"),
            ReceiverSnapshot("L", "R3", 169, 169, status="ATTACHED"),
        ]
        plan = plan_next_window(JournalPosition("R2", 168), receivers, max_entries=50)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertTrue(plan.rotated)
        self.assertEqual(plan.start, JournalPosition("R3", 169))
        self.assertEqual(plan.end, JournalPosition("R3", 169))

        # Une fois la seule entree du receiver R3 consommee, plus rien a lire.
        idle_plan = plan_next_window(JournalPosition("R3", 169), receivers, max_entries=50)
        self.assertIsNone(idle_plan)

    def test_online_receiver_window_may_include_declared_last_sequence(self) -> None:
        receivers = [
            ReceiverSnapshot("QGPL", "R2", 100, 110, status="ONLINE"),
            ReceiverSnapshot("QGPL", "R10", 111, 130, status="ATTACHED"),
        ]
        plan = plan_next_window(
            None,
            receivers,
            max_entries=20,
            bootstrap=JournalPosition("R2", 100),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.start, JournalPosition("R2", 100))
        self.assertEqual(plan.end, JournalPosition("R2", 110))

    def test_in_range_bootstrap_empty_scan_does_not_use_tail_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            receivers = [ReceiverSnapshot("DEMOLIB", "DEMOJRN3760", 163410000, 163416209)]
            bootstrap = finite_tail_bootstrap(receivers, max_entries=2000)
            runner = FakeRunner(CapturedWindow(scanned_to=JournalPosition(bootstrap.receiver, bootstrap.sequence + 1999)))
            # scanned_to must match planned end
            plan = plan_next_window(None, receivers, max_entries=2000, bootstrap=bootstrap)
            assert plan is not None
            runner.captured = CapturedWindow(scanned_to=plan.end)
            service = ContinuousCaptureService(
                FakeCatalog(receivers),
                runner,
                coordinator,
                checkpoint,
                max_entries=2000,
                bootstrap=bootstrap,
                sleep=lambda _: None,
            )
            result = service.run_once()
            self.assertEqual(result.status, "empty_scan")
            self.assertEqual(runner.windows[0].start.sequence, bootstrap.sequence)
            self.assertLessEqual(runner.windows[0].end.sequence, 163416209)
            self.assertEqual(checkpoint.load(), plan.end)

    def test_run_once_anchors_the_catalog_on_the_checkpoint_receiver(self) -> None:
        """Regression 2026-09-20: the durable checkpoint sat on DEMOJRN4115 while
        the bounded catalog query only returned the newest eight receivers
        (DEMOJRN4143..4150) after a weekend of rotations. The service stopped on
        'checkpoint receiver is missing from metadata' even though DEMOJRN4115
        was still ONLINE. The snapshot request must carry the checkpoint
        receiver so the catalog can anchor the span instead of a tail window.
        """
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            checkpoint.commit(JournalPosition("DEMOJRN4115", 355013677))
            receivers = [
                ReceiverSnapshot("DEMOLIB", "DEMOJRN4115", 349833721, 355014589),
                ReceiverSnapshot("DEMOLIB", "DEMOJRN4116", 355014590, 355020000),
            ]
            catalog = FakeCatalog(receivers)
            service = ContinuousCaptureService(
                catalog,
                FakeRunner(CapturedWindow(scanned_to=JournalPosition("DEMOJRN4115", 355013687))),
                coordinator,
                checkpoint,
                max_entries=10,
                sleep=lambda _: None,
            )
            result = service.run_once()
            self.assertEqual(catalog.required, ["DEMOJRN4115"])
            self.assertNotEqual(result.status, "failed")

    def test_run_once_reports_a_purged_checkpoint_receiver_explicitly(self) -> None:
        """A checkpoint whose receiver was purged from the journal chain must
        fail closed with an actionable error — never silently skip entries."""
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            checkpoint.commit(JournalPosition("DEMOJRN3866", 159602291))
            catalog = FakeCatalog([])  # receiver absent from the chain
            service = ContinuousCaptureService(
                catalog,
                FakeRunner(CapturedWindow(scanned_to=JournalPosition("R2", 110))),
                coordinator,
                checkpoint,
                max_entries=10,
                sleep=lambda _: None,
            )
            with self.assertRaisesRegex(
                ReceiverPlanningError,
                "DEMOJRN3866 is absent from the journal chain",
            ):
                service.run_once()

    def test_run_once_without_checkpoint_requests_a_tail_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            receivers = [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]
            catalog = FakeCatalog(receivers)
            runner = FakeRunner(CapturedWindow(scanned_to=JournalPosition("R2", 109)))
            service = ContinuousCaptureService(
                catalog,
                runner,
                coordinator,
                checkpoint,
                max_entries=10,
                bootstrap=JournalPosition("R2", 100),
                sleep=lambda _: None,
            )
            service.run_once()
            self.assertEqual(catalog.required, [None])

    def test_empty_scan_advances_cursor_without_creating_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(Path(directory) / "raw"), checkpoint)
            runner = FakeRunner(
                CapturedWindow(scanned_to=JournalPosition("R2", 110))
            )
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                runner,
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                sleep=lambda _: None,
            )

            result = service.run_once()

            self.assertEqual(result.status, "empty_scan")
            self.assertEqual(checkpoint.load(), JournalPosition("R2", 110))
            self.assertEqual(list((Path(directory) / "raw").iterdir()), [])
            self.assertEqual(service.metrics.snapshot()["empty_scans"], 1)

    def test_raw_is_published_before_checkpoint_and_scanned_end_is_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            store = FileObjectStore(root / "raw")
            coordinator = RawFirstCaptureCoordinator(store, checkpoint)
            manifest, payload = raw_bytes(
                root / "staging",
                receiver="R2",
                sequence=105,
                high_watermark=JournalPosition("R2", 110),
            )
            runner = FakeRunner(
                CapturedWindow(
                    scanned_to=JournalPosition("R2", 110),
                    manifest=manifest,
                    payload=payload,
                )
            )
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                runner,
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                sleep=lambda _: None,
            )

            result = service.run_once()

            self.assertEqual(result.status, "published")
            self.assertEqual(checkpoint.load(), JournalPosition("R2", 110))
            self.assertEqual(service.metrics.snapshot()["events_published"], 1)
            self.assertEqual(service.metrics.snapshot()["batches_published"], 1)
            self.assertEqual(len(list((root / "raw").glob("*.manifest.json"))), 1)

    def test_metrics_expose_stage_timings_payload_and_same_receiver_lag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            manifest, payload = raw_bytes(
                root / "staging",
                receiver="R2",
                sequence=105,
                high_watermark=JournalPosition("R2", 110),
            )
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                FakeRunner(
                    CapturedWindow(
                        scanned_to=JournalPosition("R2", 110),
                        manifest=manifest,
                        payload=payload,
                    )
                ),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                sleep=lambda _: None,
            )

            service.run_once()

            metrics = service.metrics.snapshot()
            last_poll = metrics["last_poll"]
            assert isinstance(last_poll, dict)
            for field in (
                "poll_ms",
                "catalog_ms",
                "capture_ms",
                "publish_ms",
                "checkpoint_ms",
            ):
                self.assertGreaterEqual(last_poll[field], 0)
            self.assertEqual(last_poll["payload_bytes"], len(payload))
            self.assertEqual(last_poll["manifest_bytes"], len(manifest))
            self.assertEqual(
                last_poll["source_tail"],
                {"receiver": "R2", "sequence": 110},
            )
            self.assertEqual(last_poll["lag_sequences"], 0)
            self.assertEqual(metrics["payload_bytes_published"], len(payload))
            self.assertEqual(metrics["manifest_bytes_published"], len(manifest))

    def test_receiver_rotation_uses_transition_after_raw_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            checkpoint.commit(JournalPosition("R2", 110))
            store = FileObjectStore(root / "raw")
            coordinator = RawFirstCaptureCoordinator(store, checkpoint)
            manifest, payload = raw_bytes(
                root / "staging",
                receiver="R10",
                sequence=115,
                high_watermark=JournalPosition("R10", 120),
            )
            runner = FakeRunner(
                CapturedWindow(
                    scanned_to=JournalPosition("R10", 120),
                    manifest=manifest,
                    payload=payload,
                )
            )
            service = ContinuousCaptureService(
                FakeCatalog(
                    [
                        ReceiverSnapshot("QGPL", "R2", 100, 110),
                        ReceiverSnapshot("QGPL", "R10", 111, 120),
                    ]
                ),
                runner,
                coordinator,
                checkpoint,
                max_entries=11,
                sleep=lambda _: None,
            )

            result = service.run_once()

            self.assertEqual(result.status, "published")
            self.assertEqual(checkpoint.load(), JournalPosition("R10", 120))
            self.assertEqual(service.metrics.snapshot()["receiver_rotations"], 1)

    def test_catch_up_uses_larger_windows_when_lag_exceeds_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint.commit(JournalPosition("R2", 100))
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"),
                checkpoint,
            )
            runner = FakeRunner(CapturedWindow(scanned_to=JournalPosition("R2", 150)))
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 200)]),
                runner,
                coordinator,
                checkpoint,
                max_entries=10,
                catch_up_max_entries=50,
                sleep=lambda _: None,
            )

            result = service.run_once()

            self.assertEqual(result.status, "empty_scan")
            self.assertEqual(runner.windows[0].start, JournalPosition("R2", 101))
            self.assertEqual(runner.windows[0].end, JournalPosition("R2", 150))
            self.assertEqual(checkpoint.load(), JournalPosition("R2", 150))

    def test_malformed_runner_artifact_does_not_advance_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            runner = FakeRunner(
                CapturedWindow(
                    scanned_to=JournalPosition("R2", 110),
                    manifest=b"{}",
                    payload=b"not-jsonl",
                )
            )
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                runner,
                coordinator,
                checkpoint,
                max_entries=10,
                bootstrap=JournalPosition("R2", 100),
                sleep=lambda _: None,
            )

            with self.assertRaises(ValueError):
                service.run_once()

            self.assertIsNone(checkpoint.load())
            self.assertEqual(service.metrics.snapshot()["errors"], 1)

    def test_run_sleeps_on_idle_and_empty_tail_but_not_on_published_or_catch_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            sleeps: list[float] = []

            class EmptyRunner:
                def capture(self, window):
                    return CapturedWindow(scanned_to=window.end)

            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 130)]),
                EmptyRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=5.0,
                sleep=sleeps.append,
            )

            service.run(max_polls=4)

            self.assertEqual(service.metrics.empty_scans, 3)
            self.assertEqual(service.metrics.idle_polls, 1)
            # Attente adaptative : au plancher (1 s) faute d'oisiveté
            # consécutive avant ce premier sommeil.
            self.assertEqual(sleeps, [1.0])

    def test_run_does_not_sleep_after_a_published_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            manifest, payload = raw_bytes(
                root / "staging",
                receiver="R2",
                sequence=105,
                high_watermark=JournalPosition("R2", 110),
            )
            sleeps: list[float] = []
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                FakeRunner(
                    CapturedWindow(
                        scanned_to=JournalPosition("R2", 110),
                        manifest=manifest,
                        payload=payload,
                    )
                ),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=5.0,
                sleep=sleeps.append,
            )

            service.run(max_polls=3)

            self.assertEqual(service.metrics.batches_published, 1)
            self.assertEqual(service.metrics.idle_polls, 2)
            self.assertEqual(sleeps, [1.0])

    def test_run_adaptive_idle_wait_doubles_up_to_poll_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint.commit(JournalPosition("R2", 110))
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"),
                checkpoint,
            )
            sleeps: list[float] = []
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                FakeRunner(CapturedWindow(scanned_to=JournalPosition("R2", 110))),
                coordinator,
                checkpoint,
                max_entries=11,
                poll_seconds=5.0,
                sleep=sleeps.append,
            )

            service.run(max_polls=6)

            # Croissance par doublement à partir du plancher (1 s), plafonnée
            # à poll_seconds (5 s) ; le dernier poll (max_polls atteint) ne
            # dort pas.
            self.assertEqual(sleeps, [1.0, 2.0, 4.0, 5.0, 5.0])

    def test_run_adaptive_idle_wait_resets_after_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            manifest, payload = raw_bytes(
                root / "staging",
                receiver="R2",
                sequence=105,
                high_watermark=JournalPosition("R2", 110),
            )

            class OnceThenIdleRunner:
                def __init__(self) -> None:
                    self.calls = 0

                def capture(self, window):
                    self.calls += 1
                    if self.calls == 1:
                        return CapturedWindow(
                            scanned_to=JournalPosition("R2", 110),
                            manifest=manifest,
                            payload=payload,
                        )
                    return CapturedWindow(scanned_to=window.end)

            sleeps: list[float] = []
            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 110)]),
                OnceThenIdleRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=5.0,
                sleep=sleeps.append,
            )

            service.run(max_polls=4)

            # Poll 1 publie (pas de sommeil, réinitialise le plancher) ;
            # polls 2-3 sont oisifs et repartent bien à 1 s puis doublent.
            self.assertEqual(sleeps, [1.0, 2.0])

    def test_idle_poll_invalidates_cached_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint.commit(JournalPosition("R2", 110))
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"),
                checkpoint,
            )

            class CountingCatalog:
                def __init__(self) -> None:
                    self.snapshots = 0
                    self.invalidations = 0

                def snapshot(self, required_receiver=None):
                    self.snapshots += 1
                    return [ReceiverSnapshot("QGPL", "R2", 100, 110)]

                def invalidate(self) -> None:
                    self.invalidations += 1

            catalog = CountingCatalog()
            service = ContinuousCaptureService(
                catalog,
                FakeRunner(CapturedWindow(scanned_to=JournalPosition("R2", 110))),
                coordinator,
                checkpoint,
                max_entries=11,
                sleep=lambda _: None,
            )

            result = service.run_once()

            self.assertEqual(result.status, "idle")
            self.assertEqual(catalog.snapshots, 1)
            self.assertEqual(catalog.invalidations, 1)

    def test_idle_poll_retains_receiver_cache_when_tail_probe_tracks_new_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint.commit(JournalPosition("R2", 110))
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(Path(directory) / "raw"), checkpoint)

            class CountingCatalog:
                calls = 0

                def snapshot(self, required_receiver=None):
                    self.calls += 1
                    return [ReceiverSnapshot("QGPL", "R2", 100, 110, status="ATTACHED")]

            inner = CountingCatalog()
            tail = [110]
            catalog = CachedReceiverCatalog(
                inner, ttl_polls=30, ttl_seconds=60, clock=lambda: 0.0,
                tail_probe=lambda: ReceiverSnapshot("QGPL", "R2", 100, tail[0], status="ATTACHED"),
            )
            service = ContinuousCaptureService(
                catalog, FakeRunner(CapturedWindow(scanned_to=JournalPosition("R2", 111))),
                coordinator, checkpoint, max_entries=11, sleep=lambda _: None,
            )

            self.assertEqual(service.run_once().status, "idle")
            self.assertEqual(service.run_once().status, "idle")
            tail[0] = 111
            self.assertEqual(service.run_once().status, "empty_scan")
            self.assertEqual(inner.calls, 1)
            self.assertEqual(checkpoint.load(), JournalPosition("R2", 111))

    def test_run_recovers_from_reader_timeout_without_advancing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            sleeps: list[float] = []
            statuses: list[str] = []
            error_types: list[str] = []
            error_heads: list[str] = []

            class FlakyRunner:
                def __init__(self) -> None:
                    self.calls = 0

                def capture(self, window):
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("bounded IBM i reader timed out")
                    return CapturedWindow(scanned_to=window.end)

            class CountingCatalog:
                def __init__(self) -> None:
                    self.invalidations = 0

                def snapshot(self, required_receiver=None):
                    return [ReceiverSnapshot("QGPL", "R2", 100, 130)]

                def invalidate(self) -> None:
                    self.invalidations += 1

            catalog = CountingCatalog()
            service = ContinuousCaptureService(
                catalog,
                FlakyRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=5.0,
                sleep=sleeps.append,
            )

            def _observe(result, observed_metrics: dict[str, object]) -> None:
                statuses.append(result.status)
                error_type = observed_metrics.get("last_error_type")
                if isinstance(error_type, str):
                    error_types.append(error_type)
                error_head = observed_metrics.get("last_error_head")
                if isinstance(error_head, str):
                    error_heads.append(error_head)

            metrics = service.run(max_polls=3, on_result=_observe)

            self.assertEqual(statuses, ["error", "empty_scan", "empty_scan"])
            self.assertEqual(metrics["errors"], 1)
            self.assertEqual(error_types, ["RuntimeError"])
            self.assertEqual(error_heads, ["bounded IBM i reader timed out"])
            self.assertEqual(metrics["empty_scans"], 2)
            self.assertEqual(checkpoint.load(), JournalPosition("R2", 121))
            self.assertEqual(catalog.invalidations, 1)
            self.assertEqual(sleeps, [5.0])

    def test_run_stops_immediately_when_ibmi_userid_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            sleeps: list[float] = []
            statuses: list[str] = []

            class DisabledRunner:
                def capture(self, window):
                    raise RuntimeError("SQLNonTransientConnectionException User ID is disabled.:CDCUSER")

            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 130)]),
                DisabledRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=30.0,
                sleep=sleeps.append,
            )

            def _observe(result, observed_metrics: dict[str, object]) -> None:
                statuses.append(result.status)

            with self.assertRaises(RuntimeError):
                service.run(max_polls=6, on_result=_observe)

            self.assertEqual(statuses, ["error"])
            self.assertEqual(sleeps, [])
            self.assertIsNone(checkpoint.load())

    def test_userid_disabled_matches_jtopen_class_names(self) -> None:
        self.assertTrue(
            _is_ibmi_userid_disabled(
                RuntimeError("IBM i receiver catalog failed:AS400SecurityException")
            )
        )
        self.assertTrue(
            _is_ibmi_userid_disabled(
                RuntimeError("connect_error=USER_DISABLED")
            )
        )
        self.assertTrue(_is_ibmi_userid_disabled(IbmiUserDisabledError("disabled")))
        # SQLNonTransientConnectionException couvre aussi une source coupée ou
        # en maintenance : sans marqueur auth explicite, ce n'est pas un blocage.
        self.assertFalse(
            _is_ibmi_userid_disabled(
                RuntimeError("bounded IBM i reader failed:SQLNonTransientConnectionException")
            )
        )
        self.assertFalse(
            _is_ibmi_userid_disabled(RuntimeError("bounded IBM i reader timed out"))
        )
        self.assertFalse(
            _is_ibmi_userid_disabled(RuntimeError("bounded IBM i reader exited"))
        )

    def test_run_stops_on_jtopen_security_class_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
            sleeps: list[float] = []

            class DisabledRunner:
                def capture(self, window):
                    raise RuntimeError(
                        "IBM i receiver catalog failed:AS400SecurityException"
                    )

            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 130)]),
                DisabledRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=30.0,
                sleep=sleeps.append,
            )

            with self.assertRaises(RuntimeError):
                service.run(max_polls=6)

            self.assertEqual(sleeps, [])
            self.assertIsNone(checkpoint.load())

    def test_run_opens_the_circuit_after_consecutive_source_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(root / "raw"), checkpoint
            )
            sleeps: list[float] = []

            class BrokenRunner:
                def capture(self, window):
                    raise RuntimeError("bounded IBM i reader timed out")

            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 130)]),
                BrokenRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=5.0,
                sleep=sleeps.append,
            )

            with self.assertRaisesRegex(
                CaptureCircuitOpenError,
                "3 consecutive capture errors",
            ):
                service.run(max_polls=20, max_consecutive_errors=3)

            self.assertEqual(service.metrics.errors, 3)
            self.assertEqual(sleeps, [5.0, 5.0])
            self.assertIsNone(checkpoint.load())

    def test_successful_poll_resets_the_consecutive_error_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(root / "raw"), checkpoint
            )

            class IntermittentRunner:
                def __init__(self) -> None:
                    self.calls = 0

                def capture(self, window):
                    self.calls += 1
                    if self.calls in {1, 2, 4, 5}:
                        raise RuntimeError("bounded IBM i reader timed out")
                    return CapturedWindow(scanned_to=window.end)

            service = ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 200)]),
                IntermittentRunner(),
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=0,
                sleep=lambda _: None,
            )

            metrics = service.run(max_polls=5, max_consecutive_errors=3)

            self.assertEqual(metrics["errors"], 4)
            self.assertEqual(checkpoint.load(), JournalPosition("R2", 110))


class SourcePauseTests(unittest.TestCase):
    """La pause source est un état, pas une erreur de capture : la boucle
    attend l'échéance sans sign-on puis reprend sur le checkpoint durable."""

    def _service(self, runner, clock, sleep):
        root = Path(tempfile.mkdtemp())
        checkpoint = JsonCheckpointStore(root / "checkpoint.json")
        coordinator = RawFirstCaptureCoordinator(FileObjectStore(root / "raw"), checkpoint)
        return (
            ContinuousCaptureService(
                FakeCatalog([ReceiverSnapshot("QGPL", "R2", 100, 200)]),
                runner,
                coordinator,
                checkpoint,
                max_entries=11,
                bootstrap=JournalPosition("R2", 100),
                poll_seconds=0,
                sleep=sleep,
                utc_now=lambda: clock[0],
            ),
            checkpoint,
        )

    def test_pause_waits_for_retry_after_then_resumes(self) -> None:
        clock = [datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)]
        sleeps: list[float] = []
        statuses: list[str] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)

        class MaintenanceRunner:
            def __init__(self) -> None:
                self.calls = 0

            def capture(self, window):
                self.calls += 1
                if self.calls == 1:
                    raise SourceUnavailablePausedError(
                        "source en pause",
                        retry_after=clock[0] + timedelta(seconds=150),
                        reason_code="SOURCE_UNAVAILABLE",
                    )
                return CapturedWindow(scanned_to=window.end)

        service, checkpoint = self._service(MaintenanceRunner(), clock, sleep)
        metrics = service.run(
            max_polls=2,
            on_result=lambda result, m: statuses.append(result.status),
        )

        # 150 s d'attente en tranches bornées, puis reprise sans erreur.
        self.assertEqual(sleeps, [60.0, 60.0, 30.0])
        self.assertEqual(statuses, ["source_paused", "empty_scan", "empty_scan"])
        # La tentative qui a ouvert la pause est comptée, l'attente ne l'est pas.
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(checkpoint.load(), JournalPosition("R2", 121))

    def test_stop_during_pause_exits_cleanly(self) -> None:
        clock = [datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)]

        class DownRunner:
            def capture(self, window):
                raise SourceUnavailablePausedError(
                    "source en pause",
                    retry_after=clock[0] + timedelta(hours=4),
                    reason_code="SOURCE_UNAVAILABLE",
                )

        service, _ = self._service(DownRunner(), clock, lambda s: None)
        metrics = service.run(max_polls=5, stop=lambda: True)
        self.assertEqual(metrics["polls"], 0)

    def test_authentication_blocked_propagates_immediately(self) -> None:
        clock = [datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)]
        sleeps: list[float] = []

        class RefusedRunner:
            def capture(self, window):
                raise SourceAuthenticationBlockedError("gate blocked")

        service, checkpoint = self._service(RefusedRunner(), clock, sleeps.append)
        with self.assertRaises(SourceAuthenticationBlockedError):
            service.run(max_polls=10)
        self.assertEqual(sleeps, [])
        self.assertIsNone(checkpoint.load())


if __name__ == "__main__":
    unittest.main()
