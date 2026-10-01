#!/usr/bin/env python3
"""Upload local snapshot raw batches to the dedicated DEV prefix.

The destination is S3 by default, or GCS with
``QUADRINGENT_STORAGE_BACKEND=gcs``; on GCS each object is written once and
an identical replay is a no-op. Prints only file counts, bytes and hashes.
Never prints row payloads.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

from quadringent.storage_layout import snapshot_object_key as _snapshot_object_key


def snapshot_object_key(prefix: str, table: str, filename: str) -> str:
    """Clé d'un objet d'instantané — délègue à la disposition unique
    (``quadringent.storage_layout``) : ``<préfixe>/<table>/snapshot/<fichier>``,
    la table en premier segment (fixé le 24 septembre 2026 : ce module
    publiait auparavant sous ``<préfixe>/snapshot/<table>/...``, un ordre
    différent de celui du lecteur/chargeur, si bien que le chargeur ne
    cherchait jamais l'instantané là où il se trouvait)."""

    return _snapshot_object_key(prefix, table, filename)


def publish_snapshot_batches(*, root: Path, bucket: str, prefix: str, table: str, backend: str,
                              store=None, client=None) -> list[dict[str, str]]:
    """Publie les lots ``batch-*`` de ``root`` et renvoie leurs clés publiées.

    Renvoyé pour être écrit dans la preuve de copie initiale
    (``quadringent_initial_copy_job.py::build_evidence``) : sans capacité de
    listage générique sur ``ObjectStore`` (choix délibéré du dépôt — voir
    ``quadringent_destination_loader.py::discover_new_batches_from_manifest_
    keys``), le chargeur ne peut pas redécouvrir seul les clés de l'instantané.
    La preuve porte donc la liste exacte des paires ``(payload_key,
    manifest_key)`` publiées ici, dans l'ordre des fichiers.

    Les clés renvoyées sont les noms de fichiers nus (``batch-<id>.jsonl``),
    pas le chemin complet : c'est le contrat de ``object_store.
    read_published_batch``, déjà utilisé ainsi pour les lots de journal
    (``quadringent_destination_loader.discover_new_batches``). Le chargeur
    résout le préfixe complet lui-même (``quadringent.storage_layout.
    snapshot_prefix``), comme il le fait déjà pour le préfixe de journal.
    """

    payloads = sorted(root.glob("batch-*.jsonl"))
    manifests = {p.stem: p for p in sorted(root.glob("batch-*.manifest.json"))}
    published: list[dict[str, str]] = []
    for payload_path in payloads:
        manifest_name = payload_path.name.replace(".jsonl", ".manifest.json")
        manifest_path = root / manifest_name
        if not manifest_path.exists():
            raise SystemExit(f"missing manifest for snapshot batch {payload_path.name}")
        for path in (payload_path, manifest_path):
            key = snapshot_object_key(prefix, table, path.name)
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if backend == "gcs":
                store.put_once(key, body)
            else:
                client.put_object(Bucket=bucket, Key=key, Body=body, Metadata={"sha256": digest})
        published.append({"payload_key": payload_path.name, "manifest_key": manifest_name})
    return published


def main() -> int:
    root = Path(os.environ["AS400_RAW_DIRECTORY"])
    bucket = os.environ["AS400_RAW_BUCKET"]
    prefix = os.environ.get("AS400_RAW_PREFIX", "").strip("/")
    table = os.environ.get("ISERIES_TABLE") or os.environ.get("AS400_SNAPSHOT_TABLE")
    if not table:
        raise SystemExit("ISERIES_TABLE or AS400_SNAPSHOT_TABLE is required")
    from quadringent.storage_backend import backend_from_environment

    backend = backend_from_environment(os.environ)
    store = None
    client = None
    if backend == "gcs":
        from quadringent.gcs_backend import GcsObjectStore

        store = GcsObjectStore(bucket)
    else:
        import boto3

        client = boto3.client("s3")
    payloads = sorted(root.glob("batch-*.jsonl"))
    manifests = sorted(root.glob("batch-*.manifest.json"))
    published = publish_snapshot_batches(
        root=root, bucket=bucket, prefix=prefix, table=table, backend=backend, store=store, client=client
    )
    total_bytes = sum((root / name).stat().st_size for pair in published for name in (
        Path(pair["payload_key"]).name, Path(pair["manifest_key"]).name
    ))
    print(
        json.dumps(
            {
                "event": "snapshot_published",
                "payload_files": len(payloads),
                "manifest_files": len(manifests),
                "uploaded": len(published) * 2,
                "bytes": total_bytes,
                "batches": published,
            },
            sort_keys=True,
        )
    )
    if not payloads:
        raise SystemExit("no snapshot payload files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
