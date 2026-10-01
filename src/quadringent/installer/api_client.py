"""Client HTTP ``/v2`` pour la CLI ``quadringent`` (contrat §5, tâche 19).

Toute sortie de la CLI est du JSON — jamais de texte libre — pour rester
scriptable par un humain ou un agent. La configuration (URL + jeton) vient,
dans cet ordre : variables d'environnement (``QUADRINGENT_URL``,
``QUADRINGENT_TOKEN``), puis un fichier de configuration privé
(``~/.quadringent/cli.json``, permissions ``0600`` exigées — refusé sinon,
fail-closed). ``ApiClient`` accepte un ``transport`` httpx injectable (tests :
``httpx.MockTransport`` ou ``httpx.ASGITransport(app=...)`` contre
``TestClient``/l'app FastAPI directement — jamais de réseau réel en test).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING
import uuid

if TYPE_CHECKING:
    import httpx

DEFAULT_CONFIG_PATH = Path.home() / ".quadringent" / "cli.json"

# ``httpx`` est dans l'extra ``api`` (voir pyproject.toml), pas une
# dépendance du paquet de base ``quadringent`` (``install``/``uninstall``/
# ``status`` n'en ont pas besoin) — import différé pour ne pas casser ces
# commandes quand l'extra n'est pas installé.

# Contrat §2.6 : catalogue de codes d'erreur stable -> code de sortie stable
# de la CLI (jamais 0/1 génériques pour une erreur applicative connue — un
# script appelant peut brancher sur le code exact sans parser le JSON).
_EXIT_CODES = {
    "invalid_request": 2,
    "not_found": 3,
    "insufficient_role": 4,
    "wrong_confirmation": 4,
    "wrong_environment": 4,
    "idempotency_key_conflict": 5,
    "pending_confirmation_required": 6,
    "capability_unavailable": 7,
    "action_in_progress": 7,
    "store_unavailable": 8,
    "executor_unavailable": 8,
}
EXIT_NETWORK_ERROR = 9
EXIT_CONFIG_ERROR = 10
EXIT_UNKNOWN_ERROR = 1


class ConfigError(RuntimeError):
    """Configuration manquante ou invalide (URL/jeton introuvables, fichier trop ouvert)."""


@dataclass(frozen=True)
class ClientConfig:
    base_url: str
    token: str | None


def load_config(*, env: dict[str, str] | None = None, config_path: Path = DEFAULT_CONFIG_PATH) -> ClientConfig:
    """Résout l'URL et le jeton — environnement prioritaire, sinon fichier ``0600``."""

    environ = env if env is not None else os.environ
    url = environ.get("QUADRINGENT_URL")
    token = environ.get("QUADRINGENT_TOKEN")
    if url:
        return ClientConfig(base_url=url.rstrip("/"), token=token)

    if not config_path.exists():
        raise ConfigError(
            f"aucune configuration : définir QUADRINGENT_URL (et QUADRINGENT_TOKEN), "
            f"ou créer {config_path} (permissions 0600)"
        )
    mode = stat.S_IMODE(config_path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"{config_path} doit être en permissions 0600 (lisible seulement par son propriétaire) — "
            f"actuellement {oct(mode)}"
        )
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{config_path} illisible ou n'est pas un JSON valide : {error}") from error
    if not isinstance(data, dict) or not data.get("url"):
        raise ConfigError(f"{config_path} doit contenir au moins {{'url': '...'}}")
    return ClientConfig(base_url=str(data["url"]).rstrip("/"), token=data.get("token"))


@dataclass(frozen=True)
class ApiResult:
    """Résultat d'un appel — ``exit_code`` est stable, dérivé de ``body['error']['code']``."""

    exit_code: int
    body: object
    idempotency_key: str | None = None


class ApiClient:
    """Client mince ``/v2`` — une méthode par verbe HTTP, jamais de logique métier ici."""

    def __init__(self, config: ClientConfig, *, transport: "httpx.BaseTransport | None" = None) -> None:
        import httpx

        self._httpx = httpx
        headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
        self._http = httpx.Client(base_url=config.base_url, headers=headers, transport=transport, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, path: str, *, params: dict[str, object] | None = None) -> ApiResult:
        return self._call("GET", path, params=params)

    def stream(self, path: str, *, params: dict[str, object] | None = None):
        """Contexte ``httpx`` de streaming brut (SSE) — voir ``v2_commands.py::_cmd_events_stream``."""

        return self._http.stream("GET", path, params=params, headers={"Accept": "text/event-stream"})

    def delete(self, path: str) -> ApiResult:
        return self._call("DELETE", path, idempotent=True)

    def write(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> ApiResult:
        """POST/PATCH avec ``Idempotency-Key`` — généré si absent, toujours renvoyé dans le résultat."""

        key = idempotency_key or f"cli-{uuid.uuid4().hex}"
        return self._call(method, path, json_body=json_body, idempotent=True, idempotency_key=key)

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> ApiResult:
        headers = {}
        if idempotent:
            headers["Idempotency-Key"] = idempotency_key or f"cli-{uuid.uuid4().hex}"
        try:
            response = self._http.request(method, path, json=json_body, params=params, headers=headers)
        except self._httpx.HTTPError as error:
            return ApiResult(
                exit_code=EXIT_NETWORK_ERROR,
                body={"error": {"code": "network_error", "message": str(error), "retryable": True}},
            )
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"code": "invalid_response", "message": response.text[:2000], "retryable": False}}
        if response.is_success:
            return ApiResult(exit_code=0, body=body, idempotency_key=headers.get("Idempotency-Key"))
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return ApiResult(
            exit_code=_EXIT_CODES.get(code, EXIT_UNKNOWN_ERROR),
            body=body,
            idempotency_key=headers.get("Idempotency-Key"),
        )
