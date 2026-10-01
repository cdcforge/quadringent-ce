#!/usr/bin/env python3
"""Job de copie initiale : instantané JDBC -> publication -> preuve.

Compose trois briques existantes, sans rien réécrire :

1. ``io.quadringent.as400.ReadOnlyTableSnapshot`` (Java, sous-processus —
   comme ``java_catalog.py``, jamais ``docker run``) écrit l'instantané
   local (format ``as400-raw-v1``) dans ``AS400_SNAPSHOT_OUTPUT_DIR``, un
   répertoire vide à chaque tentative (``emptyDir`` monté par le manifest,
   voir ``v2/executor/manifests.py::build_initial_copy_job``).
2. ``as400_snapshot_publish`` (fonctions importées en mémoire, jamais un
   sous-processus ``docker``) publie ce répertoire vers le stockage objet
   du site.
3. Ce module écrit la preuve JSON attendue par
   ``v2/executor/evidence.py::EvidenceReader`` (même format que
   ``InitialCopyEvidence.to_dict()`` — vérifié par un test qui la relit
   avec ``InitialCopyEvidence.from_dict``), à la clé ``AS400_EVIDENCE_KEY``.
   C'est cette preuve, une fois lue par le control plane, qui autorise la
   transition ``copying -> live``.

Contrat d'environnement (posé par ``build_initial_copy_job``) :

* ``ISERIES_HOST``/``ISERIES_USER`` (texte, pas des identifiants) et
  ``ISERIES_PASSWORD`` (``envFrom.secretRef``, jamais en clair) ;
* ``ISERIES_SCHEMA``/``ISERIES_TABLE`` ;
* ``AS400_JAVA``/``AS400_JAVA_CLASSPATH`` (défauts posés par
  ``docker/Dockerfile``) ;
* ``AS400_SNAPSHOT_RUN_ID``, ``AS400_SNAPSHOT_OUTPUT_DIR`` ;
* ``QUADRINGENT_STORAGE_BACKEND``, ``AS400_RAW_BUCKET``, ``AS400_RAW_PREFIX`` ;
* ``AS400_BOOTSTRAP_RECEIVER``/``AS400_BOOTSTRAP_RECEIVER_LIBRARY``/
  ``AS400_BOOTSTRAP_SEQUENCE``/``AS400_BOOTSTRAP_OBSERVED_AT`` (la position
  de bascule lue par le control plane *avant* de lancer ce Job — jamais
  recalculée ici) ;
* ``AS400_EVIDENCE_KEY``, ``AS400_PIPELINE_ID``, ``AS400_TABLE_ID``.

Jamais de valeur en clair dans un argument ou un journal : le mot de passe
ne transite que par l'environnement du sous-processus Java.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

READER_CLASS = "io.quadringent.as400.ReadOnlyTableSnapshot"
SNAPSHOT_SUMMARY = re.compile(r"\brows=(\d+)\b")
MAX_STDERR_TAIL = 4000


class InitialCopyJobError(RuntimeError):
    """Configuration invalide ou étape en échec — le Job doit s'arrêter (fail-closed)."""


