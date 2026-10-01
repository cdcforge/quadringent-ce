#!/usr/bin/env python3
"""DEV source-only continuous IBM i journal capture.

The process deliberately separates three concerns:

* a read-only IBM i receiver catalog executed through JTOpen/JDBC;
* the bounded Java/JTOpen/Debezium reader;
* the Python raw-first publisher and its durable checkpoint (S3/DynamoDB by
  default, or GCS with ``QUADRINGENT_STORAGE_BACKEND=gcs``).

It never loads Snowflake. Credentials are read from the environment supplied
by the approved runtime and are never included in commands, logs, exceptions,
or metrics.

Continuous capture keeps one JVM alive (AS400 + JDBC + RetrieveJournal) and
talks to it over stdin/stdout. Catalog snapshots are cached. The loop sleeps
only when idle so catch-up does not busy-wait or spawn Java per poll.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import resource
import re
import signal
import sys
import time

from quadringent.checkpoint import DynamoDbCheckpointStore
from quadringent.fleet_capture import (
    FleetConfigurationError,
    FleetTableRouter,
    FleetWindowCoordinator,
    fleet_mode_from_environment,
)
from quadringent.console_snapshot import (
    ConsoleSnapshotBuilder,
    FileSnapshotSink,
    FluxIdentity,
    S3SnapshotSink,
    ThrottledSink,
)
from quadringent.continuous import ContinuousCaptureService, finite_tail_bootstrap
from quadringent.contract import JournalPosition
from quadringent.continuous import budget_exhausted, lag_trend, should_log_poll
from quadringent.java_catalog import CachedReceiverCatalog, WorkerReceiverCatalog
from quadringent.java_worker import (
    DEFAULT_JOURNAL_BUFFER_SIZE,
    JavaWindowRunner,
    PersistentJavaWorker,
    WORKER_CLASS,
)
from quadringent.object_store import RawFirstCaptureCoordinator, S3ObjectStore
from quadringent.site_config import current as current_site
from quadringent.storage_backend import backend_from_environment
from quadringent.source_gate import (
    DynamoDbSourceGate,
    SourceAuthenticationBlockedError,
    SourceConfigurationBlockedError,
    SourceGatePolicy,
    SourceUnavailablePausedError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reserve-run-id', help='Reserve a fresh isolated DEV run before source I/O')
    parser.add_argument('--proof-window-id',help='Enable receipted scans for one timed proof-table window')
    parser.add_argument('--proof-window-seconds',type=int,default=600)
    parser.add_argument('--proof-window-count', type=int, default=None,
                        help='Bound a durable chain of timed proof-table windows (1 to 128)')
    parser.add_argument("--max-polls", type=int, default=None)
    parser.add_argument("--metrics-interval-seconds", type=int, default=0,
                        help="summarise quiet polls on this interval instead of "
                             "logging every one; 0 logs everything")
    parser.add_argument("--max-seconds", type=int, default=None,
                        help="wall-clock budget; the loop stops between polls so "
                             "the closing lag trend is still emitted")
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=3,
        help="open the capture circuit after this many consecutive source errors",
    )
    parser.add_argument("--console-snapshot-path", default=None,
                        help="write the console document to this file, replacing "
                             "it atomically at each publication")
    parser.add_argument("--console-snapshot-interval-seconds", type=float, default=10.0,
                        help="minimum delay between two publications; the S3 cost "
                             "of this project is a request cost, so this bound is "
                             "explicit. The closing document is always written")
    args = parser.parse_args()
    if args.max_seconds is not None and args.max_seconds <= _reader_timeout_seconds():
        # Sinon la boucle s'arrête avant le premier poll, sans rien signaler.
        raise ValueError("--max-seconds must exceed AS400_READER_TIMEOUT_SECONDS")
    # Le mode flotte est validé avant toute I/O — et avant la résolution du
    # site : un Job mal configuré doit échouer sans réservation, sans lecture
    # S3 et sans contacter IBM i.
    fleet = fleet_mode_from_environment(os.environ)
    fleet_tables: tuple[str, ...] = fleet.tables if fleet is not None else ()
    if fleet is not None:
        if args.proof_window_id is not None or args.proof_window_count is not None:
            raise FleetConfigurationError(
                "fleet capture reads several tables: no single-table proof window"
            )
        if (os.environ.get("ISERIES_TABLE") or "").strip().upper() not in fleet_tables:
            raise FleetConfigurationError("ISERIES_TABLE must belong to the fleet tables")
    proof_options=_proof_window_options(args.proof_window_id,args.proof_window_seconds)
    window_count = _proof_chain_options(
        args.proof_window_id, args.proof_window_count,
        args.proof_window_seconds, args.max_seconds,
    )

    backend = backend_from_environment(os.environ)
    if backend != "aws" and (args.reserve_run_id is not None or proof_options is not None
                             or os.environ.get("AS400_CONSOLE_SNAPSHOT_S3_KEY")):
        # Réservation, fenêtres de preuve et console S3 sont liées au site AWS
        # déclaré : les refuser avant toute I/O plutôt que de les simuler.
        raise ValueError("run reservation, proof windows and S3 console snapshot require the aws backend")
    if args.reserve_run_id is not None:
        _reserve_run(args.reserve_run_id)

    checkpoint = _checkpoint_store(backend, _required("AS400_STREAM_KEY"))
    object_store = _object_store(backend, os.environ.get("AS400_RAW_PREFIX", ""))
    fleet_router = (
        FleetTableRouter(
            root=fleet.table_root,
            tables=fleet.tables,
            store_factory=lambda prefix: _object_store(backend, prefix),
            checkpoint_factory=lambda stream_key: _checkpoint_store(backend, stream_key),
        )
        if fleet is not None
        else None
    )
    coordinator = RawFirstCaptureCoordinator(object_store, checkpoint)
    if fleet_router is not None:
        # Les treize tables d'abord, le curseur du journal ensuite.
        coordinator = FleetWindowCoordinator(coordinator, fleet_router)
    # Reject an omitted durable chain budget before preparing a window or
    # constructing the JVM: catalog.snapshot() below already contacts IBM i.
    from quadringent.proof_windows import prepare_window_chain
    if window_count is None:
        prepare_window_chain(object_store, checkpoint,
                             initial_window_id=args.proof_window_id, window_count=None)
    else:
        # Validate an existing run contract before prepare_window can write a
        # different root intent. Only explicit absence permits a fresh chain.
        try:
            object_store.get_bounded('window-chain.json', 1024 * 1024)
        except FileNotFoundError:
            pass
        else:
            prepare_window_chain(object_store, checkpoint,
                                 initial_window_id=args.proof_window_id,
                                 window_count=window_count)
    if proof_options:
        from quadringent.proof_windows import prepare_window
        run_id=object_store.prefix.rsplit('/',1)[-1]
        marker=json.loads(object_store.get_bounded('reservation.json',4096))
        from quadringent.run_reservation import is_reservation_marker
        if not is_reservation_marker(marker, run_id=run_id):
            raise ValueError('missing or incompatible isolated run reservation')
        prepare_window(object_store,checkpoint,now=datetime.now(timezone.utc),**proof_options)
        if window_count is not None:
            prepare_window_chain(object_store, checkpoint,
                                 initial_window_id=args.proof_window_id,
                                 window_count=window_count)
    java = os.environ.get("AS400_JAVA", "java")
    classpath = _required("AS400_JAVA_CLASSPATH")
    host = _required("ISERIES_HOST")
    user = _required("ISERIES_USER")
    batch_entries = _positive("AS400_BATCH_ENTRIES", 20000)
    catch_up_entries = _positive("AS400_CATCH_UP_BATCH_ENTRIES", 50000)
    max_decoded_entries = _positive(
        "ISERIES_MAX_DECODED_ENTRIES",
        max(batch_entries, catch_up_entries),
    )
    # La garde source borne les sign-ons IBM i à travers les redémarrages du
    # pod : 3 concessions maximum, blocage durable sur refus d'authentification,
    # pause bornée sur indisponibilité (maintenance, serveur fermé).
    source_gate = _source_gate(backend, DynamoDbSourceGate.key_for(host, user))
    worker = PersistentJavaWorker(
        java=java,
        classpath=classpath,
        host=host,
        user=user,
        schema=_required("ISERIES_SCHEMA"),
        table=_required("ISERIES_TABLE"),
        tables=fleet_tables if fleet_tables else os.environ.get("ISERIES_TABLES"),
        timeout_seconds=_reader_timeout_seconds(),
        retrieve_timeout_ms=_retrieve_timeout_ms(),
        journal_buffer_size=_positive(
            "ISERIES_JOURNAL_BUFFER_SIZE",
            DEFAULT_JOURNAL_BUFFER_SIZE,
        ),
        max_server_entries=max(batch_entries, catch_up_entries, max_decoded_entries),
        class_name=os.environ.get("AS400_JAVA_WORKER_CLASS", WORKER_CLASS),
        connect_gate=source_gate,
    )
    receiver_catalog = WorkerReceiverCatalog(
        worker,
        limit=_positive("AS400_RECEIVER_METADATA_LIMIT", 20),
    )
    catalog = CachedReceiverCatalog(
        receiver_catalog,
        # An RJ poll takes seconds, so a 2 s TTL expired every time and the
        # catalogue was refetched on the worker RetrieveJournal shares.
        ttl_polls=_positive("AS400_RECEIVER_CATALOG_CACHE_POLLS", 30),
        ttl_seconds=float(os.environ.get("AS400_RECEIVER_CATALOG_CACHE_SECONDS", "60")),
        max_stale_reuse=_positive("AS400_RECEIVER_CATALOG_MAX_STALE", 5),
        # Sonde bornee (un seul receiver ATTACHED) a chaque poll : une entree
        # fraiche devient visible au poll suivant sans refaire un catalogue
        # complet. AS400_TAIL_PROBE=false revient au comportement historique.
        tail_probe=receiver_catalog.tail if _flag("AS400_TAIL_PROBE", True) else None,
    )
    runner = JavaWindowRunner(
        worker,
        max_decoded_entries=max_decoded_entries,
        empty_probe=_flag("AS400_EMPTY_PROBE", True),
    )
    initial_checkpoint = checkpoint.load()
    try:
        catalog_rows = [
            {
                "receiver": item.receiver,
                "status": item.status,
                "first_sequence": item.first_sequence,
                "last_sequence": item.last_sequence,
            }
            for item in catalog.snapshot(
                required_receiver=(
                    initial_checkpoint.receiver if initial_checkpoint else None
                )
            )
        ]
    except (
        SourceUnavailablePausedError,
        SourceAuthenticationBlockedError,
        SourceConfigurationBlockedError,
    ) as gate_error:
        # La garde a tranché avant tout sign-on : la boucle de service porte la
        # politique (attente de pause, arrêt bloqué) avec le document console.
        catalog_rows = []
        print(
            json.dumps(
                {"event": "source_gate", "decision": type(gate_error).__name__},
                sort_keys=True,
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "event": "catalog_receivers",
                "count": len(catalog_rows),
                "oldest": catalog_rows[0]["receiver"] if catalog_rows else None,
                "newest": catalog_rows[-1]["receiver"] if catalog_rows else None,
                "receivers": catalog_rows,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    bootstrap = _bootstrap_position()
    if os.environ.get("AS400_BOOTSTRAP_RECEIVER") == "__TAIL__":
        receivers = list(catalog.snapshot())
        bootstrap = finite_tail_bootstrap(receivers, batch_entries)
        tail = receivers[-1]
        print(
            json.dumps(
                {
                    "event": "bootstrap_tail",
                    "receiver": bootstrap.receiver,
                    "sequence": bootstrap.sequence,
                    "status": tail.status,
                    "source_tail_sequence": tail.last_sequence,
                    "finite": True,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    service = ContinuousCaptureService(
        catalog,
        runner,
        coordinator,
        checkpoint,
        max_entries=batch_entries,
        catch_up_max_entries=catch_up_entries,
        bootstrap=bootstrap,
        poll_seconds=float(os.environ.get("AS400_POLL_SECONDS", "5")),
        min_poll_seconds=float(os.environ.get("AS400_MIN_POLL_SECONDS", "1")),
        # AS400_RECEIPTED_SCANS : posée par ``build_reader_deployment`` même
        # en mode une seule table (constat du 24 septembre 2026 — voir
        # storage_layout.py) — sans reçus, le chargeur de destination
        # (``list_receipt_keys``) ne découvre jamais les lots publiés par un
        # lecteur mono-table.
        receipted_scans=bool(proof_options) or fleet_router is not None or _flag("AS400_RECEIPTED_SCANS", False),
        proof_window_id=args.proof_window_id,
        proof_window_count=window_count,
    )

    # Le document de console. Il existe pour qu'on n'ait jamais à ouvrir les
    # logs de ce pod pour savoir si le flux avance.
    console = ConsoleSnapshotBuilder(identity=_flux_identity())
    console_sink = _console_sink(args.console_snapshot_path)
    console_out = (
        ThrottledSink(sink=console_sink, interval_s=args.console_snapshot_interval_seconds)
        if console_sink is not None
        else None
    )

    lag_samples: list[int] = []
    last_log_at = [time.monotonic()]
    skipped_quiet = [0]

    def report(result, metrics: dict[str, object]) -> None:
        window = result.window
        safe_window = None
        if window is not None:
            safe_window = {
                "receiver": window.end.receiver,
                "start_sequence": window.start.sequence,
                "end_sequence": window.end.sequence,
                "rotated": window.rotated,
                "sequence_reset": window.sequence_reset,
            }
        if fleet_router is not None and coordinator.last_report is not None:
            routing = coordinator.last_report
            metrics = {
                **metrics,
                "fleet": {
                    "tables": routing.routed_tables,
                    "published": routing.published_tables,
                    "already_covered": routing.reused_tables,
                    "events": routing.routed_events,
                    "payload_bytes": routing.payload_bytes,
                },
            }
        lag_value = metrics.get("last_lag_sequences")
        if isinstance(lag_value, int):
            lag_samples.append(lag_value)
        # L'historique échantillonne *chaque* poll, y compris ceux que le log
        # tait : c'est la différence entre une série traçable et un résumé.
        console.observe(result, metrics)
        if console_out is not None:
            _publish_console_snapshot(console_out, console.encode())
        if not should_log_poll(
            status=result.status,
            polls=int(metrics.get("polls") or 0),
            seconds_since_last_log=time.monotonic() - last_log_at[0],
            interval_s=args.metrics_interval_seconds,
        ):
            skipped_quiet[0] += 1
            return
        if skipped_quiet[0]:
            metrics = {**metrics, "quiet_polls_since_last_log": skipped_quiet[0]}
            skipped_quiet[0] = 0
        last_log_at[0] = time.monotonic()
        print(
            json.dumps(
                {
                    "event": "capture_poll",
                    "status": result.status,
                    "event_count": result.event_count,
                    "window": safe_window,
                    "metrics": metrics,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    started_at = time.monotonic()
    reader_budget = _reader_timeout_seconds()

    def _budget_reached() -> bool:
        return budget_exhausted(
            started_at=started_at,
            now=time.monotonic(),
            max_seconds=args.max_seconds,
            window_seconds=reader_budget,
        )

    # SIGTERM demande une sortie de boucle, pas une mort subite : le finally
    # ferme le worker JVM et publie le dernier document de console.
    terminated = [False]

    def _on_sigterm(_signum, _frame) -> None:
        terminated[0] = True

    signal.signal(signal.SIGTERM, _on_sigterm)

    def _stop_requested() -> bool:
        return terminated[0] or _budget_reached()

    try:
        metrics = service.run(
            max_polls=args.max_polls,
            max_consecutive_errors=args.max_consecutive_errors,
            stop=_stop_requested,
            on_result=report,
        )
    except SourceAuthenticationBlockedError as error:
        console.mark_stopped("STOPPED_AUTH_BLOCKED", type(error).__name__)
        raise
    except SourceConfigurationBlockedError as error:
        # Erreur de configuration (ex. AS400_SOURCE_TIME_ZONE incohérent avec
        # la source) — jamais présentée comme un refus d'authentification ni
        # comme une coupure : état console distinct, voir source_gate.py.
        console.mark_stopped("STOPPED_CONFIG_BLOCKED", type(error).__name__)
        raise
    except BaseException as error:
        console.mark_stopped("STOPPED_FAIL_CLOSED", type(error).__name__)
        raise
    else:
        stop_state, stop_reason, exit_code = _capture_stop(
            service, budget_reached=_budget_reached(), terminated=terminated[0]
        )
        console.mark_stopped(stop_state, stop_reason)
    finally:
        print(
            json.dumps({"event": "lag_trend", **lag_trend(lag_samples)}, sort_keys=True),
            flush=True,
        )
        if console_out is not None:
            # Le dernier état part quoi qu'il arrive : un run qui s'arrête est
            # précisément le moment où l'écran doit dire pourquoi.
            console.observe_cpu_seconds(_cpu_seconds())
            _publish_console_snapshot(console_out, console.encode(), final=True)
        worker.close()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_s = usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime
    events = int(metrics.get("events_published") or 0)
    cpu_ms_per_event = round(cpu_s * 1000.0 / events, 4) if events > 0 else None
    print(
        json.dumps(
            {
                "event": "capture_finished",
                "state": stop_state,
                "proof_window_count": window_count,
                "proof_windows_complete": service.proof_windows_complete,
                "metrics": metrics,
                "cpu_user_s": round(usage.ru_utime + children.ru_utime, 6),
                "cpu_sys_s": round(usage.ru_stime + children.ru_stime, 6),
                "cpu_s": round(cpu_s, 6),
                "cpu_ms_per_event": cpu_ms_per_event,
            },
            sort_keys=True,
        )
    )
    return exit_code


def _proof_chain_options(window_id, count, duration_seconds, max_seconds):
    if count is None:
        return None
    if window_id is None or type(count) is not int or not 1 <= count <= 128:
        raise ValueError('proof chain requires a window ID and count from 1 to 128')
    # Each successor starts at the actual previous closure, so grace can
    # accumulate across every window. Include the final reader reserve too.
    minimum = count * (duration_seconds + 60) + math.ceil(_reader_timeout_seconds())
    if type(max_seconds) is not int or max_seconds <= minimum:
        raise ValueError('proof chain requires max-seconds above its windows, closure grace and reader reserve')
    return count


def _capture_stop(service, *, budget_reached, terminated=False):
    if service.proof_window_count is not None and service.proof_windows_complete:
        return ('STOPPED_PROOF_CHAIN',
                'Fenêtres de capture clôturées ; livraison Snowflake à vérifier', 0)
    if terminated:
        return ('STOPPED_SIGTERM', 'signal SIGTERM reçu', 0)
    if service.proof_window_count is not None:
        return ('STOPPED_BUDGET',
                'Chaîne de capture incomplète ; livraison Snowflake non certifiée', 2)
    return ('STOPPED_BUDGET',
            'budget atteint' if budget_reached else 'nombre de polls atteint', 0)


def _flux_identity() -> FluxIdentity:
    """L'identité du flux, lue une fois, au même endroit que le reste.

    Elle est dérivée de l'environnement déjà requis par la capture : aucun
    paramètre nouveau à régler, et rien qui puisse décrire un flux autrement
    que ce que le worker lit réellement.
    """

    journal_library = os.environ.get("AS400_JOURNAL_LIBRARY", "")
    table = _required("ISERIES_TABLE")
    schema = os.environ.get("ISERIES_SCHEMA", "").strip()
    job = os.environ.get("AS400_JOB_NAME", "as400-capture")
    reader_path = "RetrieveJournal" if _flag("AS400_USE_RETRIEVE_JOURNAL", True) else "DISPLAY_JOURNAL"
    # La flotte déclare sa population réelle : AS400_FLEET_TABLES porte les
    # tables que le lecteur capture, ISERIES_TABLES le sous-ensemble
    # explicite, ISERIES_TABLE le repli mono-table — dans cet ordre.
    declared = (
        os.environ.get("AS400_FLEET_TABLES")
        or os.environ.get("ISERIES_TABLES")
        or table
    )
    return FluxIdentity(
        id=os.environ.get("AS400_STREAM_KEY", table).lower(),
        label=f"{table} — lecture par {reader_path}",
        journal=os.environ.get("AS400_JOURNAL_NAME", ""),
        journal_library=journal_library,
        objects=tuple(
            f"{schema}.{item.strip()}" if schema and '.' not in item.strip() else item.strip()
            for item in declared.split(",")
            if item.strip()
        ),
        reader_path=reader_path,
        target=os.environ.get("AS400_TARGET_LABEL", "raw S3 — aucune cible déclarée"),
        job=job,
    )


def _proof_window_options(window_id, duration_seconds):
    if window_id is None:
        return None
    if not isinstance(window_id,str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',window_id) is None:
        raise ValueError('invalid proof window identity')
    if type(duration_seconds) is not int or not 600<=duration_seconds<=3600:
        raise ValueError('invalid proof window duration')
    site = current_site()
    expected={'AS400_RAW_BUCKET':site.raw_bucket,
              'ISERIES_HOST':site.ibmi_host,'ISERIES_USER':site.ibmi_user,
              'AS400_CHECKPOINT_TABLE':site.checkpoint_table,
              'ISERIES_SCHEMA':site.source_schema,'ISERIES_TABLE':site.proof_table,
              'AS400_JOURNAL_NAME':site.journal_name}
    if any(os.environ.get(key)!=value for key,value in expected.items()):
        raise ValueError('proof window requires the declared site capture configuration')
    if re.fullmatch(re.escape(site.stream_prefix)+r'/runs/[a-z0-9][a-z0-9-]{0,79}',os.environ.get('AS400_RAW_PREFIX','')) is None:
        raise ValueError('proof window requires isolated archive run')
    if os.environ.get('AS400_BOOTSTRAP_RECEIVER') or os.environ.get('AS400_BOOTSTRAP_SEQUENCE'):
        raise ValueError('proof window requires existing checkpoint, no bootstrap')
    if os.environ.get('ISERIES_TABLES',site.proof_table)!=site.proof_table:
        raise ValueError('proof window requires the declared proof table only')
    return {'window_id':window_id,'stream_id':_required('AS400_STREAM_KEY').lower(),
            'duration_seconds':duration_seconds}


def _object_store(backend: str, prefix: str):
    if backend == "gcs":
        from quadringent.gcs_backend import GcsObjectStore

        return GcsObjectStore(_required("AS400_RAW_BUCKET"), prefix, client=_gcs_client())
    return S3ObjectStore(_required("AS400_RAW_BUCKET"), prefix)


def _checkpoint_store(backend: str, stream_key: str):
    if backend == "gcs":
        from quadringent.gcs_backend import GcsCheckpointStore

        return GcsCheckpointStore(_required("AS400_CHECKPOINT_BUCKET"), stream_key, client=_gcs_client())
    return DynamoDbCheckpointStore(_required("AS400_CHECKPOINT_TABLE"), stream_key)


def _source_gate(backend: str, gate_key: str):
    if backend == "gcs":
        from quadringent.gcs_backend import GcsSourceGate

        return GcsSourceGate(_required("AS400_CHECKPOINT_BUCKET"), gate_key,
                             policy=_source_gate_policy(), client=_gcs_client())
    return DynamoDbSourceGate(_required("AS400_CHECKPOINT_TABLE"), gate_key, policy=_source_gate_policy())


_GCS_CLIENT = None


def _gcs_client():
    """Un seul client GCS par processus, identifiants ADC du runtime."""
    global _GCS_CLIENT
    if _GCS_CLIENT is None:
        from google.cloud import storage

        _GCS_CLIENT = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
    return _GCS_CLIENT


def _reserve_run(run_id: str) -> None:
    import boto3
    from quadringent.run_reservation import reserve_run

    site = current_site()
    session = boto3.Session(region_name=site.aws_region)
    if session.client('sts').get_caller_identity().get('Account') != site.aws_account_id:
        raise ValueError('Run reservation requires the declared site AWS account')
    reserve_run(session.client('s3'), bucket=_required('AS400_RAW_BUCKET'),
                prefix=_required('AS400_RAW_PREFIX'), run_id=run_id, site=site)


def _console_sink(path: str | None):
    """Où poser le document. Un fichier, un objet S3, ou rien du tout.

    « Rien du tout » est un choix explicite et silencieux : un run de mesure
    n'a pas à publier un état de console, et l'absence de destination ne doit
    pas faire échouer une capture.
    """

    if path:
        return FileSnapshotSink(Path(path))
    key = os.environ.get("AS400_CONSOLE_SNAPSHOT_S3_KEY")
    if not key:
        return None
    import boto3  # importé ici : la capture locale n'a pas besoin d'AWS

    return S3SnapshotSink(
        bucket=_required("AS400_RAW_BUCKET"),
        key=key,
        client=boto3.client("s3"),
    )


def _publish_console_snapshot(console_out, payload: bytes, *, final: bool = False) -> bool:
    """Publie le snapshot sans laisser une erreur d'observabilité arrêter la capture."""

    try:
        if final:
            console_out.flush(payload)
        else:
            console_out.write(payload)
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "console_snapshot_write_failed",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return False
    return True


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime


