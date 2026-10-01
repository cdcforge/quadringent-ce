#!/usr/bin/env python3
"""Contrat du découpage RRN de la copie historique : couverture disjointe,
ordinaux ordonnés par RRN, reprise par le registre."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# append, pas insert : `scripts/quadringent_control_plane.py` ne doit jamais
# masquer le package `src/quadringent_control_plane` aux autres modules de test.
if str(ROOT / "scripts") not in sys.path:
    sys.path.append(str(ROOT / "scripts"))

import quadringent_fleet_chunked_snapshot as chunked  # noqa: E402
import site_fixture  # noqa: E402

SITE = site_fixture.build_test_site()

RUN_ID = "438b9fdd-d15e-458a-8349-d11b8f6291a4"


class PlanBandsTest(unittest.TestCase):
    def test_bands_cover_the_whole_space_without_overlap(self) -> None:
        bands = chunked.plan_bands(101, 3)
        self.assertEqual(3, len(bands))
        self.assertEqual(1, bands[0]["rrn_start"])
        self.assertEqual(101, bands[-1]["rrn_end"])
        for left, right in zip(bands, bands[1:]):
            self.assertEqual(left["rrn_end"] + 1, right["rrn_start"])

    def test_ordinal_base_orders_chunks_by_rrn(self) -> None:
        bands = chunked.plan_bands(10_000_000, 2)
        for band in bands:
            self.assertEqual(band["rrn_start"] - 1, band["ordinal_base"])
        self.assertLess(bands[0]["ordinal_base"], bands[1]["ordinal_base"])

    def test_single_row_table(self) -> None:
        bands = chunked.plan_bands(1, 3)
        self.assertEqual(1, len(bands))
        self.assertEqual({"rrn_start": 1, "rrn_end": 1, "ordinal_base": 0}, bands[0])

    def test_invalid_inputs_refused(self) -> None:
        for max_rrn, workers in ((0, 1), (-5, 2), (10, 0)):
            with self.assertRaises(chunked.ChunkedError):
                chunked.plan_bands(max_rrn, workers)


class PlanChunksTest(unittest.TestCase):
    def test_chunks_cover_the_band(self) -> None:
        band = {"rrn_start": 7, "rrn_end": 21, "ordinal_base": 6}
        chunks = chunked.plan_chunks(band, 5)
        self.assertEqual([(7, 11), (12, 16), (17, 21)],
                         [(c["rrn_start"], c["rrn_end"]) for c in chunks])

    def test_zero_chunk_size_refused(self) -> None:
        with self.assertRaises(chunked.ChunkedError):
            chunked.plan_chunks({"rrn_start": 1, "rrn_end": 9, "ordinal_base": 0}, 0)


class LedgerTest(unittest.TestCase):
    def test_ledger_round_trip_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            ledger = {
                "table": "SALE",
                "run_id": "438b9fdd-d15e-458a-8349-d11b8f6291a4",
                "chunk_rows": 5_000_000,
                "max_rrn": 75_000_000,
                "chunks": [],
            }
            chunked.save_ledger(workdir, ledger)
            loaded = chunked.load_ledger(workdir)
            self.assertEqual(ledger, loaded)
            self.assertFalse((workdir / "ledger.json.tmp").exists())

    def test_missing_ledger_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(chunked.load_ledger(Path(tmp)))

    def test_ledger_is_valid_json_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            chunked.save_ledger(workdir, {"chunks": [{"worker": 0, "rrn_start": 1,
                                                    "rrn_end": 5, "rows": 4,
                                                    "status": "published",
                                                    "ordinal_offset": 0}]})
            json.loads((workdir / "ledger.json").read_text())


def _args(workdir: Path, **overrides) -> SimpleNamespace:
    values = {
        "table": "SALE",
        "workdir": workdir,
        "image": "img:test",
        "jar": "",
        "repo": workdir,
        "publish_root": workdir,
        "run_id": RUN_ID,
        "chunk_rows": 10,
        "max_attempts": 2,
        "publish_attempts": 2,
        "reader_timeout_seconds": 60,
        "publish_timeout_seconds": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ledger(chunks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "table": "SALE",
        "run_id": RUN_ID,
        "chunk_rows": 10,
        "max_rrn": 20,
        "chunks": chunks,
    }


def _completed(command, stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(command, rc, stdout=stdout, stderr="")


def _ok_subprocess(command, **_kwargs):
    joined = " ".join(str(part) for part in command)
    if "quadringent_fleet_history.py" in joined:
        return _completed(command, stdout='"status": "PUBLISHED"')
    return _completed(command)


class _ReaderOk:
    """Lecteur factice : dépose un lot local comme le ferait le conteneur."""

    PAYLOAD = b'{"sequence":1}\n'

    def __init__(self, rows: int = 7) -> None:
        self.calls: list[dict[str, str]] = []
        self.rows = rows

    def __call__(self, *reader_args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(reader_args[6])
        host_dir = Path(reader_args[4])
        staging = host_dir / str(reader_args[5])
        staging.mkdir(parents=True, exist_ok=True)
        (staging / ("batch-" + "c" * 32 + ".jsonl")).write_bytes(self.PAYLOAD)
        summary = (
            f"snapshot_summary table=SALES.SALE rows={self.rows} columns=3 "
            f"batches=1 elapsed_ms=5 run_id={RUN_ID}"
        )
        return _completed(reader_args[0], stdout=summary)


class RunBandTest(unittest.TestCase):
    def test_read_done_without_staging_is_reread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            args = _args(workdir)
            ledger = _ledger(
                [
                    {
                        "worker": 0,
                        "rrn_start": 1,
                        "rrn_end": 10,
                        "ordinal_offset": 0,
                        "rows": 7,
                        "status": "read_done",
                    }
                ]
            )
            reader = _ReaderOk()
            output = io.StringIO()
            with mock.patch.object(chunked, "_docker_reader", reader), mock.patch.object(
                chunked.subprocess, "run", _ok_subprocess
            ), redirect_stdout(output):
                chunked.run_band(
                    0,
                    {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                    args,
                    ledger,
                    threading.Lock(),
                )
            self.assertEqual(1, len(reader.calls))
            self.assertEqual("1", reader.calls[0]["AS400_SNAPSHOT_RRN_START"])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertIn("read_done_without_staging", {e["event"] for e in events})
            record = ledger["chunks"][0]
            self.assertEqual("published", record["status"])

    def test_reader_timeout_marks_the_chunk_failed_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            args = _args(workdir)
            ledger = _ledger([])

            def timing_out(*_a, **_k):
                raise subprocess.TimeoutExpired("docker", 60)

            with mock.patch.object(chunked, "_docker_reader", timing_out), mock.patch.object(
                chunked.subprocess, "run", _ok_subprocess
            ), mock.patch.object(chunked.time, "sleep", lambda _s: None):
                with self.assertRaises(chunked.ChunkedError):
                    chunked.run_band(
                        0,
                        {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                        args,
                        ledger,
                        threading.Lock(),
                    )
            record = ledger["chunks"][0]
            self.assertEqual("failed", record["status"])
            self.assertEqual(0, record["rows"])
            # Rejouable : une seconde passe avec un lecteur sain publie la tranche.
            reader = _ReaderOk()
            with mock.patch.object(chunked, "_docker_reader", reader), mock.patch.object(
                chunked.subprocess, "run", _ok_subprocess
            ):
                chunked.run_band(
                    0,
                    {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                    args,
                    ledger,
                    threading.Lock(),
                )
            self.assertEqual(1, len(reader.calls))
            self.assertEqual("published", ledger["chunks"][0]["status"])

    def test_publish_failure_marks_the_chunk_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            args = _args(workdir)
            ledger = _ledger(
                [
                    {
                        "worker": 0,
                        "rrn_start": 1,
                        "rrn_end": 10,
                        "ordinal_offset": 0,
                        "rows": 7,
                        "status": "read_done",
                    }
                ]
            )
            chunk_dir = workdir / "w0-r1-10"
            chunk_dir.mkdir()
            (chunk_dir / ("batch-" + "a" * 32 + ".jsonl")).write_text("{}\n")

            def failing(command, **_kwargs):
                joined = " ".join(str(part) for part in command)
                if "quadringent_fleet_history.py" in joined:
                    return _completed(command, rc=1)
                return _completed(command)

            reader = _ReaderOk()
            with mock.patch.object(chunked, "_docker_reader", reader), mock.patch.object(
                chunked.subprocess, "run", failing
            ), mock.patch.object(chunked.time, "sleep", lambda _s: None):
                with self.assertRaises(chunked.ChunkedError):
                    chunked.run_band(
                        0,
                        {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                        args,
                        ledger,
                        threading.Lock(),
                    )
            self.assertEqual(0, len(reader.calls))
            self.assertEqual("failed", ledger["chunks"][0]["status"])

    def test_progress_document_is_written_to_s3_on_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            args = _args(workdir)
            ledger = _ledger([])
            commands: list[list[str]] = []

            def capturing(command, **kwargs):
                commands.append([str(part) for part in command])
                return _ok_subprocess(command, **kwargs)

            reader = _ReaderOk(rows=7)
            with mock.patch.object(chunked, "_docker_reader", reader), mock.patch.object(
                chunked.subprocess, "run", capturing
            ):
                chunked.run_band(
                    0,
                    {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                    args,
                    ledger,
                    threading.Lock(),
                )
            progress_path = workdir / "history-progress.json"
            document = json.loads(progress_path.read_text())
            self.assertEqual("history-progress", document["kind"])
            self.assertEqual(RUN_ID, document["run_id"])
            self.assertEqual("SALE", document["table"])
            self.assertEqual(20, document["max_rrn"])
            self.assertEqual(10, document["chunk_rows"])
            self.assertEqual(
                [
                    {
                        "worker": 0,
                        "rrn_start": 1,
                        "rrn_end": 10,
                        "ordinal_offset": 0,
                        "rows": 7,
                        "bytes": len(_ReaderOk.PAYLOAD),
                        "status": "published",
                    }
                ],
                document["chunks"],
            )
            self.assertEqual(
                {
                    "planned_rows": 7,
                    "published_rows": 7,
                    "published_bytes": len(_ReaderOk.PAYLOAD),
                    "published_objects": 1,
                },
                document["totals"],
            )
            uploads = [
                " ".join(parts)
                for parts in commands
                if "put_object" in " ".join(parts)
            ]
            self.assertTrue(uploads)
            self.assertIn(
                f"{SITE.history_progress_prefix}{RUN_ID}.json", uploads[-1]
            )
            # read_done puis published : deux écritures de progression.
            self.assertEqual(2, len(uploads))

    def test_progress_publish_failure_never_stops_the_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            args = _args(workdir)
            ledger = _ledger([])

            def failing_progress(command, **kwargs):
                joined = " ".join(str(part) for part in command)
                if "put_object" in joined:
                    return _completed(command, rc=1)
                return _ok_subprocess(command, **kwargs)

            output = io.StringIO()
            with mock.patch.object(chunked, "_docker_reader", _ReaderOk()), mock.patch.object(
                chunked.subprocess, "run", failing_progress
            ), redirect_stdout(output):
                chunked.run_band(
                    0,
                    {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                    args,
                    ledger,
                    threading.Lock(),
                )
            self.assertEqual("published", ledger["chunks"][0]["status"])
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertIn("progress_publish_failed", {e["event"] for e in events})


class DockerReaderTest(unittest.TestCase):
    def test_password_travels_by_env_file_never_on_the_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            seen: dict[str, object] = {}

            def capturing(command, **kwargs):
                seen["command"] = [str(part) for part in command]
                env_path = Path(command[command.index("--env-file") + 1])
                seen["env_path"] = env_path
                seen["env_body"] = env_path.read_text()
                seen["env_mode"] = stat.S_IMODE(env_path.stat().st_mode)
                return _completed(command)

            environment = {
                "ISERIES_HOST": "192.0.2.10",
                "ISERIES_USER": "USER",
                "ISERIES_PASSWORD": "s3cret-value",
                "AS400_DATABASE_PORT": "446",
                "AS400_SIGNON_PORT": "9476",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                chunked.subprocess, "run", capturing
            ):
                chunked._docker_reader(
                    "img:test", "", "SALE", RUN_ID, workdir, "w0-r1-10", {}
                )
            command = seen["command"]
            joined = " ".join(command)
            self.assertNotIn("s3cret-value", joined)
            self.assertNotIn("ISERIES_PASSWORD", joined)
            self.assertEqual("ISERIES_PASSWORD=s3cret-value\n", seen["env_body"])
            self.assertEqual(0o600, seen["env_mode"])
            self.assertIn("AS400_DATABASE_PORT=446", joined)
            self.assertIn("AS400_SIGNON_PORT=9476", joined)
            env_path = seen["env_path"]
            # Le secret vit dans un répertoire temporaire dédié, jamais sous
            # la racine persistée : un SIGKILL ne laisse rien à relire.
            self.assertFalse(env_path.is_relative_to(workdir))
            self.assertFalse(env_path.exists())
            self.assertFalse(env_path.parent.exists())

    def test_named_container_is_removed_before_launch_and_on_timeout(self) -> None:
        """Un timeout tue le CLI, pas le conteneur : l'orphelin nommé doit
        être retiré explicitement pour ne pas écrire dans le staging suivant."""

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            calls: list[str] = []

            def timing_out(command, **_kwargs):
                joined = " ".join(str(part) for part in command)
                calls.append(joined)
                if joined.startswith("docker run"):
                    raise subprocess.TimeoutExpired("docker", 60)
                return _completed(command)

            environment = {
                "ISERIES_HOST": "192.0.2.10",
                "ISERIES_USER": "USER",
                "ISERIES_PASSWORD": "s3cret",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                chunked.subprocess, "run", timing_out
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    chunked._docker_reader(
                        "img:test",
                        "",
                        "SALE",
                        RUN_ID,
                        workdir,
                        "w0-r1-10",
                        {},
                        container_name="chunked-abc-w0-r1-10-a1",
                    )
            run_calls = [c for c in calls if c.startswith("docker run")]
            self.assertEqual(1, len(run_calls))
            self.assertIn("--name chunked-abc-w0-r1-10-a1", run_calls[0])
            rm_calls = [
                c for c in calls if c == "docker rm -f chunked-abc-w0-r1-10-a1"
            ]
            # Un retrait avant le lancement (orphelin du même nom) et un
            # après le timeout (conteneur survivant).
            self.assertEqual(2, len(rm_calls))


class ReaderAttemptLogTest(unittest.TestCase):
    def test_attempt_logs_survive_the_staging_wipe(self) -> None:
        """Les journaux d'échec vivent hors du staging : la tentative
        suivante et le nettoyage de publication ne les effacent pas."""

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            args = _args(workdir, max_attempts=2)
            ledger = _ledger([])

            def failing(*reader_args, **_kwargs):
                return _completed(reader_args[0], stdout="reader boom", rc=1)

            with mock.patch.object(chunked, "_docker_reader", failing), mock.patch.object(
                chunked.subprocess, "run", _ok_subprocess
            ), mock.patch.object(chunked.time, "sleep", lambda _s: None):
                with self.assertRaises(chunked.ChunkedError):
                    chunked.run_band(
                        0,
                        {"rrn_start": 1, "rrn_end": 10, "ordinal_base": 0},
                        args,
                        ledger,
                        threading.Lock(),
                    )
            logs = sorted((workdir / "logs").glob("*-reader-attempt-*.log"))
            self.assertEqual(
                [
                    "w0-r1-10-reader-attempt-1.log",
                    "w0-r1-10-reader-attempt-2.log",
                ],
                [path.name for path in logs],
            )
            self.assertIn("reader boom", logs[0].read_text())
            # Le staging ne porte plus les journaux.
            staging = workdir / "w0-r1-10"
            if staging.is_dir():
                self.assertEqual(
                    [], [p.name for p in staging.glob("*reader-attempt*")]
                )


class MainValidationTest(unittest.TestCase):
    """Identifiants refusés avant interpolation et reprise bornée au découpage
    enregistré dans le registre."""

    ENVIRONMENT = {
        "ISERIES_HOST": "192.0.2.10",
        "ISERIES_USER": "USER",
        "ISERIES_PASSWORD": "s3cret",
    }

    def _argv(self, workdir: Path, publish_root: Path, *extra: str) -> list[str]:
        return [
            "quadringent_fleet_chunked_snapshot.py",
            "--table",
            "SALE",
            "--workdir",
            str(workdir),
            "--repo",
            str(workdir),
            "--publish-root",
            str(publish_root),
            "--image",
            "img:test",
            *extra,
        ]

    def _run(self, argv: list[str]) -> int:
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(
            os.environ, self.ENVIRONMENT, clear=False
        ):
            return chunked.main()

    def test_malicious_table_is_refused_before_any_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            for bad in ("SALE;id", "SALE`id`", "SALE$(id)", "SALE/x", "EV NT"):
                with self.subTest(table=bad):
                    argv = self._argv(workdir, Path(tmp))
                    argv[argv.index("--table") + 1] = bad
                    self.assertEqual(2, self._run(argv))

    def test_malicious_run_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            for bad in ("x; rm -rf /", "a b", "../escape", "x" * 65):
                with self.subTest(run_id=bad):
                    self.assertEqual(
                        2, self._run(self._argv(workdir, Path(tmp), "--run-id", bad))
                    )

    def test_malformed_ledger_run_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            chunked.save_ledger(
                workdir,
                {
                    "table": "SALE",
                    "run_id": "bad;id",
                    "chunk_rows": 10,
                    "workers": 2,
                    "chunks": [],
                },
            )
            self.assertEqual(2, self._run(self._argv(workdir, Path(tmp))))

    def test_resume_refuses_divergent_chunk_rows_or_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            chunked.save_ledger(
                workdir,
                {
                    "table": "SALE",
                    "run_id": RUN_ID,
                    "chunk_rows": 10,
                    "workers": 2,
                    "max_rrn": 20,
                    "chunks": [],
                },
            )
            for extra in (
                (),
                ("--chunk-rows", "20", "--workers", "2"),
                ("--chunk-rows", "10", "--workers", "3"),
            ):
                with self.subTest(extra=extra):
                    self.assertEqual(
                        2, self._run(self._argv(workdir, Path(tmp), *extra))
                    )
            # Le découpage enregistré reprend sans rien relire.
            argv = self._argv(
                workdir, Path(tmp), "--chunk-rows", "10", "--workers", "2", "--dry-run"
            )
            self.assertEqual(0, self._run(argv))

    def test_workers_is_recorded_in_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            argv = self._argv(
                workdir, Path(tmp), "--chunk-rows", "10", "--workers", "2", "--dry-run"
            )
            with mock.patch.object(chunked, "probe_bound", return_value=20):
                self.assertEqual(0, self._run(argv))
            ledger = chunked.load_ledger(workdir)
            self.assertEqual(2, ledger["workers"])
            self.assertEqual(10, ledger["chunk_rows"])


if __name__ == "__main__":
    unittest.main()
