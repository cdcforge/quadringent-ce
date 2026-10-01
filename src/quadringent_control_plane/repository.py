"""Dépôt en mémoire de projections Quadringent, strictement en lecture seule."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from threading import Condition, Lock
from typing import Mapping
from urllib.error import URLError
from urllib.parse import ParseResult, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quadringent.site_config import SiteConfig, current as _current_site
from . import fleet_sidecar as _fleet_sidecar
from .fleet_catalog_refresh import FLEET_CATALOG_FILE, FLEET_SIDECAR_FILE
from .fleet_plan import parse_fleet_catalog
from .fleet_progression import RUN_STATE_FORMAT
from .projection import FLEET_CAPABILITY_IDS
from .fleet_sidecar import (
    SIDECAR_FORMAT_VERSION as FLEET_PLAN_FORMAT_VERSION,
    parse_fleet_ui_sidecar,
)
from .model import (
    FleetCapabilityProjection,
    PipelineProjection,
    ProjectionError,
    SourceDescriptor,
    StageProjection,
)
from .projection import build_overview, project_console_document


MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 5.0
# Cadence de la sonde catalogue ~5 min : au-delà, on ne prétend plus rien
# sur l'authentification source.
_AUTH_PROBE_MAX_AGE = timedelta(seconds=900)
_RECOVERABLE_INCIDENT_TYPES = frozenset(
    {"capture_auth_blocked", "capture_stopped", "capture_connection_failure", "capture_timeout"}
)
_PARKED_RUN_STATES = frozenset({"STOPPED_FAIL_CLOSED", "STOPPED_AUTH_BLOCKED"})


def _site() -> SiteConfig:
    return _current_site()
EVENT_HISTORY_LIMIT = 64
_S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


@dataclass(frozen=True)
class ProjectionSource:
    descriptor: SourceDescriptor
    window_proof_origin: str | None = None
    fleet_origin: str | None = None
    fleet_run_origin: str | None = None


@dataclass(frozen=True)
class SourceSnapshot:
    id: str
    evidence_kind: str
    environment: str
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "evidence_kind": self.evidence_kind,
            "environment": self.environment,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class ProjectionSnapshot:
    revision: int
    generated_at: datetime
    pipelines: tuple[PipelineProjection, ...]
    sources: tuple[SourceSnapshot, ...]

    def to_dict(self) -> dict[str, object]:
        overview = build_overview(
            self.pipelines,
            self.revision,
            self.generated_at,
            source_environments=tuple(source.environment for source in self.sources),
        )
        overview["sources"] = [source.to_dict() for source in self.sources]
        return overview


def parse_source_spec(spec: str, *, environment: str = "local") -> ProjectionSource:
    """Parse une source déclarée, sans refléter la valeur contrôlée par l'appelant."""
    try:
        evidence_kind, source_id, origin = spec.split(":", 2)
        parsed = urlparse(origin)
        if evidence_kind not in {"live", "historical", "simulation"}:
            raise ValueError
        if parsed.scheme not in {"file", "http", "https", "s3"}:
            raise ValueError
        if parsed.scheme == "file" and not _is_canonical_file_uri(origin, parsed):
            raise ValueError
        if parsed.scheme in {"http", "https"} and (not parsed.netloc or parsed.username or parsed.password):
            raise ValueError
        if parsed.scheme == "s3" and not _is_canonical_s3_uri(parsed):
            raise ValueError
        return ProjectionSource(SourceDescriptor(source_id, evidence_kind, environment, origin))
    except (AttributeError, ProjectionError, ValueError):
        raise ValueError("Spécification de source invalide") from None


