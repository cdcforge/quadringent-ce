#!/usr/bin/env python3
"""Copie historique d'une table IBM i vers son préfixe de journal.

Une table, un run : le lecteur JDBC produit des lots `as400-raw-v1`, puis ces
lots sont publiés sous `<racine>/<table>/journal/`, exactement là où le
flux devient vivant. Le préfixe porte donc l'image initiale puis les
changements, sans zone parallèle à maintenir.

Trois garanties portent la mesure :

- le volume **lu** est celui du lecteur (`rows=` de la ligne de résumé), jamais
  celui d'un fichier supposé ;
- le volume **publié** est relu dans S3 après écriture, clé par clé, avec
  l'empreinte du manifeste ;
- republier un lot déjà présent avec la même empreinte ne réécrit rien : la
  reprise est idempotente et ne peut pas doubler une ligne.

Le script ne se connecte pas à Snowflake et ne modifie aucun curseur : le
chargement appartient au tuyau de la table et la continuité au journal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


def _s3_client():
    """Client S3, importé au dernier moment : les fonctions pures restent testables."""

    import boto3  # noqa: PLC0415

    from quadringent.site_config import current as current_site  # noqa: PLC0415

    return boto3.client("s3", region_name=current_site().aws_region)

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

READER_CLASS = "io.quadringent.as400.ReadOnlyTableSnapshot"
PASSTHROUGH_ENV = (
    "AS400_DATABASE_PORT",
    "AS400_SIGNON_PORT",
    "AS400_SNAPSHOT_SOCKET_TIMEOUT_MS",
    "AS400_SNAPSHOT_LOGIN_TIMEOUT_MS",
)
BATCH = re.compile(r"^batch-([0-9a-f]{32})[.](jsonl|manifest[.]json)$")
SUMMARY = re.compile(r"\brows=(\d+)\b.*\bbatches=(\d+)\b")
# Le prefixe d'un journal porte deux origines legitimes : l'image initiale
# (marquee ici) et le flux vivant (non marque). Seule la premiere doit etre
# unique : refuser les lots vivants bloquerait toute table deja en service.
HISTORY_KIND = "history-snapshot"
_TABLE = re.compile(r"^[A-Z0-9_]{1,30}$")


class HistoryError(RuntimeError):
    """La copie historique ne peut pas être attestée."""


def table_journal_prefix(table: str) -> str:
    """Préfixe du journal d'une table déclarée, identique au flux vivant."""

    if not _TABLE.fullmatch(table):
        raise HistoryError("invalid table identifier")
    return _site().journal_prefix_for(table)


def _site():
    from quadringent.site_config import current as current_site  # noqa: PLC0415

    return current_site()


def _bucket() -> str:
    return _site().raw_bucket


def _docker_rm(container_name: str) -> None:
    """Retire un conteneur nommé ; le nettoyage ne masque jamais l'erreur."""

    try:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - un conteneur absent ou un CLI mort ne dit rien
        pass


def _reader_command(
    image: str,
    table: str,
    run_id: str,
    container_dir: str,
    env_file: Path,
    container_name: str = "",
) -> list[str]:
    environment = {
        "ISERIES_HOST": os.environ["ISERIES_HOST"],
        "ISERIES_USER": os.environ["ISERIES_USER"],
        "ISERIES_SCHEMA": os.environ.get("ISERIES_SCHEMA") or _site().source_schema,
        "ISERIES_TABLE": table,
        "AS400_RAW_DIRECTORY": container_dir,
        "AS400_SNAPSHOT_RUN_ID": run_id,
        "AS400_SNAPSHOT_BATCH_SIZE": os.environ.get("AS400_SNAPSHOT_BATCH_SIZE", "5000"),
        "AS400_SNAPSHOT_FETCH_SIZE": os.environ.get("AS400_SNAPSHOT_FETCH_SIZE", "500"),
        "AS400_SNAPSHOT_QUERY_TIMEOUT_SECONDS": os.environ.get(
            "AS400_SNAPSHOT_QUERY_TIMEOUT_SECONDS", "120"
        ),
        "AS400_TLS": os.environ.get("AS400_TLS", "true"),
        "AS400_TLS_CA_FILE": _site().tls_ca_file,
        "AS400_JAVA_CLASSPATH": "/app/probe.jar:/app/lib/*",
    }
    for name in PASSTHROUGH_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            environment[name] = value
    # Le mot de passe transite par un env-file 0600, jamais par un -e visible
    # dans `ps` ou `docker inspect`.
    command = [
        "docker",
        "run",
        "--rm",
    ]
    if container_name:
        command.extend(["--name", container_name])
    command.extend(
        [
            "--platform",
            "linux/amd64",
            "--entrypoint",
            "/bin/bash",
            "--env-file",
            str(env_file),
        ]
    )
    for name, value in environment.items():
        command.extend(["-e", f"{name}={value}"])
    command.extend(["-v", f"{_host_dir}:/work", image])
    # `-c` est obligatoire : sans lui, bash traite la chaine comme un nom de
    # fichier de script et echoue en 127 sans jamais executer la lecture.
    command.extend(
        [
            "-c",
            f"mkdir -p {container_dir} && java -Djava.awt.headless=true "
            f"-cp /app/probe.jar:/app/lib/* {READER_CLASS}",
        ]
    )
    return command