def _bootstrap_position() -> JournalPosition | None:
    receiver = os.environ.get("AS400_BOOTSTRAP_RECEIVER")
    sequence = os.environ.get("AS400_BOOTSTRAP_SEQUENCE")
    if receiver == "__TAIL__":
        return None
    if receiver is None and sequence is None:
        return None
    if receiver is None or sequence is None:
        raise ValueError("AS400_BOOTSTRAP_RECEIVER and AS400_BOOTSTRAP_SEQUENCE are both required")
    return JournalPosition(receiver, int(sequence))


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _reader_timeout_seconds() -> float:
    raw = os.environ.get("AS400_READER_TIMEOUT_SECONDS", "300")
    timeout_seconds = float(raw)
    if not math.isfinite(timeout_seconds) or timeout_seconds < 15:
        raise ValueError("AS400_READER_TIMEOUT_SECONDS must be >= 15")
    return timeout_seconds


def _retrieve_timeout_ms() -> int:
    raw = os.environ.get("AS400_RETRIEVE_TIMEOUT_MS", "60000")
    timeout_ms = int(raw)
    if timeout_ms < 1000:
        raise ValueError("AS400_RETRIEVE_TIMEOUT_MS must be >= 1000")
    return timeout_ms


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _positive(name: str, default: int) -> int:
    value = os.environ.get(name)
    result = default if value is None or not value.strip() else int(value)
    if result < 1:
        raise ValueError(f"{name} must be positive")
    return result


