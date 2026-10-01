"""Chantier « pipeline-exec » : chaîne de bout en bout via ``entrypoint.build_app``.

Source -> destination -> table découverte -> choix de clé -> démarrage du
pipeline -> charge Kubernetes réelle créée par ``KubernetesPipelineExecutor``,
rejoué contre l'application construite par ``build_app`` — jamais
``create_v2_app`` directement, pour prouver que le câblage de production
(``entrypoint.py::build_pipeline_executor``) fonctionne de bout en bout.

Deux substitutions restent nécessaires, jamais de vrai réseau ni de vraie
JVM (règle du worktree) :

- l'API Kubernetes est un transport HTTP factice en mémoire (même pattern
  que ``tests/test_k8s_jobs.py``), injecté en monkeypatchant
  ``k8s_jobs.https_transport`` — la seule fonction par laquelle
  ``entrypoint._executor_clients_from_environment`` obtient un transport ;
- le lecteur de frontière (``JavaBoundaryReader``) invoque un exécutable
  ``AS400_JAVA`` factice (script Python) qui imprime le format texte attendu
  de ``ReadOnlyReceiverCatalog`` — jamais une vraie JVM/IBM i.

La découverte de tables (Job Kubernetes éphémère,
``KubernetesJobTableDiscoveryClient``) est déjà couverte de bout en bout par
``tests/test_v2_diagnostic_jobs.py`` ; ce test seed directement une table
« déjà découverte » en base pour rester focalisé sur l'exécuteur de
pipeline, sans dupliquer ce mécanisme.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane import k8s_jobs, k8s_pods
from quadringent_control_plane.v2 import entrypoint, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox

_FAKE_CA_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDBTCCAe2gAwIBAgIUW+HIrjo+H4hfdCKnHj6DDXhhCN8wDQYJKoZIhvcNAQEL\n"
    "BQAwEjEQMA4GA1UEAwwHZmFrZS1jYTAeFw0yNjA5MjQxMDMwNDVaFw0yNjA5MjUx\n"
    "MDMwNDVaMBIxEDAOBgNVBAMMB2Zha2UtY2EwggEiMA0GCSqGSIb3DQEBAQUAA4IB\n"
    "DwAwggEKAoIBAQDW9Lp61e6FiOUjfLeyeEuW/pFwHTDHhAF48mlerp9O0x0MSEC3\n"
    "OcXLrtpVNyzOf24y3e3oKD9r7lRxvAMrULZAtezQY+ozS8OVsMojzetZiH4kWpDp\n"
    "R/71ZYepwxTJ3knsKz5qB4llCiOFgCZeA89mSMTrZgphNkxlWlNmI8SMiYo3AwBY\n"
    "95hI8HsLmYtRnH70iRYkbOjfYFVpcSJqNRTbYzm2mogBozrSC6o5zb8dSrx3H+V9\n"
    "hAyxAnUWko++7JD7mn9lMfuuXMWEJZwFfmJx5rLUHUPkasEMkbTOTfd6d+FYgVcN\n"
    "DotIH5Kv4wT5skMDmeStjHpcO+Z7I7b0z5KXAgMBAAGjUzBRMB0GA1UdDgQWBBRj\n"
    "ibNJM7wBt9psoMhlTytuejZpezAfBgNVHSMEGDAWgBRjibNJM7wBt9psoMhlTytu\n"
    "ejZpezAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQAmliI4Ah54\n"
    "5FLzUY4jWTxZL+6hPo+Ep1lJX90No0UxBp61//qmjRf/dncfzbypc5i0zTEC7VRX\n"
    "ysHHkV9GMWeIaLl1mlmDV8Bx2Ha7tWgsa9wMaarbBnqGoA2Xs/70fNQaACy/VG+b\n"
    "vcoso4kcq3iBBh1myjeD2j+/0qtqPf0tEa5yAFH1UPXx8XS937+wFLv6Mjr7+AAD\n"
    "kAEgYJtjIWQN3ppta+HKgHpnM/6o/BCSj1yS3CSQuwx6hqpC3g1sTgUahlyDDpjE\n"
    "KjpaGNbEY8J7KkMenyJTkFYLGWgN61pSUyhr+Lcy2Q9w1oAlXfn1RbMVWSmqszRc\n"
    "B8t6NharRpYd\n"
    "-----END CERTIFICATE-----\n"
)


class _FakeKubernetesApi:
    """Transport HTTP Kubernetes en mémoire — jamais de vrai réseau.

    Couvre les trois ressources requises par l'exécuteur de pipeline
    (Jobs, Deployments, Secrets), avec les mêmes codes de statut que la
    vraie API (`201`/`200`/`404`/`409`), pour que
    ``KubernetesJobsClient``/``KubernetesDeploymentsClient``/
    ``KubernetesSecretsClient`` (jamais modifiés) s'y comportent
    normalement.
    """

    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, body, content_type):
        from quadringent_control_plane.k8s_jobs import JobsResponse

        self.calls.append((method, path))
        import json as _json

        if method == "POST":
            # `path` est déjà le chemin de collection (ex. ".../jobs") — le
            # nom de l'objet posé n'apparaît que dans le manifeste, jamais
            # dans l'URL d'une création Kubernetes.
            manifest = _json.loads(body)
            key = f"{path}/{manifest['metadata']['name']}"
            if key in self.objects:
                return JobsResponse(status=409, body=None)
            self.objects[key] = manifest
            return JobsResponse(status=201, body=manifest)
        if method == "GET":
            manifest = self.objects.get(path)
            if manifest is None:
                return JobsResponse(status=404, body=None)
            # Un Job de copie initiale factice est toujours vu "terminé avec
            # succès" — ce test vérifie la création de la charge, pas la
            # boucle de réconciliation (déjà couverte par
            # ``test_v2_reconciler_lifespan.py``/``test_v2_executor_kubernetes.py``).
            if "/jobs/" in path:
                manifest = {**manifest, "status": {"succeeded": 1}}
            return JobsResponse(status=200, body=manifest)
        if method in ("PUT",):
            manifest = _json.loads(body)
            self.objects[path] = manifest
            return JobsResponse(status=200, body=manifest)
        if method == "PATCH":
            existing = self.objects.get(path, {})
            patch = _json.loads(body)
            merged = {**existing, **patch}
            self.objects[path] = merged
            return JobsResponse(status=200, body=merged)
        if method == "DELETE":
            self.objects.pop(path, None)
            return JobsResponse(status=200, body=None)
        raise AssertionError(f"méthode inattendue : {method}")


def _fake_service_account(tmp_path: Path, monkeypatch, *, namespace: str) -> None:
    sa_root = tmp_path / "serviceaccount"
    sa_root.mkdir()
    (sa_root / "token").write_text("header.payload.signature\n", encoding="utf-8")
    (sa_root / "namespace").write_text(namespace, encoding="utf-8")
    (sa_root / "ca.crt").write_text(_FAKE_CA_PEM, encoding="utf-8")
    monkeypatch.setattr(k8s_jobs, "SERVICE_ACCOUNT_ROOT", str(sa_root))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.10")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")


def _fake_java(tmp_path: Path) -> Path:
    """Script exécutable qui imprime un catalogue de receveurs valide —
    remplace ``AS400_JAVA`` pour ``JavaBoundaryReader``, jamais une vraie
    JVM/IBM i."""

    script = tmp_path / "fake-java.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "print('as400-receiver-catalog-v1')\n"
        "print('receiver\\tQGPL\\tRCV0001\\tATTACHED\\t1\\t4200')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture()
def wired_app(tmp_path, monkeypatch):
    _fake_service_account(tmp_path, monkeypatch, namespace="quadringent-demo")
    fake_java = _fake_java(tmp_path)
    monkeypatch.setenv("AS400_JAVA", str(fake_java))
    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")

    fake_api = _FakeKubernetesApi()
    monkeypatch.setattr(k8s_jobs, "https_transport", lambda *a, **k: fake_api)

    dsn = f"sqlite:///{tmp_path / 'pipeline-exec-integration.sqlite3'}"
    environ = {
        entrypoint.ENV_DATABASE_URL: dsn,
        "QUADRINGENT_V2_SECRET_KEY": SecretBox.generate_key().decode("ascii"),
        "QUADRINGENT_V2_TOKEN_PEPPER": "pepper-de-test-pipeline-exec",
        "QUADRINGENT_SITE_ID": "qpipelineexec",
        "QUADRINGENT_V2_CAPTURE_IMAGE": "registry.example.test/quadringent/capture@sha256:" + "0" * 64,
        "QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT": "quadringent-capture",
        "QUADRINGENT_RAW_BUCKET": "acme-raw",
        "QUADRINGENT_RAW_PREFIX_ROOT": "quadringent/sales",
        "QUADRINGENT_STORAGE_BACKEND": "aws",
        "QUADRINGENT_CHECKPOINT_TABLE": "acme-checkpoints",
    }
    app, engine = entrypoint.build_app(environ)
    assert app.state.pipeline_executor is not None, "l'exécuteur doit être branché pour ce test"
    try:
        # ``build_app`` exige une identité authentifiée (``require_authentication=True``,
        # cookie de session sécurisé) — même parcours que
        # ``test_build_app_requires_authentication_by_default``.
        with TestClient(app, base_url="https://testserver") as client:
            created = client.post(
                "/v2/setup/first-admin",
                json={"email": "operateur@example.com"},
                headers=_idempotency_key("install-pipeline-exec"),
            )
            assert created.status_code == 201
            activation_token = created.json()["after"]["activation_token"]
            activated = client.post(
                "/v2/users/activate",
                json={"token": activation_token, "password": "un-mot-de-passe-robuste"},
                headers=_idempotency_key("activate-pipeline-exec"),
            )
            assert activated.status_code == 200
            login = client.post(
                "/v2/auth/login",
                json={"email": "operateur@example.com", "password": "un-mot-de-passe-robuste"},
            )
            assert login.status_code == 200
            yield client, engine, fake_api
    finally:
        engine.dispose()


def _idempotency_key(label: str) -> dict[str, str]:
    return {"Idempotency-Key": label}


def test_pipeline_start_creates_a_real_kubernetes_job_via_build_app(wired_app) -> None:
    client, engine, fake_api = wired_app

    created_source = client.post(
        "/v2/sources",
        json={
            "display_name": "IBM i — as400.example.test",
            "ibmi_host": "as400.example.test",
            "ibmi_user": "QDTOPER",
            "secret": {"kind": "inline", "value": "un-mot-de-passe-ibmi"},
        },
        headers=_idempotency_key("create-source-pe-1"),
    )
    assert created_source.status_code == 201
    source_id = created_source.json()["after"]["id"]

    created_destination = client.post(
        "/v2/destinations",
        json={"snowflake_account": "acme-xy12345"},
        headers=_idempotency_key("create-destination-pe-1"),
    )
    assert created_destination.status_code == 201
    destination_id = created_destination.json()["after"]["id"]

    # Le fuseau du site est requis par l'exécuteur (voir
    # ``KubernetesPipelineExecutor._load_context``) — posé par le test de
    # source normalement (sonde réelle), seedé ici directement.
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.update()
            .where(v2_schema.sources.c.id == source_id)
            .values(detected_timezone="Europe/Paris")
        )
        connection.execute(
            v2_schema.tables.insert(),
            {
                "id": "tbl-pe-1",
                "source_id": source_id,
                "schema_name": "SALES",
                "table_name": "ORDHDR",
                "journal_library": "QGPL",
                "journal_name": "QSQJRN",
                "readiness": "ready",
                "key_strategy": "primary_key",
                "key_columns": "ORDER_ID",
            },
        )

    started = client.post(
        "/v2/tables/tbl-pe-1/pipeline",
        json={"destination_id": destination_id},
        headers=_idempotency_key("start-pipeline-pe-1"),
    )
    assert started.status_code == 200, started.text
    pipeline_after = started.json()["after"]
    assert pipeline_after["declared_state"] == "copying"

    # Une charge Kubernetes réelle a bien été créée par un client
    # Kubernetes factice — jamais un simulacre applicatif : c'est le
    # transport HTTP qui a reçu les appels.
    job_calls = [path for method, path in fake_api.calls if method == "POST" and "/jobs" in path]
    assert job_calls, "aucun Job de copie initiale n'a été créé"
    deployment_calls = [path for method, path in fake_api.calls if method == "POST" and "/deployments" in path]
    assert deployment_calls, "aucun Deployment de lecteur n'a été créé"

    (job_manifest,) = [m for k, m in fake_api.objects.items() if "/jobs/" in k]
    assert job_manifest["metadata"]["name"].startswith("qdt-copy-")
    container = job_manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["securityContext"]["runAsNonRoot"] is True
    assert job_manifest["spec"]["template"]["spec"]["serviceAccountName"] == "quadringent-capture"
    secret_names = {ref["secretRef"]["name"] for ref in container["envFrom"]}
    assert secret_names == {f"qdt-destination-{destination_id}", f"qdt-source-{source_id}"}
    # Jamais de mot de passe/clé en clair dans le manifeste.
    import json as _json

    blob = _json.dumps(job_manifest)
    assert "un-mot-de-passe-ibmi" not in blob
