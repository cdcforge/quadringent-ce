"""Contrat borné des actions métier DEV du control plane.

Aucun appel AWS, Snowflake ou Kubernetes n'est effectué ici. L'exécuteur
injecté, s'il existe, reste synchrone, testable et agnostique du fournisseur.
Sans exécuteur, le contrat échoue fermé.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from http import HTTPStatus
import json
from threading import Lock
from typing import Mapping
from urllib.parse import unquote
import re
import secrets

from quadringent.site_config import SiteConfig, current as _current_site

ACTION_IDS = frozenset({"refresh", "prepare", "start", "pause", "resume"})
# Identités du site déclaré — résolues à l'accès via ``__getattr__``, jamais
# figées à une installation dans le code.
ACTION_FLEET_ID: str
ACTION_ENVIRONMENT: str
CONFIRMATIONS: dict[str, str | None]
MAX_ACTION_BYTES = 4 * 1024


def _site() -> SiteConfig:
    return _current_site()


def _confirmations(site: SiteConfig) -> dict[str, str | None]:
    """Jetons d'action dérivés de l'identité déclarée du site."""

    declared = f"{site.site_id.upper()} {site.fleet_environment}"
    return {
        "refresh": None,
        "prepare": f"PREPARE {declared}",
        "start": f"START {declared}",
        "pause": f"PAUSE {declared}",
        "resume": f"RESUME {declared}",
    }


def __getattr__(name: str) -> object:
    site_attributes = {
        "ACTION_FLEET_ID": lambda site: site.fleet_id,
        "ACTION_ENVIRONMENT": lambda site: site.environment,
        "CONFIRMATIONS": _confirmations,
    }
    resolver = site_attributes.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver(_site())
OVERALL_STATES = frozenset({"succeeded", "failed", "conflict", "unavailable"})
INTENT_STATES = frozenset({"recorded", "rejected"})
EXECUTION_STATES = frozenset({"completed", "failed", "not_started"})
OBSERVED_EFFECT_STATES = frozenset({"succeeded", "failed", "unknown"})
RECEIPT_FIELDS = ("id", "action", "fleet_id", "environment", "created_at", "state", "stages")
STAGE_FIELDS = ("state", "code", "message")
SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_MESSAGE = re.compile(r"^[\w][\w .,'’-]{0,119}$", re.UNICODE)
SAFE_RECEIPT_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ACTIONS_MARKER = "/actions/"
_PIPELINES_PREFIX = "/v1/pipelines/"


@dataclass(frozen=True)
class PipelineActionInvocation:
    pipeline_id: str
    action: str
    fleet_id: str
    environment: str


@dataclass(frozen=True)
class ActionPayload:
    fleet_id: str
    environment: str
    confirmation: str | None


@dataclass(frozen=True)
class ActionHttpResult:
    status: HTTPStatus
    body: Mapping[str, object]


class MalformedExecutor(ValueError):
    """Résultat d'exécuteur hors contrat : jamais renvoyé tel quel au client."""


class PipelineActionGate:
    """Sérialise une action par pipeline et refuse un second appel concurrent."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[str, Lock] = {}

    def try_begin(self, pipeline_id: str) -> bool:
        with self._guard:
            lock = self._locks.get(pipeline_id)
            if lock is None:
                lock = Lock()
                self._locks[pipeline_id] = lock
        return lock.acquire(blocking=False)

    def end(self, pipeline_id: str) -> None:
        with self._guard:
            lock = self._locks.get(pipeline_id)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                return


def parse_action_route(path: str) -> tuple[str, str] | None:
    """Retourne (pipeline_id, action) seulement pour une route d'action valide."""
    if not path.startswith(_PIPELINES_PREFIX):
        return None
    rest = path[len(_PIPELINES_PREFIX) :]
    encoded_id, separator, action = rest.partition(_ACTIONS_MARKER)
    if not separator or not encoded_id or "/" in encoded_id:
        return None
    if action not in ACTION_IDS:
        return None
    pipeline_id = unquote(encoded_id)
    if not pipeline_id:
        return None
    return pipeline_id, action


