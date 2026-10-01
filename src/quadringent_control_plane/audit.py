"""Journal local durable : aucun secret, corps HTTP ou détail d'exception brut."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from threading import Lock
from typing import Mapping


class ActionAuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = Lock()
        self.healthy = True
        self._append({"event": "audit_started"})

    def _append(self, record: dict[str, object]) -> None:
        document = {"at": datetime.now(timezone.utc).isoformat(), **record}
        encoded = (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode()
        try:
            with self._lock:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "ab") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
        except OSError:
            self.healthy = False
            raise

    def begin(self, action: str, pipeline_id: str, actor: str) -> str:
        if not self.healthy:
            raise OSError("audit indisponible")
        request_id = secrets.token_urlsafe(18)
        self._append({"event": "action_requested", "request_id": request_id,
                      "action": action, "pipeline_id": pipeline_id, "actor": actor})
        return request_id

    def finish(self, request_id: str, status: int, receipt: Mapping[str, object]) -> None:
        stages = receipt.get("stages")
        observed = stages.get("observed_effect") if isinstance(stages, Mapping) else None
        self._append({"event": "action_result", "request_id": request_id,
                      "status": status, "receipt_id": receipt.get("id"), "state": receipt.get("state"),
                      "observed_effect": observed.get("state") if isinstance(observed, Mapping) else None})
