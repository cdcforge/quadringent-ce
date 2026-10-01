"""Contrats manifeste <-> script réellement exécuté dans le conteneur.

Constat du 24 septembre 2026 (premier démarrage réel d'un pipeline sur
GKE, ``QDC_ORDERS``) : les tests de ``test_v2_executor_manifests.py``
vérifient la *forme* d'un manifeste (noms, secrets, budgets) mais jamais
qu'un script réel, démarré avec exactement ce ``command``/``args``/``env``,
accepte ses arguments et résout sa configuration avant tout I/O réseau.
Trois défauts ont échappé à ces tests :

1. la copie initiale héritait de l'ENTRYPOINT de capture continue
   (``as400_continuous_capture.py``), qui refusait ``--schema``/``--table`` ;
2. le lecteur posait ``AS400_FLEET_TABLES`` seule (sans
   ``AS400_FLEET_TABLE_ROOT``), ce que
   ``quadringent.fleet_capture.fleet_mode_from_environment`` refuse
   (``FleetConfigurationError``) — et omettait ``ISERIES_HOST``/
   ``ISERIES_USER``/``ISERIES_SCHEMA``/``ISERIES_TABLE``/
   ``AS400_RAW_BUCKET``/le checkpoint, tous exigés par
   ``scripts/as400_continuous_capture.py`` avant tout I/O ;
3. le chargeur de destination héritait du même ENTRYPOINT par défaut, et
   ``scripts/quadringent_destination_loader.py`` n'était de toute façon pas
   copié dans l'image, pas plus que deux modules qu'il importe
   (``snowflake_destination``, ``snowflake_streaming_loader``).

Chaque test ci-dessous rend un manifeste pour un cas réaliste, en extrait
``command``/``args``/``env``, vérifie que le script visé est bien copié dans
``docker/Dockerfile``, puis exécute la résolution réelle de configuration du
script (son analyseur d'arguments et ses fonctions ``_required``/``from_
environment``) jusqu'au point où une connexion réseau serait ouverte, sans
jamais l'ouvrir.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import EvidenceReader, InitialCopyEvidence
from quadringent_control_plane.v2.executor.manifests import (
    InitialCopyDesiredSpec,
    LoaderDesiredSpec,
    LoaderTableSpec,
    ReaderDesiredSpec,
    TableBootstrap,
    build_initial_copy_job,
    build_loader_deployment,
    build_reader_deployment,
)

DOCKERFILE = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

BOUNDARY = JournalBoundary(
    receiver_library="TESTLIB",
    receiver_name="RCV0001",
    last_sequence=4200,
    observed_at=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
)


def _dockerfile_copies(source_path: str) -> bool:
    """Le Dockerfile de capture copie-t-il ce fichier du dépôt vers l'image ?"""

    return any(
        line.strip().startswith("COPY") and source_path in line for line in DOCKERFILE.splitlines()
    )


def _container_env(manifest: dict) -> dict[str, str]:
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry["value"] for entry in container.get("env", [])}


