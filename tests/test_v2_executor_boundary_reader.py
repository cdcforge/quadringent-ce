"""Lecteur de frontière réel — `executor/boundary_reader.py` (chantier
« pipeline-exec »).

Jamais de vraie JVM ni d'IBM i réel ici (règle du worktree) : un faux
catalogue de receveurs déterministe est injecté via ``catalog_factory``,
comme le fait déjà ``diagnostic_jobs.py`` pour ses clients Kubernetes
factices. Vérifie la traduction ``ReceiverSnapshot`` (ATTACHED) ->
``JournalBoundary``, le déchiffrement en mémoire du mot de passe (jamais
transmis en clair à l'appelant), et les refus explicites (table/source
introuvable, aucun journal découvert, aucun receveur ATTACHED).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quadringent.continuous import ReceiverSnapshot
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.executor.boundary_reader import (
    BoundaryUnavailableError,
    JavaBoundaryReader,
)


class _FakeCatalog:
    def __init__(self, snapshots, *, error: Exception | None = None) -> None:
        self._snapshots = snapshots
        self._error = error

    def snapshot(self, required_receiver=None):
        if self._error is not None:
            raise self._error
        return self._snapshots


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'boundary-reader.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(
    engine,
    secret_box: SecretBox,
    *,
    journal_library="QGPL",
    journal_name="QSQJRN",
    tls_pinned_pem: str | None = None,
) -> None:
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
                "secret_ciphertext": secret_box.encrypt("un-mot-de-passe-secret"),
                "detected_timezone": "Europe/Paris",
                "tls_pinned_pem": tls_pinned_pem,
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {
                "id": "tbl1",
                "source_id": "src1",
                "schema_name": "SALES",
                "table_name": "ORDHDR",
                "journal_library": journal_library,
                "journal_name": journal_name,
            },
        )


def _reader(engine, secret_box, factory) -> JavaBoundaryReader:
    return JavaBoundaryReader(
        engine,
        secret_box,
        catalog_factory=factory,
        now=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    )


def test_read_boundary_uses_attached_receiver_last_sequence(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box)
    seen_calls = []

    def factory(*, host, user, password, journal_library, journal_name, ca_file=None):
        seen_calls.append((host, user, password, journal_library, journal_name, ca_file))
        return _FakeCatalog(
            [
                ReceiverSnapshot(
                    receiver_library="QGPL",
                    receiver="RCV0001",
                    status="SAVED",
                    first_sequence=1,
                    last_sequence=1000,
                ),
                ReceiverSnapshot(
                    receiver_library="QGPL",
                    receiver="RCV0002",
                    status="ATTACHED",
                    first_sequence=1001,
                    last_sequence=4200,
                ),
            ]
        )

    reader = _reader(engine, secret_box, factory)
    boundary = reader.read_boundary(source_id="src1", table_id="tbl1")

    assert boundary.receiver_library == "QGPL"
    assert boundary.receiver_name == "RCV0002"
    assert boundary.last_sequence == 4200
    assert boundary.observed_at == datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    # Le mot de passe déchiffré n'est jamais transmis en clair ailleurs qu'à
    # la fabrique du catalogue (sous-processus JVM éphémère).
    (call,) = seen_calls
    assert call == ("as400.example.com", "QSVCUSER", "un-mot-de-passe-secret", "QGPL", "QSQJRN", None)


def test_read_boundary_on_fresh_attached_receiver_with_no_entries_yet(engine) -> None:
    """Receveur ATTACHED tout juste créé, sans entrée écrite : `last_sequence`
    est absent — la bascule doit démarrer à `first_sequence`, jamais à une
    valeur inventée (`bootstrap_sequence = first_sequence - 1`)."""

    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box)

    def factory(**_kwargs):
        return _FakeCatalog(
            [
                ReceiverSnapshot(
                    receiver_library="QGPL", receiver="RCV0001", status="ATTACHED",
                    first_sequence=501, last_sequence=None,
                ),
            ]
        )

    reader = _reader(engine, secret_box, factory)
    boundary = reader.read_boundary(source_id="src1", table_id="tbl1")

    assert boundary.last_sequence == 500
    assert boundary.bootstrap_sequence == 500


def test_read_boundary_rejects_unknown_table(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box)
    reader = _reader(engine, secret_box, lambda **_k: _FakeCatalog([]))
    with pytest.raises(BoundaryUnavailableError) as excinfo:
        reader.read_boundary(source_id="src1", table_id="missing")
    assert excinfo.value.code == "not_found"


def test_read_boundary_rejects_a_table_without_a_discovered_journal(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
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
                "secret_ciphertext": secret_box.encrypt("secret"),
                "detected_timezone": "Europe/Paris",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "SALES", "table_name": "ORDHDR"},
        )
    reader = _reader(engine, secret_box, lambda **_k: _FakeCatalog([]))
    with pytest.raises(BoundaryUnavailableError) as excinfo:
        reader.read_boundary(source_id="src1", table_id="tbl1")
    assert excinfo.value.code == "capability_unavailable"


def test_read_boundary_rejects_no_attached_receiver(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box)
    factory = lambda **_k: _FakeCatalog(
        [ReceiverSnapshot(receiver_library="QGPL", receiver="RCV0001", status="SAVED", first_sequence=1, last_sequence=100)]
    )
    reader = _reader(engine, secret_box, factory)
    with pytest.raises(BoundaryUnavailableError) as excinfo:
        reader.read_boundary(source_id="src1", table_id="tbl1")
    assert excinfo.value.code == "capability_unavailable"


def test_read_boundary_wraps_catalog_failures(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box)
    factory = lambda **_k: _FakeCatalog([], error=RuntimeError("IBM i receiver catalog failed"))
    reader = _reader(engine, secret_box, factory)
    with pytest.raises(BoundaryUnavailableError) as excinfo:
        reader.read_boundary(source_id="src1", table_id="tbl1")
    assert excinfo.value.code == "executor_unavailable"
    # Jamais le détail JDBC brut dans le message exposé.
    assert "IBM i receiver catalog failed" not in str(excinfo.value)


def test_read_boundary_rejects_table_source_mismatch(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box)
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src2",
                "org_id": "default",
                "display_name": "Autre site",
                "ibmi_host": "other.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": secret_box.encrypt("autre-secret"),
                "detected_timezone": "Europe/Paris",
            },
        )
    reader = _reader(engine, secret_box, lambda **_k: _FakeCatalog([]))
    with pytest.raises(BoundaryUnavailableError) as excinfo:
        reader.read_boundary(source_id="src2", table_id="tbl1")
    assert excinfo.value.code == "not_found"


def test_without_a_classpath_construction_fails_explicitly(engine, monkeypatch) -> None:
    monkeypatch.delenv("AS400_JAVA_CLASSPATH", raising=False)
    secret_box = SecretBox(SecretBox.generate_key())
    with pytest.raises(BoundaryUnavailableError) as excinfo:
        JavaBoundaryReader(engine, secret_box)
    assert excinfo.value.code == "capability_unavailable"


# --- CA épinglé écrit en fichier temporaire 0600 (suite chantier 2026-09-24) --


def _attached_snapshot() -> list[ReceiverSnapshot]:
    return [
        ReceiverSnapshot(
            receiver_library="QGPL", receiver="RCV0001", status="ATTACHED",
            first_sequence=1, last_sequence=4200,
        ),
    ]


def test_read_boundary_without_a_pin_never_passes_a_ca_file(engine) -> None:
    secret_box = SecretBox(SecretBox.generate_key())
    _seed(engine, secret_box, tls_pinned_pem=None)
    seen_ca_files = []

    def factory(*, ca_file=None, **_kwargs):
        seen_ca_files.append(ca_file)
        return _FakeCatalog(_attached_snapshot())

    reader = _reader(engine, secret_box, factory)
    reader.read_boundary(source_id="src1", table_id="tbl1")

    assert seen_ca_files == [None]


def test_read_boundary_with_a_pin_writes_a_0600_temp_file_with_the_pem_content(engine) -> None:
    import os
    import stat

    secret_box = SecretBox(SecretBox.generate_key())
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _seed(engine, secret_box, tls_pinned_pem=pem)
    captured_paths = []

    def factory(*, ca_file=None, **_kwargs):
        assert ca_file is not None
        # Le fichier existe, contient exactement le PEM, et n'est lisible/
        # inscriptible que par son propriétaire (0600) — jamais un secret,
        # mais un PEM privé épinglé ne doit pas traîner en clair pour tout
        # le monde sur le disque du control plane.
        with open(ca_file, encoding="us-ascii") as handle:
            assert handle.read() == pem
        mode = stat.S_IMODE(os.stat(ca_file).st_mode)
        assert mode == 0o600
        captured_paths.append(ca_file)
        return _FakeCatalog(_attached_snapshot())

    reader = _reader(engine, secret_box, factory)
    reader.read_boundary(source_id="src1", table_id="tbl1")

    # Nettoyage garanti : le fichier n'existe plus une fois l'appel terminé.
    (path,) = captured_paths
    assert not os.path.exists(path)


def test_read_boundary_cleans_up_the_ca_file_even_when_the_catalog_raises(engine) -> None:
    import os

    secret_box = SecretBox(SecretBox.generate_key())
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _seed(engine, secret_box, tls_pinned_pem=pem)
    captured_paths = []

    def factory(*, ca_file=None, **_kwargs):
        captured_paths.append(ca_file)
        return _FakeCatalog([], error=RuntimeError("connexion JDBC refusée"))

    reader = _reader(engine, secret_box, factory)
    with pytest.raises(BoundaryUnavailableError):
        reader.read_boundary(source_id="src1", table_id="tbl1")

    (path,) = captured_paths
    assert not os.path.exists(path)


def test_read_boundary_never_reuses_the_same_ca_file_path_across_calls(engine) -> None:
    """Deux lectures successives (même source) écrivent — et nettoient —
    chacune leur propre fichier, jamais un chemin partagé qui pourrait
    laisser une fenêtre où un fichier attendu par un appel a déjà été
    supprimé par l'autre."""

    secret_box = SecretBox(SecretBox.generate_key())
    pem = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"
    _seed(engine, secret_box, tls_pinned_pem=pem)
    captured_paths = []

    def factory(*, ca_file=None, **_kwargs):
        captured_paths.append(ca_file)
        return _FakeCatalog(_attached_snapshot())

    reader = _reader(engine, secret_box, factory)
    reader.read_boundary(source_id="src1", table_id="tbl1")
    reader.read_boundary(source_id="src1", table_id="tbl1")

    assert len(captured_paths) == 2
    assert captured_paths[0] != captured_paths[1]