def _read_table(image: str, table: str, workdir: Path) -> tuple[int, int, Path]:
    """Exécute une lecture JDBC complète et retourne les volumes lus."""

    container_dir = "/work/out"
    run_id = str(uuid.uuid4())
    # Le nom prévisible permet de retirer le conteneur orphelin : `--rm`
    # ne protège pas d'un CLI tué par le timeout de subprocess.run.
    container_name = f"quadringent-history-{uuid.uuid4().hex[:12]}"
    # Le mot de passe transite par un env-file créé en 0600 dès l'ouverture
    # (O_EXCL), dans le répertoire temporaire de la tentative — jamais sous
    # une racine persistée, jamais par un -e visible dans `ps`.
    env_file = workdir / ".reader.env"
    descriptor = os.open(env_file, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"ISERIES_PASSWORD={os.environ['ISERIES_PASSWORD']}\n")
    command = _reader_command(
        image, table, run_id, container_dir, env_file, container_name
    )
    _docker_rm(container_name)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        _docker_rm(container_name)
        raise HistoryError(f"reader timed out for {table} after 7200s") from None
    finally:
        env_file.unlink(missing_ok=True)
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise HistoryError(f"reader failed for {table} (rc={completed.returncode})")
    match = SUMMARY.search(output)
    if match is None:
        raise HistoryError(f"reader summary is missing for {table}")
    rows = int(match.group(1))
    batches = int(match.group(2))
    return rows, batches, workdir / "out"


def _existing_objects(client: object, prefix: str, run_id: str | None = None) -> dict[str, str]:
    """Inventaire des lots d'historique déjà publiés sous un préfixe.

    Seuls les objets marqués `history-snapshot` sont retournés : un lot du flux
    vivant partage le préfixe sans être une tentative d'image initiale, et le
    compter comme tel interdirait toute table déjà en service.

    Avec `run_id`, seuls les lots estampillés d'une autre tentative sont
    « étrangers » : une copie découpée publie ses tranches au fil de l'eau, et
    les tranches déjà posées portent le même run. Un objet historique sans
    marqueur de run reste étranger — il vient d'une tentative antérieure.
    """

    foreign: dict[str, str] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix + "/"):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            name = key.rsplit("/", 1)[-1]
            match = BATCH.fullmatch(name)
            if match is None or not name.endswith(".jsonl"):
                continue
            head = client.head_object(Bucket=_bucket(), Key=key)
            metadata = head.get("Metadata", {})
            if str(metadata.get("kind", "")) != HISTORY_KIND:
                continue
            if run_id is not None and str(metadata.get("run", "")) == run_id:
                continue
            foreign[match.group(1)] = key
    return foreign


def _existing_digest(client: object, key: str) -> str | None:
    """Empreinte d'un objet déjà posé, ou None s'il est absent.

    Seule l'absence avérée (404) rend None : toute autre erreur S3 remonte —
    traiter un échec de lecture comme « absent » autoriserait un écrasement
    silencieux du raw, qui est immuable.
    """

    try:
        head = client.head_object(Bucket=_bucket(), Key=key)
    except Exception as error:
        code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    # Un objet présent sans empreinte connue rend "" : l'appelant refuse de
    # l'écraser, il ne peut pas prouver que le contenu est identique.
    return str(head.get("Metadata", {}).get("sha256", ""))


