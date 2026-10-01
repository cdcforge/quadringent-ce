from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from threading import Event, Lock, Thread
import unittest
from unittest.mock import patch

from quadringent_control_plane.repository import (
    MAX_DOCUMENT_BYTES,
    ProjectionRepository,
    _read_document,
    parse_source_spec,
)


NOW = datetime.now(timezone.utc)


def document(*, counter: int = 120) -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": counter}},
    }


class ProjectionRepositoryTests(unittest.TestCase):
    def test_s3_source_is_explicit_canonical_and_environment_scoped(self) -> None:
        source = parse_source_spec(
            "live:dev-sale:s3://example-corp-int-example-corp/console/sale.json",
            environment="dev",
        )

        self.assertEqual(
            source.descriptor.origin,
            "s3://example-corp-int-example-corp/console/sale.json",
        )
        for invalid in (
            "live:dev:s3://bucket",
            "live:dev:s3://bucket/../escape.json",
            "live:dev:s3://user:secret@bucket/key.json",
            "live:dev:s3://bucket/key.json?versionId=secret",
            "live:dev:s3:///missing-bucket.json",
        ):
            with self.assertRaises(ValueError):
                parse_source_spec(invalid)

    def test_s3_reader_heads_then_reads_a_bounded_object(self) -> None:
        raw = json.dumps(document()).encode("utf-8")

        class Body:
            def __init__(self) -> None:
                self.read_sizes: list[int] = []

            def read(self, size: int) -> bytes:
                self.read_sizes.append(size)
                return raw

        class S3:
            def __init__(self) -> None:
                self.body = Body()
                self.calls: list[tuple[str, dict[str, object]]] = []

            def head_object(self, **kwargs: object) -> dict[str, object]:
                self.calls.append(("head", kwargs))
                return {"ContentLength": len(raw)}

            def get_object(self, **kwargs: object) -> dict[str, object]:
                self.calls.append(("get", kwargs))
                return {"Body": self.body}

        client = S3()
        with patch(
            "quadringent_control_plane.repository._s3_client", return_value=client
        ):
            decoded = _read_document("s3://example-corp-int-example-corp/console/sale.json")

        self.assertEqual(decoded["format_version"], "as400-console-v1")
        self.assertEqual(client.calls, [
            (
                "head",
                {"Bucket": "example-corp-int-example-corp", "Key": "console/sale.json"},
            ),
            (
                "get",
                {
                    "Bucket": "example-corp-int-example-corp",
                    "Key": "console/sale.json",
                    "Range": f"bytes=0-{MAX_DOCUMENT_BYTES}",
                },
            ),
        ])
        self.assertEqual(client.body.read_sizes, [MAX_DOCUMENT_BYTES + 1])

    def test_oversized_s3_snapshot_is_rejected_before_get(self) -> None:
        class S3:
            def head_object(self, **_: object) -> dict[str, object]:
                return {"ContentLength": MAX_DOCUMENT_BYTES + 1}

            def get_object(self, **_: object) -> dict[str, object]:
                raise AssertionError("un objet trop grand ne doit pas être téléchargé")

        with patch(
            "quadringent_control_plane.repository._s3_client", return_value=S3()
        ):
            with self.assertRaises(ValueError):
                _read_document("s3://example-corp-int-example-corp/console/sale.json")

    def test_overlapping_refreshes_cannot_publish_an_old_failure_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            repository = ProjectionRepository(
                [parse_source_spec(f"live:dev-cntr:file://{path}")]
            )
            repository.refresh()

            slow_failure_started = Event()
            release_slow_failure = Event()
            recent_refresh_attempted = Event()
            recent_refresh_finished = Event()
            snapshot_finished = Event()
            call_lock = Lock()
            calls = 0

            def controlled_read(_: str) -> dict[str, object]:
                nonlocal calls
                with call_lock:
                    calls += 1
                    call = calls
                if call == 1:
                    slow_failure_started.set()
                    if not release_slow_failure.wait(timeout=2):
                        raise AssertionError("Le test n'a pas libéré le refresh lent")
                    raise OSError("erreur source sûre pour le test")
                return document(counter=999)

            def refresh_recent() -> None:
                recent_refresh_attempted.set()
                repository.refresh()
                recent_refresh_finished.set()

            def read_snapshot() -> None:
                repository.snapshot()
                snapshot_finished.set()

            with patch(
                "quadringent_control_plane.repository._read_document",
                side_effect=controlled_read,
            ):
                slow = Thread(target=repository.refresh)
                recent = Thread(target=refresh_recent)
                snapshot_reader = Thread(target=read_snapshot)
                slow.start()
                self.assertTrue(slow_failure_started.wait(timeout=1))
                snapshot_reader.start()
                snapshot_did_not_wait_for_io = snapshot_finished.wait(timeout=0.25)
                recent.start()
                self.assertTrue(recent_refresh_attempted.wait(timeout=1))

                # Sans sérialisation, le succès récent publie avant que l'ancien
                # échec soit libéré et celui-ci l'écrase ensuite.
                recent_refresh_finished.wait(timeout=0.25)
                release_slow_failure.set()
                slow.join(timeout=2)
                recent.join(timeout=2)
                snapshot_reader.join(timeout=2)

        self.assertFalse(slow.is_alive())
        self.assertFalse(recent.is_alive())
        self.assertFalse(snapshot_reader.is_alive())
        self.assertTrue(snapshot_did_not_wait_for_io)
        final = repository.snapshot()
        self.assertEqual(final.sources[0].status, "available")
        self.assertEqual(final.pipelines[0].status, "degraded")
        self.assertEqual(final.pipelines[0].counters["events_published"], 999)

    def test_retained_pipeline_shares_no_nested_objects_with_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.json"
            incident = document()
            incident["run"] = {
                "state": "STOPPED_FAIL_CLOSED",
                "last_error": {"type": "JdbcFailure"},
            }
            incident["lag"] = {
                "current": {"value": 1},
                "verdict": {"value": "STABLE"},
                "series": {
                    "resolution_s": 5.0,
                    "sample_count": 1,
                    "unknown_sample_count": 0,
                    "buckets": [
                        {
                            "start_s": 0.0,
                            "end_s": 4.0,
                            "min": 1,
                            "max": 1,
                            "last": 1,
                            "samples": 1,
                            "unknown_samples": 0,
                        }
                    ],
                },
            }
            path.write_text(json.dumps(incident), encoding="utf-8")
            repository = ProjectionRepository(
                [parse_source_spec(f"live:dev-cntr:file://{path}")]
            )
            observed = repository.refresh()
            original = observed.pipelines[0]
            self.assertIsInstance(original.incident, dict)
            original.incident["context"] = {"labels": ["historique"]}

            path.unlink()
            unavailable = repository.refresh()

        retained = unavailable.pipelines[0]
        self.assertIsNot(retained, original)
        self.assertIsNot(retained.quality, original.quality)
        self.assertIsNot(retained.counters, original.counters)
        self.assertIsNot(retained.incident, original.incident)
        self.assertIsNot(retained.stages, original.stages)
        for retained_stage, original_stage in zip(retained.stages, original.stages):
            self.assertIsNot(retained_stage, original_stage)
        self.assertIsNotNone(retained.lag_series)
        self.assertIsNot(retained.lag_series, original.lag_series)
        self.assertIsNot(retained.lag_series.buckets, original.lag_series.buckets)
        self.assertIsNot(
            retained.lag_series.buckets[0], original.lag_series.buckets[0]
        )

        retained.quality["coverage"] = "mutée"
        retained.counters["events_published"] = -1
        retained.incident["context"]["labels"].append("mutée")

        self.assertEqual(original.quality["coverage"], "partial")
        self.assertEqual(original.counters["events_published"], 120)
        self.assertEqual(original.incident["context"]["labels"], ["historique"])

    def test_refresh_increments_revision_only_when_projection_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            repository = ProjectionRepository([parse_source_spec(f"live:dev-cntr:file://{path}")])

            first = repository.refresh()
            second = repository.refresh()
            path.write_text(json.dumps(document(counter=121)), encoding="utf-8")
            third = repository.refresh()

        self.assertEqual(second.revision, first.revision)
        self.assertEqual(second.to_dict(), first.to_dict())
        self.assertEqual(third.revision, first.revision + 1)
        self.assertEqual(third.pipelines[0].counters["events_published"], 121)

    def test_failed_source_remains_visible_without_raw_origin_or_payload(self) -> None:
        source = parse_source_spec("simulation:demo:file:///path/that/does/not/exist.json")

        snapshot = ProjectionRepository([source]).refresh()

        self.assertEqual(snapshot.sources[0].status, "unavailable")
        self.assertEqual(snapshot.sources[0].id, "demo")
        self.assertEqual(snapshot.sources[0].error, "source_refresh_failed")
        self.assertEqual(snapshot.pipelines, ())
        self.assertNotIn("/path/that/does/not/exist", json.dumps(snapshot.to_dict()))

    def test_refresh_failure_retains_last_incident_as_unknown_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.json"
            incident = document()
            incident["run"] = {
                "state": "STOPPED_FAIL_CLOSED",
                "last_error": {"type": "JdbcFailure", "password": "never-output"},
            }
            path.write_text(json.dumps(incident), encoding="utf-8")
            repository = ProjectionRepository(
                [parse_source_spec(f"live:dev-cntr:file://{path}", environment="DEV")]
            )

            observed = repository.refresh()
            path.unlink()
            unavailable = repository.refresh()

        self.assertEqual(observed.pipelines[0].status, "incident")
        self.assertEqual(unavailable.revision, observed.revision + 1)
        self.assertEqual(len(unavailable.pipelines), 1)
        retained = unavailable.pipelines[0]
        self.assertEqual(retained.status, "unknown")
        self.assertEqual(retained.quality["freshness"], "stale")
        self.assertEqual(retained.observed_at, observed.pipelines[0].observed_at)
        self.assertEqual(retained.incident, observed.pipelines[0].incident)
        self.assertEqual(retained.counters, observed.pipelines[0].counters)
        self.assertEqual(unavailable.sources[0].status, "unavailable")
        self.assertEqual(unavailable.sources[0].error, "source_refresh_failed")
        self.assertNotIn("password", json.dumps(unavailable.to_dict()))

    def test_snapshot_scope_uses_sorted_lowercase_source_environments(self) -> None:
        unavailable = ProjectionRepository([]).refresh().to_dict()
        self.assertEqual(unavailable["scope"], {"kind": "unavailable", "environments": []})

        single = ProjectionRepository(
            [parse_source_spec("simulation:one:file:///missing.json", environment="DEV")]
        ).refresh().to_dict()
        self.assertEqual(single["scope"], {"kind": "single", "environments": ["dev"]})

        mixed = ProjectionRepository(
            [
                parse_source_spec("simulation:one:file:///missing-one.json", environment="PROD"),
                parse_source_spec("simulation:two:file:///missing-two.json", environment="dev"),
                parse_source_spec("simulation:three:file:///missing-three.json", environment="DEV"),
            ]
        ).refresh().to_dict()
        self.assertEqual(mixed["scope"], {"kind": "mixed", "environments": ["dev", "prod"]})

    def test_parse_source_spec_accepts_only_explicit_safe_origins(self) -> None:
        source = parse_source_spec("historical:soak24:https://approved.example/console.json", environment="dev")

        self.assertEqual(source.descriptor.id, "soak24")
        self.assertEqual(source.descriptor.evidence_kind, "historical")
        self.assertEqual(source.descriptor.environment, "dev")
        for invalid in (
            "live:only-two", "live:demo:ftp://example/file", "preview:demo:file:///tmp/x",
            "live:demo:file://host/tmp/x", "live:demo:file:///tmp/x?query", "live:demo:file:///tmp/x#fragment",
            "live:demo:file:relative/path", "live:demo:file:/tmp/x", "live:demo:file:////tmp/x",
        ):
            with self.assertRaises(ValueError) as captured:
                parse_source_spec(invalid)
            self.assertNotIn(invalid, str(captured.exception))

    def test_duplicate_source_ids_are_rejected_before_refresh_without_reflection(self) -> None:
        source = parse_source_spec("live:secret-source:file:///tmp/one.json")

        with self.assertRaises(ValueError) as captured:
            ProjectionRepository([source, source])

        self.assertNotIn("secret-source", str(captured.exception))

    def test_oversized_file_is_rejected_before_unbounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.json"
            path.touch()
            with path.open("r+b") as opened:
                opened.truncate(MAX_DOCUMENT_BYTES + 1)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("lecture complète")):
                with self.assertRaises(ValueError):
                    _read_document(f"file://{path}")

    def test_file_reader_uses_a_bounded_read_after_a_small_stat(self) -> None:
        class GrowingFile:
            def __init__(self) -> None:
                self.read_sizes: list[int] = []

            def __enter__(self) -> GrowingFile:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self, size: int) -> bytes:
                self.read_sizes.append(size)
                return b"x" * (MAX_DOCUMENT_BYTES + 1)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small-before-race.json"
            path.write_bytes(b"{}")
            growing = GrowingFile()
            with patch.object(Path, "open", return_value=growing):
                with self.assertRaises(ValueError):
                    _read_document(f"file://{path}")

        self.assertEqual(growing.read_sizes, [MAX_DOCUMENT_BYTES + 1])

    def test_wait_after_rechecks_predicate_after_spurious_wakeup(self) -> None:
        repository = ProjectionRepository([])
        wait_calls: list[float | None] = []

        def controlled_wait(timeout: float | None = None) -> bool:
            wait_calls.append(timeout)
            if len(wait_calls) == 2:
                repository._snapshot = replace(repository._snapshot, revision=1)
            return True

        with patch.object(repository._condition, "wait", side_effect=controlled_wait):
            snapshot = repository.wait_after(0, timeout_s=1)

        self.assertEqual(len(wait_calls), 2)
        self.assertEqual(snapshot.revision, 1)

    def test_wait_after_returns_new_snapshot_and_timeout_keeps_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            repository = ProjectionRepository([parse_source_spec(f"live:dev-cntr:file://{path}")])
            first = repository.refresh()

            timed_out = repository.wait_after(first.revision, timeout_s=0.001)

        self.assertEqual(timed_out.revision, first.revision)


if __name__ == "__main__":
    unittest.main()
