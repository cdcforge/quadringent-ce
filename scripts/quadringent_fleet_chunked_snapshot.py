#!/usr/bin/env python3
"""Copie historique découpée par bornes RRN d'une table IBM i.

Une table trop grande pour une lecture JDBC unique (mesure : ~68 M de
lignes, connexion perdue après 16 h et 48 M de lignes) devient une succession
de tranches bornées par `RRN(table)`. Chaque tranche est lue, publiée sous le
même marqueur de tentative, relue dans S3 puis supprimée localement : une
coupure ne perd que la tranche courante, et le disque ne porte qu'une tranche
à la fois.

Trois garanties portent la mesure :

- un seul `run_id` pour toute la copie : les identifiants d'événement restent
  `sha256(source | journal | SNAPSHOT:<run> | ordinal)` et l'image relue est
  une seule tentative, pas plusieurs ;
- l'ordinal d'une tranche = base de sa bande + lignes déjà lues dans la bande :
  les ordinaux suivent l'ordre physique RRN et ne se recouvrent jamais ;
- chaque objet publié porte `kind=history-snapshot` et `run=<run_id>` : la
  publication refuse toute tentative étrangère déjà présente sous le préfixe.

Le registre (`ledger.json` dans le répertoire de travail) rend la reprise
explicite : une tranche terminée n'est jamais relue, une tranche lue mais non
publiée est republiée, une tranche interrompue est réécrite depuis zéro dans
un répertoire neuf.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid


# run_id et table sont interpolés dans des commandes `docker run ... -c` et
# dans le Python embarqué : seul un identifiant borné et sans caractère shell
# est accepté, le même contrat que quadringent_fleet_history.py.
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TABLE_ARG = re.compile(r"[A-Za-z0-9_]{1,64}")
# Le résumé du lecteur reprend le run_id tel qu'il a été passé : le motif
# suit exactement le contrat validé en entrée, sans exiger un UUID — une
# reprise avec un identifiant non-UUID valide doit pouvoir relire la ligne.
SUMMARY = re.compile(r"\brows=(\d+)\b.*\bbatches=(\d+)\b.*\brun_id=([A-Za-z0-9_-]{1,64})\b")
RRN_PROBE = re.compile(r"\bmax_rrn=(\d+)\b")
PUBLISHED = re.compile(r'"status":\s*"PUBLISHED"')
READER_CLASS = "io.quadringent.as400.ReadOnlyTableSnapshot"
READER_TIMEOUT_SECONDS = 3 * 3600
PUBLISH_TIMEOUT_SECONDS = 2 * 3600
VERIFY_TIMEOUT_SECONDS = 900
PROGRESS_TIMEOUT_SECONDS = 120
PASSTHROUGH_ENV = (
    "AS400_DATABASE_PORT",
    "AS400_SIGNON_PORT",
    "AS400_SNAPSHOT_SOCKET_TIMEOUT_MS",
    "AS400_SNAPSHOT_LOGIN_TIMEOUT_MS",
)


def _site():
    from quadringent.site_config import current  # noqa: PLC0415

    return current()


class ChunkedError(RuntimeError):
    """La copie découpée ne peut pas être attestée."""


def plan_bands(max_rrn: int, workers: int) -> list[dict[str, int]]:
    """Découpe l'espace RRN en `workers` bandes contiguës.

    La base ordinale d'une bande est `rrn_start - 1` : les ordinaux publiés
    suivent l'ordre RRN global, et un trou ordinal ne peut provenir que d'une
    ligne supprimée — jamais du découpage.
    """

    if max_rrn < 1 or workers < 1:
        raise ChunkedError("invalid bound or worker count")
    band_width = (max_rrn + workers - 1) // workers
    bands: list[dict[str, int]] = []
    start = 1
    while start <= max_rrn:
        end = min(start + band_width - 1, max_rrn)
        bands.append({"rrn_start": start, "rrn_end": end, "ordinal_base": start - 1})
        start = end + 1
    return bands


def plan_chunks(band: dict[str, int], chunk_rows: int) -> list[dict[str, int]]:
    """Découpe une bande en tranches de `chunk_rows` RRNs au plus."""

    if chunk_rows < 1:
        raise ChunkedError("chunk size must be positive")
    chunks: list[dict[str, int]] = []
    start = band["rrn_start"]
    while start <= band["rrn_end"]:
        end = min(start + chunk_rows - 1, band["rrn_end"])
        chunks.append({"rrn_start": start, "rrn_end": end})
        start = end + 1
    return chunks


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


def _write_env_file(directory: Path, name: str) -> Path:
    """Écrit l'env-file du secret en 0600 dès la création, sans fenêtre 0644."""

    path = directory / name
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"ISERIES_PASSWORD={os.environ['ISERIES_PASSWORD']}\n")
    return path


