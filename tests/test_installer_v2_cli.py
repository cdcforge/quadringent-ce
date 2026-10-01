"""CLI ``quadringent`` — sous-commandes ``/v2`` (contrat §5, tâche 19).

Aucun réseau réel : chaque commande passe par ``httpx.MockTransport``
rejoué sur ``TestClient(app)`` (voir ``tests/_cli_transport.py``). Couvre :
sortie JSON, codes de sortie stables par code d'erreur, ``dry_run``,
``Idempotency-Key`` auto-générée et affichée, résolution de configuration
(variables d'environnement, fichier ``~/.quadringent/cli.json``).
"""

from __future__ import annotations

import io
import json
import stat

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox

from _cli_transport import asgi_app_transport
from quadringent.installer.api_client import ClientConfig, ConfigError, load_config
from quadringent.installer.cli import _build_parser
from quadringent.installer.v2_commands import run_v2_command


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'cli.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "x",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dest1",
                "org_id": "default",
                "snowflake_account": "acct123",
                "key_pair_ciphertext": "x",
                "setup_script": "-- x",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "SALES", "table_name": "ORDERS"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "pipe1", "table_id": "tbl1", "destination_id": "dest1", "declared_state": "live"},
        )
    try:
        yield engine
    finally:
        engine.dispose()


class _FakeExecutor:
    def execute(self, *, pipeline_id: str, event: str) -> None:  # noqa: ARG002 — protocole PipelineExecutorProtocol
        pass


@pytest.fixture()
def app(engine):
    return create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        enable_mcp=False,
        pipeline_executor=_FakeExecutor(),
    )


@pytest.fixture()
def config() -> ClientConfig:
    return ClientConfig(base_url="http://testserver", token=None)


def _run(app, config, argv: list[str]) -> tuple[int, dict[str, object]]:
    parser = _build_parser()
    args = parser.parse_args(argv)
    out = io.StringIO()
    rc = run_v2_command(args, stdout=out, transport=asgi_app_transport(app), config=config)
    return rc, json.loads(out.getvalue())


def test_sources_list_returns_json_and_exit_zero(app, config) -> None:
    rc, output = _run(app, config, ["sources", "list"])
    assert rc == 0
    assert output["result"]["items"][0]["id"] == "src1"


def test_sources_get_unknown_maps_to_exit_code_3(app, config) -> None:
    rc, output = _run(app, config, ["sources", "get", "does-not-exist"])
    assert rc == 3
    assert output["result"]["error"]["code"] == "not_found"


def test_sources_pause_dry_run_generates_and_prints_idempotency_key(app, config) -> None:
    rc, output = _run(app, config, ["sources", "pause", "src1", "--dry-run"])
    assert rc == 0
    # Chantier 4 : le dry_run détaille aussi les pipelines qui seraient
    # pausés (`pipelines.would_transition`/`skipped`) — cette fixture a un
    # pipeline `live` pour `src1`, donc il apparaît dans `would_transition`.
    dry_run = output["result"]["dry_run"]
    assert dry_run["would_transition"] == {"paused": True}
    assert dry_run["pipelines"]["would_transition"] == [
        {"pipeline_id": "pipe1", "from": "live", "to": "paused"}
    ]
    assert output["idempotency_key"].startswith("cli-")


def test_sources_pause_then_resume_applies(app, config) -> None:
    rc, output = _run(app, config, ["sources", "pause", "src1"])
    assert rc == 0
    assert output["result"]["after"]["paused"] is True

    rc, output = _run(app, config, ["sources", "resume", "src1"])
    assert rc == 0
    assert output["result"]["after"]["paused"] is False


def test_tables_choose_key_writes_columns(app, config) -> None:
    rc, output = _run(
        app,
        config,
        [
            "tables", "choose-key", "tbl1",
            "--key-strategy", "unique_index",
            "--key-column", "ORDER_ID",
        ],
    )
    assert rc == 0
    assert output["result"]["after"]["key_strategy"] == "unique_index"
    assert output["result"]["after"]["key_columns"] == ["ORDER_ID"]


def test_pipeline_remove_without_confirmation_maps_to_exit_code_6(app, config) -> None:
    rc, output = _run(app, config, ["pipelines", "remove", "pipe1"])
    assert rc == 6
    assert output["result"]["error"]["code"] == "pending_confirmation_required"


def test_pipeline_remove_confirmation_roundtrip(app, config) -> None:
    rc, output = _run(app, config, ["pipelines", "remove", "pipe1"])
    assert rc == 6

    rc, pending = _run(app, config, ["confirmations", "list", "--state", "pending"])
    assert rc == 0
    confirmation_id = pending["result"]["items"][0]["id"]

    rc, approved = _run(app, config, ["confirmations", "approve", confirmation_id])
    assert rc == 0
    assert approved["result"]["after"]["state"] == "approved"

    rc, output = _run(app, config, ["pipelines", "remove", "pipe1", "--confirmation-token", confirmation_id])
    assert rc == 0
    assert output["result"]["after"]["declared_state"] == "stopped"


def test_actions_pause_all_dry_run(app, config) -> None:
    rc, output = _run(app, config, ["actions", "pause-all", "--dry-run"])
    assert rc == 0
    assert output["result"]["dry_run"] == {"would_apply": "pause_all"}


def test_audit_tail_returns_json(app, config) -> None:
    _run(app, config, ["sources", "pause", "src1"])
    rc, output = _run(app, config, ["audit", "tail"])
    assert rc == 0
    assert any(row["action"] == "source.pause" for row in output["result"]["items"])


class TestLoadConfig:
    def test_env_vars_take_priority(self, tmp_path) -> None:
        config = load_config(env={"QUADRINGENT_URL": "https://example.test/", "QUADRINGENT_TOKEN": "t"}, config_path=tmp_path / "missing.json")
        assert config.base_url == "https://example.test"
        assert config.token == "t"

    def test_missing_config_raises(self, tmp_path) -> None:
        with pytest.raises(ConfigError):
            load_config(env={}, config_path=tmp_path / "cli.json")

    def test_config_file_must_be_0600(self, tmp_path) -> None:
        config_path = tmp_path / "cli.json"
        config_path.write_text(json.dumps({"url": "https://example.test"}), encoding="utf-8")
        config_path.chmod(0o644)
        with pytest.raises(ConfigError):
            load_config(env={}, config_path=config_path)

    def test_config_file_0600_is_read(self, tmp_path) -> None:
        config_path = tmp_path / "cli.json"
        config_path.write_text(json.dumps({"url": "https://example.test", "token": "t"}), encoding="utf-8")
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        config = load_config(env={}, config_path=config_path)
        assert config.base_url == "https://example.test"
        assert config.token == "t"
