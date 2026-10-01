"""Sidecar UI persistant, dérivé d'un catalogue metadata-only et d'un FleetPlan.

Enveloppe versionnée distincte de `/v1/overview` et de `quadringent-fleet-v1`.
Le nested `overview` reste parseable par le contrat Overview actuel ; le bloc
`fleet` porte les preuves agrégées. Aucune ligne métier, aucun coût inventé.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Mapping

from quadringent.site_config import SiteConfig, current as _current_site
from quadringent_control_plane import fleet as _fleet
from quadringent_control_plane.fleet import (
    MAX_CONCURRENCY,
    FleetError,
)
from quadringent_control_plane.fleet_plan import (
    CATALOG_FORMAT_VERSION,
    CONTINUITY_BROKEN,
    CONTINUITY_PROVEN,
    CONTINUITY_UNCERTAIN,
    IDENTITY_BLOCKED,
    IDENTITY_KEYED,
    IDENTITY_RRN,
    PLAN_FORMAT_VERSION,
    FleetCatalog,
    FleetPlan,
    TablePlan,
    build_fleet_plan,
    parse_fleet_catalog,
)
from quadringent_control_plane.model import (
    PASS_THROUGH_FIELDS,
    ObservabilityProjection,
    PipelineProjection,
    StageProjection,
)
from quadringent_control_plane.projection import (
    CLOCK_SKEW_TOLERANCE,
    FRESH_FOR,
    PUBLIC_COUNTERS,
    build_overview,
)


SIDECAR_FORMAT_VERSION = "quadringent-fleet-ui-sidecar-v1"
# Identités et périmètres du site déclaré — résolus à l'usage via
# ``__getattr__`` ; jamais figés à une installation.
PIPELINE_ID: str
OVERVIEW_ENVIRONMENT: str
EVIDENCE_KIND = "historical"
PROVENANCE_KIND = "metadata_catalog"
COST_STATUS_UNKNOWN = "unknown"
COST_UNKNOWN_BECAUSE = "coût réel non observé"
OBSERVABILITY_STATUS_UNOBSERVED = "unobserved"
OBSERVABILITY_UNOBSERVED_REASON = (
    "Observabilité non observée : aucune preuve SLO n'est fournie par le catalogue metadata-only"
)
PROMISE_BLOCKED = "blocked"
PROMISE_POSSIBLE = "possible"
MAX_CATALOG_BYTES = 2 * 1024 * 1024
FILE_MODE = 0o644

# Progression de la copie historique. L'orchestrateur de copie publie un
# document `history-progress` par run sous le préfixe déclaré du site ; le
# sidecar le relit sans jamais l'inventer : absent ou invalide, les champs
# restent nuls et le statut le dit.
HISTORY_PROGRESS_BUCKET: str
HISTORY_PROGRESS_PREFIX: str
HISTORY_PROGRESS_KIND = "history-progress"
HISTORY_PROGRESS_MAX_KEYS = 512
HISTORY_PROGRESS_MAX_BYTES = 256 * 1024
_HISTORY_PROGRESS_STATUSES = ("pending", "running", "read_done", "published", "failed")
_HISTORY_PROGRESS_DOC_KEYS = (
    "kind",
    "run_id",
    "table",
    "updated_at",
    "max_rrn",
    "chunk_rows",
    "chunks",
    "totals",
)
_HISTORY_PROGRESS_CHUNK_KEYS = (
    "worker",
    "rrn_start",
    "rrn_end",
    "ordinal_offset",
    "rows",
    "bytes",
    "status",
)
_HISTORY_PROGRESS_TOTAL_KEYS = (
    "planned_rows",
    "published_rows",
    "published_bytes",
    "published_objects",
)
_PROGRESS_KEYS = (
    "status",
    "run_id",
    "updated_at",
    "planned_rows",
    "published_rows",
    "published_bytes",
    "published_objects",
    "chunks_pending",
    "chunks_running",
    "chunks_read_done",
    "chunks_published",
    "chunks_failed",
)
_PROGRESS_OBSERVED = ("running", "complete", "failed")

_SIDECAR_KEYS = (
    "format_version",
    "generated_at",
    "overview",
    "fleet",
)
_FLEET_KEYS = (
    "environment",
    "source_schema",
    "destination_namespace",
    "observed_at",
    "provenance",
    "freshness",
    "continuity",
    "live_blocked",
    "certification_blocked",
    "live_promise",
    "certification_promise",
    "promise_blockers",
    "history_admitted",
    "cutover_checkpoint",
    "cutover_required_before_history",
    "journal",
    "identity",
    "observed_totals",
    "historical",
    "cost",
    "tables",
)
_PROVENANCE_KEYS = ("kind", "catalog_format", "plan_format", "observed_at")
_IDENTITY_KEYS = ("keyed_count", "rrn_count", "blocked_count", "keyed", "rrn", "blocked")
_TOTAL_KEYS = ("table_count", "row_count", "data_size")
_HISTORICAL_KEYS = (
    "max_concurrency",
    "byte_budget",
    "admitted_count",
    "excluded_count",
    "lanes",
)
_COST_KEYS = ("status", "observed", "unknown_because")
_TABLE_KEYS = (
    "name",
    "row_count",
    "data_size",
    "journal_images",
    "identity_status",
    "identity_source",
    "candidate_key",
    "live_possible",
    "certification_possible",
    "historical_admitted",
    "historical_lane",
    "blocked_reasons",
    "copied_rows",
    "copied_bytes",
    "history_progress",
)
_OVERVIEW_KEYS = ("revision", "generated_at", "scope", "pipelines", "sources")
_OBSERVABILITY_KEYS = ("status", "quality", "observed_at", "reason", "checks", "alerts")
_SENSITIVE_TOKENS = (
    "host",
    "user",
    "password",
    "secret",
    "token",
    "credential",
    "credit",
    "path",
)
_JSON_SCALARS = (str, int, float, bool, type(None))


def __getattr__(name: str) -> object:
    """Périmètres du site déclaré, résolus à l'accès — jamais figés au code."""

    site_attributes = {
        "PIPELINE_ID": lambda site: site.sidecar_pipeline_id,
        "OVERVIEW_ENVIRONMENT": lambda site: site.environment,
        "HISTORY_PROGRESS_BUCKET": lambda site: site.raw_bucket,
        "HISTORY_PROGRESS_PREFIX": lambda site: site.history_progress_prefix,
    }
    resolver = site_attributes.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver(_site())


