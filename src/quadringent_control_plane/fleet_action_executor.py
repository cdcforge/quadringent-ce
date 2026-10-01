"""Adaptateur UI borné vers les runtimes prepare/start DEV.

Aucune mutation hors PrepareRuntime/HistoryRuntime. La projection et
supports relisent les stores sans écriture. Les identifiants techniques
et secrets ne sortent jamais du contrat public.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from quadringent.site_config import SiteConfig, current as _current_site
from .fleet_job_launcher import job_is_terminal
from quadringent_control_plane import fleet as _fleet
from quadringent_control_plane.fleet import FleetError, JournalCheckpoint
from quadringent_control_plane.fleet_history_runtime import (
    HISTORY_FORMAT_VERSION,
    HistoryRuntime,
    PHASE_HISTORICAL,
    PHASE_HISTORY_FAILED,
    PHASE_STARTING,
)
from quadringent_control_plane.fleet_history_runtime import (
    _STATE_KEYS as _HISTORY_STATE_KEYS,
)
from quadringent_control_plane.fleet_history_runtime import (
    _assert_receipt_matches as _assert_history_receipt_matches,
)
from quadringent_control_plane.fleet_history_runtime import (
    _closed_mapping as _closed_history_mapping,
)
from quadringent_control_plane.fleet_history_runtime import (
    _parse_checkpoint as _parse_history_checkpoint,
)
from quadringent_control_plane.fleet_history_runtime import (
    _parse_concurrency as _parse_history_concurrency,
)
from quadringent_control_plane.fleet_history_runtime import (
    _parse_lanes as _parse_history_lanes,
)
from quadringent_control_plane.fleet_history_runtime import (
    _parse_manifest as _parse_history_manifest,
)
from quadringent_control_plane.fleet_history_runtime import (
    _parse_receipt as _parse_history_receipt,
)
from quadringent_control_plane.fleet_history_runtime import (
    _require_safe_reader_id as _require_history_reader_id,
)
from quadringent_control_plane.fleet_history_runtime import (
    _require_token as _require_history_token,
)
from quadringent_control_plane.fleet_history_runtime import CODE_INVALID_RUNTIME_STATE as HISTORY_INVALID_STATE
from quadringent_control_plane.fleet_plan import (
    CONTINUITY_PROVEN,
    FleetPlan,
    lane_composition,
)
from quadringent_control_plane.fleet_progression import (
    parse_run_state,
    public_phase,
    public_table_states,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    PHASE_PREPARED,
    PHASE_PREPARE_FAILED,
    PHASE_PREPARING,
    PREPARE_FORMAT_VERSION,
    PrepareRuntime,
    ReaderLaunchRequest,
    ReaderReceipt,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _STATE_KEYS as _PREPARE_STATE_KEYS,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _assert_receipt_matches as _assert_prepare_receipt_matches,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _closed_mapping as _closed_prepare_mapping,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _parse_checkpoint as _parse_prepare_checkpoint,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _parse_manifest as _parse_prepare_manifest,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _parse_receipt as _parse_prepare_receipt,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    _require_token as _require_prepare_token,
)
from quadringent_control_plane.fleet_prepare_runtime import CODE_INVALID_RUNTIME_STATE as PREPARE_INVALID_STATE


RUNTIME_FORMAT_VERSION = "quadringent-fleet-runtime-v1"
# Identités du site déclaré — résolues à l'accès via ``__getattr__``.
EXECUTOR_FLEET_ID: str
EXECUTOR_ENVIRONMENT: str
EXECUTOR_PIPELINE_ID: str


def _site() -> SiteConfig:
    return _current_site()


def __getattr__(name: str) -> object:
    site_attributes = {
        "EXECUTOR_FLEET_ID": lambda site: site.fleet_id,
        "EXECUTOR_ENVIRONMENT": lambda site: site.environment,
        "EXECUTOR_PIPELINE_ID": lambda site: site.site_id,
    }
    resolver = site_attributes.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver(_site())
PUBLIC_PHASES = frozenset(
    {
        "NOT_PREPARED",
        "PREPARED",
        "HISTORICAL",
        "CATCHING_UP",
        "LIVE",
        "RECONCILING",
        "CERTIFIED",
        "PAUSED",
        "BLOCKED",
        "UNKNOWN",
    }
)
# Une voie expose toute phase domaine — READY n'a pas d'équivalent agrégé
# (le plancher job la couvre), le reste partage le vocabulaire public.
PUBLIC_TABLE_PHASES = PUBLIC_PHASES | {"READY"}
PHASE_PAUSED = "PAUSED"
PHASE_RESUMED = "RESUMED"
PHASE_NOT_PREPARED = "NOT_PREPARED"
PHASE_BLOCKED = "BLOCKED"
PHASE_UNKNOWN = "UNKNOWN"
SUPPORTED_ACTIONS = frozenset({"prepare", "start", "pause", "resume", "refresh"})

# États console d'une capture volontairement garée — la même loi que la
# réconciliation de projection : la reprise n'est offerte que quand la
# cause est authentification/arrêt sûr et que la sonde catalogue est fraîche.
_PARKED_RUN_STATES = frozenset({"STOPPED_FAIL_CLOSED", "STOPPED_AUTH_BLOCKED"})
_PROBE_MAX_AGE_SECONDS = 900.0
CODE_NOT_PAUSED = "not_paused"

# La phase HISTORICAL (et au-delà) mesure l'avancement des tables, persisté
# dans fleet-run.json — jamais l'état du lecteur lui-même. Sans cette
# distinction, un lecteur arrêté fail-closed restait annoncé
# "already_started" : la raison publiée doit refléter l'arrêt réel du
# lecteur, une par état garé, jamais un générique "reader_stopped" qui
# perdrait la cause.
_START_STOP_REASONS = {
    "STOPPED_FAIL_CLOSED": "reader_stopped_fail_closed",
    "STOPPED_AUTH_BLOCKED": "reader_stopped_auth_blocked",
}
# Distincte de "unsupported_action" (aucun lecteur console monté sur ce
# déploiement) : ici le lecteur est bien monté, mais les preuves de reprise
# (continuité, fraîcheur de sonde, Job relisible) ne sont pas réunies —
# "impossible dans cet état", pas "non supporté par ce déploiement".
CODE_RESUME_NOT_READY = "resume_not_ready"
# Le déploiement sait suspendre, mais la phase courante ne s'y prête pas —
# CERTIFIED, par exemple, n'est pas dans _PAUSABLE_PHASES. Publier
# "unsupported_action" dans ce cas affirmait que ce déploiement ne savait pas
# suspendre, ce qui est faux : l'interface en déduisait qu'il fallait passer par
# l'exploitation alors que rien ne manquait au produit.
CODE_NOT_PAUSABLE_IN_PHASE = "not_pausable_in_phase"

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_MESSAGE = re.compile(r"^[\w][\w .,'’-]{0,119}$", re.UNICODE)
_SENSITIVE_TOKENS = ("host", "user", "password", "secret", "token", "credential")
_CORRUPT = object()

_MSG_INTENT = "Intention enregistree"
_MSG_EXECUTION_DONE = "Execution terminee"
_MSG_EFFECT_DONE = "Effet observe"
_MSG_EXECUTION_FAILED = "Execution echouee"
_MSG_EFFECT_FAILED = "Effet non observe"
_MSG_UNSUPPORTED = "Action non supportee"
_MSG_INVALID_SCOPE = "Perimetre invalide"
_MSG_PREPARE_FAILED = "Echec de preparation"
_MSG_HISTORY_FAILED = "Echec du demarrage historique"
_MSG_PAUSE_FAILED = "Echec de la suspension"
_MSG_RESUME_FAILED = "Echec de la reprise"
_MSG_READER_RELAUNCHED = "Lecteur relance au point d'arret mesure"
_MSG_REFRESH_FAILED = "Echec du releve de catalogue"

PLAN_APPLIED = "applied"
PLAN_ACTION_IN_FLIGHT = "action_in_flight"
PLAN_KEPT_INCOMPATIBLE = "kept_incompatible"
PLAN_INVALID = "invalid_plan"
# Phases qui valident l'état persisté contre le plan actif : un échange
# incompatible y ferait basculer la projection vers UNKNOWN. BLOCKED garde
# la même garde — un état en reprise reste lié au plan qui l'a produit.
_PLAN_GUARDED_PHASES = frozenset(
    {
        PHASE_PREPARED,
        PHASE_HISTORICAL,
        "CATCHING_UP",
        "LIVE",
        "RECONCILING",
        "CERTIFIED",
        PHASE_PAUSED,
        PHASE_BLOCKED,
    }
)
# La suspension suspende le lecteur et le job historique : éligible tant
# que la capture tourne, quelle que soit la profondeur domaine atteinte.
_PAUSABLE_PHASES = frozenset(
    {
        PHASE_PREPARED,
        PHASE_HISTORICAL,
        "CATCHING_UP",
        "LIVE",
        "RECONCILING",
    }
)


class FleetActionExecutor:
    def __init__(
        self,
        plan: FleetPlan,
        prepare_store: object,
        history_store: object,
        checkpoint_provider: object,
        reader_launcher: object,
        history_launcher: object,
        pause_runtime: object | None = None,
        run_store: object | None = None,
        console_reader: object | None = None,
        jobs: object | None = None,
    ) -> None:
        self._plan = plan
        self._prepare_store = prepare_store
        self._history_store = history_store
        self._checkpoint_provider = checkpoint_provider
        self._reader_launcher = reader_launcher
        self._history_launcher = history_launcher
        self._pause_runtime = pause_runtime
        # Document console relu à la demande : c'est lui qui dit que la
        # capture est parquée et où elle s'est arrêtée — jamais d'I/O ici,
        # la lecture est déléguée à l'appelant.
        self._console_reader = console_reader
        self._jobs = jobs
        # État domaine durable écrit par le pilote de progression — quand
        # il est présent et de la génération courante, il prime sur la
        # phase job pour la projection publique.
        self._run_store = run_store
        # Compteur consulté par update_plan : la sérialisation reste déléguée
        # à PipelineActionGate et aux runtimes ; aucun verrou n'est ajouté ici.
        self._active_actions = 0
        self._catalog_refresher: object | None = None

    @property
    def plan(self) -> FleetPlan:
        """Plan actif — la référence change à chaque relevé accepté."""

        return self._plan

    def attach_catalog_refresher(self, refresher: object) -> None:
        """Raccorde le relevé borné du catalogue ; active l'action refresh."""

        if refresher is None or not callable(getattr(refresher, "refresh", None)):
            raise ValueError("catalog refresher must expose refresh()")
        self._catalog_refresher = refresher

    def update_plan(self, plan: object) -> str:
        """Échange le plan actif hors action en vol ; borné par la phase persistée.

        Un plan qui ferait basculer la phase persistée vers UNKNOWN (groupe
        journal, voies historiques ou concurrence différents) est conservé de
        côté plutôt qu'appliqué — la mesure reste durable via le sidecar.
        """

        if self._active_actions:
            return PLAN_ACTION_IN_FLIGHT
        return self._swap_plan(plan)

    def _swap_plan(self, plan: object) -> str:
        if type(plan) is not FleetPlan:
            return PLAN_INVALID
        try:
            phase, _checkpoint, _tables = self._observe()
        except Exception:
            phase = PHASE_UNKNOWN
        if phase in _PLAN_GUARDED_PHASES and not _same_launch_plan(self._plan, plan):
            return PLAN_KEPT_INCOMPATIBLE
        self._plan = plan
        return PLAN_APPLIED

    def supports(self, invocation: object) -> bool:
        try:
            if not self._in_scope(invocation):
                return False
            action = getattr(invocation, "action", None)
            if action == "refresh":
                return self._catalog_refresher is not None
            phase, _checkpoint, _tables = self._observe()
            if action == "prepare":
                return phase == PHASE_NOT_PREPARED
            if action == "start":
                return phase == PHASE_PREPARED
            if action == "pause":
                return (
                    self._pause_runtime is not None
                    and phase in _PAUSABLE_PHASES
                )
            if action == "resume":
                return (
                    self._pause_runtime is not None and phase == PHASE_PAUSED
                ) or self._parked_resume() is not None
            return False
        except Exception:
            return False

    def execute(self, invocation: object) -> dict[str, dict[str, str]]:
        self._active_actions += 1
        try:
            return self._execute(invocation)
        except FleetError as error:
            return _failed_stages(_safe_code(getattr(error, "code", None)), _safe_message(getattr(error, "safe_message", None)))
        except Exception:
            return _failed_stages("execution_failed", _MSG_EXECUTION_FAILED)
        finally:
            self._active_actions -= 1

    def project(self) -> dict[str, object]:
        try:
            phase, checkpoint, table_states = self._observe()
        except Exception:
            phase, checkpoint, table_states = PHASE_UNKNOWN, None, None
        projection = _public_projection(
            phase,
            checkpoint,
            self._pause_runtime is not None,
            self._catalog_refresher is not None,
            table_states,
            _observed_run_state(self._console_reader),
        )
        # Une capture parquée dont la cause est résolue n'est pas PAUSED :
        # la reprise est offerte sur la preuve fraîche, pas sur la phase job.
        if self._parked_resume() is not None:
            capabilities = dict(projection["capabilities"])
            capabilities["resume"] = {"state": "available", "reason": None}
            projection = {**projection, "capabilities": capabilities}
        return projection

    def _parked_resume(self) -> dict[str, object] | None:
        """Contexte de reprise d'une capture parquée, ou None.

        Mêmes gardes que la réconciliation de projection : run console parqué,
        continuité prouvée, sonde catalogue fraîche. Aucune connexion source
        n'est tentée — les deux preuves sont déjà relevées par ailleurs.
        """

        reader = self._console_reader
        if not callable(reader):
            return None
        try:
            console = reader()
        except Exception:
            return None
        if type(console) is not dict:
            return None
        run = console.get("run")
        if type(run) is not dict or run.get("state") not in _PARKED_RUN_STATES:
            return None
        position = console.get("position")
        checkpoint_raw = position.get("checkpoint") if type(position) is dict else None
        try:
            checkpoint = _parse_prepare_checkpoint(checkpoint_raw)
        except Exception:
            return None
        plan = self._plan
        if type(plan) is not FleetPlan or plan.continuity != CONTINUITY_PROVEN:
            return None
        try:
            observed = datetime.fromisoformat(plan.observed_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            return None
        if observed.tzinfo is None or not timedelta(0) <= (
            datetime.now(timezone.utc) - observed
        ) <= timedelta(seconds=_PROBE_MAX_AGE_SECONDS):
            return None
        prepare = _load_store(self._prepare_store)
        if type(prepare) is not dict or prepare.get("phase") != PHASE_PREPARED:
            return None
        try:
            intent_id = _require_prepare_token(prepare.get("intent_id"), "invalid_intent")
            journal_library = _require_prepare_token(
                prepare.get("journal_library"), "invalid_journal"
            )
            journal_name = _require_prepare_token(
                prepare.get("journal_name"), "invalid_journal"
            )
            manifest = _parse_prepare_manifest(prepare.get("manifest"))
            reader_kind = _require_prepare_token(
                prepare.get("reader_kind"), "invalid_reader"
            )
            reader_count = prepare.get("reader_count")
        except Exception:
            return None
        if type(reader_count) is not int or type(reader_count) is bool or reader_count < 1:
            return None
        # L'absence est une preuve API, jamais la conséquence d'une exception.
        # Un Job existant, même Complete, conserve le nom jusqu'au nettoyage TTL.
        receipt = prepare.get("receipt")
        reader_id = receipt.get("reader_id") if type(receipt) is dict else None
        if not isinstance(reader_id, str) or not reader_id or self._jobs is None:
            return None
        try:
            if self._jobs.read_job(reader_id) is not None:
                return None
        except Exception:
            return None
        return {
            "intent_id": intent_id,
            "journal_library": journal_library,
            "journal_name": journal_name,
            "manifest": manifest,
            "reader_kind": reader_kind,
            "reader_count": reader_count,
            "checkpoint": checkpoint,
        }

    def _execute(self, invocation: object) -> dict[str, dict[str, str]]:
        if not self._in_scope(invocation):
            return _failed_stages("invalid_scope", _MSG_INVALID_SCOPE)
        action = getattr(invocation, "action", None)
        if action == "prepare":
            return self._execute_prepare()
        if action == "start":
            return self._execute_start()
        if action == "pause":
            return self._execute_pause()
        if action == "resume":
            return self._execute_resume()
        if action == "refresh":
            return self._execute_refresh()
        return _failed_stages("unsupported_action", _MSG_UNSUPPORTED)

    def _execute_refresh(self) -> dict[str, dict[str, str]]:
        """Un cycle de relevé synchrone ; le reçu porte la mesure du catalogue."""

        refresher = self._catalog_refresher
        if refresher is None:
            return _failed_stages("unsupported_action", _MSG_UNSUPPORTED)
        try:
            outcome = refresher.refresh()
        except Exception:
            return _failed_stages("catalog_refresh_failed", _MSG_REFRESH_FAILED)
        plan = getattr(outcome, "plan", None)
        if type(plan) is not FleetPlan:
            return _failed_stages(_refresh_failure_code(outcome), _MSG_REFRESH_FAILED)
        # L'action en vol est celle-ci : la garde anti-vol vise le relevé de
        # fond ; seule la garde de phase s'applique ici.
        swap = self._swap_plan(plan)
        code = "catalog_refreshed" if swap == PLAN_APPLIED else "catalog_kept"
        return {
            "intent": _stage("recorded", "intent_recorded", _MSG_INTENT),
            "execution": _stage("completed", "execution_completed", _MSG_EXECUTION_DONE),
            "observed_effect": _stage("succeeded", code, _refresh_message(outcome, swap)),
        }

    def _execute_pause(self) -> dict[str, dict[str, str]]:
        if self._pause_runtime is None:
            return _failed_stages("unsupported_action", _MSG_UNSUPPORTED)
        outcome = self._pause_runtime.pause()
        error_code = getattr(outcome, "error_code", None)
        if error_code is not None or getattr(outcome, "phase", None) != PHASE_PAUSED:
            return _failed_stages(_pause_code(error_code), _MSG_PAUSE_FAILED)
        paused, state_error = self._pause_runtime.is_paused()
        if state_error is not None or not paused:
            return _failed_stages(_pause_code(state_error), _MSG_PAUSE_FAILED)
        return _success_stages()

    def _execute_resume(self) -> dict[str, dict[str, str]]:
        if self._pause_runtime is not None:
            paused, state_error = self._pause_runtime.is_paused()
            if state_error is None and paused:
                outcome = self._pause_runtime.resume()
                error_code = getattr(outcome, "error_code", None)
                if error_code is not None or getattr(outcome, "phase", None) != PHASE_RESUMED:
                    return _failed_stages(_pause_code(error_code), _MSG_RESUME_FAILED)
                paused_after, state_error = self._pause_runtime.is_paused()
                if state_error is not None or paused_after:
                    return _failed_stages(_pause_code(state_error), _MSG_RESUME_FAILED)
                return _success_stages()
        # Non pausée : la capture parquée fail-closed repart par un Job neuf.
        parked = self._parked_resume()
        if parked is not None:
            return self._resume_parked(parked)
        if self._pause_runtime is None:
            return _failed_stages("unsupported_action", _MSG_UNSUPPORTED)
        return _failed_stages(_pause_code(CODE_NOT_PAUSED), _MSG_RESUME_FAILED)

    def _resume_parked(self, context: dict[str, object]) -> dict[str, dict[str, str]]:
        """Relance le lecteur parqué sur un Job neuf au checkpoint durable.

        Le run_id dérive de l'intention prepare : même nom de Job, même
        préfixe de réservation, après absence vérifiée du Job précédent.
        ``backoffLimit: 0`` borne la politique : un seul sign-on
        tenté par action opérateur, zéro boucle de retry.
        """

        request = ReaderLaunchRequest(
            intent_id=context["intent_id"],
            reader_kind=context["reader_kind"],
            reader_count=context["reader_count"],
            journal_library=context["journal_library"],
            journal_name=context["journal_name"],
            manifest=context["manifest"],
            checkpoint=context["checkpoint"],
        )
        try:
            receipt = self._reader_launcher.launch(request)
        except FleetError as error:
            return _failed_stages(
                _safe_code(getattr(error, "code", None)),
                _safe_message(getattr(error, "safe_message", None)),
            )
        except Exception:
            return _failed_stages("resume_failed", _MSG_RESUME_FAILED)
        if type(receipt) is not ReaderReceipt:
            return _failed_stages("resume_failed", _MSG_RESUME_FAILED)
        reader_id = getattr(receipt, "reader_id", None)
        if type(reader_id) is not str or not reader_id:
            return _failed_stages("resume_failed", _MSG_RESUME_FAILED)
        # Un Job du même nom mais terminé ne redémarrerait jamais : la
        # relecture refuse de déclarer une reprise qui n'existe pas.
        job = self._read_reader_job(reader_id)
        if job is None:
            return _failed_stages("reader_not_created", _MSG_RESUME_FAILED)
        if job_is_terminal(job):
            return _failed_stages("reader_failed", _MSG_RESUME_FAILED)
        return {
            "intent": _stage("recorded", "intent_recorded", _MSG_INTENT),
            "execution": _stage(
                "completed", "execution_completed", _MSG_EXECUTION_DONE
            ),
            "observed_effect": _stage(
                "succeeded", "reader_relaunched", _MSG_READER_RELAUNCHED
            ),
        }

    def _read_reader_job(self, name: str) -> dict[str, object] | None:
        jobs = self._jobs
        if jobs is None:
            return None
        read_job = getattr(jobs, "read_job", None)
        if not callable(read_job):
            return None
        try:
            job = read_job(name)
        except Exception:
            return None
        return job if type(job) is dict else None

    def _execute_prepare(self) -> dict[str, dict[str, str]]:
        runtime = PrepareRuntime(
            self._plan,
            self._prepare_store,
            self._checkpoint_provider,
            self._reader_launcher,
        )
        outcome = runtime.prepare()
        persisted = _load_store(self._prepare_store)
        if (
            persisted is _CORRUPT
            or type(persisted) is not dict
            or persisted.get("phase") != PHASE_PREPARED
            or getattr(outcome, "phase", None) != PHASE_PREPARED
        ):
            return _failed_stages("prepare_failed", _MSG_PREPARE_FAILED)
        return _success_stages()

    def _execute_start(self) -> dict[str, dict[str, str]]:
        runtime = HistoryRuntime(
            self._plan,
            self._prepare_store,
            self._history_store,
            self._history_launcher,
        )
        outcome = runtime.start()
        persisted = _load_store(self._history_store)
        if (
            persisted is _CORRUPT
            or type(persisted) is not dict
            or persisted.get("phase") != PHASE_HISTORICAL
            or getattr(outcome, "phase", None) != PHASE_HISTORICAL
        ):
            return _failed_stages("history_failed", _MSG_HISTORY_FAILED)
        return _success_stages()

    def _in_scope(self, invocation: object) -> bool:
        pipeline_id = getattr(invocation, "pipeline_id", None)
        fleet_id = getattr(invocation, "fleet_id", None)
        environment = getattr(invocation, "environment", None)
        action = getattr(invocation, "action", None)
        if pipeline_id != _site().site_id:
            return False
        if fleet_id != _site().fleet_id:
            return False
        if type(environment) is not str or environment.casefold() != _site().environment:
            return False
        if action not in SUPPORTED_ACTIONS:
            return False
        return True

    def _observe(
        self,
    ) -> tuple[str, dict[str, object] | None, dict[str, str] | None]:
        plan = self._plan
        if type(plan) is not FleetPlan:
            return PHASE_UNKNOWN, None, None
        prepare_raw = _load_store(self._prepare_store)
        history_raw = _load_store(self._history_store)
        prepare_kind, prepare_checkpoint = _inspect_prepare(prepare_raw, plan)
        history_kind, history_checkpoint = _inspect_history(history_raw, plan)
        if prepare_kind == "unknown" or history_kind == "unknown":
            return PHASE_UNKNOWN, None, None
        if self._pause_runtime is not None and prepare_kind == "prepared":
            paused, state_error = self._pause_runtime.is_paused()
            if state_error is not None:
                return PHASE_UNKNOWN, None, None
            if paused:
                # Une suspension explicite prime sur la phase d'exécution :
                # c'est la décision opérateur, et elle reste réversible.
                checkpoint = prepare_checkpoint if history_kind != "historical" else history_checkpoint
                return PHASE_PAUSED, checkpoint, None
        if history_kind == "historical":
            if prepare_kind != "prepared":
                return PHASE_UNKNOWN, None, None
            if history_checkpoint != prepare_checkpoint:
                return PHASE_UNKNOWN, None, None
            # Le pilote de progression a la main une fois le run domaine
            # persisté : sa phase agrégée et ses phases par table priment
            # sur la phase job — un run corrompu ou d'une génération morte
            # n'avance rien, il dégrade l'observation en UNKNOWN.
            run_state = _load_run_state(self._run_store, prepare_raw)
            if run_state == "corrupt" or run_state == "stale":
                return PHASE_UNKNOWN, None, None
            if run_state is not None:
                run = run_state
                return (
                    public_phase(run, PHASE_HISTORICAL),
                    history_checkpoint,
                    public_table_states(run),
                )
            return PHASE_HISTORICAL, history_checkpoint, None
        if history_kind == "blocked":
            return PHASE_BLOCKED, None, None
        if prepare_kind == "prepared" and history_kind == "absent":
            return PHASE_PREPARED, prepare_checkpoint, None
        if prepare_kind == "blocked":
            return PHASE_BLOCKED, None, None
        if prepare_kind == "absent" and history_kind == "absent":
            return PHASE_NOT_PREPARED, None, None
        return PHASE_UNKNOWN, None, None


def _observed_run_state(reader: object) -> str | None:
    """État console brut du lecteur (``run.state``), ou ``None``.

    Lecture tolérante aux pannes — sert uniquement à ajuster la raison
    publiée par ``start``/``resume`` dans les capacités. Elle ne remplace
    jamais ``_parked_resume`` : celui-là seul revalide tout avant d'exécuter
    une reprise. ``None`` couvre aussi bien l'absence de lecteur console que
    tout document illisible ou hors contrat.
    """

    if not callable(reader):
        return None
    try:
        console = reader()
    except Exception:
        return None
    if type(console) is not dict:
        return None
    run = console.get("run")
    if type(run) is not dict:
        return None
    state = run.get("state")
    return state if isinstance(state, str) else None


def _load_store(store: object) -> object:
    try:
        raw = store.load()
    except Exception:
        return _CORRUPT
    if raw is None:
        return None
    if type(raw) is not dict:
        return _CORRUPT
    return raw


def _load_run_state(
    run_store: object | None, prepare_raw: dict[str, object]
) -> object:
    """Le run domaine persisté, ou un verdict borné.

    ``None`` quand aucun état n'a encore été écrit (le pilote n'a pas
    démarré) ; ``"corrupt"`` sur un document hors contrat ; ``"stale"``
    quand le run appartient à une génération prepare antérieure — la
    réconciliation à l'intention courante est la seule preuve que le run
    décrit la flotte observée.
    """

    if run_store is None:
        return None
    raw = _load_store(run_store)
    if raw is None:
        return None
    if raw is _CORRUPT:
        return "corrupt"
    try:
        intent, run, _created_at = parse_run_state(raw)
    except Exception:
        return "corrupt"
    if intent != prepare_raw.get("intent_id"):
        return "stale"
    return run


def _inspect_prepare(raw: object, plan: FleetPlan) -> tuple[str, dict[str, object] | None]:
    if raw is _CORRUPT:
        return "unknown", None
    if raw is None:
        return "absent", None
    phase = raw.get("phase") if type(raw) is dict else None
    if phase == PHASE_PREPARED:
        checkpoint = _validate_prepared(raw, plan)
        if checkpoint is None:
            return "unknown", None
        return "prepared", checkpoint
    if phase in {PHASE_PREPARING, PHASE_PREPARE_FAILED}:
        return "blocked", None
    return "unknown", None


def _inspect_history(raw: object, plan: FleetPlan) -> tuple[str, dict[str, object] | None]:
    if raw is _CORRUPT:
        return "unknown", None
    if raw is None:
        return "absent", None
    phase = raw.get("phase") if type(raw) is dict else None
    if phase == PHASE_HISTORICAL:
        checkpoint = _validate_historical(raw, plan)
        if checkpoint is None:
            return "unknown", None
        return "historical", checkpoint
    if phase in {PHASE_STARTING, PHASE_HISTORY_FAILED}:
        return "blocked", None
    return "unknown", None


def _validate_prepared(raw: object, plan: FleetPlan) -> dict[str, object] | None:
    try:
        state = _closed_prepare_mapping(raw, _PREPARE_STATE_KEYS)
        if state["format_version"] != PREPARE_FORMAT_VERSION:
            return None
        if state["environment"] != _fleet.ENVIRONMENT:
            return None
        if state["phase"] != PHASE_PREPARED:
            return None
        intent_id = _require_prepare_token(state["intent_id"], PREPARE_INVALID_STATE)
        checkpoint = _parse_prepare_checkpoint(state["checkpoint"])
        manifest = _parse_prepare_manifest(state["manifest"])
        if manifest != _fleet.MANIFEST:
            return None
        receipt = _parse_prepare_receipt(state["receipt"])
        _assert_prepare_receipt_matches(receipt, intent_id, checkpoint, manifest)
        if state["needs_recovery"] is not False or state["error_code"] is not None:
            return None
        groups = plan.journal_groups
        if type(groups) is not tuple or len(groups) != 1:
            return None
        group = groups[0]
        if (
            state["journal_library"] != group.library
            or state["journal_name"] != group.name
            or state["reader_kind"] != group.reader_kind
            or state["reader_count"] != group.reader_count
        ):
            return None
        return _public_checkpoint(checkpoint.to_dict())
    except (FleetError, TypeError, ValueError, AttributeError, KeyError, IndexError):
        return None


def _validate_historical(raw: object, plan: FleetPlan) -> dict[str, object] | None:
    try:
        state = _closed_history_mapping(raw, _HISTORY_STATE_KEYS)
        if state["format_version"] != HISTORY_FORMAT_VERSION:
            return None
        if state["environment"] != _fleet.ENVIRONMENT:
            return None
        if state["phase"] != PHASE_HISTORICAL:
            return None
        intent_id = _require_history_token(state["intent_id"], HISTORY_INVALID_STATE)
        prepare_intent_id = _require_history_token(state["prepare_intent_id"], HISTORY_INVALID_STATE)
        reader_id = _require_history_reader_id(state["reader_id"])
        checkpoint = _parse_history_checkpoint(state["checkpoint"])
        manifest = _parse_history_manifest(state["manifest"])
        if manifest != _fleet.MANIFEST:
            return None
        lanes = _parse_history_lanes(state["lanes"], code=HISTORY_INVALID_STATE)
        max_concurrency = _parse_history_concurrency(state["max_concurrency"], code=HISTORY_INVALID_STATE)
        if (
            lane_composition(lanes) != lane_composition(plan.historical_lanes)
            or max_concurrency != plan.max_concurrency
        ):
            return None
        receipt = _parse_history_receipt(state["receipt"])
        _assert_history_receipt_matches(
            receipt,
            intent_id=intent_id,
            prepare_intent_id=prepare_intent_id,
            reader_id=reader_id,
            checkpoint=checkpoint,
            manifest=manifest,
            lanes=lanes,
            max_concurrency=max_concurrency,
        )
        if state["needs_recovery"] is not False or state["error_code"] is not None:
            return None
        return _public_checkpoint(checkpoint.to_dict())
    except (FleetError, TypeError, ValueError, AttributeError, KeyError, IndexError):
        return None


def _public_checkpoint(value: object) -> dict[str, object] | None:
    if type(value) is JournalCheckpoint:
        value = value.to_dict()
    if type(value) is not dict:
        return None
    if set(value) != {"receiver", "sequence"}:
        return None
    receiver = value.get("receiver")
    sequence = value.get("sequence")
    if type(receiver) is not str or not receiver.strip() or receiver != receiver.strip():
        return None
    if type(sequence) is not int or type(sequence) is bool or sequence < 0:
        return None
    if _is_sensitive(receiver):
        return None
    return {"receiver": receiver, "sequence": sequence}


def _public_table_state(value: object) -> dict[str, object] | None:
    """État public d'une voie : phase contractuelle + compteurs mesurés.

    ``None`` sur tout écart de forme — la voie dégrade alors la projection
    entière plutôt que de publier un compteur invérifiable.
    """

    if type(value) is not dict or set(value) != {"phase", "copied_rows", "total_rows"}:
        return None
    phase = value.get("phase")
    copied = value.get("copied_rows")
    total = value.get("total_rows")
    if phase not in PUBLIC_TABLE_PHASES:
        return None
    for count in (copied, total):
        if count is not None and (
            type(count) is not int or type(count) is bool or count < 0
        ):
            return None
    if copied is not None and total is not None and copied > total:
        return None
    return {"phase": phase, "copied_rows": copied, "total_rows": total}


def _public_projection(
    phase: str,
    checkpoint: dict[str, object] | None,
    pause_supported: bool = False,
    refresh_supported: bool = False,
    table_states: dict[str, dict[str, object]] | None = None,
    run_state: str | None = None,
) -> dict[str, object]:
    if phase not in PUBLIC_PHASES:
        phase = PHASE_UNKNOWN
        checkpoint = None
        table_states = None
    public_checkpoint = _public_checkpoint(checkpoint)
    # États réels par table quand le run domaine est là ; sinon la phase
    # agrégée est déclinée uniformément — le contrat liste toujours la
    # totalité du manifeste.
    states: list[dict[str, object]] = []
    if table_states is not None:
        if set(table_states) == set(_fleet.MANIFEST) and all(
            _public_table_state(state) is not None for state in table_states.values()
        ):
            states = [
                {"name": name, **_public_table_state(table_states[name])}
                for name in _fleet.MANIFEST
            ]
        else:
            phase = PHASE_UNKNOWN
            public_checkpoint = None
    if not states:
        states = [
            {"name": name, "phase": phase, "copied_rows": None, "total_rows": None}
            for name in _fleet.MANIFEST
        ]
    return {
        "format_version": RUNTIME_FORMAT_VERSION,
        "fleet_id": _site().fleet_id,
        "environment": _site().environment,
        "pipeline_id": _site().site_id,
        "phase": phase,
        "checkpoint": public_checkpoint,
        "capabilities": _capabilities(phase, pause_supported, refresh_supported, run_state),
        "table_states": states,
    }


def _capabilities(
    phase: str,
    pause_supported: bool = False,
    refresh_supported: bool = False,
    run_state: str | None = None,
) -> dict[str, dict[str, object]]:
    unavailable_pause = {"state": "unavailable", "reason": "unsupported_action"}
    parked = run_state in _PARKED_RUN_STATES
    if phase == PHASE_NOT_PREPARED:
        prepare = {"state": "available", "reason": None}
        start = {"state": "unavailable", "reason": "not_prepared"}
    elif phase == PHASE_PREPARED:
        prepare = {"state": "unavailable", "reason": "already_prepared"}
        start = {"state": "available", "reason": None}
    elif phase in {"HISTORICAL", "CATCHING_UP", "LIVE", "RECONCILING", "CERTIFIED"}:
        prepare = {"state": "unavailable", "reason": "already_prepared"}
        if parked:
            # Le run domaine avance encore (ou est figé) sans que ça ne dise
            # rien du lecteur : ici il est garé, donc "already_started"
            # mentirait sur l'état réel du runtime déployé.
            start = {
                "state": "unavailable",
                "reason": _START_STOP_REASONS.get(run_state, "reader_stopped"),
            }
        else:
            start = {"state": "unavailable", "reason": "already_started"}
    elif phase == PHASE_PAUSED:
        prepare = {"state": "unavailable", "reason": "paused"}
        start = {"state": "unavailable", "reason": "paused"}
    elif phase == PHASE_BLOCKED:
        prepare = {"state": "unavailable", "reason": "needs_recovery"}
        start = {"state": "unavailable", "reason": "needs_recovery"}
    else:
        prepare = {"state": "unavailable", "reason": "invalid_runtime_state"}
        start = {"state": "unavailable", "reason": "invalid_runtime_state"}
    # "unsupported_action" ne doit désigner qu'une seule chose : ce déploiement
    # n'a pas de runtime de suspension. Toute autre indisponibilité porte sa
    # propre raison, sans quoi l'interface ne peut pas distinguer un produit
    # incomplet d'un état qui ne s'y prête pas.
    unavailable_reason = (
        CODE_NOT_PAUSABLE_IN_PHASE if pause_supported else "unsupported_action"
    )
    pause = {"state": "unavailable", "reason": unavailable_reason}
    resume = {"state": "unavailable", "reason": unavailable_reason}
    if phase == PHASE_PAUSED:
        pause = {"state": "unavailable", "reason": "already_paused"}
        resume = {"state": "available", "reason": None}
    elif phase in _PAUSABLE_PHASES and pause_supported:
        pause = {"state": "available", "reason": None}
        resume = {"state": "unavailable", "reason": "not_paused"}
    if parked and phase != PHASE_PAUSED:
        # Le lecteur console est garé et lisible (sinon ``run_state`` serait
        # None) : la reprise existe dans ce déploiement, mais les preuves
        # qu'exécuter la relançabilité exigent (continuité, sonde fraîche,
        # Job relisible — voir ``_parked_resume``) ne sont pas encore
        # réunies ici. ``project()`` la fait passer à "available" quand
        # elles le sont ; sinon cette raison distingue explicitement
        # "impossible dans cet état" de "unsupported_action" (aucun lecteur
        # console monté du tout, cas où ``run_state`` serait resté None).
        resume = {"state": "unavailable", "reason": CODE_RESUME_NOT_READY}
    refresh = (
        {"state": "available", "reason": None}
        if refresh_supported
        else dict(unavailable_pause)
    )
    return {
        "prepare": prepare,
        "start": start,
        "pause": pause,
        "resume": resume,
        "refresh": refresh,
    }


def _success_stages() -> dict[str, dict[str, str]]:
    return {
        "intent": _stage("recorded", "intent_recorded", _MSG_INTENT),
        "execution": _stage("completed", "execution_completed", _MSG_EXECUTION_DONE),
        "observed_effect": _stage("succeeded", "effect_observed", _MSG_EFFECT_DONE),
    }


def _failed_stages(code: str, message: str) -> dict[str, dict[str, str]]:
    return {
        "intent": _stage("recorded", "intent_recorded", _MSG_INTENT),
        "execution": _stage("failed", _safe_code(code), _safe_message(message)),
        "observed_effect": _stage("failed", "effect_failed", _MSG_EFFECT_FAILED),
    }


def _stage(state: str, code: str, message: str) -> dict[str, str]:
    return {"state": state, "code": _safe_code(code), "message": _safe_message(message)}


def _safe_code(value: object) -> str:
    if type(value) is str and _SAFE_CODE.fullmatch(value) is not None and not _is_sensitive(value):
        return value
    return "execution_failed"


def _safe_message(value: object) -> str:
    if (
        type(value) is str
        and _SAFE_MESSAGE.fullmatch(value) is not None
        and not _is_sensitive(value)
        and "://" not in value
    ):
        return value
    return _MSG_EXECUTION_FAILED


def _is_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in _SENSITIVE_TOKENS)