def _publish(
    workdir: Path,
    table: str,
    allow_partial_replace: bool,
    run_id: str | None = None,
) -> dict[str, object]:
    """Publie les lots d'une table, sans jamais mélanger deux tentatives.

    Une identité de lot contient le run qui l'a produite. Republier une
    deuxième tentative sous le même préfixe doublerait les lignes en
    destination : c'est donc refusé, et une tentative précédente incomplète
    doit être retirée explicitement, jamais recouverte en silence.

    Avec `run_id`, le répertoire est une tranche de la tentative : les lots
    déjà publiés du même run restent en place, et chaque objet posé porte le
    marqueur `run` pour que les tranches suivantes sachent les distinguer
    d'une tentative étrangère.
    """

    client = _s3_client()
    prefix = table_journal_prefix(table)
    local_ids = {
        BATCH.fullmatch(path.name).group(1)
        for path in workdir.iterdir()
        if BATCH.fullmatch(path.name) is not None and path.name.endswith(".jsonl")
    }
    remote = _existing_objects(client, prefix, run_id)
    foreign = {batch: key for batch, key in remote.items() if batch not in local_ids}
    removed: list[str] = []
    if foreign:
        if not allow_partial_replace:
            raise HistoryError(
                f"prefix already holds {len(foreign)} batch(es) from another attempt; "
                "refusing to mix two snapshots of the same table"
            )
        for key in foreign.values():
            client.delete_object(Bucket=_bucket(), Key=key)
            client.delete_object(Bucket=_bucket(), Key=key.replace(".jsonl", ".manifest.json"))
            removed.append(key)
        # La re-vérification garde le même run : les tranches déjà posées de
        # la tentative courante ne sont pas des objets étrangers à purger.
        if _existing_objects(client, prefix, run_id):
            raise HistoryError("partial attempt could not be cleared")
    published: list[dict[str, object]] = []
    reused = 0
    total_bytes = 0
    for path in sorted(workdir.iterdir()):
        match = BATCH.fullmatch(path.name)
        if match is None:
            continue
        key = f"{prefix}/{path.name}"
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        remote_digest = _existing_digest(client, key)
        if remote_digest == digest:
            reused += 1
            continue
        if remote_digest is not None:
            raise HistoryError(
                f"remote object {path.name} already exists with a different digest; "
                "raw objects are immutable and are never overwritten"
            )
        # Le manifeste d'abord : un lot n'est jamais visible avant sa preuve.
        metadata = {"sha256": digest, "kind": HISTORY_KIND}
        if run_id is not None:
            metadata["run"] = run_id
        if path.suffix == ".json":
            client.put_object(
                Bucket=_bucket(),
                Key=key,
                Body=payload,
                Metadata=metadata,
                ContentType="application/json",
            )
        else:
            client.put_object(
                Bucket=_bucket(),
                Key=key,
                Body=payload,
                Metadata=metadata,
                ContentType="application/x-ndjson",
            )
        readback = _existing_digest(client, key)
        if readback != digest:
            raise HistoryError(f"published object does not read back for {path.name}")
        published.append({"key": key, "bytes": len(payload), "sha256": digest})
        total_bytes += len(payload)
    return {
        "prefix": prefix,
        "published_objects": len(published),
        "reused_objects": reused,
        "removed_objects": len(removed),
        "published_bytes": total_bytes,
        "objects": published[:4],
    }