def _site() -> SiteConfig:
    return _current_site()


def generate_fleet_ui_sidecar(
    payload: Mapping[str, object],
    *,
    max_concurrency: int = MAX_CONCURRENCY,
    historical_byte_budget: int | None = None,
    generated_at: datetime | None = None,
    history_progress: Mapping[str, object] | None = None,
) -> dict[str, object]:
    catalog = parse_fleet_catalog(payload)
    return build_fleet_ui_sidecar(
        catalog,
        max_concurrency=max_concurrency,
        historical_byte_budget=historical_byte_budget,
        generated_at=generated_at,
        history_progress=history_progress,
    )


def build_fleet_ui_sidecar(
    catalog: FleetCatalog,
    *,
    max_concurrency: int = MAX_CONCURRENCY,
    historical_byte_budget: int | None = None,
    generated_at: datetime | None = None,
    history_progress: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if type(catalog) is not FleetCatalog:
        raise FleetError("invalid_catalog", "Catalogue de flotte invalide")
    now = datetime.now(timezone.utc) if generated_at is None else generated_at
    if type(now) is not datetime or now.tzinfo is None:
        raise FleetError("invalid_generated_at", "Horodatage de génération invalide")
    now = now.astimezone(timezone.utc)
    plan = build_fleet_plan(
        catalog,
        max_concurrency=max_concurrency,
        historical_byte_budget=historical_byte_budget,
    )
    sidecar = _sidecar_document(
        plan, generated_at=now, history_progress=history_progress
    )
    _assert_sidecar_safe(sidecar)
    return sidecar


def parse_fleet_ui_sidecar(payload: Mapping[str, object]) -> dict[str, object]:
    data = _closed_mapping(payload, _SIDECAR_KEYS, code="invalid_sidecar")
    if data["format_version"] != SIDECAR_FORMAT_VERSION:
        raise FleetError("invalid_sidecar", "Version de sidecar inconnue")
    _require_utc(data["generated_at"], "Horodatage de sidecar invalide")
    overview = _parse_overview(data["overview"])
    fleet = _parse_fleet(data["fleet"])
    if overview["generated_at"] != data["generated_at"]:
        raise FleetError("invalid_sidecar", "Horodatage de sidecar incohérent")
    if fleet["observed_at"] != overview["pipelines"][0]["observed_at"]:
        raise FleetError("invalid_sidecar", "Horodatage de preuve incohérent")
    _assert_sidecar_safe(data)
    return {
        "format_version": SIDECAR_FORMAT_VERSION,
        "generated_at": data["generated_at"],
        "overview": overview,
        "fleet": fleet,
    }


def load_fleet_catalog_file(path: str | Path) -> FleetCatalog:
    raw = _read_catalog_bytes(path)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FleetError("invalid_catalog", "Catalogue JSON invalide") from None
    if type(payload) is not dict:
        raise FleetError("invalid_catalog", "Catalogue JSON non autorisé")
    return parse_fleet_catalog(payload)


def write_fleet_ui_sidecar(path: str | Path, payload: Mapping[str, object]) -> None:
    document = parse_fleet_ui_sidecar(payload)
    _write_atomic_json(path, document)


def generate_fleet_ui_sidecar_file(
    catalog_path: str | Path,
    output_path: str | Path,
    *,
    max_concurrency: int = MAX_CONCURRENCY,
    historical_byte_budget: int | None = None,
    generated_at: datetime | None = None,
    history_progress: Mapping[str, object] | None = None,
) -> dict[str, object]:
    catalog = load_fleet_catalog_file(catalog_path)
    sidecar = build_fleet_ui_sidecar(
        catalog,
        max_concurrency=max_concurrency,
        historical_byte_budget=historical_byte_budget,
        generated_at=generated_at,
        history_progress=history_progress,
    )
    write_fleet_ui_sidecar(output_path, sidecar)
    return sidecar


def _sidecar_document(
    plan: FleetPlan,
    *,
    generated_at: datetime,
    history_progress: Mapping[str, object] | None = None,
) -> dict[str, object]:
    observed = _parse_utc(plan.observed_at)
    observed_iso = _utc_iso(observed)
    generated_iso = _utc_iso(generated_at)
    freshness = _freshness(generated_at - observed)
    overview = _overview_document(plan, generated_at=generated_at, observed_iso=observed_iso, freshness=freshness)
    fleet = _fleet_proofs(plan, observed_iso=observed_iso, freshness=freshness, history_progress=history_progress)
    return {
        "format_version": SIDECAR_FORMAT_VERSION,
        "generated_at": generated_iso,
        "overview": overview,
        "fleet": fleet,
    }


def _overview_document(
    plan: FleetPlan,
    *,
    generated_at: datetime,
    observed_iso: str,
    freshness: str,
) -> dict[str, object]:
    history_admitted = _history_admitted(plan)
    summary = _overview_summary(history_admitted=history_admitted)
    stages = (
        StageProjection(
            "source",
            "healthy",
            observed_iso,
            "Catalogue metadata-only observé",
            "Le manifeste du site et le journal unique sont décrits par le catalogue",
        ),
        StageProjection(
            "capture",
            "unknown",
            None,
            "Capture non observée",
            "Aucune preuve de run de capture n'est fournie",
        ),
        StageProjection(
            "raw",
            "unknown",
            None,
            "Raw non observé",
            "Aucune publication raw n'est observée",
        ),
        StageProjection(
            "load",
            "unknown",
            None,
            "Chargement non observé",
            "Aucune preuve de chargement n'est fournie",
        ),
        StageProjection(
            "destination",
            "unknown",
            None,
            "Destination non observée",
            "Aucune preuve d'application destination n'est fournie",
        ),
    )
    pipeline = PipelineProjection(
        id=_site().sidecar_pipeline_id,
        environment=_site().environment,
        status="unknown",
        quality={
            "coverage": "partial",
            "freshness": freshness,
            "evidence_kind": EVIDENCE_KIND,
        },
        summary=summary,
        observed_at=observed_iso,
        stages=stages,
        lag_sequences=None,
        lag_seconds=None,
        lag_series=None,
        counters={name: None for name in sorted(PUBLIC_COUNTERS)},
        incident=None,
        observability=_unobserved_observability(freshness),
        lag_verdict_reason="verdict_not_declared",
        destination_reason="destination_not_attached",
    )
    overview = build_overview(
        (pipeline,),
        0,
        generated_at,
        source_environments=(_site().environment,),
    )
    overview["sources"] = [
        {
            "id": _site().sidecar_pipeline_id,
            "evidence_kind": EVIDENCE_KIND,
            "environment": _site().environment,
            "status": "available",
            "error": None,
        }
    ]
    return overview


def _fleet_proofs(
    plan: FleetPlan,
    *,
    observed_iso: str,
    freshness: str,
    history_progress: Mapping[str, object] | None = None,
) -> dict[str, object]:
    keyed = tuple(table.name for table in plan.tables if table.identity_status == IDENTITY_KEYED)
    rrn = tuple(table.name for table in plan.tables if table.identity_status == IDENTITY_RRN)
    blocked = tuple(table.name for table in plan.tables if table.identity_status == IDENTITY_BLOCKED)
    identity_blocks = len(blocked) > 0
    live_promise_blocked = plan.live_blocked or identity_blocks
    certification_promise_blocked = plan.certification_blocked or identity_blocks
    blockers: list[str] = []
    if plan.continuity == CONTINUITY_UNCERTAIN:
        blockers.append("uncertain_continuity")
    elif plan.continuity == CONTINUITY_BROKEN:
        blockers.append("broken_continuity")
    elif plan.continuity != CONTINUITY_PROVEN:
        raise FleetError("invalid_journal", "Continuité de journal inconnue")
    if identity_blocks:
        blockers.append("unproven_identity")
    admitted = tuple(table for table in plan.tables if table.historical_lane is not None)
    group = plan.journal_groups[0]
    return {
        "environment": _fleet.ENVIRONMENT,
        "source_schema": plan.source_schema,
        "destination_namespace": _fleet.DESTINATION_NAMESPACE,
        "observed_at": observed_iso,
        "provenance": {
            "kind": PROVENANCE_KIND,
            "catalog_format": CATALOG_FORMAT_VERSION,
            "plan_format": PLAN_FORMAT_VERSION,
            "observed_at": observed_iso,
        },
        "freshness": freshness,
        "continuity": plan.continuity,
        "live_blocked": plan.live_blocked,
        "certification_blocked": plan.certification_blocked,
        "live_promise": PROMISE_BLOCKED if live_promise_blocked else PROMISE_POSSIBLE,
        "certification_promise": PROMISE_BLOCKED if certification_promise_blocked else PROMISE_POSSIBLE,
        "promise_blockers": blockers,
        "history_admitted": _history_admitted(plan),
        "cutover_checkpoint": plan.cutover_checkpoint.to_dict(),
        "cutover_required_before_history": True,
        "journal": group.to_dict(),
        "identity": {
            "keyed_count": len(keyed),
            "rrn_count": len(rrn),
            "blocked_count": len(blocked),
            "keyed": list(keyed),
            "rrn": list(rrn),
            "blocked": list(blocked),
        },
        "observed_totals": {
            "table_count": _fleet.TABLE_COUNT,
            "row_count": plan.observed_row_count,
            "data_size": plan.observed_data_size,
        },
        "historical": {
            "max_concurrency": plan.max_concurrency,
            "byte_budget": plan.historical_byte_budget,
            "admitted_count": len(admitted),
            "excluded_count": _fleet.TABLE_COUNT - len(admitted),
            "lanes": [lane.to_dict() for lane in plan.historical_lanes],
        },
        "cost": {
            "status": COST_STATUS_UNKNOWN,
            "observed": None,
            "unknown_because": COST_UNKNOWN_BECAUSE,
        },
        "tables": [
            _table_proof(table, progress=_progress_for(history_progress, table.name))
            for table in plan.tables
        ],
    }


def _progress_for(
    history_progress: Mapping[str, object] | None, table: str
) -> dict[str, object] | None:
    """Valide un document history-progress brut ; jamais d'exception vers le sidecar.

    Un document invalide n'est pas « absent » : il est rendu ``invalid`` pour
    que l'écran distingue « rien n'a été publié » de « ce qui est publié ne se
    lit pas ».
    """

    if history_progress is None:
        return None
    raw = history_progress.get(table)
    if raw is None:
        return None
    try:
        return parse_history_progress(raw)
    except FleetError:
        return {"status": "invalid"}


def _table_proof(
    table: TablePlan, *, progress: dict[str, object] | None = None
) -> dict[str, object]:
    public_progress: dict[str, object] | None = None
    copied_rows: int | None = None
    copied_bytes: int | None = None
    if progress is not None:
        public_progress = {name: progress.get(name) for name in _PROGRESS_KEYS}
        if progress["status"] in _PROGRESS_OBSERVED:
            copied_rows = progress["published_rows"]  # type: ignore[assignment]
            # Les octets copiés viennent des lots publiés mesurés par
            # l'orchestrateur — le contrat les valide, rien n'est estimé.
            copied_bytes = progress["published_bytes"]  # type: ignore[assignment]
    return {
        "name": table.name,
        "row_count": table.row_count,
        "data_size": table.data_size,
        "journal_images": table.journal_images,
        "identity_status": table.identity_status,
        "identity_source": table.identity_source,
        "candidate_key": None if table.candidate_key is None else list(table.candidate_key),
        "live_possible": table.live_possible,
        "certification_possible": table.certification_possible,
        "historical_admitted": table.historical_lane is not None,
        "historical_lane": table.historical_lane,
        "blocked_reasons": list(table.blocked_reasons),
        "copied_rows": copied_rows,
        "copied_bytes": copied_bytes,
        "history_progress": public_progress,
    }


def parse_history_progress(payload: object) -> dict[str, object]:
    """Valide un document ``history-progress`` publié par l'orchestrateur.

    Fail-closed : toute incohérence (totaux ≠ somme des tranches publiées,
    table hors manifeste, statut inconnu) rend le document inutilisable.
    """

    data = _closed_mapping(
        payload, _HISTORY_PROGRESS_DOC_KEYS, code="invalid_history_progress"
    )
    if data["kind"] != HISTORY_PROGRESS_KIND:
        raise FleetError("invalid_history_progress", "Nature de progression inconnue")
    run_id = data["run_id"]
    if type(run_id) is not str or not run_id:
        raise FleetError("invalid_history_progress", "Run de progression invalide")
    table = data["table"]
    if table not in _fleet.MANIFEST:
        raise FleetError("invalid_history_progress", "Table de progression hors manifeste")
    _require_utc(data["updated_at"], "Horodatage de progression invalide")
    for name in ("max_rrn", "chunk_rows"):
        value = data[name]
        if type(value) is not int or value < 1:
            raise FleetError("invalid_history_progress", "Bornes de progression invalides")
    totals = _closed_mapping(
        data["totals"], _HISTORY_PROGRESS_TOTAL_KEYS, code="invalid_history_progress"
    )
    for name in _HISTORY_PROGRESS_TOTAL_KEYS:
        value = totals[name]
        if type(value) is not int or value < 0:
            raise FleetError("invalid_history_progress", "Totaux de progression invalides")
    chunks = data["chunks"]
    if type(chunks) is not list:
        raise FleetError("invalid_history_progress", "Tranches de progression invalides")
    counts = dict.fromkeys(_HISTORY_PROGRESS_STATUSES, 0)
    published_rows = 0
    published_bytes = 0
    seen_chunks: set[tuple[int, int, int]] = set()
    for item in chunks:
        chunk = _closed_mapping(
            item, _HISTORY_PROGRESS_CHUNK_KEYS, code="invalid_history_progress"
        )
        for name in ("worker", "rrn_start", "rrn_end", "ordinal_offset", "rows", "bytes"):
            value = chunk[name]
            if type(value) is not int or value < 0:
                raise FleetError(
                    "invalid_history_progress", "Tranche de progression invalide"
                )
        if chunk["rrn_start"] < 1 or chunk["rrn_end"] < chunk["rrn_start"]:
            raise FleetError(
                "invalid_history_progress", "Bornes de tranche invalides"
            )
        if chunk["rrn_end"] > data["max_rrn"]:
            raise FleetError(
                "invalid_history_progress", "Tranche hors borne observée"
            )
        if chunk["status"] not in _HISTORY_PROGRESS_STATUSES:
            raise FleetError("invalid_history_progress", "Statut de tranche inconnu")
        key = (chunk["worker"], chunk["rrn_start"], chunk["rrn_end"])
        if key in seen_chunks:
            raise FleetError("invalid_history_progress", "Tranche de progression dupliquée")
        seen_chunks.add(key)
        counts[chunk["status"]] += 1
        if chunk["status"] == "published":
            published_rows += chunk["rows"]
            published_bytes += chunk["bytes"]
    if totals["published_rows"] != published_rows:
        raise FleetError("invalid_history_progress", "Totaux de progression incohérents")
    if totals["published_bytes"] != published_bytes:
        raise FleetError("invalid_history_progress", "Octets de progression incohérents")
    if published_rows > totals["planned_rows"]:
        raise FleetError("invalid_history_progress", "Progression historique incohérente")
    if counts["failed"]:
        status = "failed"
    elif counts["published"] == len(chunks) and chunks:
        status = "complete"
    else:
        status = "running"
    return {
        "status": status,
        "run_id": run_id,
        "table": table,
        "updated_at": data["updated_at"],
        "planned_rows": totals["planned_rows"],
        "published_rows": totals["published_rows"],
        "published_bytes": totals["published_bytes"],
        "published_objects": totals["published_objects"],
        "chunks_pending": counts["pending"],
        "chunks_running": counts["running"],
        "chunks_read_done": counts["read_done"],
        "chunks_published": counts["published"],
        "chunks_failed": counts["failed"],
    }


def load_history_progress_documents(
    client: object,
    *,
    bucket: str | None = None,
    prefix: str | None = None,
    max_keys: int = HISTORY_PROGRESS_MAX_KEYS,
    max_doc_bytes: int = HISTORY_PROGRESS_MAX_BYTES,
) -> dict[str, dict[str, object]]:
    """Relit les documents ``history-progress`` : le plus récent par table.

    Sans ``bucket``/``prefix`` explicites, le périmètre déclaré du site est
    utilisé. Le listage et chaque lecture sont bornés (``max_keys``,
    ``max_doc_bytes``). Un objet illisible ou invalide rend l'entrée
    ``{"status": "invalid"}`` — l'appelant décide de la projection, jamais
    d'un zéro inventé. Le résultat est indexé par nom de table du manifeste.
    """

    if max_keys < 1 or max_doc_bytes < 1:
        raise FleetError("invalid_history_progress", "Budget de lecture invalide")
    site = _site()
    bucket = site.raw_bucket if bucket is None else bucket
    prefix = site.history_progress_prefix if prefix is None else prefix
    keys: list[str] = []
    listed = 0
    continuation: dict[str, object] = {}
    while True:
        page = client.list_objects_v2(  # type: ignore[attr-defined]
            Bucket=bucket, Prefix=prefix, MaxKeys=1000, **continuation
        )
        for item in page.get("Contents") or ():
            listed += 1
            if listed > max_keys:
                raise FleetError(
                    "invalid_history_progress", "Inventaire de progression borné dépassé"
                )
            key = item.get("Key")
            if type(key) is str and key.endswith(".json"):
                keys.append(key)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
        continuation = {"ContinuationToken": token}

    # Le nom de clé est un run_id, pas une table : la table se lit dans le
    # document, et « le plus récent » se tranche sur son updated_at validé —
    # jamais sur l'ordre lexical des clés.
    documents: dict[str, dict[str, object]] = {}
    invalid_tables: set[str] = set()
    for key in sorted(keys):
        raw = b""
        try:
            response = client.get_object(Bucket=bucket, Key=key)  # type: ignore[attr-defined]
            body = response["Body"]
            try:
                size = response.get("ContentLength")
                if type(size) is int and size > max_doc_bytes:
                    raise FleetError(
                        "invalid_history_progress", "Document de progression borné dépassé"
                    )
                raw = body.read(max_doc_bytes + 1)
            finally:
                body.close()
            if len(raw) > max_doc_bytes:
                raise FleetError(
                    "invalid_history_progress", "Document de progression borné dépassé"
                )
            decoded = json.loads(raw.decode("utf-8"))
            parsed = parse_history_progress(decoded)
        except Exception:
            # Un document invalide reste attribuable à sa table si le champ
            # `table` seul se lit : « invalide » n'est jamais rendu « absent ».
            try:
                candidate = json.loads(raw.decode("utf-8"))
            except Exception:
                candidate = None
            if isinstance(candidate, dict) and candidate.get("table") in _fleet.MANIFEST:
                invalid_tables.add(candidate["table"])
            continue
        table = parsed["table"]
        invalid_tables.discard(table)
        current = documents.get(table)
        if current is None or _parse_utc(str(parsed["updated_at"])) >= _parse_utc(
            str(current["updated_at"])
        ):
            documents[table] = parsed
    for table in invalid_tables:
        if table not in documents:
            documents[table] = {"status": "invalid"}
    return documents


def _history_admitted(plan: FleetPlan) -> bool:
    return any(table.historical_lane is not None for table in plan.tables)


def _unobserved_observability(freshness: str) -> ObservabilityProjection:
    return ObservabilityProjection(
        status=OBSERVABILITY_STATUS_UNOBSERVED,
        quality={
            "coverage": "partial",
            "freshness": freshness,
            "evidence_kind": EVIDENCE_KIND,
        },
        observed_at=None,
        reason=OBSERVABILITY_UNOBSERVED_REASON,
        checks=(),
        alerts=(),
    )


def _overview_summary(*, history_admitted: bool) -> str:
    if history_admitted:
        return f"Preuve catalogue {_site().fleet_environment} : copie historique admissible, live et certification bloqués"
    return f"Preuve catalogue {_site().fleet_environment} : copie historique non admise, live et certification bloqués"


def _freshness(age) -> str:
    if age < -CLOCK_SKEW_TOLERANCE:
        return "clock_untrusted"
    if age <= FRESH_FOR:
        return "fresh"
    return "stale"


def _parse_overview(payload: object) -> dict[str, object]:
    data = _closed_mapping(payload, _OVERVIEW_KEYS, code="invalid_sidecar")
    revision = data["revision"]
    if type(revision) is not int or revision < 0:
        raise FleetError("invalid_sidecar", "Révision de sidecar invalide")
    generated_at = _require_utc(data["generated_at"], "Horodatage de sidecar invalide")
    scope = data["scope"]
    if type(scope) is not dict or scope.get("kind") != "single":
        raise FleetError("invalid_sidecar", "Portée de sidecar invalide")
    environments = scope.get("environments")
    if type(environments) is not list or environments != [_site().environment]:
        raise FleetError("invalid_sidecar", "Portée de sidecar invalide")
    pipelines = data["pipelines"]
    sources = data["sources"]
    if type(pipelines) is not list or len(pipelines) != 1:
        raise FleetError("invalid_sidecar", "Pipeline de sidecar invalide")
    if type(sources) is not list or len(sources) != 1:
        raise FleetError("invalid_sidecar", "Source de sidecar invalide")
    pipeline = _parse_overview_pipeline(pipelines[0])
    source = _parse_overview_source(sources[0])
    if pipeline["id"] != _site().sidecar_pipeline_id or source["id"] != _site().sidecar_pipeline_id:
        raise FleetError("invalid_sidecar", "Identité de pipeline invalide")
    if pipeline["status"] == "healthy":
        raise FleetError("invalid_sidecar", "Promesse live non autorisée")
    if pipeline["quality"]["evidence_kind"] == "live":
        raise FleetError("invalid_sidecar", "Preuve live non observée")
    if pipeline["lag_sequences"] is not None or pipeline["lag_seconds"] is not None:
        raise FleetError("invalid_sidecar", "Retard non observé")
    return {
        "revision": revision,
        "generated_at": generated_at,
        "scope": {"kind": "single", "environments": [_site().environment]},
        "pipelines": [pipeline],
        "sources": [source],
    }


def _parse_overview_pipeline(payload: object) -> dict[str, object]:
    if type(payload) is not dict:
        raise FleetError("invalid_sidecar", "Pipeline de sidecar invalide")
    required = {
        "id",
        "environment",
        "status",
        "quality",
        "summary",
        "observed_at",
        "stages",
        "lag_sequences",
        "lag_seconds",
        "lag_series",
        "counters",
        "incident",
        "observability",
    }
    keys = set(payload)
    if not required <= keys <= required | set(PASS_THROUGH_FIELDS):
        raise FleetError("invalid_sidecar", "Schéma JSON non autorisé")
    if payload["environment"] != _site().environment:
        raise FleetError("invalid_sidecar", "Environnement hors site déclaré")
    quality = payload["quality"]
    if type(quality) is not dict or set(quality) != {"coverage", "freshness", "evidence_kind"}:
        raise FleetError("invalid_sidecar", "Qualité d'observation invalide")
    stages = payload["stages"]
    if type(stages) is not list or [stage.get("id") for stage in stages] != [
        "source",
        "capture",
        "raw",
        "load",
        "destination",
    ]:
        raise FleetError("invalid_sidecar", "Étapes de sidecar invalides")
    counters = payload["counters"]
    if type(counters) is not dict or set(counters) != set(PUBLIC_COUNTERS):
        raise FleetError("invalid_sidecar", "Compteurs de sidecar invalides")
    for value in counters.values():
        if value is not None:
            raise FleetError("invalid_sidecar", "Compteur non observé")
    if payload["lag_series"] is not None or payload["incident"] is not None:
        raise FleetError("invalid_sidecar", "Mesure runtime non observée")
    _parse_unobserved_observability(payload["observability"], pipeline_quality=quality)
    return payload


def _parse_unobserved_observability(payload: object, *, pipeline_quality: Mapping[str, str]) -> dict[str, object]:
    data = _closed_mapping(payload, _OBSERVABILITY_KEYS, code="invalid_sidecar")
    if data["status"] != OBSERVABILITY_STATUS_UNOBSERVED:
        raise FleetError("invalid_sidecar", "Observabilité non observée")
    if data["status"] in {"pass", "breach"}:
        raise FleetError("invalid_sidecar", "Succès d'observabilité inventé")
    if data["observed_at"] is not None:
        raise FleetError("invalid_sidecar", "Horodatage d'observabilité inventé")
    if data["reason"] != OBSERVABILITY_UNOBSERVED_REASON:
        raise FleetError("invalid_sidecar", "Raison d'observabilité invalide")
    quality = data["quality"]
    if type(quality) is not dict or set(quality) != {"coverage", "freshness", "evidence_kind"}:
        raise FleetError("invalid_sidecar", "Qualité d'observabilité invalide")
    if quality.get("evidence_kind") != EVIDENCE_KIND or quality.get("coverage") != "partial":
        raise FleetError("invalid_sidecar", "Preuve live d'observabilité non observée")
    if quality.get("freshness") != pipeline_quality.get("freshness"):
        raise FleetError("invalid_sidecar", "Fraîcheur d'observabilité incohérente")
    checks = data["checks"]
    alerts = data["alerts"]
    if type(checks) is not list or type(alerts) is not list:
        raise FleetError("invalid_sidecar", "Preuve d'observabilité invalide")
    if checks or alerts:
        raise FleetError("invalid_sidecar", "Mesure d'observabilité inventée")
    return data


def _parse_overview_source(payload: object) -> dict[str, object]:
    if type(payload) is not dict:
        raise FleetError("invalid_sidecar", "Source de sidecar invalide")
    if set(payload) != {"id", "evidence_kind", "environment", "status", "error"}:
        raise FleetError("invalid_sidecar", "Schéma JSON non autorisé")
    if payload["environment"] != _site().environment:
        raise FleetError("invalid_sidecar", "Environnement hors site déclaré")
    if payload["evidence_kind"] != EVIDENCE_KIND:
        raise FleetError("invalid_sidecar", "Nature de preuve invalide")
    if payload["status"] != "available" or payload["error"] is not None:
        raise FleetError("invalid_sidecar", "Disponibilité source invalide")
    return payload


def _parse_fleet(payload: object) -> dict[str, object]:
    data = _closed_mapping(payload, _FLEET_KEYS, code="invalid_sidecar")
    if data["environment"] != _fleet.ENVIRONMENT:
        raise FleetError("invalid_environment", "Environnement hors site déclaré")
    if data["destination_namespace"] != _fleet.DESTINATION_NAMESPACE:
        raise FleetError("invalid_destination", "Espace de destination hors site déclaré")
    _closed_mapping(data["provenance"], _PROVENANCE_KEYS, code="invalid_sidecar")
    identity = _closed_mapping(data["identity"], _IDENTITY_KEYS, code="invalid_sidecar")
    totals = _closed_mapping(data["observed_totals"], _TOTAL_KEYS, code="invalid_sidecar")
    historical = _closed_mapping(data["historical"], _HISTORICAL_KEYS, code="invalid_sidecar")
    cost = _closed_mapping(data["cost"], _COST_KEYS, code="invalid_sidecar")
    tables = data["tables"]
    if type(tables) is not list or len(tables) != _fleet.TABLE_COUNT:
        raise FleetError("invalid_manifest", "Manifeste du site incomplet")
    names = []
    for item in tables:
        table = _closed_mapping(item, _TABLE_KEYS, code="invalid_sidecar")
        names.append(table["name"])
        copied_rows = table["copied_rows"]
        if copied_rows is not None and (type(copied_rows) is not int or copied_rows < 0):
            raise FleetError("invalid_sidecar", "Copie historique invalide")
        copied_bytes = table["copied_bytes"]
        if copied_bytes is not None and (
            type(copied_bytes) is not int or copied_bytes < 0
        ):
            raise FleetError("invalid_sidecar", "Octets copiés invalides")
        _validate_sidecar_progress(table["history_progress"], copied_rows, copied_bytes)
        if table["name"] not in _fleet.MANIFEST:
            raise FleetError("unknown_table", "Table hors manifeste")
    if tuple(names) != _fleet.MANIFEST:
        raise FleetError("invalid_manifest", "Manifeste du site hors ordre")
    if totals["table_count"] != _fleet.TABLE_COUNT:
        raise FleetError("invalid_manifest", "Manifeste du site incomplet")
    if cost["status"] != COST_STATUS_UNKNOWN or cost["observed"] is not None:
        raise FleetError("invalid_sidecar", "Coût réel non observé")
    keyed = identity["keyed"]
    rrn = identity["rrn"]
    blocked = identity["blocked"]
    if type(keyed) is not list or type(rrn) is not list or type(blocked) is not list:
        raise FleetError("invalid_identity", "Preuve d'identité invalide")
    if (
        identity["keyed_count"] != len(keyed)
        or identity["rrn_count"] != len(rrn)
        or identity["blocked_count"] != len(blocked)
    ):
        raise FleetError("invalid_identity", "Preuve d'identité incohérente")
    if identity["keyed_count"] + identity["rrn_count"] + identity["blocked_count"] != _fleet.TABLE_COUNT:
        raise FleetError("invalid_identity", "Preuve d'identité incomplète")
    if data["live_promise"] not in {PROMISE_BLOCKED, PROMISE_POSSIBLE}:
        raise FleetError("invalid_sidecar", "Promesse live invalide")
    if data["certification_promise"] not in {PROMISE_BLOCKED, PROMISE_POSSIBLE}:
        raise FleetError("invalid_sidecar", "Promesse certifiée invalide")
    identity_blocks = identity["blocked_count"] > 0
    if identity_blocks and data["live_promise"] != PROMISE_BLOCKED:
        raise FleetError("invalid_sidecar", "Promesse live non autorisée")
    if identity_blocks and data["certification_promise"] != PROMISE_BLOCKED:
        raise FleetError("invalid_sidecar", "Promesse certifiée non autorisée")
    if data["continuity"] != CONTINUITY_PROVEN and data["live_promise"] != PROMISE_BLOCKED:
        raise FleetError("unproven_continuity", "Passage live bloqué par la continuité")
    if data["continuity"] != CONTINUITY_PROVEN and data["certification_promise"] != PROMISE_BLOCKED:
        raise FleetError("unproven_continuity", "Certification bloquée par la continuité")
    if type(data["cutover_required_before_history"]) is not bool or data["cutover_required_before_history"] is not True:
        raise FleetError("missing_start_checkpoint", "Cutover obligatoire avant toute histoire")
    if type(historical["lanes"]) is not list:
        raise FleetError("invalid_lane", "Voies historiques invalides")
    return data


def _validate_sidecar_progress(
    value: object, copied_rows: object, copied_bytes: object
) -> None:
    """Rejoue la règle de projection : jamais de progression sans preuve."""

    if value is None:
        if copied_rows is not None or copied_bytes is not None:
            raise FleetError("invalid_sidecar", "Copie sans progression observée")
        return
    progress = _closed_mapping(value, _PROGRESS_KEYS, code="invalid_sidecar")
    status = progress["status"]
    if status == "invalid":
        if (
            copied_rows is not None
            or copied_bytes is not None
            or any(
                progress[name] is not None for name in _PROGRESS_KEYS if name != "status"
            )
        ):
            raise FleetError("invalid_sidecar", "Progression invalide non déclarée")
        return
    if status not in _PROGRESS_OBSERVED:
        raise FleetError("invalid_sidecar", "Statut de progression inconnu")
    if type(progress["run_id"]) is not str or not progress["run_id"]:
        raise FleetError("invalid_sidecar", "Run de progression invalide")
    _require_utc(progress["updated_at"], "Horodatage de progression invalide")
    numeric = (
        "planned_rows",
        "published_rows",
        "published_bytes",
        "published_objects",
        "chunks_pending",
        "chunks_running",
        "chunks_read_done",
        "chunks_published",
        "chunks_failed",
    )
    for name in numeric:
        item = progress[name]
        if type(item) is not int or item < 0:
            raise FleetError("invalid_sidecar", "Compte de progression invalide")
    if progress["published_rows"] != copied_rows:
        raise FleetError("invalid_sidecar", "Copie et progression incohérentes")
    if progress["published_bytes"] != copied_bytes:
        raise FleetError("invalid_sidecar", "Octets copiés et progression incohérents")


def _read_catalog_bytes(path: str | Path) -> bytes:
    try:
        target = Path(path)
        size = target.stat().st_size
        if size > MAX_CATALOG_BYTES:
            raise FleetError("invalid_catalog", "Catalogue trop volumineux")
        return target.read_bytes()
    except FleetError:
        raise
    except OSError:
        raise FleetError("invalid_catalog", "Catalogue illisible") from None


def _write_atomic_json(path: str | Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    if target.exists() and target.is_dir():
        raise FleetError("invalid_sidecar", "Écriture du sidecar impossible")
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise FleetError("invalid_sidecar", "Écriture du sidecar impossible") from None
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(directory),
        )
    except OSError:
        raise FleetError("invalid_sidecar", "Écriture du sidecar impossible") from None
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, FILE_MODE)
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        os.chmod(target, FILE_MODE)
    except OSError:
        raise FleetError("invalid_sidecar", "Écriture du sidecar impossible") from None
    finally:
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _parse_utc(value: object) -> datetime:
    if type(value) is not str:
        raise FleetError("invalid_catalog", "Horodatage de catalogue invalide")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FleetError("invalid_catalog", "Horodatage de catalogue invalide") from None
    if parsed.tzinfo is None:
        raise FleetError("invalid_catalog", "Horodatage de catalogue invalide")
    return parsed.astimezone(timezone.utc)


