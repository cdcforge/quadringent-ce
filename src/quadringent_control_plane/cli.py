#!/usr/bin/env python3
"""Lance le control plane Quadringent ; les capacités déclarent les actions réelles."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
from typing import Mapping

from quadringent_control_plane.fleet_catalog_refresh import (
    DEFAULT_REFRESH_SECONDS,
    FLEET_CATALOG_FILE,
    FLEET_SIDECAR_FILE,
    PERSISTENT_STATE_ENV,
    REFRESH_INTERVAL_ENV,
    FleetCatalogRefresher,
    seed_fleet_state,
)
from quadringent_control_plane.fleet_composition import (
    FleetLaunchConfig,
    build_fleet_action_executor,
)
from quadringent_control_plane.fleet_evidence import collect_fleet_evidence
from quadringent_control_plane.fleet_plan import build_fleet_plan
from quadringent_control_plane.fleet_progression import (
    RUN_STATE_FILE,
    FleetProgression,
    ProgressionOutcome,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from quadringent_control_plane.fleet_sidecar import load_fleet_catalog_file
from quadringent_control_plane.k8s_jobs import client_from_environment
from quadringent_control_plane.repository import (
    ProjectionRepository,
    _read_document,
    bind_fleet_proofs,
    bind_fleet_runs,
    bind_window_proofs,
    parse_source_spec,
)
from quadringent_control_plane.auth import AuthConfig
from quadringent_control_plane.audit import ActionAuditLog
from quadringent_control_plane.connections import ConnectionsStore, connections_state_path
from quadringent_control_plane.server import is_loopback_host, serve
from quadringent_control_plane.version import version


def main() -> int:
    parser = argparse.ArgumentParser(description="Control plane Quadringent local et lecture seule")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--state-dir", default="", help="répertoire durable des liaisons locales")
    parser.add_argument("--infrastructure-costs-source", default="", help="preuve locale de coûts : file:///chemin/infrastructure-costs.json")
    parser.add_argument("--window-proof",action="append",default=[],help="DEV sidecar: source-id=file:///... or dedicated S3 window URI")
    parser.add_argument(
        "--fleet-proof",
        action="append",
        default=[],
        help="Sidecar flotte DEV : source-id=file:///chemin/absolu.json",
    )
    parser.add_argument("--environment", default="local")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8844)
    parser.add_argument("--refresh-seconds", type=float, default=2.0)
    parser.add_argument(
        "--ui-dist",
        default="",
        help="répertoire ui/dist servi en same-origin ; vide = API seule",
    )
    parser.add_argument(
        "--v2-upstream-port",
        type=int,
        default=int(os.environ.get("QUADRINGENT_V2_UPSTREAM_PORT") or 0) or None,
        help=(
            "port loopback du control plane v2 (FastAPI) ; active le relais "
            "/v2 et /mcp à travers ce serveur v1 (défaut : QUADRINGENT_V2_UPSTREAM_PORT, "
            "vide = aucun relais, comportement inchangé)"
        ),
    )
    parser.add_argument(
        "--fleet-catalog",
        default="",
        help="catalogue metadata-only des 13 tables : active le lancement réel",
    )
    parser.add_argument(
        "--fleet-state-dir",
        default="",
        help="répertoire des intentions prepare/history (obligatoire avec --fleet-catalog)",
    )
    parser.add_argument(
        "--fleet-job-template",
        default="",
        help="modèle de Job JSON fourni par l'exploitant (obligatoire avec --fleet-catalog)",
    )
    parser.add_argument(
        "--fleet-raw-prefix-root",
        default="",
        help="préfixe S3 racine des runs de flotte (obligatoire avec --fleet-catalog)",
    )
    parser.add_argument(
        "--fleet-run",
        action="append",
        default=[],
        help="état domaine durable : source-id=file:///chemin/fleet-run.json",
    )
    parser.add_argument(
        "--fleet-progression-seconds",
        type=float,
        default=float(os.environ.get("QUADRINGENT_FLEET_PROGRESSION_SECONDS") or 30),
        help="intervalle du pilote de progression mesurée (défaut : 30 s)",
    )
    parser.add_argument(
        "--auth-user-header",
        default="",
        help="en-tête d'identité posé par le proxy d'authentification (active l'auth)",
    )
    parser.add_argument(
        "--auth-groups-header",
        default="",
        help="en-tête de groupes séparés par des virgules (avec --auth-user-header)",
    )
    parser.add_argument(
        "--auth-operator-groups",
        default="",
        help="groupes autorisés à exécuter les actions (obligatoire avec --auth-user-header)",
    )
    parser.add_argument(
        "--auth-admin-groups",
        default="",
        help="groupes administrateurs, membres gérés dans l'IdP du site",
    )
    parser.add_argument(
        "--auth-proxy-secret",
        default=os.environ.get("QUADRINGENT_AUTH_PROXY_SECRET", ""),
        help="secret partagé posé par le proxy (en-tête x-quadringent-proxy-secret) ; "
        "recommandé : prouve que la requête a transité par le proxy",
    )
    parser.add_argument(
        "--telemetry",
        default=os.environ.get("QUADRINGENT_TELEMETRY", "off"),
        help="télémétrie produit opt-in — on/true/1/yes active, tout le reste est off",
    )
    parser.add_argument(
        "--telemetry-url",
        default=os.environ.get("QUADRINGENT_TELEMETRY_URL", ""),
        help="endpoint télémétrie https (défaut : endpoint vendeur documenté)",
    )
    arguments = parser.parse_args()
    auth = _build_auth(arguments, parser)
    if not is_loopback_host(arguments.host) and auth is None:
        parser.error("--host doit cibler une adresse loopback sans --auth-user-header")
    if arguments.refresh_seconds <= 0:
        parser.error("--refresh-seconds doit être > 0")
    if arguments.v2_upstream_port is not None and not 0 < arguments.v2_upstream_port <= 65535:
        parser.error("--v2-upstream-port doit être un port TCP valide")
    try:
        sources = [
            parse_source_spec(spec, environment=arguments.environment)
            for spec in arguments.source
        ]
        sources=bind_window_proofs(sources,arguments.window_proof)
        sources=bind_fleet_proofs(sources,arguments.fleet_proof)
        sources=bind_fleet_runs(sources,arguments.fleet_run)
    except ValueError as error:
        parser.error(str(error))
    repository = ProjectionRepository(sources, infrastructure_costs_source=arguments.infrastructure_costs_source or None)
    _seed_fleet_state(arguments)
    repository.refresh()
    ui_dist = arguments.ui_dist.strip() or None
    action_executor = _build_action_executor(arguments, parser, sources)
    catalog_refresh = _build_catalog_refresher(arguments, action_executor)
    server = serve(
        repository,
        host=arguments.host,
        port=arguments.port,
        ui_dist=ui_dist,
        action_executor=action_executor,
        auth=auth,
        audit_log=ActionAuditLog(Path(arguments.fleet_state_dir) / "actions.jsonl") if action_executor is not None else None,
        connections_store=_build_connections_store(arguments),
        v2_upstream_port=arguments.v2_upstream_port,
    )
    stopping = Event()
    refresher = Thread(
        target=_refresh_loop,
        args=(repository, stopping, arguments.refresh_seconds),
        daemon=True,
    )
    refresher.start()
    catalog_thread = None
    if catalog_refresh is not None:
        probe, interval = catalog_refresh
        # intervalle 0 : l'action refresh reste disponible, le relevé de fond non
        if interval > 0:
            catalog_thread = Thread(
                target=_catalog_refresh_loop,
                args=(probe, action_executor, stopping, interval),
                daemon=True,
            )
            catalog_thread.start()
    progression_thread = _start_progression(arguments, action_executor, sources, stopping)
    telemetry = _start_telemetry(arguments)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()
        refresher.join(timeout=arguments.refresh_seconds + 1)
        if catalog_thread is not None:
            catalog_thread.join(timeout=2)
        if progression_thread is not None:
            progression_thread.join(timeout=2)
        if telemetry is not None:
            telemetry.stop()
            telemetry.join(timeout=2)
    return 0


def _start_telemetry(arguments: argparse.Namespace):
    """Démarre la télémétrie opt-in ; retourne le démon ou None."""
    from quadringent_control_plane.telemetry import TelemetryConfig, maybe_start

    enabled = (arguments.telemetry or "").strip().lower() in {"1", "true", "on", "yes"}
    config = TelemetryConfig.from_env()
    endpoint = (arguments.telemetry_url or "").strip() or config.endpoint
    # Le garde https s'applique aussi à l'argument : jamais d'émission en clair.
    if not endpoint.startswith("https://"):
        endpoint = config.endpoint
    config = TelemetryConfig(
        enabled=enabled,
        endpoint=endpoint,
        state_dir=config.state_dir,
    )
    return maybe_start(
        config,
        version=version(),
        table_count=_fleet_table_count(arguments),
    )


def _fleet_table_count(arguments: argparse.Namespace) -> int:
    catalog = getattr(arguments, "fleet_catalog", "") or ""
    if not catalog:
        return 0
    try:
        return len(load_fleet_catalog_file(catalog).tables)
    except Exception:
        return 0


def _build_auth(arguments: argparse.Namespace, parser: argparse.ArgumentParser):
    """Construit la confiance proxy ; fail-closed sur une config incomplète."""

    try:
        return AuthConfig.build(
            user_header=arguments.auth_user_header,
            groups_header=arguments.auth_groups_header,
            operator_groups=arguments.auth_operator_groups,
            admin_groups=arguments.auth_admin_groups,
            proxy_secret=arguments.auth_proxy_secret,
        )
    except ValueError as error:
        parser.error(str(error))


def _build_action_executor(
    arguments: argparse.Namespace, parser: argparse.ArgumentParser, sources: list
) -> object | None:
    """Construit le lancement réel seulement si l'exploitant le demande en entier."""

    requested = {
        name: (getattr(arguments, name) or "").strip()
        for name in (
            "fleet_catalog",
            "fleet_state_dir",
            "fleet_job_template",
            "fleet_raw_prefix_root",
        )
    }
    provided = {name for name, value in requested.items() if value}
    if not provided:
        return None
    if len(provided) != len(requested):
        missing = ", ".join(sorted(set(requested) - provided))
        parser.error(f"--fleet-catalog exige aussi : {missing}")
    try:
        catalog_path = _launch_catalog(
            requested["fleet_catalog"], Path(requested["fleet_state_dir"])
        )
        plan = build_fleet_plan(load_fleet_catalog_file(catalog_path))
        bound = next(
            (source for source in sources if source.fleet_run_origin is not None),
            sources[0] if sources else None,
        )
        console_origin = bound.descriptor.origin if bound is not None else None

        def console_reader() -> object:
            # La preuve console de la pipeline : même origine que la
            # projection — la reprise juge l'état parqué sur le document
            # que l'opérateur voit, pas sur une copie privée.
            return _read_document(console_origin) if console_origin else None

        return build_fleet_action_executor(
            plan=plan,
            config=FleetLaunchConfig(
                state_directory=Path(requested["fleet_state_dir"]),
                raw_prefix_root=requested["fleet_raw_prefix_root"],
                job_template_path=Path(requested["fleet_job_template"]),
            ),
            client=client_from_environment(),
            console_reader=console_reader,
        )
    except Exception as error:
        parser.error(f"lancement de flotte indisponible : {getattr(error, 'code', None) or type(error).__name__}")


