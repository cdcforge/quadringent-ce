"""Exécuteur Kubernetes v2 (chantier 4) — `executor/kubernetes.py`.

Vérifie de bout en bout, avec des clients Kubernetes et une sonde de
bascule factices (jamais de vrai réseau, jamais d'IBM i réel — cf. règles
du worktree) :

- ``start`` lit la position du journal une seule fois, lance le Job de
  copie initiale avec un ``run_id`` UUID et l'annotation de bascule, puis
  fait apparaître la table dans le Deployment de lecteur du journal.
- ``pause`` retire la table du jeu de tables du lecteur *avant* même que
  ``declared_state`` soit persisté (l'exécuteur recalcule la transition).
- Un redémarrage du control plane (nouvel appel identique) ne recrée ni ne
  met à jour un objet déjà conforme — idempotence de la réconciliation.
- Plus aucune table live sur un journal supprime son Deployment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import EvidenceReader, InitialCopyEvidence
from quadringent_control_plane.v2.executor.kubernetes import ExecutorConfig, ExecutorError, KubernetesPipelineExecutor
from quadringent_control_plane.v2.services.pipelines import PipelinesService


class _FakeJobsClient:
    def __init__(self) -> None:
        self.created: dict[str, dict] = {}

    def create_job(self, manifest):
        name = manifest["metadata"]["name"]
        if name in self.created:
            from quadringent_control_plane.k8s_jobs import JobAlreadyExists

            raise JobAlreadyExists()
        self.created[name] = manifest
        return manifest

    def read_job(self, name):
        return self.created.get(name)


class _FakeDeploymentsClient:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.create_calls: list[str] = []
        self.replace_calls: list[str] = []
        self.delete_calls: list[str] = []

    def read_deployment(self, name):
        return self.objects.get(name)

    def create_deployment(self, manifest):
        name = manifest["metadata"]["name"]
        self.objects[name] = manifest
        self.create_calls.append(name)
        return manifest

    def replace_deployment(self, name, manifest):
        self.objects[name] = manifest
        self.replace_calls.append(name)
        return manifest

    def delete_deployment(self, name):
        self.objects.pop(name, None)
        self.delete_calls.append(name)


class _FakeBoundaryReader:
    """Sonde de bascule déterministe — jamais d'IBM i réel dans les tests."""

    def __init__(self, sequence: int = 4200) -> None:
        self.sequence = sequence
        self.calls: list[tuple[str, str]] = []

    def read_boundary(self, *, source_id: str, table_id: str) -> JournalBoundary:
        self.calls.append((source_id, table_id))
        return JournalBoundary(
            receiver_library="QGPL",
            receiver_name="RCV0001",
            last_sequence=self.sequence,
            observed_at=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
        )


class _FakeEvidenceObjectStore:
    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        raise FileNotFoundError(key)


def _seed(engine, *, declared_state: str = "not_started", tls_pinned_pem: str | None = None) -> tuple[str, str, str]:
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
                "detected_timezone": "Europe/Paris",
                "tls_pinned_pem": tls_pinned_pem,
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": "default",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {
                "id": "tbl1",
                "source_id": "src1",
                "schema_name": "SALES",
                "table_name": "ORDHDR",
                "journal_library": "QGPL",
                "journal_name": "QSQJRN",
            },
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": declared_state},
        )
    return "src1", "tbl1", "ppl1"


