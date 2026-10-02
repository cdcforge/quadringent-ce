#!/usr/bin/env python3
"""Sauvegarde logique du Postgres du control plane v2 vers le stockage objet du site.

``pg_dump`` (format custom, ``-Fc``) puis publication via
``quadringent.storage_backend`` — le même mécanisme S3/GCS que le reste du
produit, avec l'identité IRSA/Workload Identity déjà déclarée du site plutôt
qu'un jeu d'identifiants dédié. ``put_once`` (immuable) : chaque sauvegarde
porte un nom unique horodaté, jamais un écrasement.

L'image control-plane (Python, sans client ``pg_dump``) ne peut pas exécuter
``pg_dump`` elle-même : le CronJob de la chart lance ``pg_dump`` dans un
initContainer dédié (image ``postgres``, qui l'embarque) vers un volume
partagé, le décode entièrement avec ``pg_restore`` sans base et écrit un reçu
privé SHA256. Ce script avec ``--dump-file --validation-file`` publie ce
fichier déjà produit. ``--dump-file`` omis : ce script exécute ``pg_dump``
lui-même (utilisable hors chart, avec un client ``pg_dump`` disponible).

Variables d'environnement consommées : ``QUADRINGENT_PG_BACKUP_DSN``
(DSN ``postgresql://``, requis seulement sans ``--dump-file``),
``QUADRINGENT_PG_BACKUP_PREFIX`` (préfixe objet, ex. ``postgres-backups``),
plus celles de ``quadringent.storage_backend``
(``QUADRINGENT_STORAGE_BACKEND``, ``AS400_RAW_BUCKET``,
``AS400_CHECKPOINT_TABLE``/``AS400_CHECKPOINT_BUCKET``).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import tempfile

from quadringent.storage_backend import StorageBackend

ENV_DSN = "QUADRINGENT_PG_BACKUP_DSN"
ENV_PREFIX = "QUADRINGENT_PG_BACKUP_PREFIX"
DEFAULT_PREFIX = "postgres-backups"


def _require(name: str, environ: dict[str, str]) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"{name} est obligatoire pour la sauvegarde Postgres")
    return value


def dump(dsn: str, destination: Path) -> None:
    """Exécute ``pg_dump`` en format custom (compressé, restaurable par ``pg_restore``)."""

    subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(destination), dsn],
        check=True,
    )


def validate_archive(content: bytes, validation_file: Path | None = None) -> str:
    """Vérifie le format, puis décode toute l'archive si pg_restore est disponible.

    Sans outil local, un reçu lié aux octets de l'initContainer est obligatoire.
    Ce reçu atteste un décodage local, sans authentification indépendante.
    Le décodage SQL ne se connecte à aucune base et ne restaure aucune donnée.
    """
    if len(content) <= 5 or not content.startswith(b"PGDMP"):
        raise ValueError("archive Postgres vide ou hors format custom")
    executable = shutil.which("pg_restore")
    if executable is None:
        if validation_file is None:
            raise ValueError("validation pg_restore ou reçu local obligatoire")
        # Reçu de l'initContainer de confiance ; aucune authentification indépendante.
        try:
            with validation_file.open("rb") as handle:
                encoded = handle.read(4097)
            if len(encoded) > 4096:
                raise ValueError("reçu trop grand")
            receipt = json.loads(encoded)
        except (OSError, ValueError) as error:
            raise ValueError("reçu de validation illisible") from error
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"schema", "validator", "bytes", "sha256"}
            or receipt["schema"] != "quadringent.pg-backup-validation.v1"
            or receipt["validator"] != "pg_restore-full-sql-decode"
            or type(receipt["bytes"]) is not int
            or receipt["bytes"] != len(content)
            or receipt["sha256"] != hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("reçu de validation non conforme aux octets de l'archive")
        return "recu-initContainer-decode-pg_restore-sans-restauration"
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "archive.dump"
        path.write_bytes(content)
        try:
            subprocess.run(
                [executable, "--file", os.devnull, str(path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as from_error:
            raise ValueError("archive Postgres refusée par pg_restore") from from_error
    return "decode-pg_restore-sans-restauration"


def backup_filename(*, now: datetime | None = None) -> str:
    """Nom d'objet unique (horodaté) — relatif au préfixe de l'object store."""

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"quadringent-postgres-{stamp}.dump"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump-file",
        default="",
        help="fichier déjà produit par pg_dump (initContainer dédié) ; sans cette option, exécute pg_dump lui-même",
    )
    parser.add_argument(
        "--validation-file", default="",
        help="reçu privé de décodage pg_restore produit par l'initContainer de confiance",
    )
    arguments = parser.parse_args(argv)
    if arguments.validation_file and not arguments.dump_file:
        parser.error("--validation-file exige --dump-file")
    environ = os.environ
    prefix = (environ.get(ENV_PREFIX) or "").strip() or DEFAULT_PREFIX

    if arguments.dump_file:
        content = Path(arguments.dump_file).read_bytes()
    else:
        dsn = _require(ENV_DSN, environ)
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "postgres.dump"
            dump(dsn, dump_path)
            content = dump_path.read_bytes()

    validation = validate_archive(content, Path(arguments.validation_file) if arguments.validation_file else None)
    backend = StorageBackend.from_environment(environ)
    store = backend.object_store(prefix)
    filename = backup_filename()
    store.put_once(filename, content)
    expected_hash = hashlib.sha256(content).hexdigest()
    observed = store.get_bounded(filename, len(content))
    if len(observed) != len(content) or hashlib.sha256(observed).hexdigest() != expected_hash:
        raise ValueError("relecture de la sauvegarde non conforme")
    print(
        f"sauvegarde publiée et relue : {prefix}/{filename} "
        f"({len(content)} octets, sha256={expected_hash}, validation={validation})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