def _refresh_loop(repository: ProjectionRepository, stopping: Event, refresh_seconds: float) -> None:
    while not stopping.wait(refresh_seconds):
        repository.refresh()


def _persistent_fleet_state() -> bool:
    """Vrai quand la chart monte un PVC : le sidecar régénéré y est durable."""

    return (os.environ.get(PERSISTENT_STATE_ENV) or "").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


def _seed_fleet_state(arguments: argparse.Namespace) -> None:
    """Copie les graines ConfigMap vers l'état durable avant le premier refresh."""

    catalog = (getattr(arguments, "fleet_catalog", "") or "").strip()
    state_dir = (getattr(arguments, "state_dir", "") or getattr(arguments, "fleet_state_dir", "") or "").strip()
    if not catalog or not state_dir or not _persistent_fleet_state():
        return
    try:
        result = seed_fleet_state(
            catalog_source=Path(catalog),
            sidecar_source=Path(catalog).parent / FLEET_SIDECAR_FILE,
            state_directory=Path(state_dir),
        )
    except Exception as error:
        _note("fleet_state_seed", "error", getattr(error, "code", None) or type(error).__name__)
        return
    if set(result.values()) != {"kept"}:
        _note("fleet_state_seed", "done", result)


def _launch_catalog(catalog_arg: str, state_directory: Path) -> str:
    """Catalogue de démarrage : la copie durable si elle se relit, sinon la graine."""

    candidate = state_directory / FLEET_CATALOG_FILE
    try:
        load_fleet_catalog_file(candidate)
    except Exception:
        return catalog_arg
    return str(candidate)


