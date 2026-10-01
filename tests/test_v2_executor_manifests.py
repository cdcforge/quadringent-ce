"""Construction pure des objets Kubernetes désirés — `executor/manifests.py`.

Vérifie les noms déterministes, les variables d'environnement attendues
(`QUADRINGENT_STORAGE_BACKEND`, `AS400_SOURCE_TIME_ZONE`, bootstrap
receiver/séquence, budget vs délai du lecteur), la référence de Secret pour
tout identifiant (jamais de valeur en clair), et le répertoire de sortie
dérivé du `run_id` UUID pour la copie initiale.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.manifests import (
    ENV_MAX_SECONDS,
    ENV_READER_TIMEOUT_SECONDS,
    ENV_SOURCE_TIME_ZONE,
    ENV_STORAGE_BACKEND,
    ENV_TABLE_BOOTSTRAP_JSON,
    ManifestError,
    InitialCopyDesiredSpec,
    ReaderDesiredSpec,
    TableBootstrap,
    build_initial_copy_job,
    build_reader_deployment,
    initial_copy_job_name,
    reader_deployment_name,
    spec_hash,
    with_spec_hash,
)

BOUNDARY = JournalBoundary(
    receiver_library="QGPL",
    receiver_name="RCV0001",
    last_sequence=4200,
    observed_at=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc),
)
RUN_ID = "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1"


def _table(table_id="tbl-1") -> TableBootstrap:
    return TableBootstrap(
        table_id=table_id, schema_name="SALES", table_name="ORDHDR", boundary=BOUNDARY
    )


def _reader_spec(**overrides) -> ReaderDesiredSpec:
    values = dict(
        source_id="src-1",
        journal_library="QGPL",
        journal_name="QSQJRN",
        image="registry.example.test/quadringent/reader:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        source_time_zone="Europe/Paris",
        raw_prefix="raw/example",
        reader_timeout_seconds=300,
        tables=(_table(),),
        destination_secret_ref="qdt-destination-dst-1",
        ibmi_secret_ref="qdt-source-src-1",
        service_account_name="quadringent-capture",
        ibmi_host="as400.example.test",
        ibmi_user="TESTUSER",
        raw_bucket="quadringent-raw-example",
        checkpoint_location="quadringent-checkpoint-example",
    )
    values.update(overrides)
    return ReaderDesiredSpec(**values)


def test_reader_deployment_name_is_deterministic() -> None:
    name1 = reader_deployment_name("src-1", "QGPL", "QSQJRN")
    name2 = reader_deployment_name("src-1", "QGPL", "QSQJRN")
    assert name1 == name2
    assert name1.startswith("qdt-reader-")


def test_reader_deployment_name_differs_per_journal() -> None:
    name_a = reader_deployment_name("src-1", "QGPL", "QSQJRN")
    name_b = reader_deployment_name("src-1", "QGPL", "QSQJRN2")
    assert name_a != name_b


def test_reader_deployment_requires_at_least_one_table() -> None:
    with pytest.raises(ManifestError):
        _reader_spec(tables=())


def test_reader_deployment_requires_source_time_zone() -> None:
    with pytest.raises(ManifestError):
        _reader_spec(source_time_zone="  ")


def test_reader_deployment_requires_secret_refs_not_plaintext() -> None:
    with pytest.raises(ManifestError):
        _reader_spec(destination_secret_ref="")


def test_reader_deployment_carries_required_env_vars() -> None:
    manifest = build_reader_deployment(_reader_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry["value"] for entry in container["env"]}
    assert env[ENV_STORAGE_BACKEND] == "gcs"
    assert env[ENV_SOURCE_TIME_ZONE] == "Europe/Paris"
    assert env[ENV_READER_TIMEOUT_SECONDS] == "300"
    # Le budget (max_seconds) doit toujours dépasser le délai du lecteur —
    # une égalité tronque le dernier poll (leçon de qualification réelle).
    assert int(env[ENV_MAX_SECONDS]) > int(env[ENV_READER_TIMEOUT_SECONDS])
    assert "tbl-1" in env[ENV_TABLE_BOOTSTRAP_JSON]


def test_reader_starts_from_the_older_receiver_even_if_its_sequence_is_higher() -> None:
    older = TableBootstrap(
        table_id="tbl-old", schema_name="SALES", table_name="ORDHDR",
        boundary=JournalBoundary("QGPL", "RCV0001", 100,
                                 datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)),
    )
    newer = TableBootstrap(
        table_id="tbl-new", schema_name="SALES", table_name="ORDLINE",
        boundary=JournalBoundary("QGPL", "RCV0002", 1,
                                 datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)),
    )
    manifest = build_reader_deployment(_reader_spec(tables=(older, newer)))
    env = {entry["name"]: entry["value"] for entry in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AS400_BOOTSTRAP_RECEIVER"] == "RCV0001"
    assert env["AS400_BOOTSTRAP_SEQUENCE"] == "100"


def test_reader_deployment_never_carries_a_plaintext_secret() -> None:
    manifest = build_reader_deployment(_reader_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    secret_refs = {entry["secretRef"]["name"] for entry in container["envFrom"]}
    assert secret_refs == {"qdt-destination-dst-1", "qdt-source-src-1"}
    assert "value" not in {**{e["name"]: e for e in container["env"]}}  # sanity: structure attendue


def test_reader_deployment_single_replica_when_not_paused() -> None:
    manifest = build_reader_deployment(_reader_spec(paused=False))
    assert manifest["spec"]["replicas"] == 1


def test_reader_deployment_scales_to_zero_when_paused() -> None:
    manifest = build_reader_deployment(_reader_spec(paused=True))
    assert manifest["spec"]["replicas"] == 0


def test_reader_deployment_uses_recreate_strategy_to_enforce_one_reader() -> None:
    manifest = build_reader_deployment(_reader_spec())
    assert manifest["spec"]["strategy"]["type"] == "Recreate"


def test_initial_copy_job_requires_uuid_run_id() -> None:
    with pytest.raises(ManifestError):
        InitialCopyDesiredSpec(
            pipeline_id="pipe-1",
            table_id="tbl-1",
            schema_name="SALES",
            table_name="ORDHDR",
            source_id="src-1",
            image="registry.example.test/quadringent/copy:1.0.0",
            namespace="quadringent",
            storage_backend="gcs",
            source_time_zone="Europe/Paris",
            raw_prefix="raw/example",
            boundary=BOUNDARY,
            run_id="not-a-uuid",
            evidence_key="raw/example/tbl-1/evidence/not-a-uuid.json",
            destination_secret_ref="qdt-destination-dst-1",
            ibmi_secret_ref="qdt-source-src-1",
            service_account_name="quadringent-capture",
        )


def _copy_spec(**overrides) -> InitialCopyDesiredSpec:
    values = dict(
        pipeline_id="pipe-1",
        table_id="tbl-1",
        schema_name="SALES",
        table_name="ORDHDR",
        source_id="src-1",
        image="registry.example.test/quadringent/copy:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        source_time_zone="Europe/Paris",
        raw_prefix="raw/example",
        boundary=BOUNDARY,
        run_id=RUN_ID,
        evidence_key=f"raw/example/tbl-1/evidence/{RUN_ID}.json",
        destination_secret_ref="qdt-destination-dst-1",
        ibmi_secret_ref="qdt-source-src-1",
        service_account_name="quadringent-capture",
        ibmi_host="as400.example.test",
        ibmi_user="TESTUSER",
        raw_bucket="quadringent-raw-example",
    )
    values.update(overrides)
    return InitialCopyDesiredSpec(**values)


def test_initial_copy_job_name_derives_from_run_id_never_reused() -> None:
    name_a = initial_copy_job_name("tbl-1", RUN_ID)
    other_run = "1c1e7b0a-6b9a-4a34-9a4e-0000000000a2"
    name_b = initial_copy_job_name("tbl-1", other_run)
    assert name_a != name_b


def test_initial_copy_job_carries_boundary_and_evidence_key_env() -> None:
    manifest = build_initial_copy_job(_copy_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry["value"] for entry in container["env"]}
    assert env["AS400_BOOTSTRAP_RECEIVER"] == "RCV0001"
    assert env["AS400_BOOTSTRAP_SEQUENCE"] == "4200"
    assert env["AS400_SNAPSHOT_RUN_ID"] == RUN_ID
    assert env["AS400_SNAPSHOT_OUTPUT_DIR"].endswith(RUN_ID)
    assert env["AS400_EVIDENCE_KEY"] == f"raw/example/tbl-1/evidence/{RUN_ID}.json"
    assert env["AS400_SOURCE_TIME_ZONE"] == "Europe/Paris"


def test_initial_copy_job_is_never_restarted_by_kubernetes() -> None:
    manifest = build_initial_copy_job(_copy_spec())
    assert manifest["spec"]["backoffLimit"] == 0
    assert manifest["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_spec_hash_is_stable_and_order_independent() -> None:
    spec_a = {"a": 1, "b": 2}
    spec_b = {"b": 2, "a": 1}
    assert spec_hash(spec_a) == spec_hash(spec_b)


def test_spec_hash_changes_with_content() -> None:
    assert spec_hash({"a": 1}) != spec_hash({"a": 2})


def test_with_spec_hash_embeds_annotation_without_mutating_input() -> None:
    manifest = build_reader_deployment(_reader_spec())
    stamped = with_spec_hash(manifest)
    assert "quadringent.io/spec-sha256" in stamped["metadata"]["annotations"]
    assert "quadringent.io/spec-sha256" not in manifest["metadata"]["annotations"]


# --- Chargeur de destination -------------------------------------------------

from quadringent_control_plane.v2.executor.manifests import (
    ENV_DESTINATION_DATABASE,
    ENV_DESTINATION_SCHEMA,
    ENV_LOADER_TABLE_SET_JSON,
    LoaderDesiredSpec,
    LoaderTableSpec,
    build_loader_deployment,
    loader_deployment_name,
)


def _loader_table(table_id="tbl-1") -> LoaderTableSpec:
    return LoaderTableSpec(
        table_id=table_id,
        schema_name="SALES",
        table_name="ORDHDR",
        key_columns=("ORDER_ID",),
        columns=(
            {"name": "ORDER_ID", "kind": "integer", "nullable": False},
            {"name": "LABEL", "kind": "varchar", "length": 60, "nullable": True},
        ),
    )


def _loader_spec(**overrides) -> LoaderDesiredSpec:
    values = dict(
        destination_id="dst-1",
        image="registry.example.test/quadringent/loader:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        raw_bucket="acme-raw",
        raw_prefix="raw/example",
        checkpoint_location="acme-checkpoints",
        destination_database="ACME_RAW",
        destination_schema="IBMI_TEST",
        tables=(_loader_table(),),
        destination_secret_ref="qdt-destination-dst-1",
        service_account_name="quadringent-capture",
    )
    values.update(overrides)
    return LoaderDesiredSpec(**values)


def test_loader_deployment_name_is_deterministic() -> None:
    assert loader_deployment_name("dst-1") == loader_deployment_name("dst-1")
    assert loader_deployment_name("dst-1") != loader_deployment_name("dst-2")
    assert loader_deployment_name("dst-1").startswith("qdt-loader-")


def test_build_loader_deployment_carries_table_set_and_secret_ref() -> None:
    manifest = build_loader_deployment(_loader_spec())

    assert manifest["kind"] == "Deployment"
    assert manifest["metadata"]["name"] == loader_deployment_name("dst-1")
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env[ENV_DESTINATION_DATABASE] == "ACME_RAW"
    assert env["AS400_RAW_BUCKET"] == "acme-raw"
    assert env["AS400_CHECKPOINT_BUCKET"] == "acme-checkpoints"
    assert "AS400_CHECKPOINT_TABLE" not in env  # gcs uniquement dans ce test
    assert env[ENV_DESTINATION_SCHEMA] == "IBMI_TEST"
    table_set = env[ENV_LOADER_TABLE_SET_JSON]
    assert '"table_id": "tbl-1"' not in table_set  # séparateurs compacts, pas d'espace
    assert "ORDHDR" in table_set
    assert container["envFrom"] == [{"secretRef": {"name": "qdt-destination-dst-1"}}]
    # Jamais de valeur en clair pour l'identifiant Snowflake.
    assert "SNOWFLAKE" not in str(container["env"])


def test_build_loader_deployment_single_replica_recreate_strategy() -> None:
    manifest = build_loader_deployment(_loader_spec())
    assert manifest["spec"]["replicas"] == 1
    assert manifest["spec"]["strategy"] == {"type": "Recreate"}


def test_build_loader_deployment_paused_scales_to_zero() -> None:
    manifest = build_loader_deployment(_loader_spec(paused=True))
    assert manifest["spec"]["replicas"] == 0


def test_loader_spec_requires_at_least_one_table() -> None:
    with pytest.raises(ManifestError):
        LoaderDesiredSpec(
            destination_id="dst-1",
            image="img",
            namespace="quadringent",
            storage_backend="gcs",
            raw_bucket="acme-raw",
            raw_prefix="raw/example",
            checkpoint_location="acme-checkpoints",
            destination_database="ACME_RAW",
            destination_schema="IBMI_TEST",
            tables=(),
            destination_secret_ref="qdt-destination-dst-1",
            service_account_name="quadringent-capture",
        )


def test_loader_spec_requires_a_secret_ref() -> None:
    with pytest.raises(ManifestError):
        _loader_spec(destination_secret_ref="")


def test_loader_deployment_selector_matches_destination_label() -> None:
    manifest = build_loader_deployment(_loader_spec())
    assert manifest["spec"]["selector"]["matchLabels"] == {"quadringent.io/destination-id": "dst-1"}


# -- Jobs de diagnostic (sonde de source, découverte de tables) — chantier « prod-wiring » --

import json

from quadringent_control_plane.v2.executor.manifests import (
    SourceProbeJobSpec,
    TableDiscoveryJobSpec,
    build_source_probe_job,
    build_table_discovery_job,
    source_probe_job_name,
    table_discovery_job_name,
)


def _probe_job_spec(**overrides) -> SourceProbeJobSpec:
    values = dict(
        run_id="abc123def456",
        ibmi_host="192.0.2.10",
        ibmi_user="QSECOFR",
        tls_trust="system",
        pinned_fingerprint=None,
        image="registry.example.test/quadringent/capture:1.0.0",
        namespace="quadringent",
        secret_ref='fixture-ref',
    )
    values.update(overrides)
    return SourceProbeJobSpec(**values)


def _discover_job_spec(**overrides) -> TableDiscoveryJobSpec:
    values = dict(
        run_id="abc123def456",
        ibmi_host="192.0.2.10",
        ibmi_user="QSECOFR",
        libraries=("SALES",),
        limit=500,
        search=None,
        image="registry.example.test/quadringent/capture:1.0.0",
        namespace="quadringent",
        secret_ref="qdt-discover-secret-abc123de",
    )
    values.update(overrides)
    return TableDiscoveryJobSpec(**values)


def test_source_probe_job_name_is_deterministic() -> None:
    assert source_probe_job_name("abc123def456") == source_probe_job_name("abc123def456")
    assert source_probe_job_name("abc123def456") != source_probe_job_name("other-run")


def test_source_probe_job_requires_host_and_user() -> None:
    with pytest.raises(ManifestError):
        _probe_job_spec(ibmi_host="")


def test_source_probe_job_requires_a_secret_ref() -> None:
    with pytest.raises(ManifestError):
        _probe_job_spec(secret_ref="")


def test_build_source_probe_job_never_puts_the_password_in_args_or_env() -> None:
    manifest = build_source_probe_job(_probe_job_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    blob = json.dumps(container)
    assert "PWD" not in blob and "password" not in blob.lower() or "envFrom" in container
    assert container["envFrom"] == [{"secretRef": {"name": 'fixture-ref'}}]
    assert "env" not in container or all("value" in e and e["name"] != "ISERIES_PASSWORD" for e in container.get("env", []))


def test_build_source_probe_job_runs_as_non_root_numeric_uid() -> None:
    manifest = build_source_probe_job(_probe_job_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert isinstance(pod_spec["securityContext"]["runAsUser"], int)
    container = pod_spec["containers"][0]
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_build_source_probe_job_has_bounded_resources_and_deadline() -> None:
    manifest = build_source_probe_job(_probe_job_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert "requests" in container["resources"] and "limits" in container["resources"]
    assert manifest["spec"]["activeDeadlineSeconds"] > 0
    assert manifest["spec"]["backoffLimit"] == 0
    assert manifest["spec"]["ttlSecondsAfterFinished"] > 0


def test_build_source_probe_job_uses_the_capture_image() -> None:
    manifest = build_source_probe_job(_probe_job_spec(image="registry.example.test/capture:9.9.9"))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.example.test/capture:9.9.9"


# -- ServiceAccount à identité cloud et sécurisation des charges longues ------
# (chantier « pipeline-exec ») : contrairement aux Jobs de diagnostic, le
# lecteur/la copie initiale/le rejeu/le chargeur tournent avec le
# ServiceAccount à identité cloud du site (S3/GCS/DynamoDB), jamais celui du
# control plane — voir ``ExecutorConfig.service_account_name``.


def test_reader_deployment_requires_a_service_account_name() -> None:
    with pytest.raises(ManifestError):
        _reader_spec(service_account_name="")


def test_reader_deployment_runs_with_the_capture_service_account_and_non_root_uid() -> None:
    manifest = build_reader_deployment(_reader_spec(service_account_name="quadringent-capture"))
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "quadringent-capture"
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert isinstance(pod_spec["securityContext"]["runAsUser"], int)
    container = pod_spec["containers"][0]
    assert container["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_initial_copy_job_requires_a_service_account_name() -> None:
    with pytest.raises(ManifestError):
        _copy_spec(service_account_name="")


def test_initial_copy_job_runs_with_the_capture_service_account_and_non_root_uid() -> None:
    manifest = build_initial_copy_job(_copy_spec(service_account_name="quadringent-capture"))
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "quadringent-capture"
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert isinstance(pod_spec["securityContext"]["runAsUser"], int)


def test_loader_deployment_requires_a_service_account_name() -> None:
    with pytest.raises(ManifestError):
        _loader_spec(service_account_name="")


def test_loader_deployment_runs_with_the_capture_service_account_and_non_root_uid() -> None:
    manifest = build_loader_deployment(_loader_spec(service_account_name="quadringent-capture"))
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "quadringent-capture"
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert isinstance(pod_spec["securityContext"]["runAsUser"], int)


def test_table_discovery_job_name_is_deterministic() -> None:
    assert table_discovery_job_name("abc123def456") == table_discovery_job_name("abc123def456")


def test_discover_job_requires_a_secret_ref() -> None:
    with pytest.raises(ManifestError):
        _discover_job_spec(secret_ref="")


def test_discover_job_requires_limit_in_range() -> None:
    with pytest.raises(ManifestError):
        _discover_job_spec(limit=0)
    with pytest.raises(ManifestError):
        _discover_job_spec(limit=5001)


def test_build_table_discovery_job_never_puts_the_password_in_args() -> None:
    manifest = build_table_discovery_job(_discover_job_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    args_blob = json.dumps(container["args"])
    assert "PWD" not in args_blob and "s3cret" not in args_blob
    assert container["envFrom"] == [{"secretRef": {"name": "qdt-discover-secret-abc123de"}}]


def test_build_table_discovery_job_runs_as_non_root_numeric_uid() -> None:
    manifest = build_table_discovery_job(_discover_job_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert isinstance(pod_spec["securityContext"]["runAsUser"], int)


def test_build_table_discovery_job_passes_libraries_and_search() -> None:
    manifest = build_table_discovery_job(_discover_job_spec(libraries=("SALES", "HR"), search="ORD"))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert "--libraries" in container["args"]
    assert "SALES,HR" in container["args"]
    assert "--search" in container["args"]


# --- Montage du CA épinglé (objectif B, chantier 2026-09-24) ----------------
#
# Sans ca_secret_ref (source à autorité publique ou pas encore épinglée) :
# aucun volume, aucune variable AS400_TLS_CA_FILE — jamais un défaut
# implicite. Avec ca_secret_ref (source épinglée) : Secret monté en lecture
# seule + AS400_TLS_CA_FILE pointant dessus, sur chaque charge qui parle à
# l'IBM i.

from quadringent_control_plane.v2.executor.manifests import (
    ENV_IBMI_TLS_CA_FILE,
    IBMI_CA_MOUNT_PATH,
    ReplayDesiredSpec,
    ibmi_ca_secret_ref,
    build_replay_job,
)


def _assert_no_ca_mount(pod_spec: dict, container: dict) -> None:
    assert not any(v["name"] == "ibmi-ca" for v in pod_spec.get("volumes", []))
    assert not any(m["name"] == "ibmi-ca" for m in container.get("volumeMounts", []))
    env_names = {e["name"] for e in container.get("env", [])}
    assert ENV_IBMI_TLS_CA_FILE not in env_names


def _assert_ca_mounted(pod_spec: dict, container: dict, secret_name: str) -> None:
    volume = next(v for v in pod_spec["volumes"] if v["name"] == "ibmi-ca")
    assert volume["secret"]["secretName"] == secret_name
    mount = next(m for m in container["volumeMounts"] if m["name"] == "ibmi-ca")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == IBMI_CA_MOUNT_PATH
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env[ENV_IBMI_TLS_CA_FILE] == f"{IBMI_CA_MOUNT_PATH}/ca.pem"


def test_ibmi_ca_secret_ref_is_deterministic_per_source() -> None:
    assert ibmi_ca_secret_ref("src-1") == ibmi_ca_secret_ref("src-1")
    assert ibmi_ca_secret_ref("src-1") != ibmi_ca_secret_ref("src-2")


def test_reader_deployment_mounts_no_ca_by_default() -> None:
    manifest = build_reader_deployment(_reader_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_no_ca_mount(pod_spec, pod_spec["containers"][0])


def test_reader_deployment_mounts_the_pinned_ca_when_declared() -> None:
    manifest = build_reader_deployment(_reader_spec(ca_secret_ref=ibmi_ca_secret_ref("src-1")))
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_ca_mounted(pod_spec, pod_spec["containers"][0], ibmi_ca_secret_ref("src-1"))


def test_initial_copy_job_mounts_no_ca_by_default() -> None:
    manifest = build_initial_copy_job(_copy_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_no_ca_mount(pod_spec, pod_spec["containers"][0])


def test_initial_copy_job_mounts_the_pinned_ca_when_declared() -> None:
    manifest = build_initial_copy_job(_copy_spec(ca_secret_ref=ibmi_ca_secret_ref("src-1")))
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_ca_mounted(pod_spec, pod_spec["containers"][0], ibmi_ca_secret_ref("src-1"))


def test_source_probe_job_mounts_no_ca_by_default() -> None:
    manifest = build_source_probe_job(_probe_job_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_no_ca_mount(pod_spec, pod_spec["containers"][0])


def test_source_probe_job_mounts_the_pinned_ca_when_declared() -> None:
    manifest = build_source_probe_job(_probe_job_spec(ca_secret_ref=ibmi_ca_secret_ref("src-1")))
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_ca_mounted(pod_spec, pod_spec["containers"][0], ibmi_ca_secret_ref("src-1"))


def test_table_discovery_job_mounts_no_ca_by_default() -> None:
    manifest = build_table_discovery_job(_discover_job_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_no_ca_mount(pod_spec, pod_spec["containers"][0])


def test_table_discovery_job_mounts_the_pinned_ca_when_declared() -> None:
    manifest = build_table_discovery_job(_discover_job_spec(ca_secret_ref=ibmi_ca_secret_ref("src-1")))
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_ca_mounted(pod_spec, pod_spec["containers"][0], ibmi_ca_secret_ref("src-1"))


def _replay_spec(**overrides) -> ReplayDesiredSpec:
    values = dict(
        pipeline_id="pipe-1",
        table_id="tbl-1",
        schema_name="SALES",
        table_name="ORDHDR",
        source_id="src-1",
        image="registry.example.test/quadringent/replay:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        source_time_zone="Europe/Paris",
        raw_prefix="raw/example",
        receiver_name="RCV0001",
        from_sequence=100,
        to_sequence=200,
        destination_secret_ref="qdt-destination-dst-1",
        ibmi_secret_ref="qdt-source-src-1",
        service_account_name="quadringent-capture",
    )
    values.update(overrides)
    return ReplayDesiredSpec(**values)


def test_replay_job_mounts_no_ca_by_default() -> None:
    manifest = build_replay_job(_replay_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_no_ca_mount(pod_spec, pod_spec["containers"][0])


def test_replay_job_mounts_the_pinned_ca_when_declared() -> None:
    manifest = build_replay_job(_replay_spec(ca_secret_ref=ibmi_ca_secret_ref("src-1")))
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_ca_mounted(pod_spec, pod_spec["containers"][0], ibmi_ca_secret_ref("src-1"))


def test_loader_deployment_never_mounts_a_ca_it_only_talks_to_snowflake() -> None:
    manifest = build_loader_deployment(_loader_spec())
    pod_spec = manifest["spec"]["template"]["spec"]
    _assert_no_ca_mount(pod_spec, pod_spec["containers"][0])


def test_capture_workload_manifests_never_select_the_diagnostic_worker() -> None:
    """``DiagnosticWorker`` n'exige ni fuseau, ni bibliothèque, ni table — un
    manifest de capture (lecteur, copie initiale, rejeu) qui le sélectionnerait
    par erreur (``AS400_JAVA_WORKER_CLASS``) perdrait silencieusement tous les
    contrôles de ``JournalSession.connect`` (voir ``JournalTimestamps``,
    ``verifyCapturedJournal``). Garde-fou : le nom de la classe de diagnostic
    n'apparaît nulle part dans ces trois manifests."""

    manifests = [
        build_reader_deployment(_reader_spec()),
        build_initial_copy_job(_copy_spec()),
        build_replay_job(_replay_spec()),
    ]
    for manifest in manifests:
        serialized = json.dumps(manifest)
        assert "DiagnosticWorker" not in serialized