def bind_window_proofs(sources, specs):
    """Bind explicit DEV sidecars; never change a capture writer's document."""
    bound={source.descriptor.id:source for source in sources}
    if len(bound)!=len(sources):
        raise ValueError('Duplicate capture sources')
    seen=set()
    for spec in specs:
        source_id,separator,origin=spec.partition('=')
        if not separator or source_id not in bound or source_id in seen:
            raise ValueError('Invalid window proof binding')
        source=bound[source_id]
        site=_site()
        if source.descriptor.environment!=site.environment:
            raise ValueError(f'Window proof binding requires {site.fleet_environment}')
        parse_source_spec(f'simulation:{source_id}:{origin}',environment=site.environment)
        parsed=urlparse(origin)
        if parsed.scheme=='s3':
            archive=re.compile(
                '/' + re.escape(site.stream_prefix)
                + r'/runs/[a-z0-9][a-z0-9-]{0,79}'
                + r'/(?:windows/[a-z0-9][a-z0-9-]{0,79}/destination\.json|window-chain\.json)'
            )
            if (parsed.netloc!=site.raw_bucket
                    or archive.fullmatch(parsed.path) is None):
                raise ValueError(f'Window proof outside {site.fleet_environment} archive')
        elif parsed.scheme!='file':
            raise ValueError(f'Window proofs require file or {site.fleet_environment} S3')
        bound[source_id]=replace(source,window_proof_origin=origin)
        seen.add(source_id)
    return list(bound.values())


def bind_fleet_proofs(sources, specs):
    """Bind explicit DEV file sidecars. Never query Snowflake or accept a network origin."""
    bound={source.descriptor.id:source for source in sources}
    if len(bound)!=len(sources):
        raise ValueError('Duplicate capture sources')
    seen=set()
    try:
        for spec in specs:
            source_id,separator,origin=spec.partition('=')
            if not separator or source_id not in bound or source_id in seen:
                raise ValueError('Invalid fleet proof binding')
            source=bound[source_id]
            if source.descriptor.environment!=_site().environment:
                raise ValueError(f'Fleet proof binding requires {_site().fleet_environment}')
            parsed=urlparse(origin)
            if parsed.scheme!='file' or not _is_canonical_file_uri(origin, parsed):
                raise ValueError('Invalid fleet proof binding')
            bound[source_id]=replace(source,fleet_origin=origin)
            seen.add(source_id)
    except (AttributeError, TypeError, ProjectionError):
        raise ValueError('Invalid fleet proof binding') from None
    return list(bound.values())


def bind_fleet_runs(sources, specs):
    """Lie l'état domaine durable du pilote — fichier local seulement.

    Le document attendu est ``fleet-run.json`` : son enveloppe porte la
    génération, le ``FleetRun`` sérialisé vit sous ``fleet`` — seul ce
    sous-document rejoint la projection.
    """
    bound = {source.descriptor.id: source for source in sources}
    if len(bound) != len(sources):
        raise ValueError('Duplicate capture sources')
    seen = set()
    try:
        for spec in specs:
            source_id, separator, origin = spec.partition('=')
            if not separator or source_id not in bound or source_id in seen:
                raise ValueError('Invalid fleet run binding')
            source = bound[source_id]
            if source.descriptor.environment != _site().environment:
                raise ValueError(
                    f'Fleet run binding requires {_site().fleet_environment}'
                )
            parsed = urlparse(origin)
            if parsed.scheme != 'file' or not _is_canonical_file_uri(origin, parsed):
                raise ValueError('Invalid fleet run binding')
            bound[source_id] = replace(source, fleet_run_origin=origin)
            seen.add(source_id)
    except (AttributeError, TypeError, ProjectionError):
        raise ValueError('Invalid fleet run binding') from None
    return list(bound.values())