def _reader_spec(**overrides) -> ReaderDesiredSpec:
    table = TableBootstrap(
        table_id="tbl-orders", schema_name="TESTLIB", table_name="QDC_ORDERS", boundary=BOUNDARY
    )
    values = dict(
        source_id="src-1",
        journal_library="TESTLIB",
        journal_name="TESTJRN",
        image="registry.example.test/quadringent/capture:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        source_time_zone="Europe/Paris",
        raw_prefix="raw/example",
        reader_timeout_seconds=300,
        tables=(table,),
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


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_reader_deployment_command_targets_a_script_shipped_in_the_image(backend: str) -> None:
    """Le lecteur n'a pas de ``command`` explicite : il hérite de
    l'ENTRYPOINT de ``docker/Dockerfile``. Ce test verrouille que ce script
    par défaut est bien celui copié dans l'image."""

    assert _dockerfile_copies("scripts/as400_continuous_capture.py")
    assert "ENTRYPOINT" in DOCKERFILE and "as400_continuous_capture.py" in DOCKERFILE


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_reader_deployment_env_satisfies_fleet_mode_resolution_single_table(backend: str) -> None:
    """Une seule table : ``fleet_mode_from_environment`` ne doit PAS lever
    ``FleetConfigurationError`` — c'est le bug constaté (AS400_FLEET_TABLES
    posée seule) le 24 septembre 2026 sur QDC_ORDERS."""

    from quadringent.fleet_capture import fleet_mode_from_environment

    manifest = build_reader_deployment(_reader_spec(storage_backend=backend))
    env = _container_env(manifest)

    fleet = fleet_mode_from_environment(env)
    assert fleet is None  # mono-table : jamais le mode flotte

    assert env["ISERIES_HOST"] == "as400.example.test"
    assert env["ISERIES_USER"] == "TESTUSER"
    assert env["ISERIES_SCHEMA"] == "TESTLIB"
    assert env["ISERIES_TABLE"] == "QDC_ORDERS"
    assert env["AS400_RAW_BUCKET"] == "quadringent-raw-example"
    checkpoint_key = "AS400_CHECKPOINT_BUCKET" if backend == "gcs" else "AS400_CHECKPOINT_TABLE"
    assert env[checkpoint_key] == "quadringent-checkpoint-example"
    assert env["AS400_STREAM_KEY"]


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_reader_deployment_env_satisfies_fleet_mode_resolution_multi_table(backend: str) -> None:
    """Plusieurs tables sur le même journal : le mode flotte doit résoudre
    sans erreur, avec des noms de table réels (pas des identifiants
    internes) dans ``AS400_FLEET_TABLES``."""

    from quadringent.fleet_capture import fleet_mode_from_environment

    second = TableBootstrap(
        table_id="tbl-lines", schema_name="TESTLIB", table_name="QDC_ORDER_LINES", boundary=BOUNDARY
    )
    first = TableBootstrap(
        table_id="tbl-orders", schema_name="TESTLIB", table_name="QDC_ORDERS", boundary=BOUNDARY
    )
    manifest = build_reader_deployment(_reader_spec(storage_backend=backend, tables=(first, second)))
    env = _container_env(manifest)

    fleet = fleet_mode_from_environment(env)
    assert fleet is not None
    assert set(fleet.tables) == {"QDC_ORDERS", "QDC_ORDER_LINES"}
    assert env["ISERIES_TABLE"] in fleet.tables


def _loader_spec(**overrides) -> LoaderDesiredSpec:
    table = LoaderTableSpec(
        table_id="tbl-orders",
        schema_name="TESTLIB",
        table_name="QDC_ORDERS",
        key_columns=("ORDER_ID",),
        columns=({"name": "ORDER_ID", "type": "VARCHAR", "length": 20, "scale": None, "nullable": False},),
    )
    values = dict(
        destination_id="dst-1",
        image="registry.example.test/quadringent/capture:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        raw_bucket="quadringent-raw-example",
        raw_prefix="raw/example",
        checkpoint_location="quadringent-checkpoint-example",
        destination_database="QUADRINGENT",
        destination_schema="CURATED",
        tables=(table,),
        destination_secret_ref="qdt-destination-dst-1",
        service_account_name="quadringent-capture",
    )
    values.update(overrides)
    return LoaderDesiredSpec(**values)


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_loader_deployment_command_targets_a_script_shipped_in_the_image(backend: str) -> None:
    manifest = build_loader_deployment(_loader_spec(storage_backend=backend))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    # Le chargeur tourne dans l'image du control plane, la seule qui porte
    # snowflake-connector-python et snowpipe-streaming ; cette image n'a pas
    # de /opt/venv (constaté sur GKE : « stat /opt/venv/bin/python: no such
    # file »), son interpréteur est ``python`` dans le PATH.
    control_plane = (ROOT / "docker/control-plane.Dockerfile").read_text()
    requirements = (ROOT / "docker/control-plane-requirements.in").read_text()
    # ``-P`` : /app contient un quadringent_control_plane.py qui masquerait le
    # paquet du même nom importé par le chargeur (constaté sur GKE).
    assert container["command"] == ["python", "-P", "/app/quadringent_destination_loader.py"]
    assert "COPY scripts/quadringent_control_plane.py /app/" in control_plane or "quadringent_control_plane.py" in control_plane
    assert "/opt/venv" not in control_plane
    assert "snowpipe-streaming" in requirements and "snowflake-connector-python" in requirements
    for source in ("scripts/quadringent_destination_loader.py", "src/quadringent/snowflake_destination.py",
                   "src/quadringent/snowflake_streaming_loader.py", "src/quadringent/snowflake_sql_loader.py"):
        assert any(line.startswith("COPY") and source in line for line in control_plane.splitlines()), source


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_loader_deployment_env_satisfies_the_script_real_argument_parser(backend: str) -> None:
    """Exécute ``quadringent_destination_loader.main``'s parser et la
    résolution ``parse_table_set``/``StorageBackend.from_environment`` avec
    exactement l'environnement produit par le manifeste — jusqu'au point où
    une connexion Snowflake/objet serait ouverte, sans l'ouvrir."""

    import importlib

    loader_module = importlib.import_module("quadringent_destination_loader")
    from quadringent.storage_backend import StorageBackend

    manifest = build_loader_deployment(_loader_spec(storage_backend=backend))
    env = _container_env(manifest)
    # Complète l'environnement avec les clés du Secret Snowflake (valeurs
    # factices non sensibles) — jamais portées par le manifeste lui-même
    # (``envFrom.secretRef``, vérifié ailleurs).
    env = {
        **env,
        "SNOWFLAKE_ACCOUNT": "acme-quadringent",
        "SNOWFLAKE_USER": "svc_quadringent",
        "SNOWFLAKE_ROLE": "QUADRINGENT_LOADER",
        "SNOWFLAKE_PRIVATE_KEY_PEM": 'fixture-private-key',
    }

    parser_args = loader_module.main.__globals__["argparse"].ArgumentParser().parse_args([])
    assert parser_args is not None  # le script accepte d'être invoqué sans argument positionnel

    tables = loader_module.parse_table_set(env["QUADRINGENT_LOADER_TABLE_SET_JSON"])
    assert tables[0].table_name == "QDC_ORDERS"

    backend_client = object() if backend == "gcs" else None
    storage = StorageBackend.from_environment(env, gcs_client=backend_client)
    assert storage.raw_bucket == "quadringent-raw-example"


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_loader_deployment_secret_reaches_a_non_empty_consistent_snowflake_host(backend: str) -> None:
    """Le manifeste transmet un hôte et une URL cohérents au SDK.
    Rend le manifeste, remonte l'environnement et
    l'identité Snowflake exactement comme ``main`` les assemble, puis
    construit la vraie configuration des deux clients (profil Snowpipe
    Streaming, arguments ``snowflake.connector.connect``) jusqu'au point où
    chacun recevrait ``host``/``url``, sans ouvrir de connexion."""

    import importlib

    loader_module = importlib.import_module("quadringent_destination_loader")

    manifest = build_loader_deployment(_loader_spec(storage_backend=backend))
    env = _container_env(manifest)
    account = "EXAMPLEORG-EXAMPLEACCOUNT"
    role = "QDT_ROLE_LOADER"

    profile = loader_module._streaming_profile(
        account=account,
        user="svc_quadringent",
        role=role,
        private_key_pem='fixture-private-key',
        warehouse=loader_module._snowflake_role_to_warehouse(role),
        database=env["QUADRINGENT_DESTINATION_DATABASE"],
        schema=env["QUADRINGENT_DESTINATION_SCHEMA"],
    )
    assert profile["host"] == "exampleorg-exampleaccount.snowflakecomputing.com"
    assert profile["url"] == "https://exampleorg-exampleaccount.snowflakecomputing.com"
    assert profile["account"] == account

    connector_kwargs = loader_module._connector_kwargs(
        account=account,
        user="svc_quadringent",
        role=role,
        private_key_der=b"fake-der-bytes",
        warehouse=loader_module._snowflake_role_to_warehouse(role),
    )
    assert connector_kwargs["host"] == profile["host"]
    assert connector_kwargs["host"]


# --- Copie initiale (objectif B.1, chantier 2026-09-24) ---------------------
#
# Constat : le Job héritait de l'ENTRYPOINT de capture continue, qui refuse
# --schema/--table (« unrecognized arguments »). Le script cible
# (scripts/quadringent_initial_copy_job.py) est piloté par l'environnement,
# comme ReadOnlyTableSnapshot (Java) qu'il invoque en sous-processus.

BOUNDARY_COPY = JournalBoundary(
    receiver_library="TESTLIB",
    receiver_name="RCV0001",
    last_sequence=4200,
    observed_at=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
)
RUN_ID = "1c1e7b0a-6b9a-4a34-9a4e-0000000000a1"


def _copy_spec(**overrides) -> InitialCopyDesiredSpec:
    values = dict(
        pipeline_id="pipe-1",
        table_id="tbl-orders",
        schema_name="TESTLIB",
        table_name="QDC_ORDERS",
        source_id="src-1",
        image="registry.example.test/quadringent/capture:1.0.0",
        namespace="quadringent",
        storage_backend="gcs",
        source_time_zone="Europe/Paris",
        raw_prefix="raw/example",
        boundary=BOUNDARY_COPY,
        run_id=RUN_ID,
        evidence_key=f"raw/example/tbl-orders/evidence/{RUN_ID}.json",
        destination_secret_ref="qdt-destination-dst-1",
        ibmi_secret_ref="qdt-source-src-1",
        service_account_name="quadringent-capture",
        ibmi_host="as400.example.test",
        ibmi_user="TESTUSER",
        raw_bucket="quadringent-raw-example",
    )
    values.update(overrides)
    return InitialCopyDesiredSpec(**values)


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_initial_copy_job_command_targets_a_script_shipped_in_the_image(backend: str) -> None:
    manifest = build_initial_copy_job(_copy_spec(storage_backend=backend))
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/opt/venv/bin/python", "/app/quadringent_initial_copy_job.py"]
    assert "args" not in container  # tout passe par l'environnement, comme ReadOnlyTableSnapshot
    assert _dockerfile_copies("scripts/quadringent_initial_copy_job.py")
    assert _dockerfile_copies("scripts/as400_snapshot_publish.py")
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in manifest["spec"]["template"]["spec"]["volumes"]}
    assert mounts["snapshot-work"] == "/var/run/quadringent/snapshot"
    assert "emptyDir" in volumes["snapshot-work"]


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_initial_copy_job_env_satisfies_the_script_real_config_resolution(backend: str) -> None:
    """Exécute ``InitialCopyConfig.from_environment`` — la résolution réelle
    du script — avec exactement l'environnement produit par le manifeste,
    jusqu'au point où une connexion IBM i/objet serait ouverte, sans
    l'ouvrir. Rouge avant ce correctif : ISERIES_HOST/USER, AS400_RAW_BUCKET,
    la bibliothèque de receiver et la position observée manquaient."""

    import quadringent_initial_copy_job as copy_job

    manifest = build_initial_copy_job(_copy_spec(storage_backend=backend))
    env = _container_env(manifest)
    env["ISERIES_PASSWORD"] = "s3cret-not-real"  # vient du Secret (envFrom), jamais du manifeste
    # AS400_JAVA/AS400_JAVA_CLASSPATH viennent des ENV par défaut de
    # docker/Dockerfile, pas du manifeste — reproduits ici comme le
    # conteneur réel les verrait.
    env.setdefault("AS400_JAVA", "java")
    env.setdefault("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")

    config = copy_job.InitialCopyConfig.from_environment(env)
    assert config.ibmi_host == "as400.example.test"
    assert config.schema == "TESTLIB"
    assert config.table == "QDC_ORDERS"
    assert config.receiver_library == "TESTLIB"
    assert config.receiver_name == "RCV0001"
    assert config.bootstrap_sequence == 4200
    assert config.raw_bucket == "quadringent-raw-example"
    assert str(config.output_dir).endswith(RUN_ID)
    assert config.pipeline_id == "pipe-1"
    assert config.table_id == "tbl-orders"


