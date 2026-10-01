"""Tests de la CLI (cli.py) — toujours hors ligne (--offline-fake ou fichiers locaux)."""

from __future__ import annotations

import json

from quadringent_qualification import cli
from quadringent_qualification.config import parse_config

CONFIG = """
{
  "run_id": "qual-cli-test",
  "table": {
    "qualified_name": "QUALIF_LIB.QUALIF_ORDERS",
    "primary_key": "ORDER_ID",
    "columns": [
      {"name": "ORDER_ID", "kind": "integer"},
      {"name": "LABEL", "kind": "varchar", "length": 40},
      {"name": "CODE", "kind": "char", "length": 8},
      {"name": "AMOUNT", "kind": "decimal", "precision": 11, "scale": 2},
      {"name": "EVENT_DATE", "kind": "date"},
      {"name": "UPDATED_AT", "kind": "timestamp", "timestamp_precision": 6},
      {"name": "NOTE", "kind": "varchar", "length": 80}
    ]
  },
  "source": {
    "driver": "ibmi_java", "library_whitelist": ["QUALIF_LIB"],
    "connection_secret_file": "/secrets/source.json",
    "journal_library": "QUALIF_LIB", "journal_name": "QUALJRN"
  },
  "capture": {"image": "quadringent-capture:test", "max_seconds": 30},
  "storage": {"backend": "gcs", "bucket": "qualif-bucket", "raw_prefix": "qualification/qual-cli-test"},
  "warehouse": {
    "loader": "snowflake", "account_secret_file": "/secrets/sf.json",
    "database": "QUALIF_DB", "schema": "QUALIF_SCHEMA"
  },
  "steps": ["seed"],
  "bootstrap_receiver": "R1", "bootstrap_sequence": 1
}
"""


def test_cli_run_offline_fake_writes_report_and_returns_zero(tmp_path):
    config_path = tmp_path / "run.json"
    config_path.write_text(CONFIG)
    out_dir = tmp_path / "out"
    code = cli.main(["run", "--config", str(config_path), "--steps", "all",
                      "--out-dir", str(out_dir), "--offline-fake"])
    assert code == 0
    report = json.loads((out_dir / "report.json").read_text())
    assert report["run_id"] == "qual-cli-test"
    assert report["status"] == "PASS"
    assert report["execution_mode"] == "offline_fake"
    assert (out_dir / "report.md").exists()
    assert "qual-cli-test" in (out_dir / "report.md").read_text()
    assert "simulé" in (out_dir / "report.md").read_text()


def test_real_run_keeps_its_provenance_in_json_and_markdown(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_real_adapters", cli._build_offline_fake_adapters)
    config_path = tmp_path / "run.json"
    config_path.write_text(CONFIG)
    out_dir = tmp_path / "out"
    assert cli.main(["run", "--config", str(config_path), "--out-dir", str(out_dir)]) == 0
    assert json.loads((out_dir / "report.json").read_text())["execution_mode"] == "real"
    assert "adaptateurs réels" in (out_dir / "report.md").read_text()


def test_cli_run_without_offline_fake_reports_wiring_error(tmp_path):
    config_path = tmp_path / "run.json"
    config_path.write_text(CONFIG)
    code = cli.main(["run", "--config", str(config_path), "--out-dir", str(tmp_path / "out")])
    assert code == 3


def test_real_adapter_builder_wires_all_four_product_adapters(monkeypatch):
    from quadringent_qualification import real_capture, real_source, real_storage, real_warehouse

    config = parse_config(CONFIG, fmt="json", env={})
    instances = [object() for _ in range(4)]
    seen = []

    def constructor(index):
        def build(value):
            seen.append(value)
            return instances[index]
        return build

    monkeypatch.setattr(real_source, "DockerIbmiSourceDriver", constructor(0))
    monkeypatch.setattr(real_capture, "DockerCaptureRunner", constructor(1))
    monkeypatch.setattr(real_storage, "CloudQualificationStorage", constructor(2))
    monkeypatch.setattr(real_warehouse, "SnowflakeQualificationWarehouse", constructor(3))

    assert cli.build_real_adapters(config) == tuple(instances)
    assert seen == [config, config, config.storage, config]


def test_real_adapter_failure_does_not_echo_a_secret(monkeypatch, tmp_path, capsys):
    from quadringent_qualification import real_source

    def fail(_config):
        raise RuntimeError("fixture-only-password")

    monkeypatch.setattr(real_source, "DockerIbmiSourceDriver", fail)
    config_path = tmp_path / "run.json"
    config_path.write_text(CONFIG)

    assert cli.main(["run", "--config", str(config_path)]) == 3
    assert "fixture-only-password" not in capsys.readouterr().err


def test_cli_run_invalid_config_returns_two(tmp_path):
    config_path = tmp_path / "run.json"
    config_path.write_text('{"run_id": "x"}')  # champs manquants
    code = cli.main(["run", "--config", str(config_path)])
    assert code == 2


def test_cli_run_unknown_step_returns_two(tmp_path):
    config_path = tmp_path / "run.json"
    config_path.write_text(CONFIG)
    code = cli.main(["run", "--config", str(config_path), "--steps", "bogus", "--offline-fake"])
    assert code == 2


def test_cli_report_regenerates_markdown_from_json(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "run_id": "qual-cli-test", "status": "PASS",
        "steps": [{"name": "seed", "status": "PASS", "details": {}, "started_at": "", "finished_at": ""}],
        "reconciliation": None, "freshness": None,
    }))
    code = cli.main(["report", "--run-json", str(report_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "qual-cli-test" in out


def test_cli_report_writes_to_out_markdown_file(tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "run_id": "qual-cli-test", "status": "FAIL",
        "steps": [], "reconciliation": None, "freshness": None,
    }))
    out_md = tmp_path / "summary.md"
    code = cli.main(["report", "--run-json", str(report_path), "--out-markdown", str(out_md)])
    assert code == 1
    assert "qual-cli-test" in out_md.read_text()