def _project_source(document, source, now):
    if source.fleet_origin is not None:
        sidecar = dict(_read_document(source.fleet_origin))
        if sidecar.get("format_version") == FLEET_PLAN_FORMAT_VERSION:
            parsed = parse_fleet_ui_sidecar(sidecar)
            sidecar_pipelines = parsed["overview"]["pipelines"]
            if (
                source.descriptor.environment != _fleet_sidecar.OVERVIEW_ENVIRONMENT
                or source.descriptor.id
                not in {_site().site_id, _fleet_sidecar.PIPELINE_ID}
                or type(sidecar_pipelines) is not list
                or len(sidecar_pipelines) != 1
                or sidecar_pipelines[0].get("id") != _fleet_sidecar.PIPELINE_ID
                or sidecar_pipelines[0].get("environment") != _fleet_sidecar.OVERVIEW_ENVIRONMENT
            ):
                raise ValueError("Preuve de flotte incompatible")
            # Le run domaine persisté porte les capabilities d'action : sans
            # cette injection le bloc fleet (et le bouton de reprise) ne
            # seraient jamais servis sur le chemin sidecar.
            if source.fleet_run_origin is not None:
                run_document = dict(_read_document(source.fleet_run_origin))
                fleet_document = run_document.get("fleet")
                if (
                    run_document.get("format_version") != RUN_STATE_FORMAT
                    or not isinstance(fleet_document, Mapping)
                ):
                    raise ValueError("État de flotte incompatible")
                document = dict(document)
                document["fleet"] = fleet_document
            pipeline = project_console_document(document, source.descriptor, now=now)
            pipeline = replace(pipeline, fleet_plan=deepcopy(parsed["fleet"]))
            return _reconcile_capture_recovery(
                pipeline,
                fleet_plan=parsed["fleet"],
                document=document,
                fleet_origin=source.fleet_origin,
                now=now,
            )
        document=dict(document)
        document['fleet']=sidecar
    if source.fleet_run_origin is not None:
        # Le run domaine persisté est autoritaire : ses phases mesurées
        # priment sur tout sidecar — un document hors contrat est refusé,
        # jamais contourné.
        run_document = dict(_read_document(source.fleet_run_origin))
        fleet_document = run_document.get("fleet")
        if (
            run_document.get("format_version") != RUN_STATE_FORMAT
            or not isinstance(fleet_document, Mapping)
        ):
            raise ValueError("État de flotte incompatible")
        document = dict(document)
        document["fleet"] = fleet_document
    origin=source.window_proof_origin
    if origin is None:
        return project_console_document(document,source.descriptor,now=now)
    # An explicit sidecar is authoritative for this field, even when unavailable.
    combined=dict(document)
    combined.pop('window_destination_proof',None)
    if urlparse(origin).path.endswith('/window-chain.json'):
        return _project_chain_source(combined, source, now)
    try:
        proof=dict(_read_document(origin))
    except (OSError,URLError,ValueError,UnicodeDecodeError):
        pipeline=project_console_document(combined,source.descriptor,now=now)
        return replace(pipeline,window_delivery={'state':'unavailable','reason':'window_proof_read_failed'})
    parsed=urlparse(origin)
    if parsed.scheme=='file':
        proof['storage_backend']='local'
    else:
        parts=parsed.path.split('/')
        if (proof.get('archive_run_id')!=parts[5] or proof.get('window_id')!=parts[7]
                or proof.get('storage_backend')!='s3'):
            proof={}
    combined['window_destination_proof']=proof
    return project_console_document(combined,source.descriptor,now=now)


def _project_chain_source(document, source, now):
    from .window_chain import BoundedChainStore, project_window_chain
    parsed = urlparse(source.window_proof_origin)
    pipeline = project_console_document(document, source.descriptor, now=now)
    root = parsed.path.rsplit('/', 1)[0]
    try:
        if parsed.scheme == 's3':
            from quadringent.object_store import S3ObjectStore
            store = S3ObjectStore(parsed.netloc, root.lstrip('/'), client=_s3_client())
            read = store.get_bounded
        else:
            directory = Path(root).resolve(strict=True)
            def read(key, limit):
                path = (directory / key).resolve(strict=True)
                if not path.is_relative_to(directory): raise ValueError('chain path escaped root')
                with path.open('rb') as handle: value = handle.read(limit+1)
                if len(value) > limit: raise ValueError('chain file exceeds budget')
                return value
        delivery = project_window_chain(BoundedChainStore(read), run_id=root.rsplit('/',1)[-1],
                                        flux=document.get('flux', {}), source=source.descriptor,
                                        now=now, storage_backend='s3' if parsed.scheme=='s3' else 'local')
    except Exception:
        # SDK credential/transport errors must degrade only this proof, never
        # abort the capture projection or leak provider exception details.
        delivery = {'state': 'unavailable', 'reason': 'window_chain_read_failed'}
    return replace(pipeline, window_delivery=delivery)