def test_initial_copy_job_orchestration_publishes_and_writes_evidence_read_by_the_executor(
    tmp_path,
) -> None:
    """Bout en bout hors ligne : faux sous-processus Java qui écrit un lot
    réaliste, magasin objet en mémoire (même doublure que
    ``tests/test_snapshot_publish.py``). Vérifie que la publication a bien
    lieu et que la preuve, une fois écrite, se relit avec
    ``InitialCopyEvidence.from_dict`` — le format exact attendu par
    ``EvidenceReader`` côté control plane."""

    from unittest.mock import patch

    import quadringent_initial_copy_job as copy_job
    from test_gcs_backend import FakeGcsClient

    manifest = build_initial_copy_job(_copy_spec(storage_backend="gcs"))
    env = dict(_container_env(manifest))
    env["ISERIES_PASSWORD"] = "s3cret-not-real"
    env["AS400_JAVA"] = "java"
    env["AS400_JAVA_CLASSPATH"] = "/app/probe.jar:/app/lib/*"
    # En production, AS400_SNAPSHOT_OUTPUT_DIR est un emptyDir monté par le
    # manifest (voir build_initial_copy_job) — hors conteneur, on pointe
    # vers un répertoire temporaire jetable pour le même contrat.
    env["AS400_SNAPSHOT_OUTPUT_DIR"] = str(tmp_path / "snapshot" / RUN_ID)

    class _FakeCompletedProcess:
        def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_java_runner(command, *, env, capture_output, text, check):
        # Écrit un lot réaliste dans le répertoire de sortie, comme le ferait
        # RawCaptureWriter (Java) — un fichier payload et son manifeste.
        output_dir = Path(env["AS400_RAW_DIRECTORY"])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "batch-a1.jsonl").write_text('{"event_id": "e1"}\n', encoding="utf-8")
        (output_dir / "batch-a1.manifest.json").write_text("{}", encoding="utf-8")
        summary = (
            "snapshot_summary table=TESTLIB.QDC_ORDERS rows=3 columns=5 "
            "batches=1 elapsed_ms=42 run_id=" + env["AS400_SNAPSHOT_RUN_ID"]
        )
        return _FakeCompletedProcess(0, summary)

    client = FakeGcsClient()
    config = copy_job.InitialCopyConfig.from_environment(env)
    assert str(config.output_dir) != str(tmp_path)  # sanity : chemin dérivé du run_id, jamais codé en dur

    with patch("quadringent.gcs_backend._client", return_value=client):
        rows_copied = copy_job.run_snapshot(config, base_environ=env, runner=fake_java_runner)
        assert rows_copied == 3

        snapshot_batches = copy_job.publish_snapshot(config, base_environ=env)
        published = {name for _, name in client.objects}
        # Disposition unique (quadringent.storage_layout) : la table est le
        # premier segment — corrigé le 24 septembre 2026, voir storage_layout.py.
        assert "raw/example/qdc_orders/snapshot/batch-a1.jsonl" in published
        assert "raw/example/qdc_orders/snapshot/batch-a1.manifest.json" in published
        # Noms nus (pas le chemin complet) : c'est le contrat de
        # ``object_store.read_published_batch``, que le chargeur appelle avec
        # un magasin déjà borné au préfixe d'instantané de la table.
        assert snapshot_batches == [
            {"payload_key": "batch-a1.jsonl", "manifest_key": "batch-a1.manifest.json"}
        ]

        evidence = copy_job.build_evidence(
            config, rows_copied=rows_copied, completed_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            snapshot_batches=snapshot_batches,
        )
        copy_job.write_evidence(config, evidence)

    # EvidenceReader (control plane) relit exactement ce que le Job a écrit.
    class _ReaderObjectStore:
        def __init__(self, client, bucket: str) -> None:
            from quadringent.gcs_backend import GcsObjectStore

            self._store = GcsObjectStore(bucket, "", client=client)

        def get_bounded(self, key: str, max_bytes: int) -> bytes:
            blob = self._store._bucket.get_blob(key)
            if blob is None:
                raise FileNotFoundError(key)
            return blob.download_as_bytes()

    reader = EvidenceReader(_ReaderObjectStore(client, "quadringent-raw-example"))
    read_back = reader.read(config.evidence_key)
    from quadringent_control_plane.v2.executor.evidence import SnapshotBatchRef

    assert read_back == InitialCopyEvidence(
        pipeline_id="pipe-1",
        table_id="tbl-orders",
        run_id=RUN_ID,
        boundary=BOUNDARY_COPY,
        rows_copied=3,
        completed_at=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
        snapshot_batches=(
            SnapshotBatchRef(payload_key="batch-a1.jsonl", manifest_key="batch-a1.manifest.json"),
        ),
    )


