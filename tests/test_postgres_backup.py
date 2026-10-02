"""Sauvegarde logique Postgres -> stockage objet du site (scripts/quadringent_postgres_backup.py).

``pg_dump`` n'est pas disponible dans cet environnement de test : la commande
elle-même est simulée (``dump`` monkeypatché), ce module vérifie
l'orchestration — DSN obligatoire, nom d'objet unique horodaté, publication
via l'object store du backend déclaré (``FileObjectStore`` en test, exactement
le même contrat que S3/GCS en production : ``put_once``).
"""

from __future__ import annotations

import subprocess
import hashlib
import json
from pathlib import Path

import pytest

import quadringent_postgres_backup as backup_module
from quadringent.object_store import FileObjectStore


@pytest.fixture(autouse=True)
def without_local_pg_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Les archives simulées ne dépendent pas des outils installés sur l'hôte."""
    monkeypatch.setattr(backup_module.shutil, "which", lambda command: None)


@pytest.fixture
def local_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injecte un décodeur pour les tests d'orchestration de pg_dump."""
    monkeypatch.setattr(backup_module.shutil, "which", lambda command: "/tools/pg_restore")
    monkeypatch.setattr(backup_module.subprocess, "run", lambda *args, **kwargs: None)


def validation_receipt(path: Path) -> Path:
    """Simule le reçu privé lié aux octets décodés par l'initContainer."""
    content = path.read_bytes()
    receipt = path.with_suffix(".validation.json")
    receipt.write_text(json.dumps({
        "schema": "quadringent.pg-backup-validation.v1",
        "validator": "pg_restore-full-sql-decode",
        "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    }))
    return receipt


def test_main_requires_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backup_module.ENV_DSN, raising=False)
    with pytest.raises(SystemExit, match=backup_module.ENV_DSN):
        backup_module.main([])


def test_backup_filename_is_unique_and_timestamped() -> None:
    from datetime import datetime, timezone

    name = backup_module.backup_filename(now=datetime(2026, 9, 23, 10, 30, 0, tzinfo=timezone.utc))
    assert name == "quadringent-postgres-20260923T103000Z.dump"


def test_main_dumps_and_publishes_via_the_declared_storage_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_decoder: None
) -> None:
    dumped_dsns: list[str] = []

    def fake_dump(dsn: str, destination: Path) -> None:
        dumped_dsns.append(dsn)
        destination.write_bytes(b"PGDMP-archive-simulee")

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
    assert published[0].read_bytes() == b"PGDMP-archive-simulee"


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
    dump_file.write_bytes(b"PGDMP-archive-simulee")

    exit_code = backup_module.main(["--dump-file", str(dump_file), "--validation-file", str(validation_receipt(dump_file))])

    assert exit_code == 0
    published = list((tmp_path / "postgres-backups").glob("quadringent-postgres-*.dump"))
    assert len(published) == 1
    assert published[0].read_bytes() == b"PGDMP-archive-simulee"


def test_main_defaults_the_prefix_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_decoder: None) -> None:
    monkeypatch.setattr(backup_module, "dump", lambda dsn, destination: destination.write_bytes(b"PGDMP-archive-simulee"))

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

@pytest.mark.parametrize("content", [b"", b"plain SQL", b"PGDMP"])
def test_invalid_dump_is_refused_before_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    """Une entrée vide ou hors format custom ne doit jamais être publiée."""
    path = tmp_path / "invalid.dump"
    path.write_bytes(content)
    monkeypatch.setattr(backup_module.StorageBackend, "from_environment", lambda env: pytest.fail("publication interdite"))
    with pytest.raises(ValueError, match="archive"):
        backup_module.main(["--dump-file", str(path), "--validation-file", str(validation_receipt(path))])


@pytest.mark.parametrize("same_length", [False, True])
def test_readback_mismatch_refuses_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_length: bool) -> None:
    """Un PUT réussi ne remplace pas la relecture des octets stockés."""
    path = tmp_path / "custom.dump"
    path.write_bytes(b"PGDMP" + b"archive-test" * 8)
    class Store:
        def put_once(self, key: str, content: bytes) -> bool:
            return True
        def get_bounded(self, key: str, max_bytes: int) -> bytes:
            return b"X" * path.stat().st_size if same_length else b"corrupted"
    class Backend:
        def object_store(self, prefix: str) -> Store:
            return Store()
    monkeypatch.setattr(backup_module.StorageBackend, "from_environment", lambda env: Backend())
    with pytest.raises(ValueError, match="relecture"):
        backup_module.main(["--dump-file", str(path), "--validation-file", str(validation_receipt(path))])


@pytest.mark.parametrize("failure", [False, True])
def test_pg_restore_decodes_archive_without_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    """Le validateur disponible lit toute l'archive sans connexion PostgreSQL."""
    content = b"PGDMP-archive-simulee"
    monkeypatch.setattr(backup_module.shutil, "which", lambda command: "/tools/pg_restore")
    def run(args: list[str], **kwargs: object) -> None:
        assert args[:3] == ["/tools/pg_restore", "--file", backup_module.os.devnull]
        assert Path(args[3]).read_bytes() == content
        assert kwargs["timeout"] == 120 and kwargs["check"] is True
        assert kwargs["stderr"] == subprocess.DEVNULL
        if failure:
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(backup_module.subprocess, "run", run)
    if failure:
        with pytest.raises(ValueError, match="pg_restore"):
            backup_module.validate_archive(content)
    else:
        assert backup_module.validate_archive(content) == "decode-pg_restore-sans-restauration"