class ProjectionRepository:
    def __init__(self, sources: list[ProjectionSource] | tuple[ProjectionSource, ...], *, infrastructure_costs_source: str | None = None) -> None:
        if infrastructure_costs_source:
            parsed = urlparse(infrastructure_costs_source)
            if parsed.scheme != "file" or not _is_canonical_file_uri(infrastructure_costs_source, parsed):
                raise ValueError("La preuve de coûts doit être un fichier local déclaré")
        self._infrastructure_costs_source = infrastructure_costs_source
        self._sources = tuple(bind_window_proofs(sources,[
            f'{source.descriptor.id}={source.window_proof_origin}'
            for source in sources if source.window_proof_origin is not None
        ]))
        self._sources = tuple(bind_fleet_proofs(self._sources,[
            f'{source.descriptor.id}={source.fleet_origin}'
            for source in self._sources if source.fleet_origin is not None
        ]))
        self._sources = tuple(bind_fleet_runs(self._sources, [
            f'{source.descriptor.id}={source.fleet_run_origin}'
            for source in self._sources if source.fleet_run_origin is not None
        ]))
        if len({source.descriptor.id for source in self._sources}) != len(self._sources):
            raise ValueError("Identifiants de source dupliqués")
        self._condition = Condition()
        self._refresh_lock = Lock()
        self._snapshot = ProjectionSnapshot(0, datetime.now(timezone.utc), (), ())
        self._fingerprint: str | None = None
        self._history: list[ProjectionSnapshot] = []

    def refresh(self) -> ProjectionSnapshot:
        # Les lectures I/O peuvent se terminer dans un ordre différent de leur
        # démarrage. Ce verrou dédié préserve l'ordre de publication sans
        # retenir la condition utilisée par snapshot() et les clients SSE.
        with self._refresh_lock:
            return self._refresh_serialized()

    def _refresh_serialized(self) -> ProjectionSnapshot:
        pipelines: list[PipelineProjection] = []
        source_states: list[SourceSnapshot] = []
        now = datetime.now(timezone.utc)
        with self._condition:
            previous_pipelines = {pipeline.id: pipeline for pipeline in self._snapshot.pipelines}
        for source in self._sources:
            try:
                document = _read_document(source.descriptor.origin)
                pipeline = _project_source(document,source,now)
                if self._infrastructure_costs_source:
                    from quadringent.infrastructure_costs import project_infrastructure_costs
                    try:
                        cost_document = _read_document(self._infrastructure_costs_source)
                        site = _site()
                        if (source.descriptor.environment != site.environment
                            or source.descriptor.id not in {site.site_id, site.pipeline_id, site.sidecar_pipeline_id, site.fleet_id}):
                            raise ValueError("coûts hors site")
                        costs = project_infrastructure_costs(cost_document, site=site, now=now,
                                                            evidence_kind=source.descriptor.evidence_kind)
                    except (OSError, ValueError, UnicodeDecodeError):
                        costs = {"status":"unavailable", "reason":"cost_evidence_unavailable"}
                    pipeline = replace(pipeline, infrastructure_costs=costs)
                pipelines.append(pipeline)
                source_states.append(_source_state(source, "available"))
            except (OSError, URLError, ValueError, ProjectionError, UnicodeDecodeError):
                previous = previous_pipelines.get(source.descriptor.id)
                if previous is not None:
                    pipelines.append(_unavailable_pipeline(previous))
                source_states.append(
                    _source_state(source, "unavailable", error="source_refresh_failed")
                )
        pipelines.sort(key=lambda pipeline: pipeline.id)
        canonical = {
            "pipelines": [pipeline.to_dict() for pipeline in pipelines],
            "sources": [source.to_dict() for source in source_states],
        }
        fingerprint = hashlib.sha256(_canonical_json(canonical)).hexdigest()
        with self._condition:
            revision = self._snapshot.revision
            if fingerprint != self._fingerprint:
                revision += 1
                self._fingerprint = fingerprint
            else:
                return self._snapshot
            snapshot = ProjectionSnapshot(revision, now, tuple(pipelines), tuple(source_states))
            if revision != self._snapshot.revision:
                self._history.append(snapshot)
                del self._history[:-EVENT_HISTORY_LIMIT]
                self._condition.notify_all()
            self._snapshot = snapshot
            return snapshot

    def snapshot(self) -> ProjectionSnapshot:
        with self._condition:
            return self._snapshot

    def wait_after(self, revision: int, timeout_s: float) -> ProjectionSnapshot:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("Curseur de révision invalide")
        if timeout_s < 0:
            raise ValueError("Délai invalide")
        with self._condition:
            self._condition.wait_for(lambda: self._snapshot.revision > revision, timeout_s)
            return self._snapshot

    def event_after(self, revision: int) -> tuple[str, ProjectionSnapshot]:
        """Indique si un curseur peut être rejoué ou doit être réinitialisé."""
        event, snapshots = self.events_after(revision)
        return event, snapshots[-1]

    def events_after(self, revision: int) -> tuple[str, tuple[ProjectionSnapshot, ...]]:
        """Retourne toutes les révisions retenues après un curseur SSE."""
        with self._condition:
            current = self._snapshot
            # A reconnect may carry a cursor from the previous process. Never
            # wait for that process-local number to be reached again.
            if revision > current.revision:
                return "reset", (current,)
            if revision == current.revision:
                return "cursor", (current,)
            if not self._history or revision < self._history[0].revision - 1:
                return "reset", (current,)
            return "updated", tuple(snapshot for snapshot in self._history if snapshot.revision > revision)