def execute_pipeline_action(
    *,
    raw_body: bytes,
    pipeline_id: str,
    action: str,
    snapshot: object,
    executor: object | None,
    gate: PipelineActionGate,
    now: datetime | None = None,
) -> ActionHttpResult:
    parsed = _parse_payload(raw_body, action)
    if isinstance(parsed, ActionHttpResult):
        return parsed
    pipeline = _find_pipeline(snapshot, pipeline_id)
    if pipeline is None:
        return _error(HTTPStatus.NOT_FOUND, "not_found")
    if _normalized_environment(getattr(pipeline, "environment", None)) != _site().environment:
        return _error(HTTPStatus.FORBIDDEN, "wrong_environment")
    if not _confirmation_matches(action, parsed.confirmation):
        return _error(HTTPStatus.FORBIDDEN, "wrong_confirmation")
    invocation = PipelineActionInvocation(
        pipeline_id=getattr(pipeline, "id"),
        action=action,
        fleet_id=_site().fleet_id,
        environment=_site().environment,
    )
    if not _capability_available(pipeline, action) and not executor_supports(executor, invocation):
        return _receipt_result(
            HTTPStatus.CONFLICT,
            action,
            "unavailable",
            intent=_stage("rejected", "capability_unavailable", "Capacite indisponible"),
            execution=_stage("not_started", "executor_not_started", "Execution non demarree"),
            observed=_stage("unknown", "effect_unknown", "Effet non observe"),
            now=now,
        )
    if not _has_executor(executor):
        return _receipt_result(
            HTTPStatus.CONFLICT,
            action,
            "unavailable",
            intent=_stage("rejected", "executor_unavailable", "Executeur indisponible"),
            execution=_stage("not_started", "executor_not_started", "Execution non demarree"),
            observed=_stage("unknown", "effect_unknown", "Effet non observe"),
            now=now,
        )
    if not gate.try_begin(getattr(pipeline, "id")):
        return _receipt_result(
            HTTPStatus.CONFLICT,
            action,
            "conflict",
            intent=_stage("rejected", "action_in_progress", "Action deja en cours"),
            execution=_stage("not_started", "executor_not_started", "Execution non demarree"),
            observed=_stage("unknown", "effect_unknown", "Effet non observe"),
            now=now,
        )
    try:
        return _invoke_executor(executor, invocation, action, now)
    finally:
        gate.end(getattr(pipeline, "id"))


def _parse_payload(raw_body: bytes, action: str) -> ActionPayload | ActionHttpResult:
    if action not in ACTION_IDS:
        return _error(HTTPStatus.NOT_FOUND, "not_found")
    try:
        text = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    if len(raw_body) > MAX_ACTION_BYTES:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    try:
        payload = json.loads(text)
    except ValueError:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    if not isinstance(payload, dict):
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    if set(payload.keys()) != {"fleet_id", "environment", "confirmation"}:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    fleet_id = payload.get("fleet_id")
    if fleet_id != _site().fleet_id:
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    environment = payload.get("environment")
    if not isinstance(environment, str):
        return _error(HTTPStatus.BAD_REQUEST, "invalid_request")
    if _normalized_environment(environment) != _site().environment:
        return _error(HTTPStatus.FORBIDDEN, "wrong_environment")
    confirmation = payload.get("confirmation")
    if confirmation is not None and not isinstance(confirmation, str):
        return _error(HTTPStatus.FORBIDDEN, "wrong_confirmation")
    return ActionPayload(_site().fleet_id, _site().environment, confirmation)


def _confirmation_matches(action: str, confirmation: str | None) -> bool:
    expected = _confirmations(_site())[action]
    return confirmation is None if expected is None else confirmation == expected


def _find_pipeline(snapshot: object, pipeline_id: str) -> object | None:
    pipelines = getattr(snapshot, "pipelines", ())
    for pipeline in pipelines:
        if getattr(pipeline, "id", None) == pipeline_id:
            return pipeline
    return None