def test_absent_validator_is_explicit() -> None:
    """La signature de format ne remplace jamais le reçu ou le validateur."""
    with pytest.raises(ValueError, match="validation"):
        backup_module.validate_archive(b"PGDMP-archive-simulee")


@pytest.mark.parametrize("error", [TimeoutError("lecture refusée"), FileNotFoundError("objet absent")])
def test_readback_error_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    """Une relecture impossible conserve l'échec même après publication."""
    path = tmp_path / "custom.dump"
    path.write_bytes(b"PGDMP-archive-simulee")
    class Store:
        def put_once(self, key: str, content: bytes) -> bool:
            return True
        def get_bounded(self, key: str, max_bytes: int) -> bytes:
            raise error
    class Backend:
        def object_store(self, prefix: str) -> Store:
            return Store()
    monkeypatch.setattr(backup_module.StorageBackend, "from_environment", lambda env: Backend())
    with pytest.raises(type(error)):
        backup_module.main(["--dump-file", str(path), "--validation-file", str(validation_receipt(path))])
    assert "sauvegarde publiée" not in capsys.readouterr().out


def test_validator_timeout_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un validateur bloqué échoue sans diagnostic contenant les données."""
    monkeypatch.setattr(backup_module.shutil, "which", lambda command: "/tools/pg_restore")
    def blocked(args: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(args, 120)
    monkeypatch.setattr(backup_module.subprocess, "run", blocked)
    with pytest.raises(ValueError, match="pg_restore"):
        backup_module.validate_archive(b"PGDMP-archive-simulee")


def test_forged_header_without_decoder_or_receipt_is_refused() -> None:
    """Le préfixe PGDMP seul ne suffit pas à admettre une sauvegarde."""
    with pytest.raises(ValueError, match="validation"):
        backup_module.validate_archive(b"PGDMP-body-corrompu")


@pytest.mark.parametrize("mutation", ["body", "hash", "boolean", "missing"])
def test_receipt_refuses_unbound_or_missing_validation(tmp_path: Path, mutation: str) -> None:
    """Un reçu absent, booléen ou lié à d'autres octets n'admet pas le dump."""
    path = tmp_path / "custom.dump"
    path.write_bytes(b"PGDMP-body-initial")
    receipt = validation_receipt(path)
    if mutation == "body":
        path.write_bytes(b"PGDMP-body-modifie")
    elif mutation == "hash":
        data = json.loads(receipt.read_text())
        data["sha256"] = "0" * 64
        receipt.write_text(json.dumps(data))
    elif mutation == "boolean":
        receipt.write_text('{"validated":true}')
    else:
        receipt.unlink()
    with pytest.raises(ValueError, match="validation"):
        backup_module.validate_archive(path.read_bytes(), receipt)


def test_matching_receipt_is_local_decode_not_restore(tmp_path: Path) -> None:
    """Le résultat décrit précisément la confiance locale de l'initContainer."""
    path = tmp_path / "custom.dump"
    path.write_bytes(b"PGDMP-archive-simulee")
    assert backup_module.validate_archive(path.read_bytes(), validation_receipt(path)) == (
        "recu-initContainer-decode-pg_restore-sans-restauration"
    )


@pytest.mark.parametrize("failed_command", ["pg_dump", "pg_restore", "sha256sum", None])
def test_chart_init_validation_gates_receipt(
    tmp_path: Path, failed_command: str | None
) -> None:
    """Le script réellement rendu ne produit aucun reçu après une étape en échec."""
    import os
    import yaml
    repository = Path(__file__).resolve().parents[1]
    rendered = subprocess.run(
        ["helm", "template", "test", str(repository / "chart"), "--namespace", "quadringent-demo",
         "-f", str(repository / "infra-values/values-int.yaml"),
         "--set", "postgres.enabled=true", "--set", "postgres.backup.enabled=true",
         "--set-file", "controlPlane.launch.jobTemplate=" + str(repository / "infra-values/job-template-int.json"),
         "--set-file", "controlPlane.launch.fleetCatalog=" + str(repository / "infra-values/fleet-catalog-int.json"),
         "--set-file", "controlPlane.launch.fleetSidecar=" + str(repository / "infra-values/fleet-sidecar-int.json")],
        capture_output=True, text=True, check=True,
    )
    job = next(document for document in yaml.safe_load_all(rendered.stdout) if document and document["kind"] == "CronJob" and document["metadata"]["name"].endswith("postgres-backup"))
    init = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"][0]
    directory = tmp_path / "backup"
    directory.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    content = b"PGDMP-archive-simulee"
    digest = hashlib.sha256(content).hexdigest()
    commands = {
        "pg_dump": 'printf PGDMP-archive-simulee > "$TASK_DUMP"',
        "pg_restore": 'test -s "$TASK_DUMP"',
        "sha256sum": f'printf "{digest}  %s\\n" "$TASK_DUMP"',
    }
    for name, command in commands.items():
        binary = binaries / name
        binary.write_text("#!/bin/sh\n" + ("exit 7" if failed_command == name else command) + "\n")
        binary.chmod(0o700)
    environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"], TASK_DUMP=str(directory / "postgres.dump"))
    result = subprocess.run(
        [*init["command"], init["args"][0].replace("/backup/", str(directory) + "/")],
        env=environment, capture_output=True, text=True,
    )
    receipt = directory / "postgres.validation.json"
    if failed_command is not None:
        assert result.returncode != 0 and not receipt.exists()
    else:
        assert result.returncode == 0
        assert backup_module.validate_archive(content, receipt) == "recu-initContainer-decode-pg_restore-sans-restauration"