def _docker_reader(
    image: str,
    jar: str,
    table: str,
    run_id: str,
    host_dir: Path,
    container_subdir: str,
    extra_env: dict[str, str],
    timeout: int = READER_TIMEOUT_SECONDS,
    container_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "ISERIES_HOST": os.environ["ISERIES_HOST"],
        "ISERIES_USER": os.environ["ISERIES_USER"],
        "ISERIES_SCHEMA": os.environ.get("ISERIES_SCHEMA") or _site().source_schema,
        "ISERIES_TABLE": table,
        "AS400_RAW_DIRECTORY": f"/work/{container_subdir}",
        "AS400_SNAPSHOT_RUN_ID": run_id,
        "AS400_SNAPSHOT_BATCH_SIZE": os.environ.get("AS400_SNAPSHOT_BATCH_SIZE", "100000"),
        "AS400_SNAPSHOT_FETCH_SIZE": os.environ.get("AS400_SNAPSHOT_FETCH_SIZE", "5000"),
        "AS400_SNAPSHOT_QUERY_TIMEOUT_SECONDS": os.environ.get(
            "AS400_SNAPSHOT_QUERY_TIMEOUT_SECONDS", "600"
        ),
        "AS400_TLS": os.environ.get("AS400_TLS", "true"),
        "AS400_TLS_CA_FILE": os.environ.get("AS400_TLS_CA_FILE") or _site().tls_ca_file,
        "AS400_JAVA_CLASSPATH": "/app/probe.jar:/app/lib/*",
    }
    for name in PASSTHROUGH_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            environment[name] = value
    environment.update(extra_env)
    # Le mot de passe transite par un env-file 0600, jamais par un -e visible
    # dans `ps` ou `docker inspect`. Le fichier vit dans un répertoire
    # temporaire dédié hors de publish_root : un SIGKILL ne laisse aucun
    # secret dans l'arborescence persistée.
    secrets_dir = Path(tempfile.mkdtemp(prefix="quadringent-reader-"))
    try:
        env_file = _write_env_file(secrets_dir, "reader.env")
    except Exception:
        try:
            secrets_dir.rmdir()
        except OSError:
            pass
        raise
    command = [
        "docker",
        "run",
        "--rm",
    ]
    if container_name is not None:
        # Un reste du même nom est un orphelin de tentative : retiré avant
        # le lancement pour qu'il n'écrive plus dans le staging courant.
        _docker_rm(container_name)
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
    command.extend(["-v", f"{host_dir}:/work"])
    if jar:
        command.extend(["-v", f"{jar}:/app/probe.jar"])
    command.extend([image])
    command.extend(
        [
            "-c",
            f"mkdir -p /work/{container_subdir} && java -Djava.awt.headless=true "
            f"-cp /app/probe.jar:/app/lib/* {READER_CLASS}",
        ]
    )
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # `--rm` ne protège pas d'un CLI tué par le timeout : le conteneur
        # survivant continuerait d'écrire dans le staging de la tentative
        # suivante. Le nom prévisible permet de le retirer explicitement.
        if container_name is not None:
            _docker_rm(container_name)
        raise
    finally:
        env_file.unlink(missing_ok=True)
        try:
            secrets_dir.rmdir()
        except OSError:
            pass


def probe_bound(image: str, jar: str, table: str, host_dir: Path) -> int:
    """Mesure la borne RRN haute de la table, sans lire aucune ligne métier."""

    probe_dir = host_dir / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_run = str(uuid.uuid4())
    try:
        completed = _docker_reader(
            image,
            jar,
            table,
            probe_run,
            host_dir,
            "probe",
            {"AS400_SNAPSHOT_RRN_PROBE": "true"},
            container_name=f"chunked-{probe_run[:12]}-probe",
        )
    except subprocess.TimeoutExpired:
        raise ChunkedError(f"rrn probe timed out for {table}") from None
    output = (completed.stdout or "") + (completed.stderr or "")
    match = RRN_PROBE.search(output)
    if completed.returncode != 0 or match is None:
        raise ChunkedError(f"rrn probe failed for {table} (rc={completed.returncode})")
    return int(match.group(1))