def _bounded(name: str, default: int, low: int, high: int) -> int:
    value = os.environ.get(name)
    result = default if value is None or not value.strip() else int(value)
    if not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def _source_gate_policy() -> SourceGatePolicy:
    """Bornes de la garde source. ``AS400_CONNECT_MAX_ATTEMPTS`` est plafonné
    à 3 dans le code comme dans la charte : jamais plus de trois sign-ons
    avant pause, quel que soit le réglage."""

    return SourceGatePolicy(
        max_attempts=_bounded("AS400_CONNECT_MAX_ATTEMPTS", 3, 1, 3),
        lease_seconds=_bounded("AS400_CONNECT_LEASE_SECONDS", 120, 30, 600),
        pause_seconds=_bounded("AS400_SOURCE_PAUSE_SECONDS", 900, 60, 86_400),
        pause_max_seconds=_bounded(
            "AS400_SOURCE_PAUSE_MAX_SECONDS", 14_400, 60, 86_400
        ),
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        payload = {
            "event": "capture_failed",
            "error_type": type(error).__name__,
        }
        # Seuls les messages écrits dans le dépôt sont journalisés : une erreur
        # reçue d'un fournisseur peut contenir un hôte, un utilisateur ou un
        # identifiant, et reste donc réduite à son type.
        if isinstance(error, FleetConfigurationError):
            payload["message"] = str(error)
        print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
