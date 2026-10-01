"""Relevé borné du catalogue de flotte : sonde Java, plan et sidecar durables.

Le catalogue déployé par le ConfigMap de lancement fige le plan au démarrage.
Ce module rejoue ``FleetCatalogProbe`` en sous-processus borné, revalide le
document fermé (mêmes gardes que le chargement initial), puis réécrit
atomiquement le catalogue et le sidecar UI dans le répertoire d'état durable.
Tout échec conserve le dernier plan et le dernier sidecar valides : le
serveur ne doit jamais tomber sur un relevé.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from quadringent_control_plane.fleet import FleetError, MAX_CONCURRENCY
from quadringent_control_plane.fleet_plan import (
    FleetCatalog,
    FleetPlan,
    build_fleet_plan,
    parse_fleet_catalog,
)
from quadringent_control_plane.fleet_sidecar import (
    MAX_CATALOG_BYTES,
    _write_atomic_json,
    build_fleet_ui_sidecar,
    parse_fleet_ui_sidecar,
    write_fleet_ui_sidecar,
)


PROBE_CLASS = "io.quadringent.as400.FleetCatalogProbe"
PROBE_TIMEOUT_SECONDS = 120.0
FLEET_CATALOG_FILE = "fleet-catalog.json"
FLEET_SIDECAR_FILE = "fleet-sidecar.json"
REFRESH_INTERVAL_ENV = "QUADRINGENT_CATALOG_REFRESH_SECONDS"
PERSISTENT_STATE_ENV = "QUADRINGENT_FLEET_STATE_PERSISTENT"
DEFAULT_REFRESH_SECONDS = 300.0
MAX_SEED_BYTES = MAX_CATALOG_BYTES
SEED_COPIED = "copied"
SEED_KEPT = "kept"
SEED_MISSING = "missing"
SEED_INVALID = "invalid"
SEED_ERROR = "error"

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROBE_ERROR = re.compile(r"fleet_catalog_error=([A-Z][A-Z0-9_]{0,62})")


@dataclass(frozen=True)
class CatalogRefreshOutcome:
    """Mesure d'un cycle de relevé — jamais d'exception, jamais de secret."""

    status: str  # "refreshed" ou "kept"
    reason: str | None
    observed_at: str | None
    receiver_count: int | None
    continuity: str | None
    plan: FleetPlan | None
    catalog_written: bool
    sidecar_written: bool


class FleetCatalogRefresher:
    """Un cycle borné : sonde Java, validation, écritures durables.

    Le sous-processus hérite le contrat d'environnement complet du pod
    (``ISERIES_*``, ``AS400_*``, ``QUADRINGENT_*``, mot de passe par secret) —
    rien n'est recopié ni journalisé. Le plan candidat est rendu à l'appelant :
    l'exécuteur décide seul de l'échanger ou non.
    """

    def __init__(
        self,
        *,
        state_directory: str | Path,
        java: str = "java",
        classpath: str = "",
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
        max_concurrency: int = MAX_CONCURRENCY,
        historical_byte_budget: int | None = None,
        history_progress: Callable[[], Mapping[str, object] | None] | None = None,
        runner: Callable[..., Any] | None = None,
        emit: Callable[[CatalogRefreshOutcome], None] | None = None,
    ) -> None:
        if not str(java).strip():
            raise ValueError("java binary is required")
        if not str(classpath).strip():
            raise ValueError("probe classpath is required")
        if timeout_seconds <= 0:
            raise ValueError("probe timeout must be positive")
        self._state_directory = Path(state_directory)
        self._java = str(java).strip()
        self._classpath = str(classpath).strip()
        self._environ = environ
        self._timeout_seconds = timeout_seconds
        self._max_concurrency = max_concurrency
        self._historical_byte_budget = historical_byte_budget
        self._history_progress = history_progress
        self._runner = subprocess.run if runner is None else runner
        self._emit = _emit_stderr if emit is None else emit

    @property
    def catalog_path(self) -> Path:
        return self._state_directory / FLEET_CATALOG_FILE

    @property
    def sidecar_path(self) -> Path:
        return self._state_directory / FLEET_SIDECAR_FILE

    def refresh(self) -> CatalogRefreshOutcome:
        """Rejoue la sonde une fois ; ``kept`` sur tout échec, jamais levé."""

        try:
            completed = self._runner(
                [self._java, "-cp", self._classpath, PROBE_CLASS],
                env=None if self._environ is None else dict(self._environ),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return self._kept("catalog_probe_timeout")
        except OSError:
            return self._kept("catalog_probe_unavailable")
        except Exception:
            return self._kept("catalog_probe_failed")
        if getattr(completed, "returncode", None) != 0:
            return self._kept(_probe_error_code(getattr(completed, "stderr", "")))
        stdout = getattr(completed, "stdout", "")
        if type(stdout) is not str or len(stdout.encode("utf-8", "replace")) > MAX_CATALOG_BYTES:
            return self._kept("invalid_catalog")
        try:
            payload = json.loads(stdout)
        except ValueError:
            return self._kept("invalid_catalog")
        catalog = self._parse(payload)
        if type(catalog) is not FleetCatalog:
            return self._kept(catalog)
        plan = self._build_plan(catalog)
        if type(plan) is not FleetPlan:
            return self._kept(plan)
        sidecar = self._build_sidecar(catalog)
        if type(sidecar) is not dict:
            return self._kept(sidecar)
        try:
            _write_atomic_json(self.catalog_path, payload)
            write_fleet_ui_sidecar(self.sidecar_path, sidecar)
        except Exception:
            return self._kept("catalog_write_failed")
        outcome = CatalogRefreshOutcome(
            status="refreshed",
            reason=None,
            observed_at=catalog.observed_at,
            receiver_count=len(catalog.journals[0].receivers),
            continuity=plan.continuity,
            plan=plan,
            catalog_written=True,
            sidecar_written=True,
        )
        self._emit_safe(outcome)
        return outcome

    def _parse(self, payload: object) -> FleetCatalog | str:
        try:
            return parse_fleet_catalog(payload)
        except FleetError as error:
            return _lower_code(getattr(error, "code", None))
        except Exception:
            return "invalid_catalog"

    def _build_plan(self, catalog: FleetCatalog) -> FleetPlan | str:
        try:
            return build_fleet_plan(
                catalog,
                max_concurrency=self._max_concurrency,
                historical_byte_budget=self._historical_byte_budget,
            )
        except FleetError as error:
            return _lower_code(getattr(error, "code", None))
        except Exception:
            return "invalid_catalog"

    def _build_sidecar(self, catalog: FleetCatalog) -> dict[str, object] | str:
        progress = None
        if self._history_progress is not None:
            try:
                progress = self._history_progress()
            except Exception:
                progress = None
        try:
            return build_fleet_ui_sidecar(
                catalog,
                max_concurrency=self._max_concurrency,
                historical_byte_budget=self._historical_byte_budget,
                history_progress=progress,
            )
        except FleetError as error:
            return _lower_code(getattr(error, "code", None))
        except Exception:
            return "invalid_sidecar"

    def _kept(self, code: str) -> CatalogRefreshOutcome:
        outcome = CatalogRefreshOutcome(
            status="kept",
            reason=code,
            observed_at=None,
            receiver_count=None,
            continuity=None,
            plan=None,
            catalog_written=False,
            sidecar_written=False,
        )
        self._emit_safe(outcome)
        return outcome

    def _emit_safe(self, outcome: CatalogRefreshOutcome) -> None:
        try:
            self._emit(outcome)
        except Exception:
            pass


def seed_fleet_state(
    *,
    catalog_source: str | Path,
    sidecar_source: str | Path,
    state_directory: str | Path,
) -> dict[str, str]:
    """Copie les graines ConfigMap vers l'état durable, seulement si absentes.

    Chaque entrée du résultat vaut ``copied``, ``kept``, ``missing``,
    ``invalid`` ou ``error``. Une graine invalide n'est jamais copiée : le
    repli reste la source ConfigMap relue par l'appelant.
    """

    directory = Path(state_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"catalog": SEED_ERROR, "sidecar": SEED_ERROR}
    result: dict[str, str] = {}
    for key, source, name, validate in (
        ("catalog", Path(catalog_source), FLEET_CATALOG_FILE, _validate_catalog_payload),
        ("sidecar", Path(sidecar_source), FLEET_SIDECAR_FILE, _validate_sidecar_payload),
    ):
        target = directory / name
        if target.exists():
            result[key] = SEED_KEPT
            continue
        try:
            payload = _read_seed_json(source)
        except FileNotFoundError:
            result[key] = SEED_MISSING
            continue
        except FleetError:
            result[key] = SEED_INVALID
            continue
        except OSError:
            result[key] = SEED_ERROR
            continue
        try:
            validate(payload)
        except Exception:
            result[key] = SEED_INVALID
            continue
        try:
            _write_atomic_json(target, payload)
            result[key] = SEED_COPIED
        except Exception:
            result[key] = SEED_ERROR
    return result


def _validate_catalog_payload(payload: object) -> None:
    parse_fleet_catalog(payload)


def _validate_sidecar_payload(payload: object) -> None:
    parse_fleet_ui_sidecar(payload)


def _read_seed_json(source: Path) -> dict[str, object]:
    size = source.stat().st_size
    if size > MAX_SEED_BYTES:
        raise FleetError("invalid_catalog", "Graine de catalogue trop volumineuse")
    raw = source.read_bytes()
    if len(raw) > MAX_SEED_BYTES:
        raise FleetError("invalid_catalog", "Graine de catalogue trop volumineuse")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FleetError("invalid_catalog", "Graine JSON invalide") from None
    if type(payload) is not dict:
        raise FleetError("invalid_catalog", "Graine JSON invalide")
    return payload


def _probe_error_code(stderr: object) -> str:
    """Code symbolique stderr de la sonde, réduit au contrat ``[a-z0-9_]``."""

    if type(stderr) is str:
        match = _PROBE_ERROR.search(stderr)
        if match is not None:
            return match.group(1).lower()
    return "catalog_probe_failed"


def _lower_code(value: object) -> str:
    if type(value) is str:
        lowered = value.lower()
        if _SAFE_CODE.fullmatch(lowered) is not None:
            return lowered
    return "catalog_refresh_failed"


def _emit_stderr(outcome: CatalogRefreshOutcome) -> None:
    """Trace bornée : codes et mesures du relevé, jamais de diagnostic Java."""

    print(
        json.dumps(
            {
                "event": "fleet_catalog_refresh",
                "status": outcome.status,
                "reason": outcome.reason,
                "observed_at": outcome.observed_at,
                "receivers": outcome.receiver_count,
                "continuity": outcome.continuity,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