def _pause_code(code: object) -> str:
    """Code de suspension réduit à la liste fermée du contrat d'action."""

    if type(code) is str and _SAFE_CODE.fullmatch(code) is not None and not _is_sensitive(code):
        return code
    return "pause_failed"


def _same_launch_plan(current: object, candidate: FleetPlan) -> bool:
    """Plan interchangeable : même groupe journal, voies et concurrence.

    Le checkpoint de cutover et les volumes observés peuvent avancer — les
    états persistés ne les relisent pas. Le groupe (journal, lecteur,
    continuité), les voies historiques et la concurrence, si.
    """

    if type(current) is not FleetPlan:
        return False
    return (
        candidate.journal_groups == current.journal_groups
        and lane_composition(candidate.historical_lanes)
        == lane_composition(current.historical_lanes)
        and candidate.max_concurrency == current.max_concurrency
    )


def _refresh_failure_code(outcome: object) -> str:
    reason = getattr(outcome, "reason", None)
    if type(reason) is str:
        lowered = reason.lower()
        if _SAFE_CODE.fullmatch(lowered) is not None and not _is_sensitive(lowered):
            return lowered
    return "catalog_refresh_failed"


def _refresh_message(outcome: object, swap: str) -> str:
    """Reçu mesuré : horodatage du catalogue, receivers et continuité."""

    observed = _safe_observed_at(getattr(outcome, "observed_at", None))
    receivers = getattr(outcome, "receiver_count", None)
    continuity = getattr(outcome, "continuity", None)
    parts = ["Catalogue"]
    if observed is not None:
        parts.append(observed)
    if type(receivers) is int and type(receivers) is not bool and receivers >= 0:
        parts.append(f"{receivers} receivers")
    if type(continuity) is str and continuity.strip():
        parts.append(f"continuite {continuity.strip()}")
    message = " ".join(parts)
    if swap != PLAN_APPLIED:
        message += " - plan conserve"
    if _SAFE_MESSAGE.fullmatch(message) is None or _is_sensitive(message):
        return "Catalogue releve"
    return message


def _safe_observed_at(value: object) -> str | None:
    """Horodatage ISO ramené aux caractères du contrat de message."""

    if type(value) is not str or len(value) < 16:
        return None
    stamp = value[:16].replace("T", " ").replace(":", "h").strip()
    return stamp or None