def _required(environ: dict, name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise InitialCopyJobError(f"{name} est obligatoire")
    return value


@dataclass(frozen=True)
class InitialCopyConfig:
    """Configuration résolue depuis l'environnement, avant tout I/O réseau."""

    java: str
    java_classpath: str
    ibmi_host: str
    ibmi_user: str
    ibmi_password: str
    schema: str
    table: str
    run_id: str
    output_dir: Path
    storage_backend: str
    raw_bucket: str
    raw_prefix: str
    receiver_name: str
    receiver_library: str
    bootstrap_sequence: int
    observed_at: str
    evidence_key: str
    pipeline_id: str
    table_id: str

    @classmethod
    def from_environment(cls, environ: dict) -> "InitialCopyConfig":
        try:
            bootstrap_sequence = int(_required(environ, "AS400_BOOTSTRAP_SEQUENCE"))
        except ValueError as error:
            raise InitialCopyJobError("AS400_BOOTSTRAP_SEQUENCE doit être un entier") from error
        return cls(
            java=environ.get("AS400_JAVA", "java"),
            java_classpath=_required(environ, "AS400_JAVA_CLASSPATH"),
            ibmi_host=_required(environ, "ISERIES_HOST"),
            ibmi_user=_required(environ, "ISERIES_USER"),
            ibmi_password=_required(environ, "ISERIES_PASSWORD"),
            schema=_required(environ, "ISERIES_SCHEMA"),
            table=_required(environ, "ISERIES_TABLE"),
            run_id=_required(environ, "AS400_SNAPSHOT_RUN_ID"),
            output_dir=Path(_required(environ, "AS400_SNAPSHOT_OUTPUT_DIR")),
            storage_backend=(environ.get("QUADRINGENT_STORAGE_BACKEND") or "aws").strip().lower(),
            raw_bucket=_required(environ, "AS400_RAW_BUCKET"),
            raw_prefix=_required(environ, "AS400_RAW_PREFIX"),
            receiver_name=_required(environ, "AS400_BOOTSTRAP_RECEIVER"),
            receiver_library=_required(environ, "AS400_BOOTSTRAP_RECEIVER_LIBRARY"),
            bootstrap_sequence=bootstrap_sequence,
            observed_at=_required(environ, "AS400_BOOTSTRAP_OBSERVED_AT"),
            evidence_key=_required(environ, "AS400_EVIDENCE_KEY"),
            pipeline_id=_required(environ, "AS400_PIPELINE_ID"),
            table_id=_required(environ, "AS400_TABLE_ID"),
        )


def snapshot_subprocess_env(config: InitialCopyConfig, base_environ: dict) -> dict:
    """Environnement du sous-processus Java — jamais le mot de passe ailleurs.

    Reprend l'environnement du pod (``AS400_TLS``/``AS400_TLS_CA_FILE``
    posés par ``_with_ca_mount`` y transitent tels quels) et pose les clés
    que ``ReadOnlyTableSnapshot.Settings.fromEnvironment`` (Java) exige,
    notamment ``AS400_RAW_DIRECTORY`` — nom différent de la variable posée
    par le manifest (``AS400_SNAPSHOT_OUTPUT_DIR``), traduit ici, une seule
    fois, plutôt que dans deux endroits.
    """

    env = dict(base_environ)
    env["AS400_RAW_DIRECTORY"] = str(config.output_dir)
    env["ISERIES_PASSWORD"] = config.ibmi_password
    return env


def run_snapshot(config: InitialCopyConfig, *, base_environ: dict, runner=subprocess.run) -> int:
    """Lance ``ReadOnlyTableSnapshot`` en sous-processus, renvoie les lignes copiées."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    env = snapshot_subprocess_env(config, base_environ)
    completed = runner(
        [config.java, "-cp", config.java_classpath, READER_CLASS],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or "")[-MAX_STDERR_TAIL:]
        raise InitialCopyJobError(f"instantané JDBC en échec (code {completed.returncode}) : {tail}")
    match = SNAPSHOT_SUMMARY.search(completed.stdout or "")
    if not match:
        raise InitialCopyJobError("résumé d'instantané introuvable dans la sortie du lecteur")
    return int(match.group(1))


def publish_snapshot(config: InitialCopyConfig, *, base_environ: dict) -> list[dict[str, str]]:
    """Publie ``config.output_dir`` via les fonctions réelles de
    ``as400_snapshot_publish`` — importées, jamais un sous-processus
    ``docker`` (voir ``quadringent_fleet_chunked_snapshot.py`` pour le
    contre-exemple : inadapté à un Job Kubernetes).

    Renvoie les clés publiées (``payload_key``/``manifest_key`` par lot) :
    portées ensuite par la preuve (``build_evidence``), c'est ce qui permet
    au chargeur de charger l'instantané sans capacité de listage générique
    sur ``ObjectStore`` (voir ``as400_snapshot_publish.publish_snapshot_
    batches``)."""

    import as400_snapshot_publish as publish

    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(base_environ)
        os.environ["AS400_RAW_DIRECTORY"] = str(config.output_dir)
        os.environ["QUADRINGENT_STORAGE_BACKEND"] = config.storage_backend
        os.environ["ISERIES_TABLE"] = config.table
        from quadringent.storage_backend import backend_from_environment

        backend = backend_from_environment(os.environ)
        store = None
        client = None
        if backend == "gcs":
            from quadringent.gcs_backend import GcsObjectStore

            store = GcsObjectStore(config.raw_bucket)
        else:
            import boto3

            client = boto3.client("s3")
        return publish.publish_snapshot_batches(
            root=config.output_dir,
            bucket=config.raw_bucket,
            prefix=config.raw_prefix,
            table=config.table,
            backend=backend,
            store=store,
            client=client,
        )
    finally:
        os.environ.clear()
        os.environ.update(previous)


def build_evidence(
    config: InitialCopyConfig, *, rows_copied: int, completed_at: datetime,
    snapshot_batches: list[dict[str, str]] = (),
) -> dict:
    """Même forme que ``v2/executor/evidence.py::InitialCopyEvidence.to_dict()`` —
    vérifié par un test qui la relit avec ``InitialCopyEvidence.from_dict``.
    Le contrôle plane n'écrit jamais cette preuve, seulement ce Job.

    ``snapshot_batches`` (clés publiées par ``publish_snapshot``) permet au
    chargeur de charger l'instantané avant les événements sans capacité de
    listage générique sur ``ObjectStore``."""

    return {
        "pipeline_id": config.pipeline_id,
        "table_id": config.table_id,
        "run_id": config.run_id,
        "boundary": {
            "receiver_library": config.receiver_library,
            "receiver_name": config.receiver_name,
            "last_sequence": config.bootstrap_sequence,
            "observed_at": config.observed_at,
        },
        "rows_copied": rows_copied,
        "completed_at": completed_at.astimezone(timezone.utc).isoformat(),
        "snapshot_batches": snapshot_batches,
    }


def write_evidence(config: InitialCopyConfig, evidence: dict) -> None:
    """Écrit la preuve à la clé attendue — même magasin racine (préfixe vide)
    que ``EvidenceReader`` côté control plane (``entrypoint.py::
    _storage_backend_object_store``) : ``evidence_key`` porte déjà le chemin
    complet."""

    from quadringent.storage_backend import StorageBackend

    storage = StorageBackend(kind=config.storage_backend, raw_bucket=config.raw_bucket, state_location="")
    store = storage.object_store("")
    body = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    store.put_once(config.evidence_key, body)


def main(argv: list[str] | None = None, *, environ: dict | None = None, now=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="résout et valide la configuration, sans lancer l'instantané ni publier quoi que ce soit",
    )
    args = parser.parse_args(argv)
    base_environ = dict(os.environ if environ is None else environ)
    config = InitialCopyConfig.from_environment(base_environ)
    if args.dry_run:
        print(json.dumps({"event": "initial_copy_dry_run", "table": f"{config.schema}.{config.table}"}))
        return 0
    rows_copied = run_snapshot(config, base_environ=base_environ)
    snapshot_batches = publish_snapshot(config, base_environ=base_environ)
    completed_at = now() if now is not None else datetime.now(timezone.utc)
    evidence = build_evidence(
        config, rows_copied=rows_copied, completed_at=completed_at, snapshot_batches=snapshot_batches
    )
    write_evidence(config, evidence)
    print(
        json.dumps(
            {
                "event": "initial_copy_completed",
                "table_id": config.table_id,
                "run_id": config.run_id,
                "rows_copied": rows_copied,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
