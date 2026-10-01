"""Le harnais lance les programmes du produit avec un secret sur stdin."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from quadringent_qualification.adapters import CaptureBoundary
from quadringent_qualification.config import (
    CaptureConfig, RunConfig, SourceConfig, StorageConfig, WarehouseConfig,
)
from quadringent_qualification.real_capture import DockerCaptureRunner
from quadringent_qualification.schema import Column, TableSchema


RUN_ID = "123e4567-e89b-42d3-a456-4266b4174000"
IMAGE = "registry.example/quadringent/capture@sha256:" + "a" * 64
PASSWORD = "fixture-only-password"


def _config(tmp_path: Path, *, backend: str = "gcs") -> RunConfig:
    secret = tmp_path / "source.json"
    secret.write_text(json.dumps({"host": "ibmi.example", "user": "QUALUSER", "password": PASSWORD}))
    secret.chmod(0o600)
    return RunConfig(
        run_id=RUN_ID,
        table=TableSchema("QUALTEST.QUALIF_ORDERS", (Column("ORDER_ID", "integer"),), "ORDER_ID"),
        source=SourceConfig(
            driver="ibmi_java", library_whitelist=("QUALTEST",), connection_secret_file=str(secret),
            journal_library="QUALTEST", journal_name="QUALJRN", source_time_zone="UTC",
        ),
        capture=CaptureConfig(image=IMAGE, max_seconds=90),
        storage=StorageConfig(
            backend=backend, bucket="qual-bucket", raw_prefix=f"qualification/{RUN_ID}",
            checkpoint_location="qual-state",
        ),
        warehouse=WarehouseConfig("snowflake", "/private/snowflake.json", "QUALDB", "QUALSCHEMA"),
        steps=("snapshot", "capture"),
    )


def _boundary() -> CaptureBoundary:
    return CaptureBoundary(
        receiver_library="RECVLIB", receiver_name="RECV0001", next_sequence=101,
        observed_at=datetime(2026, 9, 29, 12, 34, tzinfo=timezone.utc),
    )


class RecordingDocker:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        return CompletedProcess(command, self.returncode, self.stdout, "")


def _container_env(command: list[str], options: dict[str, object]) -> dict[str, str]:
    process_env = options["env"]
    assert isinstance(process_env, dict)
    names = [command[index + 1] for index, item in enumerate(command[:-1]) if item == "--env"]
    assert all(isinstance(name, str) and "=" not in name for name in names)
    return {name: process_env[name] for name in names}


def test_snapshot_uses_original_boundary_and_password_only_on_stdin(tmp_path: Path) -> None:
    docker = RecordingDocker(json.dumps({
        "event": "initial_copy_completed", "run_id": RUN_ID,
        "table_id": f"qual-{RUN_ID}", "rows_copied": 7,
    }) + "\n")
    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)

    result = runner.run(label="snapshot", max_seconds=90, bootstrap=_boundary(), env={})

    assert result.exit_code == 0
    assert result.event_count == 7
    assert result.events == ()
    command, options = docker.calls[0]
    container_env = _container_env(command, options)
    assert IMAGE in command
    assert "/app/quadringent_initial_copy_job.py" in command
    assert "--network" in command and "host" in command
    assert container_env["AS400_BOOTSTRAP_RECEIVER_LIBRARY"] == "RECVLIB"
    assert container_env["AS400_BOOTSTRAP_RECEIVER"] == "RECV0001"
    assert container_env["AS400_BOOTSTRAP_SEQUENCE"] == "100"
    assert container_env["AS400_BOOTSTRAP_OBSERVED_AT"] == "2026-09-29T12:34:00+00:00"
    assert container_env["AS400_RAW_PREFIX"] == f"qualification/{RUN_ID}"
    assert container_env["AS400_EVIDENCE_KEY"] == f"qualification/{RUN_ID}/qual-{RUN_ID}/evidence/{RUN_ID}.json"
    assert container_env["AS400_SOURCE_TIME_ZONE"] == "UTC"
    assert options["input"] == PASSWORD + "\n"
    assert PASSWORD not in " ".join(command)
    assert PASSWORD not in str(options["env"])
    assert PASSWORD not in result.log


def test_capture_restarts_at_same_boundary_with_run_scoped_checkpoint(tmp_path: Path) -> None:
    docker = RecordingDocker('{"event":"capture_finished","metrics":{"events_published":3}}\n')
    runner = DockerCaptureRunner(_config(tmp_path, backend="s3"), runner=docker)

    first = runner.run(label="capture-1", max_seconds=90, bootstrap=_boundary(), env={})
    second = runner.run(label="capture-2", max_seconds=90, bootstrap=_boundary(), env={})

    assert first.event_count == second.event_count == 3
    first_command, first_options = docker.calls[0]
    second_command, second_options = docker.calls[1]
    first_env = _container_env(first_command, first_options)
    second_env = _container_env(second_command, second_options)
    assert first_env["AS400_BOOTSTRAP_SEQUENCE"] == second_env["AS400_BOOTSTRAP_SEQUENCE"] == "101"
    assert first_env["AS400_STREAM_KEY"] == second_env["AS400_STREAM_KEY"]
    assert RUN_ID in first_env["AS400_STREAM_KEY"]
    assert first_env["AS400_RAW_PREFIX"] == f"qualification/{RUN_ID}/qualif_orders/journal"
    assert first_env["AS400_CHECKPOINT_TABLE"] == "qual-state"
    assert first_env["AS400_RECEIPTED_SCANS"] == "true"
    assert "/app/as400_continuous_capture.py" in first_command
    assert first_command[first_command.index("--max-seconds") + 1] == "90"
    assert second_options["input"] == PASSWORD + "\n"


def test_another_run_cannot_reuse_the_same_checkpoint_key(tmp_path: Path) -> None:
    docker = RecordingDocker('{"event":"capture_finished","metrics":{"events_published":0}}\n')
    original = _config(tmp_path)
    other_run_id = "123e4567-e89b-42d3-a456-4266b4174001"
    other = replace(
        original, run_id=other_run_id,
        storage=replace(original.storage, raw_prefix=f"qualification/{other_run_id}"),
    )
    DockerCaptureRunner(original, runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    DockerCaptureRunner(other, runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )
    first = _container_env(*docker.calls[0])
    second = _container_env(*docker.calls[1])
    assert first["AS400_STREAM_KEY"] != second["AS400_STREAM_KEY"]
    assert first["AS400_RAW_PREFIX"] != second["AS400_RAW_PREFIX"]


@pytest.mark.parametrize("label,stdout", [
    ("snapshot", "{}\n"),
    ("capture-1", '{"event":"capture_poll","event_count":3}\n'),
])
def test_successful_docker_exit_without_product_receipt_fails_closed(
    tmp_path: Path, label: str, stdout: str,
) -> None:
    docker = RecordingDocker(stdout)
    result = DockerCaptureRunner(_config(tmp_path), runner=docker).run(
        label=label, max_seconds=90, bootstrap=_boundary(), env={},
    )
    assert result.exit_code != 0
    assert result.event_count == 0


def test_invalid_run_scope_or_timezone_is_refused_before_docker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    docker = RecordingDocker("")
    bad_scope = replace(config, storage=replace(config.storage, raw_prefix="qualification/another-run"))
    with pytest.raises(ValueError, match="run prefix"):
        DockerCaptureRunner(bad_scope, runner=docker)
    bad_time_zone = replace(config, source=replace(config.source, source_time_zone=None))
    with pytest.raises(ValueError, match="time zone"):
        DockerCaptureRunner(bad_time_zone, runner=docker)
    assert docker.calls == []


def test_extra_environment_cannot_override_the_run_scope(tmp_path: Path) -> None:
    docker = RecordingDocker("")
    runner = DockerCaptureRunner(_config(tmp_path), runner=docker)
    with pytest.raises(ValueError, match="invocation"):
        runner.run(label="capture-1", max_seconds=90, bootstrap=_boundary(),
                   env={"AS400_RAW_BUCKET": "another-bucket"})
    assert docker.calls == []


def test_timed_out_docker_run_force_removes_its_named_container(tmp_path: Path) -> None:
    class TimedOutDocker:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, command: list[str], **_kwargs: object) -> CompletedProcess[str]:
            self.calls.append(command)
            if command[1] == "run":
                raise TimeoutExpired(command, 90)
            return CompletedProcess(command, 0, "", "")

    docker = TimedOutDocker()
    result = DockerCaptureRunner(_config(tmp_path), runner=docker).run(
        label="capture-1", max_seconds=90, bootstrap=_boundary(), env={},
    )

    assert result.exit_code == 124
    name = docker.calls[0][docker.calls[0].index("--name") + 1]
    assert docker.calls[1] == ["docker", "rm", "--force", "--volumes", name]
