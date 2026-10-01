"""``ExecutorError``/``BoundaryUnavailableError``/``ManifestError`` -> enveloppe API (objectif D).

Constat du 24 septembre 2026 : ``POST /v2/tables/{id}/pipeline`` (démarrage
automatique) renvoyait une 500 brute (trace Python) quand l'exécuteur
levait ``ExecutorError("capability_unavailable", "colonnes non déclarées ...")``
— jamais l'enveloppe standard ``{"error": {...}}``. Ce fichier verrouille
la conversion (``v2/app.py`` :: gestionnaires d'exception) et l'absence
d'état incohérent en base quand le démarrage échoue après la création du
pipeline (``PipelinesService.start_table_pipeline``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.errors import from_executor_error
from quadringent_control_plane.v2.executor.boundary_reader import BoundaryUnavailableError
from quadringent_control_plane.v2.executor.kubernetes import ExecutorError
from quadringent_control_plane.v2.executor.manifests import ManifestError


class _FailingExecutor:
    """Lève l'erreur injectée sur ``execute`` — jamais d'effet de bord réel."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def execute(self, *, pipeline_id: str, event: str, **kwargs: object) -> None:
        raise self._error


@pytest.fixture()
def seeded_engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'executor_error_api.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "org1",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": "org1",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "TESTLIB", "table_name": "QDC_ORDERS"},
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _start(engine, executor) -> "TestClient":
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        org_id="org1",
        pipeline_executor=executor,
    )
    return TestClient(app)


@pytest.mark.parametrize(
    "error, expected_status, expected_code",
    [
        (
            ExecutorError(
                "capability_unavailable",
                "colonnes non déclarées pour QDC_ORDERS — voir PUT /v2/tables/{id}/discovered-columns",
            ),
            409,
            "capability_unavailable",
        ),
        (ExecutorError("not_found", "pipeline introuvable pour l'exécuteur"), 404, "not_found"),
        (
            ExecutorError("executor_unavailable", "application du Deployment refusée"),
            503,
            "executor_unavailable",
        ),
        (
            BoundaryUnavailableError("executor_unavailable", "lecture de la position du journal impossible"),
            503,
            "executor_unavailable",
        ),
        (
            BoundaryUnavailableError("capability_unavailable", "aucun receveur ATTACHED pour ce journal"),
            409,
            "capability_unavailable",
        ),
        (
            ManifestError("ISERIES_HOST et ISERIES_USER sont obligatoires pour le lecteur"),
            422,
            "invalid_configuration",
        ),
    ],
)
def test_pipeline_start_never_returns_a_raw_500_on_executor_failure(
    seeded_engine, error, expected_status, expected_code
) -> None:
    client = _start(seeded_engine, _FailingExecutor(error))

    response = client.post(
        "/v2/tables/tbl1/pipeline",
        json={"destination_id": "dst1"},
        headers={"Idempotency-Key": "start-1"},
    )

    assert response.status_code == expected_status, response.text
    body = response.json()
    assert body["error"]["code"] == expected_code
    assert body["error"]["next_action"]
    assert "message" in body["error"]


def test_pipeline_start_failure_leaves_the_new_pipeline_not_started_never_half_applied(seeded_engine) -> None:
    """``start_table_pipeline`` crée d'abord le pipeline (``not_started``,
    commité) puis invoque l'exécuteur : si l'exécuteur échoue, la ligne ne
    doit jamais être trouvée dans un état intermédiaire — ``not_started``
    reste vrai (« rien n'a démarré ») et un nouvel appel à ``start`` peut
    retenter la même transition."""

    error = ExecutorError("capability_unavailable", "colonnes non déclarées pour QDC_ORDERS")
    client = _start(seeded_engine, _FailingExecutor(error))

    response = client.post(
        "/v2/tables/tbl1/pipeline",
        json={"destination_id": "dst1"},
        headers={"Idempotency-Key": "start-2"},
    )
    assert response.status_code == 409

    with seeded_engine.connect() as connection:
        rows = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.table_id == "tbl1")
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["declared_state"] == "not_started"


def test_from_executor_error_falls_back_to_executor_unavailable_for_an_unknown_code() -> None:
    api_error = from_executor_error(ExecutorError("some_future_code", "détail"))
    assert api_error.status_code == 503
    assert api_error.code == "executor_unavailable"
    assert api_error.retryable is True