def _reconcile_capture_recovery(
    pipeline: PipelineProjection,
    *,
    fleet_plan: Mapping[str, object],
    document: Mapping[str, object],
    fleet_origin: str,
    now: datetime,
) -> PipelineProjection:
    """Requalifie un incident de capture quand la sonde catalogue prouve que
    la source accepte de nouveau la connexion.

    La capture arrêtée fail-closed ne reteste jamais le sign-on : la preuve
    vient du relevé catalogue périodique, qui ouvre une connexion JTOpen
    fraîche. Un relevé frais + continuité prouvée = la cause de l'arrêt a
    disparu ; la capture est alors « prête à reprendre », pas en incident
    actif. Toute preuve absente ou périmée conserve l'incident tel quel.
    """
    incident = pipeline.incident
    if not isinstance(incident, Mapping):
        return pipeline
    if incident.get("type") not in _RECOVERABLE_INCIDENT_TYPES:
        return pipeline
    run = document.get("run")
    if not isinstance(run, Mapping) or run.get("state") not in _PARKED_RUN_STATES:
        return pipeline
    journal = fleet_plan.get("journal")
    if not isinstance(journal, Mapping) or journal.get("continuity") != "proven":
        return pipeline
    observed_raw = fleet_plan.get("observed_at")
    if not isinstance(observed_raw, str):
        return pipeline
    try:
        auth_observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
    except ValueError:
        return pipeline
    if auth_observed.tzinfo is None:
        return pipeline
    auth_observed = auth_observed.astimezone(timezone.utc)
    age = now.astimezone(timezone.utc) - auth_observed
    if age < timedelta(seconds=-60) or age > _AUTH_PROBE_MAX_AGE:
        return pipeline
    probe_iso = auth_observed.isoformat()
    stages = tuple(
        StageProjection(
            stage.id,
            "awaiting_resume" if stage.id == "capture" else stage.status,
            probe_iso if stage.id in {"source", "capture"} else stage.observed_at,
            "Prête à reprendre" if stage.id == "capture" else ("Connexion vérifiée" if stage.id == "source" else stage.headline),
            "La source accepte de nouveau la connexion" if stage.id == "capture" else ("Authentification et catalogue vérifiés" if stage.id == "source" else stage.detail),
        )
        for stage in pipeline.stages
    )
    resolved_incident = dict(incident)
    resolved_incident["cause_resolved"] = True
    resolved_incident["cause_resolved_observed_at"] = probe_iso
    capture_observed = next(
        (stage.observed_at for stage in pipeline.stages if stage.id == "capture"),
        None,
    )
    if capture_observed is not None:
        resolved_incident.setdefault("declared_at", capture_observed)
    resume: dict[str, object] = {
        "state": "ready",
        "auth_observed_at": probe_iso,
        "checkpoint": _document_checkpoint(document),
        "tail": fleet_plan.get("cutover_checkpoint"),
        "backlog_sequences": None,
        "backlog_receivers": None,
    }
    backlog = _catalog_backlog(resume["checkpoint"], fleet_origin)
    if backlog is not None:
        resume.update(backlog)
    return replace(
        pipeline,
        status="awaiting_resume",
        summary="Prête à reprendre",
        stages=stages,
        incident=resolved_incident,
        resume=resume,
    )