def _ledger_path(workdir: Path) -> Path:
    return workdir / "ledger.json"


def load_ledger(workdir: Path) -> dict[str, object] | None:
    path = _ledger_path(workdir)
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def save_ledger(workdir: Path, ledger: dict[str, object]) -> None:
    """Réécriture atomique du registre ; l'appelant sérialise les mutations."""

    payload = json.dumps(ledger, indent=1, sort_keys=True) + "\n"
    temporary = workdir / "ledger.json.tmp"
    temporary.write_text(payload)
    temporary.replace(_ledger_path(workdir))


def _chunk_record_key(record: dict[str, object]) -> tuple[int, int, int]:
    return (int(record["worker"]), int(record["rrn_start"]), int(record["rrn_end"]))


def _has_batch_files(directory: Path) -> bool:
    """Le répertoire de staging contient au moins un lot publié."""

    return directory.is_dir() and any(directory.glob("batch-*.jsonl"))


def _upsert_chunk_record(
    ledger: dict[str, object], record: dict[str, object]
) -> None:
    key = _chunk_record_key(record)
    ledger["chunks"] = [  # type: ignore[index]
        item
        for item in ledger["chunks"]  # type: ignore[index]
        if _chunk_record_key(item) != key
    ]
    ledger["chunks"].append(record)  # type: ignore[index]