@pytest.fixture()
def executor_env(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'executor.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    jobs = _FakeJobsClient()
    deployments = _FakeDeploymentsClient()
    boundary_reader = _FakeBoundaryReader()
    config = ExecutorConfig(
        namespace="quadringent",
        reader_image="registry.example.test/quadringent/reader:1.0.0",
        copy_image="registry.example.test/quadringent/copy:1.0.0",
        replay_image="registry.example.test/quadringent/replay:1.0.0",
        raw_prefix_root="raw/example",
        storage_backend="gcs",
        service_account_name="quadringent-capture",
        reader_timeout_seconds=300,
        raw_bucket="acme-raw",
        checkpoint_location="acme-checkpoints",
    )
    executor = KubernetesPipelineExecutor(
        engine,
        jobs_client=jobs,
        deployments_client=deployments,
        boundary_reader=boundary_reader,
        evidence_reader=EvidenceReader(_FakeEvidenceObjectStore()),
        config=config,
        run_id_factory=lambda: "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1",
    )
    try:
        yield engine, jobs, deployments, boundary_reader, executor
    finally:
        engine.dispose()


def test_start_creates_initial_copy_job_with_uuid_run_id_and_boundary(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")

    executor.execute(pipeline_id="ppl1", event="start")

    assert len(jobs.created) == 1
    (manifest,) = jobs.created.values()
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["AS400_BOOTSTRAP_RECEIVER"] == "RCV0001"
    assert env["AS400_BOOTSTRAP_SEQUENCE"] == "4200"
    assert env["AS400_SNAPSHOT_RUN_ID"] == "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1"


def test_start_makes_the_table_appear_in_the_journal_reader_deployment(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")

    executor.execute(pipeline_id="ppl1", event="start")

    assert len(deployments.create_calls) == 1
    (manifest,) = deployments.objects.values()
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    # Une seule table : jamais le mode flotte (AS400_FLEET_TABLES exige au
    # moins deux tables) — c'est ISERIES_TABLE qui porte la table capturée,
    # et l'annotation qui porte l'identifiant interne pour la réconciliation.
    assert "AS400_FLEET_TABLES" not in env
    assert env["ISERIES_TABLE"] == "ORDHDR"
    assert "tbl1" in manifest["metadata"]["annotations"]["quadringent.io/table-ids"]
    assert manifest["spec"]["replicas"] == 1


def test_reconcile_is_idempotent_on_control_plane_restart(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    service = PipelinesService(engine)
    executor.execute(pipeline_id="ppl1", event="start")
    calls_before = len(deployments.replace_calls) + len(deployments.create_calls)

    # Un même exécuteur rappelé sur le même état (ex. redémarrage du
    # control plane qui rejoue la réconciliation) ne doit produire aucune
    # nouvelle création ni mise à jour.
    executor._reconcile_reader_for_source("src1")

    assert len(deployments.replace_calls) + len(deployments.create_calls) == calls_before


def test_reconcile_source_public_method_reapplies_the_desired_reader(executor_env) -> None:
    """``reconcile_source`` (chantier réconciliation de dérive, 24/09/2026) :
    la surface publique appelée par ``ReconciliationLoop`` à chaque tour,
    sans ``override`` — recompose exactement le même Deployment que
    ``_reconcile_reader_for_source`` à partir du seul état déjà persisté."""

    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    executor.execute(pipeline_id="ppl1", event="start")
    calls_before = len(deployments.replace_calls) + len(deployments.create_calls)

    executor.reconcile_source("src1")

    # Idempotent : le manifeste désiré n'a pas changé, aucune écriture de plus.
    assert len(deployments.replace_calls) + len(deployments.create_calls) == calls_before


def test_reconcile_keeps_the_copy_boundary_when_the_journal_tail_advances(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    executor.execute(pipeline_id="ppl1", event="start")
    with engine.begin() as connection:
        connection.execute(
            v2_schema.pipelines.update().where(v2_schema.pipelines.c.id == "ppl1").values(declared_state="live")
        )
    reads_before = len(boundary_reader.calls)
    (manifest_before,) = deployments.objects.values()
    boundary_reader.sequence += 9

    executor.reconcile_source("src1")

    (manifest_after,) = deployments.objects.values()
    assert manifest_after["metadata"]["annotations"]["quadringent.io/spec-sha256"] == manifest_before["metadata"]["annotations"]["quadringent.io/spec-sha256"]
    assert deployments.replace_calls == []
    assert len(boundary_reader.calls) == reads_before


def test_reconcile_uses_durable_evidence_after_the_copy_job_expires(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    executor.execute(pipeline_id="ppl1", event="start")
    with engine.begin() as connection:
        connection.execute(
            v2_schema.pipelines.update().where(v2_schema.pipelines.c.id == "ppl1").values(declared_state="live")
        )
    original = next(iter(jobs.created.values()))
    environment = {item["name"]: item["value"] for item in original["spec"]["template"]["spec"]["containers"][0]["env"]}
    proof = InitialCopyEvidence(
        pipeline_id="ppl1", table_id="tbl1", run_id=environment["AS400_SNAPSHOT_RUN_ID"],
        boundary=JournalBoundary(
            receiver_library=environment["AS400_BOOTSTRAP_RECEIVER_LIBRARY"],
            receiver_name=environment["AS400_BOOTSTRAP_RECEIVER"],
            last_sequence=int(environment["AS400_BOOTSTRAP_SEQUENCE"]),
            observed_at=datetime.fromisoformat(environment["AS400_BOOTSTRAP_OBSERVED_AT"]),
        ),
        rows_copied=1, completed_at=datetime(2026, 9, 23, 11, 0, tzinfo=timezone.utc),
    )
    jobs.created.clear()  # Le TTL Kubernetes a supprimé le Job.
    executor._evidence_reader = type("Reader", (), {"read": lambda self, key: proof})()
    reads_before = len(boundary_reader.calls)
    boundary_reader.sequence += 20

    executor.reconcile_source("src1")

    assert deployments.replace_calls == []
    assert len(boundary_reader.calls) == reads_before


def test_reconcile_source_picks_up_an_image_change_without_a_transition(executor_env) -> None:
    """Panne du 24 septembre 2026 : une nouvelle image de produit déployée
    pendant qu'un pipeline reste ``copying``/``live`` n'était jamais reprise
    par les Deployments déjà en place, faute de transition ``declared_state``
    entre-temps. ``reconcile_source`` doit la reprendre au tour suivant."""

    engine, jobs, deployments, boundary_reader, executor = executor_env
    # Persisté directement en ``copying`` (pas via ``execute("start")``, qui
    # ne persiste jamais lui-même ``declared_state`` — c'est le rôle de
    # ``PipelinesService.apply_action``, hors périmètre de ce test) : le
    # Deployment de lecteur doit déjà exister pour observer sa mise à jour.
    _seed(engine, declared_state="copying")
    executor.reconcile_source("src1")
    (manifest_before,) = deployments.objects.values()
    assert manifest_before["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example.test/quadringent/reader:1.0.0"
    )

    executor._config = ExecutorConfig(
        **{
            **executor._config.__dict__,
            "reader_image": "registry.example.test/quadringent/reader:2.0.0",
        }
    )
    executor.reconcile_source("src1")

    (manifest_after,) = deployments.objects.values()
    assert manifest_after["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "registry.example.test/quadringent/reader:2.0.0"
    )
    assert deployments.replace_calls


def test_pause_removes_the_table_from_the_reader_before_state_is_persisted(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="copying")
    service = PipelinesService(engine)
    executor._reconcile_reader_for_source("src1", override={"ppl1": "copying"})
    assert deployments.objects  # le lecteur existe avant la pause

    service.apply_action("ppl1", "pause", executor=executor)

    # Plus aucune table live sur ce journal : le Deployment est retiré.
    assert deployments.objects == {}
    assert deployments.delete_calls


def test_only_one_reader_deployment_per_journal_regardless_of_table_count(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    with engine.begin() as connection:
        connection.execute(
            v2_schema.tables.insert(),
            {
                "id": "tbl2",
                "source_id": "src1",
                "schema_name": "SALES",
                "table_name": "ORDLIN",
                "journal_library": "QGPL",
                "journal_name": "QSQJRN",
            },
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl2", "table_id": "tbl2", "destination_id": "dst1", "declared_state": "copying"},
        )

    service = PipelinesService(engine)
    executor.execute(pipeline_id="ppl1", event="start")

    assert len(deployments.objects) == 1
    (manifest,) = deployments.objects.values()
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    # Deux tables : mode flotte, AS400_FLEET_TABLES porte les noms de table
    # (ce que lit ``quadringent.fleet_capture``), jamais les identifiants
    # internes — l'annotation garde les identifiants pour la réconciliation.
    assert "ORDHDR" in env["AS400_FLEET_TABLES"] and "ORDLIN" in env["AS400_FLEET_TABLES"]
    assert env["AS400_FLEET_TABLE_ROOT"]
    assert (
        "tbl1" in manifest["metadata"]["annotations"]["quadringent.io/table-ids"]
        and "tbl2" in manifest["metadata"]["annotations"]["quadringent.io/table-ids"]
    )


def test_start_fails_closed_without_a_detected_source_time_zone(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
                "detected_timezone": None,
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": "default",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {
                "id": "tbl1",
                "source_id": "src1",
                "schema_name": "SALES",
                "table_name": "ORDHDR",
                "journal_library": "QGPL",
                "journal_name": "QSQJRN",
            },
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": "not_started"},
        )

    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    with pytest.raises(ExecutorError):
        executor.execute(pipeline_id="ppl1", event="start")


class _FakeSecretsProvisioner:
    def __init__(self) -> None:
        self.source_calls: list[str] = []
        self.destination_calls: list[str] = []
        self.ca_calls: list[str] = []

    def provision_source_secret(self, source_id: str) -> str:
        self.source_calls.append(source_id)
        return f"qdt-source-{source_id}"

    def provision_destination_secret(self, destination_id: str) -> str:
        self.destination_calls.append(destination_id)
        return f"qdt-destination-{destination_id}"

    def provision_source_ca_secret(self, source_id: str) -> str | None:
        self.ca_calls.append(source_id)
        return None


def test_start_provisions_referenced_secrets_when_a_provisioner_is_injected(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-secrets.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    secrets_provisioner = _FakeSecretsProvisioner()
    executor = KubernetesPipelineExecutor(
        engine,
        jobs_client=_FakeJobsClient(),
        deployments_client=_FakeDeploymentsClient(),
        boundary_reader=_FakeBoundaryReader(),
        evidence_reader=EvidenceReader(_FakeEvidenceObjectStore()),
        config=ExecutorConfig(
            namespace="quadringent",
            reader_image="registry.example.test/quadringent/reader:1.0.0",
            copy_image="registry.example.test/quadringent/copy:1.0.0",
            replay_image="registry.example.test/quadringent/replay:1.0.0",
            raw_prefix_root="raw/example",
            storage_backend="gcs",
            service_account_name="quadringent-capture",
            raw_bucket="acme-raw",
            checkpoint_location="acme-checkpoints",
        ),
        secrets_provisioner=secrets_provisioner,
        run_id_factory=lambda: "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1",
    )

    executor.execute(pipeline_id="ppl1", event="start")

    # Le provisionnement est un upsert idempotent : `start` peut l'appeler
    # plusieurs fois (avant le Job de copie, puis avant le Deployment de
    # lecteur qui référence le même Secret) sans que ce soit une erreur —
    # seul compte qu'il ait bien été demandé pour la bonne source/destination.
    assert set(secrets_provisioner.source_calls) == {"src1"}
    assert set(secrets_provisioner.destination_calls) == {"dst1"}
    engine.dispose()


def test_start_without_a_provisioner_never_touches_secrets(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    # `executor_env` construit l'exécuteur sans `secrets_provisioner` — ce
    # test documente que c'est un no-op sûr, pas une erreur.
    executor.execute(pipeline_id="ppl1", event="start")
    assert len(jobs.created) == 1


# --- Chargeur de destination -------------------------------------------------

_COLUMNS = [
    {"name": "ORDER_ID", "kind": "integer", "nullable": False, "length": None, "precision": None, "scale": None, "timestamp_precision": 6, "ccsid": None},
    {"name": "LABEL", "kind": "varchar", "length": 60, "nullable": True, "precision": None, "scale": None, "timestamp_precision": 6, "ccsid": None},
]


def _set_discovered_columns(engine, table_id: str, columns=_COLUMNS) -> None:
    with engine.begin() as connection:
        connection.execute(
            v2_schema.tables.update().where(v2_schema.tables.c.id == table_id).values(discovered_columns=columns)
        )


def _loader_executor(engine, *, deployments=None, loader_image="registry.example.test/quadringent/loader:1.0.0",
                     reader_poll_seconds=5.0, loader_poll_seconds=10.0, loader_flush_each_batch=False,
                     loader_history_mode="streaming",
                     run_id_factory=lambda: "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1"):
    return KubernetesPipelineExecutor(
        engine,
        jobs_client=_FakeJobsClient(),
        deployments_client=deployments or _FakeDeploymentsClient(),
        boundary_reader=_FakeBoundaryReader(),
        evidence_reader=EvidenceReader(_FakeEvidenceObjectStore()),
        config=ExecutorConfig(
            namespace="quadringent",
            reader_image="registry.example.test/quadringent/reader:1.0.0",
            copy_image="registry.example.test/quadringent/copy:1.0.0",
            replay_image="registry.example.test/quadringent/replay:1.0.0",
            raw_prefix_root="raw/example",
            storage_backend="gcs",
            service_account_name="quadringent-capture",
            loader_image=loader_image,
            reader_poll_seconds=reader_poll_seconds,
            loader_poll_seconds=loader_poll_seconds,
            loader_flush_each_batch=loader_flush_each_batch,
            loader_history_mode=loader_history_mode,
            raw_bucket="acme-raw",
            checkpoint_location="acme-checkpoints",
        ),
        run_id_factory=run_id_factory,
    )


def test_without_loader_image_configured_no_loader_deployment_is_created(executor_env) -> None:
    """``executor_env`` (fixture partagée) ne déclare pas ``loader_image`` :
    compatibilité ascendante — un site sans image de chargeur ne voit
    apparaître aucun Deployment "qdt-loader-*"."""

    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started")
    _set_discovered_columns(engine, "tbl1")

    executor.execute(pipeline_id="ppl1", event="start")

    assert any(name.startswith("qdt-reader-") for name in deployments.objects)
    assert not any(name.startswith("qdt-loader-") for name in deployments.objects)


def test_start_creates_loader_deployment_when_image_configured(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-loader.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(
        engine, deployments=deployments, reader_poll_seconds=1.0, loader_poll_seconds=1.0,
        loader_flush_each_batch=True, loader_history_mode="sql",
    )

    executor.execute(pipeline_id="ppl1", event="start")

    loader_names = [name for name in deployments.objects if name.startswith("qdt-loader-")]
    assert len(loader_names) == 1
    manifest = deployments.objects[loader_names[0]]
    assert manifest["spec"]["replicas"] == 1
    assert manifest["spec"]["strategy"] == {"type": "Recreate"}
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"] == [{"secretRef": {"name": "qdt-destination-dst1"}}]
    assert {e["name"]: e["value"] for e in container["env"]}["QUADRINGENT_LOADER_POLL_SECONDS"] == "1.0"
    assert {e["name"]: e["value"] for e in container["env"]}["QUADRINGENT_STREAMING_FLUSH_EACH_BATCH"] == "true"
    assert {e["name"]: e["value"] for e in container["env"]}["QUADRINGENT_HISTORY_MODE"] == "sql"
    reader = next(value for name, value in deployments.objects.items() if name.startswith("qdt-reader-"))
    reader_env = reader["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {e["name"]: e["value"] for e in reader_env}["AS400_POLL_SECONDS"] == "1.0"
    engine.dispose()


@pytest.mark.parametrize("output_schema,history_schema,mirror_schema", [("SITE_A", "SITE_A", "SITE_A"), (None, "RAW", "CURATED")])
def test_loader_uses_persisted_destination_scope_instead_of_site_globals(tmp_path, output_schema, history_schema, mirror_schema):
    from quadringent_control_plane.v2.crypto import SecretBox
    from quadringent_control_plane.v2.services.destinations import DestinationsService
    from test_v2_destination_verifier import FakeVerifier, _all_ok_result
    dsn = f"sqlite:///{tmp_path / 'destination-scope.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    _set_discovered_columns(engine, "tbl1")
    box = SecretBox(SecretBox.generate_key())
    destinations = DestinationsService(engine, box, org_id="default")
    created, _ = destinations.create(snowflake_account="acme-sf", destination_database="CLIENT_DB", destination_schema=output_schema)
    requests = []
    destinations.verify(created.id, verifier=FakeVerifier(_all_ok_result(), captured=requests))
    assert destinations.get(created.id).verification_state == "verified"
    with engine.begin() as connection:
        connection.execute(v2_schema.pipelines.update().where(v2_schema.pipelines.c.id == "ppl1").values(destination_id=created.id))
        connection.execute(v2_schema.tables.update().where(v2_schema.tables.c.id == "tbl1").values(key_columns="ORDER_ID"))
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(engine, deployments=deployments)
    executor.execute(pipeline_id="ppl1", event="start")
    manifest = next(value for name, value in deployments.objects.items() if name.startswith("qdt-loader-"))
    env = {item["name"]: item["value"] for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["QUADRINGENT_DESTINATION_DATABASE"] == "CLIENT_DB"
    assert env["QUADRINGENT_DESTINATION_DATABASE"] == requests[0].destination_database
    assert requests[0].destination_schema == output_schema
    assert env["QUADRINGENT_DESTINATION_SCHEMA"] == history_schema
    assert env["QUADRINGENT_MIRROR_SCHEMA"] == mirror_schema
    # Le chemin manifest -> script couvre également DDL, MERGE et mesures.
    import quadringent_destination_loader as loader
    from quadringent.snowflake_streaming_loader import MirrorMergePlan, LagQueryPlan
    (table,) = loader.parse_table_set(env["QUADRINGENT_LOADER_TABLE_SET_JSON"])
    plan = loader.build_plan(table, database=env["QUADRINGENT_DESTINATION_DATABASE"],
                             schema=env["QUADRINGENT_DESTINATION_SCHEMA"], mirror_schema=env["QUADRINGENT_MIRROR_SCHEMA"])
    history = f'"CLIENT_DB"."{history_schema}"."ORDHDR_HISTORY"'
    mirror = f'"CLIENT_DB"."{mirror_schema}"."ORDHDR_MIRROR"'
    assert history in plan.history_ddl()
    assert mirror in plan.mirror_ddl()
    assert f"FROM {history}" in MirrorMergePlan(plan).merge_sql()
    assert f"MERGE INTO {mirror}" in MirrorMergePlan(plan).merge_sql()
    assert history in LagQueryPlan(plan).history_lag_sql()
    assert mirror in LagQueryPlan(plan).mirror_lag_sql()
    # Le service persiste normalement cette transition après execute.
    with engine.begin() as connection:
        connection.execute(v2_schema.pipelines.update().where(v2_schema.pipelines.c.id == "ppl1").values(declared_state="copying"))
    executor.reconcile_destination(created.id)
    assert deployments.objects[manifest["metadata"]["name"]] == manifest
    engine.dispose()


@pytest.mark.parametrize("changed_field", ["QUADRINGENT_DESTINATION_DATABASE", "QUADRINGENT_DESTINATION_SCHEMA", "QUADRINGENT_MIRROR_SCHEMA"])
def test_upgrade_stops_loader_without_reusing_checkpoint_in_another_scope(tmp_path, changed_field):
    import copy
    dsn = f"sqlite:///{tmp_path / 'scope-upgrade.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="copying")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(engine, deployments=deployments)
    executor.reconcile_destination("dst1")
    name = next(name for name in deployments.objects if name.startswith("qdt-loader-"))
    previous = copy.deepcopy(deployments.objects[name])
    env = previous["spec"]["template"]["spec"]["containers"][0]["env"]
    next(item for item in env if item["name"] == changed_field)["value"] = "OLD_SCOPE"
    deployments.objects[name] = previous
    for _ in range(2):
        with pytest.raises(ExecutorError, match="périmètre Snowflake.*copie initiale"):
            executor.reconcile_destination("dst1")
        observed = deployments.objects[name]
        assert observed["spec"]["replicas"] == 0
        assert observed["spec"]["template"] == previous["spec"]["template"]
    engine.dispose()


def test_legacy_loader_without_mirror_env_is_treated_as_single_scope(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'scope-legacy-upgrade.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="copying")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(engine, deployments=deployments)
    executor.reconcile_destination("dst1")
    name = next(iter(deployments.objects))
    container = deployments.objects[name]["spec"]["template"]["spec"]["containers"][0]
    container["env"] = [item for item in container["env"] if item["name"] != "QUADRINGENT_MIRROR_SCHEMA"]
    # L'ancien schéma CURATED s'appliquait à HISTORY et MIRROR.
    next(item for item in container["env"] if item["name"] == "QUADRINGENT_DESTINATION_SCHEMA")["value"] = "CURATED"
    with pytest.raises(ExecutorError, match="périmètre Snowflake"):
        executor.reconcile_destination("dst1")
    assert deployments.objects[name]["spec"]["replicas"] == 0
    engine.dispose()


def test_restart_initial_copy_replaces_loader_run_even_with_previous_run_active(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-loader-recopy.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    run_ids = iter((
        "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1",
        "1c1e7b0a-6b9a-4a34-9a4e-0000000000a2",
    ))
    executor = _loader_executor(engine, deployments=deployments, run_id_factory=lambda: next(run_ids))

    executor.execute(pipeline_id="ppl1", event="start")
    name = next(name for name in deployments.objects if name.startswith("qdt-loader-"))
    def loader_evidence_key() -> str:
        env = deployments.objects[name]["spec"]["template"]["spec"]["containers"][0]["env"]
        table_set = next(entry["value"] for entry in env if entry["name"] == "QUADRINGENT_LOADER_TABLE_SET_JSON")
        return json.loads(table_set)[0]["evidence_key"]

    first_key = loader_evidence_key()
    with engine.begin() as connection:
        connection.execute(
            v2_schema.pipelines.update().where(v2_schema.pipelines.c.id == "ppl1").values(declared_state="live")
        )

    executor.execute(pipeline_id="ppl1", event="restart_initial_copy")

    second_key = loader_evidence_key()
    assert first_key != second_key
    assert first_key.endswith("0000000000a1.json")
    assert second_key.endswith("0000000000a2.json")
    assert deployments.replace_calls.count(name) == 1
    with engine.connect() as connection:
        assert connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == "ppl1")
        ).mappings().one()["active_run_id"].endswith("0000000000a2")
    engine.dispose()


def test_loader_reconciliation_fails_closed_without_discovered_columns(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-loader-nocols.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    # Pas d'appel à _set_discovered_columns : discovered_columns reste NULL.
    executor = _loader_executor(engine)

    from quadringent_control_plane.v2.executor.kubernetes import ExecutorError

    with pytest.raises(ExecutorError) as excinfo:
        executor.execute(pipeline_id="ppl1", event="start")
    assert excinfo.value.code == "capability_unavailable"
    engine.dispose()


def test_loader_reconciliation_is_idempotent(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-loader-idempotent.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(engine, deployments=deployments)
    executor.execute(pipeline_id="ppl1", event="start")
    calls_before = len(deployments.replace_calls) + len(deployments.create_calls)

    executor._reconcile_loader_for_destination("dst1")

    assert len(deployments.replace_calls) + len(deployments.create_calls) == calls_before
    engine.dispose()


def test_reconcile_destination_public_method_reapplies_the_desired_loader(tmp_path) -> None:
    """``reconcile_destination`` (chantier réconciliation de dérive,
    24/09/2026) — même contrat public que ``reconcile_source``, côté
    chargeur : idempotent, appelé par ``ReconciliationLoop`` sans ``override``."""

    dsn = f"sqlite:///{tmp_path / 'executor-loader-public.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(engine, deployments=deployments)
    executor.execute(pipeline_id="ppl1", event="start")
    calls_before = len(deployments.replace_calls) + len(deployments.create_calls)

    executor.reconcile_destination("dst1")

    assert len(deployments.replace_calls) + len(deployments.create_calls) == calls_before
    engine.dispose()


def test_pause_removes_the_loader_deployment_when_no_table_remains(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-loader-pause.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="copying")
    _set_discovered_columns(engine, "tbl1")
    deployments = _FakeDeploymentsClient()
    executor = _loader_executor(engine, deployments=deployments)
    executor._reconcile_loader_for_destination("dst1", override={"ppl1": "copying"})
    assert any(name.startswith("qdt-loader-") for name in deployments.objects)

    service = PipelinesService(engine)
    service.apply_action("ppl1", "pause", executor=executor)

    assert not any(name.startswith("qdt-loader-") for name in deployments.objects)
    engine.dispose()


# --- CA épinglé jusqu'aux charges longues (suite chantier 2026-09-24) ------
#
# Les trois sites qui construisent ReaderDesiredSpec/InitialCopyDesiredSpec/
# ReplayDesiredSpec doivent lire sources.tls_pinned_pem et passer
# ca_secret_ref en conséquence — jamais un montage par défaut, jamais
# absent quand la source est épinglée.


def _assert_ca_mounted(manifest: dict, expected_secret: str) -> None:
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    volume = next(v for v in pod_spec["volumes"] if v["name"] == "ibmi-ca")
    assert volume["secret"]["secretName"] == expected_secret
    mount = next(m for m in container["volumeMounts"] if m["name"] == "ibmi-ca")
    assert mount["readOnly"] is True
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["AS400_TLS_CA_FILE"] == "/etc/quadringent/ibmi-ca/ca.pem"


def _assert_no_ca_mount(manifest: dict) -> None:
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert not any(v["name"] == "ibmi-ca" for v in pod_spec.get("volumes", []))
    assert not any(m["name"] == "ibmi-ca" for m in container.get("volumeMounts", []))


def test_initial_copy_job_never_mounts_a_ca_without_a_pin(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="not_started", tls_pinned_pem=None)

    executor.execute(pipeline_id="ppl1", event="start")

    (manifest,) = jobs.created.values()
    _assert_no_ca_mount(manifest)


def test_initial_copy_job_mounts_the_pinned_ca(executor_env) -> None:
    from quadringent_control_plane.v2.executor.manifests import ibmi_ca_secret_ref

    engine, jobs, deployments, boundary_reader, executor = executor_env
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _seed(engine, declared_state="not_started", tls_pinned_pem=pem)

    executor.execute(pipeline_id="ppl1", event="start")

    (manifest,) = jobs.created.values()
    _assert_ca_mounted(manifest, ibmi_ca_secret_ref("src1"))


def test_reader_deployment_never_mounts_a_ca_without_a_pin(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="copying", tls_pinned_pem=None)

    executor._reconcile_reader_for_source("src1", override={"ppl1": "copying"})

    (manifest,) = deployments.objects.values()
    _assert_no_ca_mount(manifest)


def test_reader_deployment_mounts_the_pinned_ca(executor_env) -> None:
    from quadringent_control_plane.v2.executor.manifests import ibmi_ca_secret_ref

    engine, jobs, deployments, boundary_reader, executor = executor_env
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _seed(engine, declared_state="copying", tls_pinned_pem=pem)

    executor._reconcile_reader_for_source("src1", override={"ppl1": "copying"})

    (manifest,) = deployments.objects.values()
    _assert_ca_mounted(manifest, ibmi_ca_secret_ref("src1"))


def test_replay_job_never_mounts_a_ca_without_a_pin(executor_env) -> None:
    engine, jobs, deployments, boundary_reader, executor = executor_env
    _seed(engine, declared_state="live", tls_pinned_pem=None)

    manifest = executor.replay_range("ppl1", from_sequence=100, to_sequence=200)

    _assert_no_ca_mount(manifest)


def test_replay_job_mounts_the_pinned_ca(executor_env) -> None:
    from quadringent_control_plane.v2.executor.manifests import ibmi_ca_secret_ref

    engine, jobs, deployments, boundary_reader, executor = executor_env
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _seed(engine, declared_state="live", tls_pinned_pem=pem)

    manifest = executor.replay_range("ppl1", from_sequence=100, to_sequence=200)

    _assert_ca_mounted(manifest, ibmi_ca_secret_ref("src1"))


def test_start_provisions_the_pinned_ca_secret_when_a_provisioner_is_injected(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'executor-secrets-ca.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine, declared_state="not_started", tls_pinned_pem="-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
    secrets_provisioner = _FakeSecretsProvisioner()
    executor = KubernetesPipelineExecutor(
        engine,
        jobs_client=_FakeJobsClient(),
        deployments_client=_FakeDeploymentsClient(),
        boundary_reader=_FakeBoundaryReader(),
        evidence_reader=EvidenceReader(_FakeEvidenceObjectStore()),
        config=ExecutorConfig(
            namespace="quadringent",
            reader_image="registry.example.test/quadringent/reader:1.0.0",
            copy_image="registry.example.test/quadringent/copy:1.0.0",
            replay_image="registry.example.test/quadringent/replay:1.0.0",
            raw_prefix_root="raw/example",
            storage_backend="gcs",
            service_account_name="quadringent-capture",
            raw_bucket="acme-raw",
            checkpoint_location="acme-checkpoints",
        ),
        secrets_provisioner=secrets_provisioner,
        run_id_factory=lambda: "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1",
    )

    executor.execute(pipeline_id="ppl1", event="start")

    # Même discipline que source_calls/destination_calls (upsert idempotent,
    # appelé avant le Job de copie puis avant le Deployment de lecteur).
    assert set(secrets_provisioner.ca_calls) == {"src1"}
    engine.dispose()
