"""Sauvegarde logique Postgres -> stockage objet du site (scripts/quadringent_postgres_backup.py).

``pg_dump`` n'est pas disponible dans cet environnement de test : la commande
elle-même est simulée (``dump`` monkeypatché), ce module vérifie
l'orchestration — DSN obligatoire, nom d'objet unique horodaté, publication
via l'object store du backend déclaré (``FileObjectStore`` en test, exactement
le même contrat que S3/GCS en production : ``put_once``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import quadringent_postgres_backup as backup_module
from quadringent.object_store import FileObjectStore


def test_main_requires_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backup_module.ENV_DSN, raising=False)
    with pytest.raises(SystemExit, match=backup_module.ENV_DSN):
        backup_module.main([])


def test_backup_filename_is_unique_and_timestamped() -> None:
    from datetime import datetime, timezone

    name = backup_module.backup_filename(now=datetime(2026, 9, 23, 10, 30, 0, tzinfo=timezone.utc))
    assert name == "quadringent-postgres-20260923T103000Z.dump"


def test_main_dumps_and_publishes_via_the_declared_storage_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dumped_dsns: list[str] = []

    def fake_dump(dsn: str, destination: Path) -> None:
        dumped_dsns.append(dsn)
        destination.write_bytes(b"fake-pg-dump-content")

    monkeypatch.setattr(backup_module, "dump", fake_dump)

    class FakeBackend:
        def object_store(self, prefix: str):
            return FileObjectStore(tmp_path / prefix)

    monkeypatch.setattr(
        backup_module.StorageBackend, "from_environment", staticmethod(lambda environ: FakeBackend())
    )
    monkeypatch.setenv(backup_module.ENV_DSN, "postgresql://quadringent:secret@postgres:5432/quadringent")
    monkeypatch.setenv(backup_module.ENV_PREFIX, "postgres-backups")

    exit_code = backup_module.main([])

    assert exit_code == 0
    assert dumped_dsns == ["postgresql://quadringent:secret@postgres:5432/quadringent"]
    published = list((tmp_path / "postgres-backups").glob("quadringent-postgres-*.dump"))
    assert len(published) == 1
    assert published[0].read_bytes() == b"fake-pg-dump-content"


def test_main_with_dump_file_skips_pg_dump_and_publishes_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode CronJob : un initContainer dédié (image postgres) a déjà produit
    le fichier ; ce script (image control-plane, sans client pg_dump) ne fait
    que le publier — jamais d'exécution de pg_dump dans ce chemin."""

    def fail_if_called(dsn: str, destination: Path) -> None:
        raise AssertionError("pg_dump ne doit pas être invoqué en mode --dump-file")

    monkeypatch.setattr(backup_module, "dump", fail_if_called)

    class FakeBackend:
        def object_store(self, prefix: str):
            return FileObjectStore(tmp_path / prefix)

    monkeypatch.setattr(
        backup_module.StorageBackend, "from_environment", staticmethod(lambda environ: FakeBackend())
    )
    monkeypatch.delenv(backup_module.ENV_DSN, raising=False)
    monkeypatch.setenv(backup_module.ENV_PREFIX, "postgres-backups")

    dump_file = tmp_path / "already-dumped.pgdump"
    dump_file.write_bytes(b"content-produced-by-the-init-container")

    exit_code = backup_module.main(["--dump-file", str(dump_file)])

    assert exit_code == 0
    published = list((tmp_path / "postgres-backups").glob("quadringent-postgres-*.dump"))
    assert len(published) == 1
    assert published[0].read_bytes() == b"content-produced-by-the-init-container"


def test_main_defaults_the_prefix_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_module, "dump", lambda dsn, destination: destination.write_bytes(b"x"))

    class FakeBackend:
        def object_store(self, prefix: str):
            assert prefix == backup_module.DEFAULT_PREFIX
            return FileObjectStore(tmp_path / prefix)

    monkeypatch.setattr(
        backup_module.StorageBackend, "from_environment", staticmethod(lambda environ: FakeBackend())
    )
    monkeypatch.setenv(backup_module.ENV_DSN, "postgresql://quadringent:secret@postgres:5432/quadringent")
    monkeypatch.delenv(backup_module.ENV_PREFIX, raising=False)

    assert backup_module.main([]) == 0