def _document_checkpoint(document: Mapping[str, object]) -> dict[str, object] | None:
    position = document.get("position")
    if not isinstance(position, Mapping):
        return None
    checkpoint = position.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    receiver = checkpoint.get("receiver")
    sequence = checkpoint.get("sequence")
    if not isinstance(receiver, str) or not isinstance(sequence, int) or isinstance(sequence, bool):
        return None
    return {"receiver": receiver, "sequence": sequence}


def _catalog_backlog(checkpoint: object, fleet_origin: str) -> dict[str, object] | None:
    """Séquences journal entre le checkpoint durable et le tail courant.

    Les bornes viennent du catalogue frais relu à côté du sidecar ; tout
    écart (catalogue illisible, receiver de checkpoint hors chaîne) rend le
    backlog non mesuré plutôt qu'inventé.
    """
    if not isinstance(checkpoint, Mapping) or not fleet_origin:
        return None
    if not fleet_origin.endswith("/" + FLEET_SIDECAR_FILE):
        return None
    catalog_origin = fleet_origin.rsplit("/", 1)[0] + "/" + FLEET_CATALOG_FILE
    try:
        catalog = parse_fleet_catalog(dict(_read_document(catalog_origin)))
    except Exception:
        return None
    journals = catalog.journals
    if len(journals) != 1:
        return None
    receivers = journals[0].receivers
    names = [receiver.name for receiver in receivers]
    try:
        index = names.index(checkpoint["receiver"])
    except ValueError:
        return {"backlog_receivers": None, "backlog_sequences": None}
    sequences = receivers[index].last_sequence - int(checkpoint["sequence"])
    for receiver in receivers[index + 1 :]:
        sequences += receiver.last_sequence - receiver.first_sequence + 1
    last = receivers[-1]
    return {
        "backlog_receivers": len(receivers) - index,
        "backlog_sequences": sequences,
        "tail": {"receiver": last.name, "sequence": last.last_sequence},
    }


def _source_state(
    source: ProjectionSource, status: str, *, error: str | None = None
) -> SourceSnapshot:
    descriptor = source.descriptor
    return SourceSnapshot(
        descriptor.id,
        descriptor.evidence_kind,
        descriptor.environment,
        status,
        error,
    )