def _count_published_rows(
    client: object, prefix: str, run_id: str | None = None
) -> int:
    """Compte les lignes publiées par la tentative, sans relire les lots.

    Le préfixe du journal est partagé avec le flux vivant : seuls les objets
    marqués `history-snapshot` sont des manifestes d'image initiale, et avec
    `run_id` seuls ceux estampillés de cette tentative comptent. Le marqueur
    est lu par `head_object` — les jsonl ne sont jamais téléchargés, seul le
    manifeste porte `event_count`.

    Un lot sans manifeste est dit explicitement : sous-compter en silence
    ferait passer une copie incomplète pour complète.
    """

    payloads: set[str] = set()
    manifests: dict[str, str] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix + "/"):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            name = key.rsplit("/", 1)[-1]
            match = BATCH.fullmatch(name)
            if match is None:
                continue
            head = client.head_object(Bucket=_bucket(), Key=key)
            metadata = head.get("Metadata", {})
            if str(metadata.get("kind", "")) != HISTORY_KIND:
                continue
            if run_id is not None and str(metadata.get("run", "")) != run_id:
                continue
            if name.endswith(".jsonl"):
                payloads.add(match.group(1))
            else:
                manifests[match.group(1)] = key
    missing = payloads - manifests.keys()
    if missing:
        raise HistoryError(
            f"{len(missing)} published batch(es) have no manifest under {prefix}"
        )
    orphan = manifests.keys() - payloads
    if orphan:
        raise HistoryError(
            f"{len(orphan)} manifest(s) have no payload under {prefix}"
        )
    rows = 0
    for key in manifests.values():
        body = json.loads(client.get_object(Bucket=_bucket(), Key=key)["Body"].read())
        rows += int(body["event_count"])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument(
        "--image",
        default=os.environ.get("AS400_FLEET_HISTORY_IMAGE", ""),
        help="Image lecteur figée du site (obligatoire, ex. via AS400_FLEET_HISTORY_IMAGE)",
    )
    parser.add_argument("--report", default="")
    parser.add_argument("--count-published", action="store_true")
    parser.add_argument(
        "--expected-rows",
        type=int,
        default=None,
        help="volume attendu du catalogue ; la publication est refusee si la lecture differe",
    )
    parser.add_argument(
        "--publish-only",
        metavar="DIRECTORY",
        default="",
        help="publier un repertoire deja lu, sans relire la source",
    )
    parser.add_argument(
        "--replace-partial",
        action="store_true",
        help="retirer une tentative precedente incomplete avant de publier (jamais en silence)",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="marqueur de tentative partagee : le repertoire est une tranche, "
        "les lots du meme run deja publies restent en place",
    )
    args = parser.parse_args()
    table = args.table.strip().upper()
    run_id = args.run_id.strip() or None
    if run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        print("ERROR: --run-id is malformed", file=sys.stderr)
        return 2
    if args.publish_only:
        # Le repertoire a deja ete lu par un conteneur detache : la publication
        # ne depend plus de la source et peut reprendre apres une interruption.
        workdir = Path(args.publish_only)
        if not workdir.is_dir():
            print("ERROR: publish directory is missing", file=sys.stderr)
            return 2
        try:
            result = _publish(workdir, table, args.replace_partial, run_id)
        except HistoryError as error:
            print(json.dumps({"status": "FAILED", "table": table, "reason": str(error)}))
            return 1
        document: dict[str, object] = {
            "status": "PUBLISHED",
            "table": table,
            "prefix": result["prefix"],
            "objects_published": result["published_objects"],
            "objects_reused": result["reused_objects"],
            "objects_removed": result["removed_objects"],
            "bytes_published": result["published_bytes"],
            "sample": result["objects"],
        }
        if args.count_published:
            client = _s3_client()
            document["rows_published"] = _count_published_rows(
                client, str(result["prefix"]), run_id
            )
        payload = json.dumps(document, sort_keys=True)
        if args.report:
            Path(args.report).write_text(payload + "\n")
        print(payload)
        return 0
    for name in ("ISERIES_HOST", "ISERIES_USER", "ISERIES_PASSWORD"):
        if not os.environ.get(name, "").strip():
            print(f"ERROR: {name} is required", file=sys.stderr)
            return 2
    if not args.image.strip():
        print("ERROR: --image or AS400_FLEET_HISTORY_IMAGE is required", file=sys.stderr)
        return 2

    global _host_dir
    with tempfile.TemporaryDirectory(prefix="quadringent-history-") as temporary:
        _host_dir = temporary
        workdir = Path(temporary)
        try:
            rows, batches, out = _read_table(args.image, table, workdir)
            if args.expected_rows is not None and rows != args.expected_rows:
                raise HistoryError(
                    f"read volume differs from the catalogue for {table} "
                    f"({rows} read vs {args.expected_rows} expected)"
                )
            result = _publish(out, table, args.replace_partial, run_id)
        except HistoryError as error:
            print(json.dumps({"status": "FAILED", "table": table, "reason": str(error)}))
            return 1
        document: dict[str, object] = {
            "status": "COPIED",
            "table": table,
            "rows_read": rows,
            "batches_read": batches,
            "prefix": result["prefix"],
            "objects_published": result["published_objects"],
            "objects_reused": result["reused_objects"],
            "objects_removed": result["removed_objects"],
            "bytes_published": result["published_bytes"],
            "sample": result["objects"],
        }
        if args.count_published:
            client = _s3_client()
            document["rows_published"] = _count_published_rows(
                client, str(result["prefix"]), run_id
            )
            document["rows_match"] = document["rows_published"] == rows

    payload = json.dumps(document, sort_keys=True)
    if args.report:
        Path(args.report).write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
