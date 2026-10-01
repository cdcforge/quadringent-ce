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
partagé, puis ce script avec ``--dump-file`` se contente de publier ce
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
    arguments = parser.parse_args(argv)
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

    backend = StorageBackend.from_environment(environ)
    store = backend.object_store(prefix)
    filename = backup_filename()
    store.put_once(filename, content)
    print(f"sauvegarde publiée : {prefix}/{filename} ({len(content)} octets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