def _unavailable_pipeline(previous: PipelineProjection) -> PipelineProjection:
    """Conserve la dernière observation sans prétendre qu'elle est encore actuelle."""
    retained = deepcopy(previous)
    replacements: dict[str, object] = {
        "status": "unknown",
        "quality": {**retained.quality, "freshness": "stale"},
        "summary": "Source indisponible : dernière observation conservée",
    }
    fields = type(retained).__dataclass_fields__
    if "window_delivery" in fields:
        replacements["window_delivery"] = (
            {**retained.window_delivery,'quality':{**retained.window_delivery.get('quality',{}),'freshness':'stale'}}
            if retained.window_delivery is not None else None
        )
    if "fleet" in fields:
        replacements["fleet"] = _retain_stale_fleet(retained.fleet)
    return replace(retained, **replacements)


def _retain_stale_fleet(fleet: object) -> object:
    """Keep a retained fleet proof but mark every capability stale, without inventing counts."""
    if fleet is None:
        return None
    unavailable = FleetCapabilityProjection("unavailable", "stale_proof")
    # La liste vient du contrat publié : une action ajoutée ne peut plus être
    # oubliée ici et disparaître d'une flotte conservée.
    capabilities = {name: unavailable for name in FLEET_CAPABILITY_IDS}
    try:
        return replace(fleet, capabilities=capabilities)
    except TypeError:
        pass
    if isinstance(fleet, Mapping):
        payload = dict(fleet)
        payload["capabilities"] = {
            name: capability.to_dict() for name, capability in capabilities.items()
        }
        return payload
    return fleet


def _read_document(origin: str) -> Mapping[str, object]:
    parsed = urlparse(origin)
    if parsed.scheme == "file":
        if not _is_canonical_file_uri(origin, parsed):
            raise ValueError("Origine de fichier invalide")
        path = Path(parsed.path)
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("Document trop volumineux")
        with path.open("rb") as document:
            raw = document.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError("Document trop volumineux")
    elif parsed.scheme in {"http", "https"}:
        request = Request(origin, headers={"Accept": "application/json"})
        opener = build_opener(_NoHttpsDowngrade())
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError("Document trop volumineux")
    elif parsed.scheme == "s3":
        if not _is_canonical_s3_uri(parsed):
            raise ValueError("Origine S3 invalide")
        raw = _read_s3_document(parsed)
    else:
        raise ValueError("Origine non prise en charge")
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Document JSON invalide")
    return decoded


class _NoHttpsDowngrade(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> Request | None:
        if urlparse(req.full_url).scheme == "https" and urlparse(newurl).scheme == "http":
            raise URLError("Redirection HTTPS non autorisée")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _is_canonical_file_uri(origin: str, parsed: ParseResult) -> bool:
    return (
        origin.startswith("file:///")
        and not origin.startswith("file:////")
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and bool(parsed.path)
        and Path(parsed.path).is_absolute()
    )


def _is_canonical_s3_uri(parsed: ParseResult) -> bool:
    key = parsed.path.removeprefix("/")
    return (
        bool(_S3_BUCKET.fullmatch(parsed.netloc))
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith("/")
        and not parsed.path.startswith("//")
        and bool(key)
        and "%" not in key
        and all(part not in {"", ".", ".."} for part in key.split("/"))
    )


def _s3_client():
    import boto3

    return boto3.client("s3")


def _read_s3_document(parsed: ParseResult) -> bytes:
    bucket = parsed.netloc
    key = parsed.path.removeprefix("/")
    try:
        client = _s3_client()
        metadata = client.head_object(Bucket=bucket, Key=key)
        size = metadata.get("ContentLength")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Taille de document S3 invalide")
        if size > MAX_DOCUMENT_BYTES:
            raise ValueError("Document trop volumineux")
        response = client.get_object(
            Bucket=bucket,
            Key=key,
            Range=f"bytes=0-{MAX_DOCUMENT_BYTES}",
        )
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise ValueError("Document S3 invalide")
        raw = body.read(MAX_DOCUMENT_BYTES + 1)
    except ValueError:
        raise
    except Exception:
        raise OSError("Lecture S3 impossible") from None
    if not isinstance(raw, bytes):
        raise ValueError("Document S3 invalide")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document trop volumineux")
    return raw
