"""The IBM i qualification driver accepts only generated DML and keeps its secret on stdin."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from quadringent_qualification.config import (
    CaptureConfig, RunConfig, SourceConfig, StorageConfig, WarehouseConfig,
)
from quadringent_qualification.generator import step_plan
from quadringent_qualification.real_source import DockerIbmiSourceDriver
from quadringent_qualification.schema import Column, TableSchema, update_sql


IMAGE = "registry.example/quadringent/capture@sha256:" + "a" * 64


def _config(secret_path: Path, *, allow_dml: bool = True, allow_rotation: bool = True) -> RunConfig:
    table = TableSchema(
        "QUALTEST.QUALIF_ORDERS",
        (
            Column("ORDER_ID", "integer"), Column("LABEL", "varchar", length=40),
            Column("CODE", "char", length=8), Column("AMOUNT", "decimal", precision=11, scale=2),
            Column("EVENT_DATE", "date"), Column("UPDATED_AT", "timestamp"),
            Column("NOTE", "varchar", length=80),
        ),
        "ORDER_ID",
    )
    return RunConfig(
        run_id="qualification-1", table=table,
        source=SourceConfig("ibmi_java", ("QUALTEST",), str(secret_path), "QUALTEST", "QUALJRN",
                            allow_dml=allow_dml, allow_rotation=allow_rotation),
        capture=CaptureConfig(IMAGE), storage=StorageConfig("s3", "qual-bucket", "qualification/1"),
        warehouse=WarehouseConfig("snowflake", "/private/snowflake.json", "QUALDB", "QUALSCHEMA"),
        steps=("seed", "snapshot", "changes1", "capture", "reconcile"),
    )


class RecordingRunner:
    def __init__(self, stdout: str = "SRC_EXEC_TOTAL=1\n") -> None:
        self.stdout = stdout
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> CompletedProcess[str]:
        self.calls.append((command, kwargs))
        return CompletedProcess(command, 0, self.stdout, "")


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"host": "ibmi.example", "user": "QUALUSER", "password": "fixture-only"}))
    path.chmod(0o600)
    return path


def test_generated_sql_runs_with_secret_only_on_stdin(secret_file: Path) -> None:
    runner = RecordingRunner()
    config = _config(secret_file)
    source = DockerIbmiSourceDriver(config, runner=runner)
    statement = step_plan("changes3", config.table).statements[0]

    result = source.execute((statement,))

    assert result.exit_code == 0
    assert len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    assert IMAGE in command
    assert "fixture-only" not in " ".join(command)
    assert "fixture-only" not in str(kwargs.get("env", {}))
    assert kwargs["input"] == "ibmi.example\nQUALUSER\nfixture-only\n" + statement + "\n"
    assert kwargs["timeout"] <= 120


def test_arbitrary_or_cross_table_dml_is_refused_before_docker(secret_file: Path) -> None:
    runner = RecordingRunner()
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)

    for statement in (
        "DELETE FROM QUALTEST.BUSINESS_ORDERS WHERE ORDER_ID = 1",
        "DELETE FROM QUALTEST.QUALIF_ORDERS WHERE ORDER_ID = 1",
        "INSERT INTO QUALTEST.QUALIF_ORDERS VALUES (1); DROP TABLE QUALTEST.BUSINESS_ORDERS",
    ):
        with pytest.raises(ValueError, match="generated"):
            source.execute((statement,))
    assert runner.calls == []


def test_only_three_run_bound_freshness_updates_are_allowed(secret_file: Path) -> None:
    runner = RecordingRunner()
    base = _config(secret_file)
    config = replace(base, steps=(*base.steps, "freshness"))
    source = DockerIbmiSourceDriver(config, runner=runner)
    allowed = update_sql(config.table, 1, {"LABEL": f"Q-{config.run_id}-2"})
    assert source.execute((allowed,)).exit_code == 0
    assert runner.calls[0][1]["input"].endswith(allowed + "\n")

    for forbidden in (
        update_sql(config.table, 2, {"LABEL": f"Q-{config.run_id}-2"}),
        update_sql(config.table, 1, {"LABEL": "Q-foreign-run-2"}),
        update_sql(config.table, 1, {"LABEL": f"Q-{config.run_id}-4"}),
    ):
        with pytest.raises(ValueError, match="generated"):
            source.execute((forbidden,))
    assert len(runner.calls) == 1

    without_freshness = DockerIbmiSourceDriver(base, runner=RecordingRunner())
    with pytest.raises(ValueError, match="generated"):
        without_freshness.execute((allowed,))


def test_source_mutations_require_two_explicit_config_flags(secret_file: Path) -> None:
    runner = RecordingRunner()
    source = DockerIbmiSourceDriver(_config(secret_file, allow_dml=False, allow_rotation=False), runner=runner)
    statement = step_plan("seed", source.config.table).statements[0]
    assert source.execute((statement,)).exit_code != 0
    assert source.rotate().exit_code != 0
    assert runner.calls == []


def test_read_only_source_modes_return_only_expected_protocol_lines(secret_file: Path) -> None:
    runner = RecordingRunner(
        'SRC_TAIL={"JOURNAL_RECEIVER_NAME":"QUALJRN0001","LAST_SEQUENCE_NUMBER":"8"}\n'
        'SRC_TAIL_COUNT=1\n'
        'unrelated diagnostic that must not enter the result\n'
    )
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    result = source.tail()
    assert result.exit_code == 0
    assert len(result.parsed("SRC_TAIL")) == 1
    assert len(result.checks) == 2
    assert "tail" in runner.calls[0][0]


def test_inspect_returns_a_single_journal_receipt(secret_file: Path) -> None:
    runner = RecordingRunner(
        'SRC_JOURNAL={"JOURNAL_LIBRARY":"QUALTEST","JOURNAL_NAME":"QUALJRN"}\n'
    )
    source = DockerIbmiSourceDriver(_config(secret_file, allow_dml=False), runner=runner)
    receipt = source.inspect()
    assert receipt.exit_code == 0
    assert receipt.parsed("SRC_JOURNAL") == [
        {"JOURNAL_LIBRARY": "QUALTEST", "JOURNAL_NAME": "QUALJRN"}
    ]
    runner.stdout += 'SRC_JOURNAL={"JOURNAL_LIBRARY":"OTHER","JOURNAL_NAME":"OTHER"}\n'
    assert source.inspect().exit_code != 0


def test_tail_requires_matching_count_receipt(secret_file: Path) -> None:
    runner = RecordingRunner(
        'SRC_TAIL={"JOURNAL_RECEIVER_NAME":"QUALJRN0001","LAST_SEQUENCE_NUMBER":"8"}\n'
    )
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    assert source.tail().exit_code != 0
    runner.stdout += 'SRC_TAIL_COUNT=2\n'
    assert source.tail().exit_code != 0


def test_source_secret_must_be_private_and_image_must_be_immutable(secret_file: Path) -> None:
    secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        DockerIbmiSourceDriver(_config(secret_file))
    secret_file.chmod(0o600)
    config = _config(secret_file)
    with pytest.raises(ValueError, match="digest"):
        DockerIbmiSourceDriver(replace(config, capture=CaptureConfig("capture:latest")))


def test_source_secret_symlink_is_refused(secret_file: Path, tmp_path: Path) -> None:
    link = tmp_path / "linked-source.json"
    link.symlink_to(secret_file)
    with pytest.raises(ValueError, match="unreadable or invalid"):
        DockerIbmiSourceDriver(_config(link))


def test_keychain_source_secret_never_enters_docker_arguments(monkeypatch: pytest.MonkeyPatch,
                                                               secret_file: Path) -> None:
    security_calls: list[list[str]] = []

    def fake_security(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        security_calls.append(command)
        return CompletedProcess(command, 0, "fixture-only\n", "")

    monkeypatch.setattr("quadringent_qualification.real_source.subprocess.run", fake_security)
    runner = RecordingRunner(stdout="SRC_EXEC_TOTAL=1\n")
    config = _config(secret_file)
    source_config = replace(
        config.source, connection_secret_file="keychain:" + "Qualification/Test",
        host="ibmi.example", user="QUALUSER",
    )
    config = replace(config, source=source_config,
                     capture=CaptureConfig("sha256:" + "a" * 64))
    source = DockerIbmiSourceDriver(config, runner=runner)
    statement = step_plan("changes3", config.table).statements[0]
    assert source.execute((statement,)).exit_code == 0
    assert security_calls == [["security", "find-generic-password", "-s", "Qualification/Test", "-w"]]
    docker_command, docker_options = runner.calls[0]
    assert "fixture-only" not in " ".join(docker_command)
    assert docker_options["input"] == "ibmi.example\nQUALUSER\nfixture-only\n" + statement + "\n"


def test_keychain_source_requires_host_and_user(secret_file: Path) -> None:
    config = _config(secret_file)
    source_config = replace(config.source, connection_secret_file="keychain:" + "Qualification/Test")
    with pytest.raises(ValueError, match="host and user"):
        DockerIbmiSourceDriver(replace(config, source=source_config))


def test_read_only_inspection_does_not_require_generator_columns(secret_file: Path) -> None:
    config = _config(secret_file, allow_dml=False)
    narrow_table = TableSchema("QUALTEST.QDC_TAIL", (Column("ID", "integer"),), "ID")
    config = replace(config, table=narrow_table)
    runner = RecordingRunner(
        'SRC_JOURNAL={"JOURNAL_LIBRARY":"QUALTEST","JOURNAL_NAME":"QUALJRN"}\n'
    )
    source = DockerIbmiSourceDriver(config, runner=runner)
    assert source.inspect().exit_code == 0
    assert len(runner.calls) == 1


def test_successful_exit_without_source_receipt_is_not_accepted(secret_file: Path) -> None:
    runner = RecordingRunner(stdout="")
    config = _config(secret_file)
    source = DockerIbmiSourceDriver(config, runner=runner)
    statement = step_plan("seed", config.table).statements[0]
    assert source.execute((statement,)).exit_code != 0
    assert source.tail().exit_code != 0
    assert source.dump().exit_code != 0
    assert source.row_positions(("QUALJRN0001", 1)).exit_code != 0
    assert source.rotate().exit_code != 0


def test_zero_row_dump_and_zero_position_oracle_need_explicit_receipts(secret_file: Path) -> None:
    runner = RecordingRunner(stdout="SRC_ROW_COUNT=0\n")
    source = DockerIbmiSourceDriver(_config(secret_file), runner=runner)
    assert source.dump().exit_code == 0
    runner.stdout = 'SRC_RECEIVER={"JOURNAL_RECEIVER_NAME":"QUALJRN0001"}\nSRC_ROWPOS_COUNT=0\n'
    assert source.row_positions(("QUALJRN0001", 1)).exit_code == 0