def _require_utc(value: object, message: str) -> str:
    if type(value) is not str:
        raise FleetError("invalid_sidecar", message)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FleetError("invalid_sidecar", message) from None
    if parsed.tzinfo is None:
        raise FleetError("invalid_sidecar", message)
    return value


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _closed_mapping(value: object, keys: tuple[str, ...], *, code: str) -> dict[str, object]:
    if type(value) is not dict:
        raise FleetError(code, "Objet JSON non autorisé")
    if set(value) != set(keys):
        raise FleetError(code, "Schéma JSON non autorisé")
    for key in value:
        if type(key) is not str:
            raise FleetError(code, "Clé JSON non autorisée")
    return value


def _assert_sidecar_safe(value: object) -> None:
    if type(value) in _JSON_SCALARS:
        if type(value) is float and not math.isfinite(value):
            raise FleetError("invalid_serialization", "Nombre JSON non fini")
        if type(value) is str:
            lowered = value.lower()
            if any(token in lowered for token in ("password", "secret", "credential")):
                raise FleetError("invalid_serialization", "Champ sensible non autorisé")
        return
    if type(value) is list:
        for item in value:
            _assert_sidecar_safe(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise FleetError("invalid_serialization", "Clé JSON non sûre")
            lowered = key.lower()
            if any(token in lowered for token in _SENSITIVE_TOKENS):
                raise FleetError("invalid_serialization", "Champ sensible non autorisé")
            _assert_sidecar_safe(item)
        return
    raise FleetError("invalid_serialization", "Type JSON non autorisé")