def _mark_chunk_status(
    args: argparse.Namespace,
    ledger: dict[str, object],
    lock: threading.Lock,
    key: tuple[int, int, int],
    status: str,
    published_bytes: int | None = None,
) -> None:
    """Transition du registre puis progression poussée, sous le même verrou.

    `published_bytes` n'est renseigné qu'à la transition « published » : il
    mesure les lots locaux réellement publiés, et un échec n'invente rien.
    """

    with lock:
        for item in ledger["chunks"]:  # type: ignore[index]
            if _chunk_record_key(item) == key:
                item["status"] = status
                item[f"{status}_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if published_bytes is not None:
                    item["bytes"] = published_bytes
        save_ledger(args.workdir, ledger)
        publish_progress(args, ledger)


def _progress_document(ledger: dict[str, object]) -> dict[str, object]:
    chunks = [
        {
            "worker": int(item["worker"]),
            "rrn_start": int(item["rrn_start"]),
            "rrn_end": int(item["rrn_end"]),
            "ordinal_offset": int(item["ordinal_offset"]),
            "rows": int(item["rows"]),
            # Octets des lots effectivement publiés ; 0 tant que la tranche
            # n'a pas terminé sa publication — rien n'est estimé.
            "bytes": int(item.get("bytes", 0)),
            "status": str(item["status"]),
        }
        for item in sorted(ledger["chunks"], key=_chunk_record_key)  # type: ignore[arg-type]
    ]
    published = [item for item in chunks if item["status"] == "published"]
    return {
        "kind": "history-progress",
        "run_id": str(ledger["run_id"]),
        "table": str(ledger["table"]),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "max_rrn": int(ledger.get("max_rrn", 0)),
        "chunk_rows": int(ledger.get("chunk_rows", 0)),
        "chunks": chunks,
        "totals": {
            "planned_rows": sum(int(item["rows"]) for item in chunks),
            "published_rows": sum(int(item["rows"]) for item in published),
            "published_bytes": sum(int(item["bytes"]) for item in published),
            "published_objects": len(published),
        },
    }


def publish_progress(args: argparse.Namespace, ledger: dict[str, object]) -> None:
    """Pousse l'état du registre vers S3 ; ne doit jamais arrêter la copie.

    Appelée sous le verrou du registre : les écritures restent ordonnées et la
    dernière transition est toujours la dernière publiée.
    """

    run_id = str(ledger["run_id"])
    try:
        document = _progress_document(ledger)
        progress_path = args.workdir / "history-progress.json"
        progress_path.write_text(json.dumps(document, sort_keys=True) + "\n")
        relative = progress_path.relative_to(args.publish_root)
        key = f"{_site().history_progress_prefix}{run_id}.json"
        script = (
            "set -a; . /data/aws_env.sh; set +a; "
            "/opt/venv/bin/python - <<'PY'\n"
            "import boto3\n"
            f"client = boto3.client('s3', region_name='{_site().aws_region}')\n"
            f"client.put_object(Bucket='{_site().raw_bucket}', Key='{key}', "
            f"Body=open('/data/{relative}', 'rb').read(), "
            "ContentType='application/json')\n"
            "print('progress_uploaded=1')\n"
            "PY"
        )
        # Nom unique par publication : plusieurs workers poussent leur
        # progression en parallèle, et un conteneur gelé au timeout doit
        # pouvoir être retiré sans ambiguïté.
        container_name = f"chunked-{run_id[:16]}-progress-{uuid.uuid4().hex[:8]}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--platform",
            "linux/amd64",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{args.publish_root}:/data",
            args.image,
            "-c",
            script,
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=PROGRESS_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            _docker_rm(container_name)
            raise
        if completed.returncode != 0:
            print(
                json.dumps(
                    {
                        "event": "progress_publish_failed",
                        "run_id": run_id,
                        "rc": completed.returncode,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    except Exception as error:  # noqa: BLE001 - la progression n'arrête pas la copie
        print(
            json.dumps(
                {
                    "event": "progress_publish_failed",
                    "run_id": run_id,
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            flush=True,
        )


def run_band(
    worker: int,
    band: dict[str, int],
    args: argparse.Namespace,
    ledger: dict[str, object],
    lock: threading.Lock,
) -> None:
    """Lit et publie les tranches d'une bande, dans l'ordre RRN."""

    chunks = plan_chunks(band, args.chunk_rows)
    ledger_chunks = {
        _chunk_record_key(record): record
        for record in ledger["chunks"]  # type: ignore[index]
        if int(record["worker"]) == worker
    }
    done_rows = sum(
        int(record["rows"])
        for record in ledger_chunks.values()
        if record["status"] == "published"
    )
    for chunk in chunks:
        key = (worker, chunk["rrn_start"], chunk["rrn_end"])
        existing = ledger_chunks.get(key)
        if existing is not None and existing["status"] == "published":
            continue
        offset = band["ordinal_base"] + done_rows
        chunk_dir = chunk_dir_name(args.workdir, worker, chunk)
        rows = -1
        if existing is not None and existing["status"] == "read_done":
            if _has_batch_files(chunk_dir):
                # L'offset enregistré fait foi : la tranche a été lue avec lui.
                offset = int(existing["ordinal_offset"])
                rows = int(existing["rows"])
            else:
                # Une lecture attestée sans staging local ne peut pas être
                # publiée : elle serait rejetée en boucle. On la relit.
                print(
                    json.dumps(
                        {
                            "event": "read_done_without_staging",
                            "worker": worker,
                            "rrn_start": chunk["rrn_start"],
                            "rrn_end": chunk["rrn_end"],
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
        if rows < 0:
            failure = ""
            # Les journaux de lecture vivent hors du staging de la tranche :
            # le wipe de début de tentative et le nettoyage de fin de
            # publication ne doivent pas effacer la cause des échecs passés.
            logs_dir = args.workdir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            for attempt in range(1, args.max_attempts + 1):
                if chunk_dir.exists():
                    # Un reste de tentative interrompue n'est jamais complété
                    # ni mélangé : la tranche est relue depuis zéro dans un
                    # répertoire neuf — le lecteur exige un répertoire vide,
                    # y compris entre deux tentatives de la même tranche.
                    for path in chunk_dir.iterdir():
                        path.unlink()
                    chunk_dir.rmdir()
                chunk_dir.mkdir(parents=True)
                container_name = (
                    f"chunked-{str(ledger['run_id'])[:16]}-w{worker}"
                    f"-r{chunk['rrn_start']}-{chunk['rrn_end']}-a{attempt}"
                )
                try:
                    completed = _docker_reader(
                        args.image,
                        args.jar,
                        args.table,
                        str(ledger["run_id"]),
                        args.workdir,
                        chunk_dir.name,
                        {
                            "AS400_SNAPSHOT_RRN_START": str(chunk["rrn_start"]),
                            "AS400_SNAPSHOT_RRN_END": str(chunk["rrn_end"]),
                            "AS400_SNAPSHOT_ROW_OFFSET": str(offset),
                        },
                        timeout=args.reader_timeout_seconds,
                        container_name=container_name,
                    )
                except subprocess.TimeoutExpired:
                    failure = f"reader timed out after {args.reader_timeout_seconds}s"
                    print(
                        json.dumps(
                            {
                                "event": "chunk_read_timeout",
                                "worker": worker,
                                "rrn_start": chunk["rrn_start"],
                                "rrn_end": chunk["rrn_end"],
                                "attempt": attempt,
                                "timeout_s": args.reader_timeout_seconds,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    time.sleep(min(30 * attempt, 120))
                    continue
                output = (completed.stdout or "") + (completed.stderr or "")
                match = SUMMARY.search(output)
                if completed.returncode == 0 and match is not None:
                    rows = int(match.group(1))
                    break
                failure = f"reader rc={completed.returncode}"
                # Sans cette trace, une tranche échouée après trois tentatives
                # ne laisse aucune cause observable : le stderr est persisté
                # par tentative, hors du staging que la tentative suivante
                # efface et que la publication supprime.
                try:
                    (
                        logs_dir / f"{chunk_dir.name}-reader-attempt-{attempt}.log"
                    ).write_text(output[-200_000:])
                except OSError:
                    pass
                print(
                    json.dumps(
                        {
                            "event": "chunk_read_failed",
                            "worker": worker,
                            "rrn_start": chunk["rrn_start"],
                            "rrn_end": chunk["rrn_end"],
                            "attempt": attempt,
                            "rc": completed.returncode,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                time.sleep(min(30 * attempt, 120))
            if rows < 0:
                with lock:
                    _upsert_chunk_record(
                        ledger,
                        {
                            "worker": worker,
                            "rrn_start": chunk["rrn_start"],
                            "rrn_end": chunk["rrn_end"],
                            "ordinal_offset": offset,
                            "rows": 0,
                            "status": "failed",
                            "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "error": failure,
                        },
                    )
                    save_ledger(args.workdir, ledger)
                    publish_progress(args, ledger)
                raise ChunkedError(
                    f"chunk {chunk['rrn_start']}-{chunk['rrn_end']} failed after "
                    f"{args.max_attempts} attempts: {failure}"
                )
            with lock:
                _upsert_chunk_record(
                    ledger,
                    {
                        "worker": worker,
                        "rrn_start": chunk["rrn_start"],
                        "rrn_end": chunk["rrn_end"],
                        "ordinal_offset": offset,
                        "rows": rows,
                        "status": "read_done",
                    },
                )
                save_ledger(args.workdir, ledger)
                publish_progress(args, ledger)
        try:
            _publish_chunk(args, chunk_dir)
        except ChunkedError:
            _mark_chunk_status(args, ledger, lock, key, "failed")
            raise
        # Les octets copiés sont mesurés sur les lots locaux publiés, au
        # moment où la publication est attestée — jamais estimés ni relus.
        chunk_bytes = sum(
            path.stat().st_size for path in chunk_dir.glob("batch-*.jsonl")
        )
        _mark_chunk_status(args, ledger, lock, key, "published", chunk_bytes)
        done_rows += rows
        # Le répertoire local d'une tranche publiée et relue n'a plus de rôle :
        # le seul état durable est S3 plus le registre.
        if chunk_dir.is_dir():
            for path in chunk_dir.iterdir():
                path.unlink()
            chunk_dir.rmdir()


def chunk_dir_name(workdir: Path, worker: int, chunk: dict[str, int]) -> Path:
    return workdir / f"w{worker}-r{chunk['rrn_start']}-{chunk['rrn_end']}"


def _publish_chunk(args: argparse.Namespace, chunk_dir: Path) -> None:
    """Publie une tranche sous le run partagé, dans le conteneur d'outillage."""

    host_hist = args.publish_root
    relative = chunk_dir.relative_to(host_hist)
    container_dir = f"/data/{relative}"
    for attempt in range(1, args.publish_attempts + 1):
        container_name = (
            f"chunked-{args.run_id[:16]}-pub-{chunk_dir.name}-a{attempt}"
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--platform",
            "linux/amd64",
            "--entrypoint",
            "/bin/bash",
            "-v",
            f"{args.repo}:/repo",
            "-v",
            f"{host_hist}:/data",
            args.image,
            "-c",
            "set -a; . /data/aws_env.sh; set +a; "
            "/opt/venv/bin/python /repo/scripts/quadringent_fleet_history.py "
            f"--table {args.table} --publish-only {container_dir} "
            f"--run-id {args.run_id}",
        ]
        # Un orphelin du même nom est un reste de tentative : retiré avant le
        # lancement pour ne pas publier deux fois la même tranche.
        _docker_rm(container_name)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=args.publish_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            _docker_rm(container_name)
            print(
                json.dumps(
                    {
                        "event": "chunk_publish_timeout",
                        "chunk": chunk_dir.name,
                        "attempt": attempt,
                        "timeout_s": args.publish_timeout_seconds,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            time.sleep(min(60 * attempt, 300))
            continue
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode == 0 and PUBLISHED.search(output):
            return
        print(
            json.dumps(
                {
                    "event": "chunk_publish_failed",
                    "chunk": chunk_dir.name,
                    "attempt": attempt,
                    "rc": completed.returncode,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        time.sleep(min(60 * attempt, 300))
    raise ChunkedError(f"publish failed for {chunk_dir.name}")


def verify_published_rows(args: argparse.Namespace) -> dict[str, object]:
    """Relit les manifestes du run dans S3 et somme leurs lignes."""

    script = (
        "set -a; . /data/aws_env.sh; set +a; "
        "/opt/venv/bin/python - <<'PY'\n"
        "import boto3, json\n"
        f"client = boto3.client('s3', region_name='{_site().aws_region}')\n"
        f"prefix = '{_site().raw_prefix_root}/{args.table.lower()}/journal/'\n"
        "paginator = client.get_paginator('list_objects_v2')\n"
        "rows = objects = 0\n"
        f"for page in paginator.paginate(Bucket='{_site().raw_bucket}', Prefix=prefix):\n"
        "    for item in page.get('Contents', []):\n"
        "        key = item['Key']\n"
        "        if not key.endswith('.manifest.json'):\n"
        "            continue\n"
        f"        head = client.head_object(Bucket='{_site().raw_bucket}', Key=key)\n"
        "        meta = head.get('Metadata', {})\n"
        f"        if meta.get('kind') != 'history-snapshot' or meta.get('run') != '{args.run_id}':\n"
        "            continue\n"
        f"        body = json.loads(client.get_object(Bucket='{_site().raw_bucket}', Key=key)['Body'].read())\n"
        "        rows += int(body['event_count']); objects += 1\n"
        "print(json.dumps({'run_rows': rows, 'run_manifests': objects}))\n"
        "PY"
    )
    container_name = f"chunked-{args.run_id[:16]}-verify"
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--platform",
        "linux/amd64",
        "--entrypoint",
        "/bin/bash",
        "-v",
        f"{args.publish_root}:/data",
        args.image,
        "-c",
        script,
    ]
    _docker_rm(container_name)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=VERIFY_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        _docker_rm(container_name)
        raise ChunkedError("remote manifest verification timed out") from None
    output = (completed.stdout or "") + (completed.stderr or "")
    match = re.search(r'\{"run_rows":\s*\d+,\s*"run_manifests":\s*\d+\}', output)
    if completed.returncode != 0 or match is None:
        raise ChunkedError("remote manifest verification failed")
    return json.loads(match.group(0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument(
        "--image",
        default=os.environ.get("AS400_CHUNKED_SNAPSHOT_IMAGE", ""),
        help="image lecteur figée du site (obligatoire, ex. via AS400_CHUNKED_SNAPSHOT_IMAGE)",
    )
    parser.add_argument("--jar", default="", help="probe.jar local monté sur /app/probe.jar")
    parser.add_argument("--repo", required=True, type=Path, help="worktree monté en /repo")
    parser.add_argument("--publish-root", required=True, type=Path, help="racine montée en /data")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--chunk-rows", type=int, default=5_000_000)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--publish-attempts", type=int, default=10)
    parser.add_argument("--reader-timeout-seconds", type=int, default=READER_TIMEOUT_SECONDS)
    parser.add_argument("--publish-timeout-seconds", type=int, default=PUBLISH_TIMEOUT_SECONDS)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.table = args.table.strip().upper()
    # table et run_id sont interpolés dans des commandes conteneur : toute
    # valeur hors de l'alphabet d'identifiants est refusée avant tout usage.
    if _TABLE_ARG.fullmatch(args.table) is None:
        print(
            "ERROR: --table must be a bounded identifier ([A-Za-z0-9_]{1,64})",
            file=sys.stderr,
        )
        return 2
    args.run_id = args.run_id.strip()
    if args.run_id and _RUN_ID.fullmatch(args.run_id) is None:
        print(
            "ERROR: --run-id must match [A-Za-z0-9_-]{1,64}",
            file=sys.stderr,
        )
        return 2
    if args.workers < 1 or args.chunk_rows < 1:
        print("ERROR: --workers and --chunk-rows must be positive", file=sys.stderr)
        return 2
    for name in ("ISERIES_HOST", "ISERIES_USER", "ISERIES_PASSWORD"):
        if not os.environ.get(name, "").strip():
            print(f"ERROR: {name} is required", file=sys.stderr)
            return 2
    if not args.image.strip():
        print("ERROR: --image or AS400_CHUNKED_SNAPSHOT_IMAGE is required", file=sys.stderr)
        return 2
    if not args.workdir.resolve().is_relative_to(args.publish_root.resolve()):
        print("ERROR: --workdir must live under --publish-root", file=sys.stderr)
        return 2
    args.workdir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    ledger = load_ledger(args.workdir)
    if ledger is None:
        run_id = args.run_id or str(uuid.uuid4())
        ledger = {
            "table": args.table,
            "run_id": run_id,
            "chunk_rows": args.chunk_rows,
            "workers": args.workers,
            "chunks": [],
        }
    else:
        run_id = str(ledger["run_id"])
        if _RUN_ID.fullmatch(run_id) is None:
            print("ERROR: ledger run_id is malformed", file=sys.stderr)
            return 2
        if args.run_id and args.run_id != run_id:
            print("ERROR: --run-id differs from the ledger", file=sys.stderr)
            return 2
        if ledger.get("table") != args.table:
            print("ERROR: ledger belongs to another table", file=sys.stderr)
            return 2
        # Le découpage est figé à la création : reprendre avec un autre
        # grain de tranche ou un autre nombre de bandes déplacerait les
        # bases ordinales et recouvrirait des lignes déjà publiées.
        if ledger.get("chunk_rows") != args.chunk_rows:
            print(
                "ERROR: --chunk-rows differs from the ledger "
                f"({args.chunk_rows} vs {ledger.get('chunk_rows')})",
                file=sys.stderr,
            )
            return 2
        if ledger.get("workers") != args.workers:
            print(
                "ERROR: --workers differs from the ledger "
                f"({args.workers} vs {ledger.get('workers')})",
                file=sys.stderr,
            )
            return 2
    args.run_id = run_id
    if "max_rrn" not in ledger:
        ledger["max_rrn"] = probe_bound(args.image, args.jar, args.table, args.workdir)
        save_ledger(args.workdir, ledger)
    max_rrn = int(ledger["max_rrn"])
    bands = plan_bands(max_rrn, args.workers)
    if args.dry_run:
        planned = sum(len(plan_chunks(band, args.chunk_rows)) for band in bands)
        print(json.dumps({
            "table": args.table,
            "run_id": run_id,
            "max_rrn": max_rrn,
            "bands": bands,
            "chunks_planned": planned,
        }))
        return 0
    threads = []
    errors: list[str] = []
    for index, band in enumerate(bands):
        def target(i: int = index, b: dict[str, int] = band) -> None:
            try:
                run_band(i, b, args, ledger, lock)
            except Exception as error:  # noqa: BLE001 - remonté au rapport
                errors.append(f"worker {i}: {error}")

        thread = threading.Thread(target=target, name=f"band-{index}")
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        print(json.dumps({"status": "FAILED", "errors": errors}))
        return 1
    verification = verify_published_rows(args)
    ledger_rows = sum(int(item["rows"]) for item in ledger["chunks"])  # type: ignore[index]
    document = {
        "status": "COPIED" if verification["run_rows"] == ledger_rows else "DIVERGED",
        "table": args.table,
        "run_id": run_id,
        "rows_read": ledger_rows,
        "rows_published": verification["run_rows"],
        "objects_published": verification["run_manifests"],
        "chunks": len(ledger["chunks"]),  # type: ignore[arg-type]
    }
    print(json.dumps(document, sort_keys=True))
    return 0 if document["status"] == "COPIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
