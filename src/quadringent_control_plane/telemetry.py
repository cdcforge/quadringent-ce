"""Télémétrie produit opt-in — rien n'est émis sans activation explicite.

La gouvernance prime : la télémétrie est désactivée par défaut, le payload est
minimal et documenté dans ``docs/product/telemetry.md``. Jamais de nom d'hôte,
d'adresse, de table métier, de donnée client ni d'identifiant utilisateur.
L'émission est bornée, silencieuse en cas d'échec et n'affecte jamais le
fonctionnement du produit.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Mapping
from urllib import request as _request

_DEFAULT_ENDPOINT = "https://telemetry.quadringent.io/v1/ping"
_INTERVAL_SECONDS = 24 * 60 * 60
_FIRST_DELAY_SECONDS = 60.0
_TIMEOUT_SECONDS = 5.0
_INSTALL_ID_FILE = "telemetry-install-id"


@dataclass(frozen=True)
class TelemetryConfig:
    """Configuration de télémétrie — ``enabled`` vient uniquement d'un choix explicite."""

    enabled: bool = False
    endpoint: str = _DEFAULT_ENDPOINT
    state_dir: Path = Path("/var/lib/quadringent")

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> "TelemetryConfig":
        source = os.environ if env is None else env
        enabled = source.get("QUADRINGENT_TELEMETRY", "").strip().lower() in {"1", "true", "on", "yes"}
        endpoint = source.get("QUADRINGENT_TELEMETRY_URL", "").strip() or _DEFAULT_ENDPOINT
        if not endpoint.startswith("https://"):
            endpoint = _DEFAULT_ENDPOINT
        state_dir = Path(source.get("QUADRINGENT_STATE_DIR", "/var/lib/quadringent"))
        return TelemetryConfig(enabled=enabled, endpoint=endpoint, state_dir=state_dir)


def _install_id(state_dir: Path) -> str:
    """Identifiant d'installation anonyme et stable — un identifiant hexadécimal local, jamais un
    identifiant du site ou du client. Seul son hachage part dans le payload."""
    path = state_dir / _INSTALL_ID_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    fresh = secrets.token_hex(16)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass
    return fresh


def build_payload(config: TelemetryConfig, *, version: str,
                  table_count: int, now: float | None = None) -> dict:
    """Le payload exact envoyé — documenté dans docs/product/telemetry.md."""
    install_hash = hashlib.sha256(_install_id(config.state_dir).encode()).hexdigest()
    return {
        "v": 1,
        "install": install_hash,
        "version": version,
        "tier": "community",
        "tables": table_count,
        "ts": int(now if now is not None else time.time()),
    }


def send_once(config: TelemetryConfig, payload: dict) -> bool:
    """Un envoi borné ; tout échec est silencieux et sans effet produit."""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = _request.Request(
            config.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "quadringent-telemetry/1"},
            method="POST",
        )
        with _request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


class TelemetryDaemon(threading.Thread):
    """Émet un ping quotidien quand la télémétrie est activée — démon borné."""

    def __init__(self, config: TelemetryConfig, *, version: str,
                 table_count: int, interval: float = _INTERVAL_SECONDS,
                 first_delay: float = _FIRST_DELAY_SECONDS) -> None:
        super().__init__(name="quadringent-telemetry", daemon=True)
        self._config = config
        self._version = version
        self._tables = table_count
        self._interval = interval
        self._first_delay = first_delay
        # Thread.join() appelle sa méthode interne _stop() sous Python 3.12.
        self._stop_event = threading.Event()

    def run(self) -> None:
        if self._stop_event.wait(self._first_delay):
            return
        while not self._stop_event.is_set():
            send_once(self._config, build_payload(
                self._config, version=self._version,
                table_count=self._tables))
            if self._stop_event.wait(self._interval):
                return

    def stop(self) -> None:
        self._stop_event.set()


def maybe_start(config: TelemetryConfig, *, version: str,
                table_count: int) -> TelemetryDaemon | None:
    """Ne démarre que sur opt-in explicite ; retourne le démon ou None."""
    if not config.enabled:
        return None
    daemon = TelemetryDaemon(config, version=version,
                             table_count=table_count)
    daemon.start()
    return daemon