def _normalized_environment(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if value.casefold() != _site().environment:
        return None
    return _site().environment


def _capability_available(pipeline: object, action: str) -> bool:
    fleet = _fleet(pipeline)
    if fleet is None:
        return False
    fleet_id = _mapping_or_attr(fleet, "fleet_id")
    if fleet_id != _site().fleet_id:
        return False
    capabilities = _mapping_or_attr(fleet, "capabilities")
    if capabilities is None:
        return False
    capability = _mapping_or_attr(capabilities, action)
    if capability is None:
        return False
    if isinstance(capability, str):
        return capability == "available"
    state = _mapping_or_attr(capability, "state")
    return state == "available"


def _fleet(pipeline: object) -> object | None:
    value = getattr(pipeline, "fleet", None)
    if value is not None:
        return value
    to_dict = getattr(pipeline, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        payload = to_dict()
    except Exception:
        return None
    if isinstance(payload, Mapping):
        return payload.get("fleet")
    return None


def _has_executor(executor: object | None) -> bool:
    if executor is None:
        return False
    return callable(getattr(executor, "execute", None)) or callable(getattr(executor, "handle", None))


def _invoke_executor(
    executor: object | None,
    invocation: PipelineActionInvocation,
    action: str,
    now: datetime | None,
) -> ActionHttpResult:
    try:
        raw = _call_executor(executor, invocation)
        stages = _normalize_executor_stages(raw)
        overall = _overall_state(stages)
    except MalformedExecutor:
        return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")
    except Exception:
        return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")
    status = HTTPStatus.OK if overall == "succeeded" else HTTPStatus.CONFLICT
    return _receipt_result(
        status,
        action,
        overall,
        intent=stages["intent"],
        execution=stages["execution"],
        observed=stages["observed_effect"],
        now=now,
    )


def executor_supports(executor: object | None, invocation: PipelineActionInvocation) -> bool:
    """Évalue une capacité locale déclarée sans appeler de fournisseur."""
    if executor is None:
        return False
    supports = getattr(executor, "supports", None)
    if not callable(supports):
        return False
    try:
        return supports(invocation) is True
    except Exception:
        return False


def _call_executor(executor: object | None, invocation: PipelineActionInvocation) -> object:
    if executor is None:
        raise MalformedExecutor("missing executor")
    method = getattr(executor, "execute", None)
    if not callable(method):
        method = getattr(executor, "handle", None)
    if not callable(method):
        raise MalformedExecutor("missing handler")
    return method(invocation)


def _normalize_executor_stages(raw: object) -> dict[str, dict[str, str]]:
    mapping = _closed_mapping(raw, {"intent", "execution", "observed_effect", "stages"})
    if mapping is None:
        raise MalformedExecutor("invalid executor result")
    if "stages" in mapping:
        if set(mapping) != {"stages"}:
            raise MalformedExecutor("unexpected executor fields")
        mapping = _closed_mapping(mapping["stages"], {"intent", "execution", "observed_effect"})
        if mapping is None:
            raise MalformedExecutor("invalid stages")
    if set(mapping) != {"intent", "execution", "observed_effect"}:
        raise MalformedExecutor("unexpected executor fields")
    intent = _normalize_stage(mapping["intent"], INTENT_STATES)
    execution = _normalize_stage(mapping["execution"], EXECUTION_STATES)
    observed = _normalize_stage(mapping["observed_effect"], OBSERVED_EFFECT_STATES)
    return {"intent": intent, "execution": execution, "observed_effect": observed}


def _normalize_stage(raw: object, allowed_states: frozenset[str]) -> dict[str, str]:
    mapping = _closed_mapping(raw, set(STAGE_FIELDS))
    if mapping is None or set(mapping) != set(STAGE_FIELDS):
        raise MalformedExecutor("invalid stage")
    state = mapping["state"]
    code = mapping["code"]
    message = mapping["message"]
    if not isinstance(state, str) or state not in allowed_states:
        raise MalformedExecutor("invalid stage state")
    if not isinstance(code, str) or SAFE_CODE.fullmatch(code) is None:
        raise MalformedExecutor("invalid stage code")
    if not isinstance(message, str) or SAFE_MESSAGE.fullmatch(message) is None:
        raise MalformedExecutor("invalid stage message")
    return {"state": state, "code": code, "message": message}


def _overall_state(stages: Mapping[str, Mapping[str, str]]) -> str:
    intent = stages["intent"]["state"]
    execution = stages["execution"]["state"]
    observed = stages["observed_effect"]["state"]
    if intent == "recorded" and execution == "completed" and observed == "succeeded":
        return "succeeded"
    if intent != "recorded":
        raise MalformedExecutor("inconsistent intent")
    if execution == "failed" and observed in {"failed", "unknown"}:
        return "failed"
    if execution == "completed" and observed == "failed":
        return "failed"
    raise MalformedExecutor("inconsistent stages")


def _receipt_result(
    status: HTTPStatus,
    action: str,
    overall: str,
    *,
    intent: Mapping[str, str],
    execution: Mapping[str, str],
    observed: Mapping[str, str],
    now: datetime | None,
) -> ActionHttpResult:
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    receipt = {
        "id": secrets.token_urlsafe(18),
        "action": action,
        "fleet_id": _site().fleet_id,
        "environment": _site().environment,
        "created_at": created.isoformat(),
        "state": overall,
        "stages": {
            "intent": dict(intent),
            "execution": dict(execution),
            "observed_effect": dict(observed),
        },
    }
    if SAFE_RECEIPT_ID.fullmatch(str(receipt["id"])) is None:
        return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")
    return ActionHttpResult(status, receipt)


def _stage(state: str, code: str, message: str) -> dict[str, str]:
    return {"state": state, "code": code, "message": message}


def _error(status: HTTPStatus, code: str) -> ActionHttpResult:
    return ActionHttpResult(status, {"error": {"code": code}})


def _closed_mapping(value: object, allowed: set[str]) -> dict[str, object] | None:
    if isinstance(value, Mapping):
        mapping = dict(value)
    elif is_dataclass(value) and not isinstance(value, type):
        mapping = {item.name: getattr(value, item.name) for item in fields(value)}
    else:
        raw = getattr(value, "__dict__", None)
        if not isinstance(raw, dict):
            mapping = {}
            for key in allowed:
                if hasattr(value, key):
                    mapping[key] = getattr(value, key)
            if not mapping:
                return None
        else:
            mapping = {key: item for key, item in raw.items() if not key.startswith("_")}
    if any(key not in allowed for key in mapping):
        return None
    return mapping


def _mapping_or_attr(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)