def _build_connections_store(arguments: argparse.Namespace) -> ConnectionsStore | None:
    """Magasin des liaisons déclarées, adossé au répertoire d'état de la flotte.

    Sans ``--fleet-state-dir``, il n'y a nulle part où écrire une liaison de
    façon durable : les routes ``/v1/connections`` restent alors absentes
    plutôt que d'accepter une création que rien ne conserverait.
    """

    state_dir = (getattr(arguments, "state_dir", "") or getattr(arguments, "fleet_state_dir", "") or "").strip()
    if not state_dir:
        return None
    Path(state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    return ConnectionsStore(AtomicJsonStateStore(connections_state_path(state_dir)))


def _build_catalog_refresher(
    arguments: argparse.Namespace, action_executor: object | None
) -> tuple[FleetCatalogRefresher, float] | None:
    """Relevé borné : état durable exigé — sans PVC le sidecar reste en lecture seule."""

    if action_executor is None or not _persistent_fleet_state():
        return None
    try:
        interval = float(
            (os.environ.get(REFRESH_INTERVAL_ENV) or "").strip()
            or DEFAULT_REFRESH_SECONDS
        )
    except (TypeError, ValueError):
        _note("fleet_catalog_refresh", "disabled", "invalid_interval")
        interval = 0.0
    classpath = (os.environ.get("AS400_JAVA_CLASSPATH") or "").strip()
    if not classpath:
        _note("fleet_catalog_refresh", "disabled", "missing_classpath")
        return None
    java = (os.environ.get("AS400_JAVA") or "java").strip()
    plan = action_executor.plan
    refresher = FleetCatalogRefresher(
        state_directory=Path(arguments.fleet_state_dir),
        java=java,
        classpath=classpath,
        max_concurrency=plan.max_concurrency,
        historical_byte_budget=plan.historical_byte_budget,
    )
    action_executor.attach_catalog_refresher(refresher)
    return refresher, interval


def _catalog_refresh_loop(
    refresher: FleetCatalogRefresher,
    action_executor: object,
    stopping: Event,
    interval_seconds: float,
) -> None:
    """Relevé périodique : tout échec conserve le dernier plan valide."""

    while not stopping.wait(interval_seconds):
        try:
            outcome = refresher.refresh()
        except Exception:
            continue
        plan = getattr(outcome, "plan", None)
        if plan is not None:
            try:
                action_executor.update_plan(plan)
            except Exception:
                pass


def _start_progression(
    arguments: argparse.Namespace,
    action_executor: object | None,
    sources: list,
    stopping: Event,
) -> Thread | None:
    """Démarre le pilote de progression mesurée, borné à l'état durable.

    Le pilote ne tourne que si le lancement réel est actif (executor) et
    que l'état persiste sur PVC — sans durabilité, un run recréé à chaque
    démarrage perdrait ses gardes de reprise. Le document de preuve lu est
    celui de la source liée par ``--fleet-run`` : même origine, même vérité.
    """

    if action_executor is None or not _persistent_fleet_state():
        return None
    if arguments.fleet_progression_seconds <= 0:
        _note("fleet_progression", "disabled", "invalid_interval")
        return None
    bound_ids = {
        spec.partition("=")[0]
        for spec in arguments.fleet_run
        if "=" in spec
    }
    bound = next(
        (source for source in sources if source.descriptor.id in bound_ids),
        sources[0] if sources else None,
    )
    if bound is None:
        return None
    # La source live est la preuve console combinée : elle porte la position
    # du lecteur et les mesures destination par voie — une lecture, deux
    # rôles de mesure distincts pour le collecteur.
    proof_origin = bound.descriptor.origin
    state_directory = Path(arguments.fleet_state_dir)
    prepare_store = AtomicJsonStateStore(state_directory / "fleet-prepare.json")
    history_store = AtomicJsonStateStore(state_directory / "fleet-history.json")
    pause_store = AtomicJsonStateStore(state_directory / "fleet-pause.json")
    catalog_path = state_directory / FLEET_CATALOG_FILE

    def evidence():
        try:
            prepare_raw = prepare_store.load()
        except Exception:
            prepare_raw = None
        try:
            history_raw = history_store.load()
        except Exception:
            history_raw = None
        try:
            pause_raw = pause_store.load()
        except Exception:
            pause_raw = None
        try:
            catalog = load_fleet_catalog_file(catalog_path)
        except Exception:
            catalog = None
        try:
            proof = _read_document(proof_origin)
        except Exception:
            proof = None
        # Les preuves de certification sont embarquées dans la preuve
        # console — même clé, même écrivain (la sonde), aucune lecture
        # supplémentaire.
        certify_documents: dict[str, object] = {}
        if isinstance(proof, Mapping):
            certify_block = proof.get("certify")
            if isinstance(certify_block, Mapping) and isinstance(
                certify_block.get("tables"), Mapping
            ):
                certify_documents = dict(certify_block["tables"])
        return collect_fleet_evidence(
            prepare_document=prepare_raw,
            history_document=history_raw,
            pause_document=pause_raw,
            catalog=catalog,
            snapshot_document=proof,
            proof_document=proof,
            certify_documents=certify_documents,
        )

    driver = FleetProgression(
        plan=action_executor.plan,
        run_store=AtomicJsonStateStore(state_directory / RUN_STATE_FILE),
        evidence_provider=evidence,
        emit=_progression_note,
    )
    thread = Thread(
        target=_progression_loop,
        args=(driver, stopping, arguments.fleet_progression_seconds),
        daemon=True,
    )
    thread.start()
    return thread


def _progression_loop(
    driver: FleetProgression, stopping: Event, interval_seconds: float
) -> None:
    """Cycle borné : un échec de collecte ou de domaine ne tue pas le fil."""

    while not stopping.wait(interval_seconds):
        try:
            driver.tick()
        except Exception as error:
            _note(
                "fleet_progression",
                "error",
                getattr(error, "code", None) or type(error).__name__,
            )


def _progression_note(outcome: ProgressionOutcome) -> None:
    """Trace bornée d'un cycle — phase et compte d'erreurs, jamais de contenu."""

    if outcome.status == "idle" and not outcome.errors:
        return
    _note(
        "fleet_progression",
        outcome.status,
        {
            "phase": outcome.phase,
            "reason": outcome.reason,
            "errors": len(outcome.errors),
        },
    )


def _note(event: str, status: str, detail: object) -> None:
    """Trace bornée du cycle catalogue : jamais de secret ni de chemin."""

    print(
        json.dumps({"event": event, "status": status, "detail": detail}, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