@pytest.mark.parametrize("backend", ["gcs", "aws"])
def test_reader_deployment_env_gives_the_script_its_journal_bootstrap(backend: str, monkeypatch) -> None:
    """Constaté sur GKE : le lecteur tournait sans position de départ
    (« an explicit bootstrap position is required », disjoncteur ouvert) —
    le manifest posait ``AS400_TABLE_BOOTSTRAP_JSON``, qu'aucun script ne lit.
    La position vient de la frontière relevée pour la copie initiale ; avec
    plusieurs tables sur un journal, la plus ancienne (dédoublonnage par
    ``event_id`` côté destination)."""
    import importlib

    later = JournalBoundary(receiver_library="TESTLIB", receiver_name="RCV0002",
                            last_sequence=5000, observed_at=BOUNDARY.observed_at)
    tables = (
        TableBootstrap(table_id="tbl-tail", schema_name="TESTLIB", table_name="QDC_TAIL", boundary=later),
        TableBootstrap(table_id="tbl-orders", schema_name="TESTLIB", table_name="QDC_ORDERS", boundary=BOUNDARY),
    )
    for spec_tables in ((tables[1],), tables):
        env = _container_env(build_reader_deployment(_reader_spec(storage_backend=backend, tables=spec_tables)))
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        capture = importlib.import_module("as400_continuous_capture")
        position = capture._bootstrap_position()
        assert (position.receiver, position.sequence) == (BOUNDARY.receiver_name, BOUNDARY.bootstrap_sequence)
