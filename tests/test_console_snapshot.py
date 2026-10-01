from __future__ import annotations

import json
from pathlib import Path
import io
from contextlib import redirect_stdout
import tempfile
import unittest

import as400_continuous_capture

from quadringent.console_snapshot import (
    FORMAT_VERSION,
    ConsoleSnapshotBuilder,
    FileSnapshotSink,
    FluxIdentity,
    S3SnapshotSink,
    ThrottledSink,
)
from quadringent.continuous import CaptureMetrics, ReceiverSnapshot
from quadringent.contract import JournalPosition


IDENTITY = FluxIdentity(
    id="sale-rj",
    label="SALE — lecture par RetrieveJournal",
    journal="DEMOJRN",
    journal_library="SALES",
    objects=("SALE",),
    reader_path="RetrieveJournal",
    target="Snowflake DEV · AS400_RD_CANONICAL",
    job="example-corp-rj-final",
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _metrics(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "polls": 1,
        "idle_polls": 0,
        "empty_scans": 0,
        "batches_published": 1,
        "events_published": 100,
        "payload_bytes_published": 4096,
        "receiver_rotations": 0,
        "errors": 0,
        "last_watermark": {"receiver": "DEMOJRN3793", "sequence": 30320399},
        "last_source_tail": {"receiver": "DEMOJRN3793", "sequence": 30320400},
        "last_lag_sequences": 1,
        "last_receiver_first_sequence": 30000000,
        "last_receiver_last_sequence": 30320400,
    }
    base.update(over)
    return base


class ConsoleSnapshotDocumentTests(unittest.TestCase):
    def _builder(self, clock: FakeClock) -> ConsoleSnapshotBuilder:
        return ConsoleSnapshotBuilder(
            identity=IDENTITY, clock=clock, _started_monotonic=clock()
        )

    def test_the_document_is_versioned_and_json_serialisable(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics())

        document = builder.document()
        self.assertEqual(document["format_version"], FORMAT_VERSION)
        self.assertEqual(json.loads(json.dumps(document)), document)
        self.assertEqual(document["flux"]["id"], "sale-rj")

    def test_an_unknown_lag_is_null_with_its_reason_never_zero(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics(last_lag_sequences=None))

        current = builder.document()["lag"]["current"]
        self.assertIsNone(current["value"])
        self.assertIn("receivers disjoints", current["unknown"])

    def test_what_the_worker_cannot_know_is_declared_not_omitted(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics())

        counters = builder.document()["counters"]
        self.assertIsNone(counters["events_in_target"]["value"])
        self.assertIn("n'interroge pas la cible", counters["events_in_target"]["unknown"])
        self.assertIsNone(counters["duplicates_in_target"]["value"])

    def test_cpu_is_unknown_until_the_pilot_reports_it(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics())
        self.assertIsNone(builder.document()["counters"]["mean_mcpu"]["value"])

        clock.advance(1421.0)
        builder.observe_cpu_seconds(42.589538)
        counters = builder.document()["counters"]
        self.assertEqual(counters["mean_mcpu"]["value"], 29.972)
        self.assertEqual(counters["cpu_ms_per_event"]["value"], 425.8954)

    def test_the_verdict_needs_two_samples_and_says_so(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics())

        verdict = builder.document()["lag"]["verdict"]
        self.assertIsNone(verdict["value"])
        self.assertIn("moins de deux", verdict["unknown"])

    def test_a_rising_floor_reaches_the_document_as_DIVERGING(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        for step in range(60):
            clock.advance(10.0)
            builder.observe(None, _metrics(last_lag_sequences=1 + step * 22_963))

        lag = builder.document()["lag"]
        self.assertEqual(lag["verdict"]["value"], "DIVERGING")
        self.assertLess(
            lag["floor_first_third"]["value"], lag["floor_last_third"]["value"]
        )

    def test_a_catch_up_keeps_its_peak_and_collapses_its_floor(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        for step in range(60):
            clock.advance(10.0)
            builder.observe(
                None, _metrics(last_lag_sequences=max(1, 30_320_398 - step * 600_000))
            )

        lag = builder.document()["lag"]
        self.assertEqual(lag["max"]["value"], 30_320_398)
        self.assertEqual(lag["floor_last_third"]["value"], 1)
        self.assertGreater(
            lag["floor_first_third"]["value"], lag["floor_last_third"]["value"]
        )

    def test_a_stopped_run_says_why(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics())
        builder.mark_stopped("STOPPED_FAIL_CLOSED", "5 délais consécutifs")

        run = builder.document()["run"]
        self.assertEqual(run["state"], "STOPPED_FAIL_CLOSED")
        self.assertEqual(run["stopped_because"], "5 délais consécutifs")

    def test_the_last_error_is_carried_so_no_pod_log_is_needed(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(
            None,
            _metrics(
                errors=1,
                last_error_type="SqlWindowTimeout",
                last_error_head="window exceeded 29s",
            ),
        )

        error = builder.document()["run"]["last_error"]
        self.assertEqual(error["type"], "SqlWindowTimeout")
        self.assertEqual(error["head"], "window exceeded 29s")

    def test_the_receiver_window_travels_from_the_capture_metrics(self) -> None:
        clock = FakeClock()
        builder = self._builder(clock)
        builder.observe(None, _metrics())

        position = builder.document()["position"]
        self.assertEqual(position["receiver_first_sequence"], 30_000_000)
        self.assertEqual(position["receiver_last_sequence"], 30_320_400)


class CaptureMetricsReceiverWindowTests(unittest.TestCase):
    """La fenêtre du receiver actif doit remonter jusqu'au snapshot.

    Sans elle, une console peut situer le curseur mais pas dire ce qu'il reste
    à lire avant la prochaine rotation.
    """

    def test_the_active_receiver_window_is_recorded(self) -> None:
        metrics = CaptureMetrics()
        metrics.observe_poll(
            poll_ms=1.0,
            catalog_ms=0.0,
            capture_ms=0.0,
            publish_ms=0.0,
            checkpoint_ms=0.0,
            payload_bytes=0,
            manifest_bytes=0,
            source_tail=JournalPosition("DEMOJRN3776", 900),
            lag_sequences=1,
            receivers=[
                ReceiverSnapshot("SALES", "DEMOJRN3775", 1, 499),
                ReceiverSnapshot("SALES", "DEMOJRN3776", 500, 900),
            ],
        )

        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["last_receiver_first_sequence"], 500)
        self.assertEqual(snapshot["last_receiver_last_sequence"], 900)

    def test_no_catalogue_leaves_the_window_unknown(self) -> None:
        metrics = CaptureMetrics()
        metrics.observe_poll(
            poll_ms=1.0,
            catalog_ms=0.0,
            capture_ms=0.0,
            publish_ms=0.0,
            checkpoint_ms=0.0,
            payload_bytes=0,
            manifest_bytes=0,
            source_tail=JournalPosition("DEMOJRN3776", 900),
            lag_sequences=1,
        )

        snapshot = metrics.snapshot()
        self.assertIsNone(snapshot["last_receiver_first_sequence"])
        self.assertIsNone(snapshot["last_receiver_last_sequence"])


class RecordingSink:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.payloads.append(payload)


class SinkTests(unittest.TestCase):
    def test_a_snapshot_write_failure_is_reported_without_stopping_capture(self) -> None:
        class FailingSink:
            def write(self, payload: bytes) -> None:
                raise RuntimeError("S3 credentials must not reach logs")

            def flush(self, payload: bytes) -> None:
                raise RuntimeError("S3 credentials must not reach logs")

        output = io.StringIO()
        with redirect_stdout(output):
            written = as400_continuous_capture._publish_console_snapshot(
                FailingSink(), b'{"flux":"safe"}'
            )
            final_written = as400_continuous_capture._publish_console_snapshot(
                FailingSink(), b'{"flux":"safe"}', final=True
            )

        self.assertFalse(written)
        self.assertFalse(final_written)
        self.assertEqual(
            [json.loads(line) for line in output.getvalue().splitlines()],
            [
                {"event": "console_snapshot_write_failed", "error_type": "RuntimeError"},
                {"event": "console_snapshot_write_failed", "error_type": "RuntimeError"},
            ],
        )

    def test_the_file_sink_never_exposes_a_half_written_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "console.json"
            FileSnapshotSink(path).write(b'{"a":1}\n')
            FileSnapshotSink(path).write(b'{"a":2}\n')

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2})
            leftovers = [item for item in path.parent.iterdir() if item != path]
            self.assertEqual(leftovers, [])

    def test_throttling_bounds_the_request_bill(self) -> None:
        clock = FakeClock()
        recorder = RecordingSink()
        throttled = ThrottledSink(sink=recorder, interval_s=10.0, clock=clock)

        for _ in range(5):
            throttled.write(b"x")
            clock.advance(1.0)

        self.assertEqual(throttled.writes, 1)
        self.assertEqual(throttled.skipped, 4)

    def test_the_last_state_is_written_whatever_the_interval(self) -> None:
        clock = FakeClock()
        recorder = RecordingSink()
        throttled = ThrottledSink(sink=recorder, interval_s=3600.0, clock=clock)
        throttled.write(b"first")
        throttled.write(b"skipped")
        throttled.flush(b"last")

        self.assertEqual(recorder.payloads, [b"first", b"last"])

    def test_the_s3_sink_overwrites_one_key_and_forbids_caching(self) -> None:
        calls: list[dict[str, object]] = []

        class FakeClient:
            def put_object(self, **kwargs: object) -> None:
                calls.append(kwargs)

        S3SnapshotSink(bucket="b", key="console/sale-rj.json", client=FakeClient()).write(
            b"{}"
        )

        self.assertEqual(calls[0]["Bucket"], "b")
        self.assertEqual(calls[0]["Key"], "console/sale-rj.json")
        self.assertEqual(calls[0]["CacheControl"], "no-store")


if __name__ == "__main__":
    unittest.main()
