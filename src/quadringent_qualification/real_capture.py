"""Copie initiale et capture bornée avec l'image immuable du produit.

Le mot de passe IBM i entre dans le conteneur par stdin, puis dans
``ISERIES_PASSWORD`` seulement à l'intérieur du processus éphémère. Docker
ne reçoit ni le secret dans ses arguments ni une clé cloud statique. Le runner
Linux doit disposer d'une identité de VM autorisée sur le bucket et l'état.
"""

from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
import re
import subprocess
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from quadringent.storage_layout import journal_prefix
from quadringent_control_plane.v2.executor.evidence import evidence_key

from .adapters import CaptureBoundary, CaptureResult
from .config import RunConfig
from .real_source import _load_credential


_IMAGE_DIGEST = re.compile(r"(?:\S+@)?sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_CAPTURE_LABEL = re.compile(r"capture-[1-9][0-9]*\Z")
_PROCESS_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
_PASSWORD_STDIN_WRAPPER = (
    'IFS= read -r ISERIES_PASSWORD || exit 64; '
    'export ISERIES_PASSWORD; exec /opt/venv/bin/python "$@"'
)
_MAX_OUTPUT_BYTES = 1024 * 1024


class DockerCaptureRunner:
    """``CaptureRunner`` avec un checkpoint et un préfixe propres au run."""

    def __init__(self, config: RunConfig, *, runner: Callable[..., Any] = subprocess.run) -> None:
        if _IMAGE_DIGEST.fullmatch(config.capture.image) is None:
            raise ValueError("qualification capture image must be pinned by digest")
        try:
            if str(UUID(config.run_id)) != config.run_id:
                raise ValueError
        except ValueError:
            raise ValueError("qualification capture run_id must be a canonical UUID") from None
        library, table = config.table.qualified_name.split(".", 1)
        if any(_IDENTIFIER.fullmatch(value) is None for value in (
            library, table, config.source.journal_library, config.source.journal_name,
        )):
            raise ValueError("qualification capture source identifier is invalid")
        prefix = config.storage.raw_prefix
        path = PurePosixPath(prefix)
        if (not prefix or "\\" in prefix or any(ord(char) < 32 for char in prefix)
                or not path.parts or path.is_absolute() or ".." in path.parts or str(path) != prefix
                or path.parts[-1] != config.run_id):
            raise ValueError("qualification capture run prefix must end with run_id")
        if config.storage.backend not in {"s3", "gcs"}:
            raise ValueError("qualification capture storage backend is invalid")
        if not config.storage.bucket or not config.storage.checkpoint_location:
            raise ValueError("qualification capture bucket and checkpoint location are required")
        if config.capture.max_seconds <= 15 or config.capture.max_consecutive_errors < 1:
            raise ValueError("qualification capture time and error budgets are invalid")
        time_zone = config.source.source_time_zone
        if not isinstance(time_zone, str) or not time_zone:
            raise ValueError("qualification source time zone is required")
        try:
            ZoneInfo(time_zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("qualification source time zone is invalid") from None

        self.config = config
        self._library = library
        self._table = table
        self._table_id = f"qual-{config.run_id}"
        self._runner = runner
        self._credential = _load_credential(
            config.source.connection_secret_file,
            configured_host=config.source.host,
            configured_user=config.source.user,
        )

    def run(self, *, label: str, max_seconds: int, bootstrap: CaptureBoundary | None,
            env: Mapping[str, str]) -> CaptureResult:
        if (label != "snapshot" and _CAPTURE_LABEL.fullmatch(label) is None
                or type(max_seconds) is not int or max_seconds <= 15
                or max_seconds > self.config.capture.max_seconds
                or not isinstance(bootstrap, CaptureBoundary) or env):
            raise ValueError("qualification capture invocation is invalid")

        snapshot = label == "snapshot"
        timeout_seconds = min(max_seconds - 1, 30)
        container_env = self._container_env(bootstrap, snapshot=snapshot, timeout_seconds=timeout_seconds)
        script = "/app/quadringent_initial_copy_job.py" if snapshot else "/app/as400_continuous_capture.py"
        script_args = [] if snapshot else [
            "--max-seconds", str(max_seconds),
            "--max-consecutive-errors", str(self.config.capture.max_consecutive_errors),
            "--metrics-interval-seconds", "10",
        ]
        container_name = f"qdt-qual-{self.config.run_id[:8]}-{label}-{uuid4().hex[:8]}"
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--interactive", "--pull=never", "--read-only",
            "--network", "host", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
            "--pids-limit", "128", "--memory", "1g", "--cpus", "1",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
        ]
        process_env = {key: os.environ[key] for key in _PROCESS_ENV if key in os.environ}
        process_env.update(container_env)
        for name in sorted(container_env):
            command.extend(("--env", name))
        command.extend((
            "--entrypoint", "/bin/sh", self.config.capture.image,
            "-c", _PASSWORD_STDIN_WRAPPER, "qualification-capture", script, *script_args,
        ))
        try:
            completed = self._runner(
                command, input=self._credential.password + "\n", env=process_env,
                text=True, capture_output=True, check=False,
                timeout=max_seconds + timeout_seconds + 60,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._cleanup_container(container_name, process_env)
            return CaptureResult(exit_code=124, events=(), log="capture_runtime_unavailable", observed_count=0)
        if completed.returncode != 0:
            return CaptureResult(exit_code=int(completed.returncode), events=(), log="capture_failed", observed_count=0)
        count = self._receipt_count(completed.stdout or "", snapshot=snapshot)
        if count is None:
            return CaptureResult(exit_code=6, events=(), log="capture_receipt_invalid", observed_count=0)
        return CaptureResult(exit_code=0, events=(), log="capture_receipt_valid", observed_count=count)

    def _cleanup_container(self, name: str, process_env: Mapping[str, str]) -> None:
        """Un délai côté client ne doit pas laisser la capture tourner seule."""
        try:
            removed = self._runner(
                ["docker", "rm", "--force", "--volumes", name],
                env=dict(process_env), text=True, capture_output=True,
                check=False, timeout=15,
            )
            if removed.returncode == 0:
                return
            inspected = self._runner(
                ["docker", "container", "inspect", name],
                env=dict(process_env), text=True, capture_output=True,
                check=False, timeout=15,
            )
            if inspected.returncode != 0 and any(
                marker in (inspected.stderr or "")
                for marker in ("No such object", "No such container")
            ):
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise RuntimeError("qualification Docker container cleanup could not be verified")

    def _container_env(self, boundary: CaptureBoundary, *, snapshot: bool,
                       timeout_seconds: int) -> dict[str, str]:
        config = self.config
        root = config.storage.raw_prefix
        values = {
            "ISERIES_HOST": self._credential.host,
            "ISERIES_USER": self._credential.user,
            "ISERIES_SCHEMA": self._library,
            "ISERIES_TABLE": self._table,
            "AS400_TLS": "true",
            "AS400_SOURCE_TIME_ZONE": config.source.source_time_zone or "",
            "AS400_JOURNAL_LIBRARY": config.source.journal_library,
            "AS400_JOURNAL_NAME": config.source.journal_name,
            "AS400_BOOTSTRAP_RECEIVER": boundary.receiver_name,
            "AS400_BOOTSTRAP_RECEIVER_LIBRARY": boundary.receiver_library,
            "AS400_BOOTSTRAP_SEQUENCE": str(boundary.last_sequence if snapshot else boundary.next_sequence),
            "AS400_BOOTSTRAP_OBSERVED_AT": boundary.observed_at.isoformat(),
            "AS400_RAW_BUCKET": config.storage.bucket,
            "AS400_RAW_PREFIX": root if snapshot else journal_prefix(root, self._table),
            "QUADRINGENT_STORAGE_BACKEND": "gcs" if config.storage.backend == "gcs" else "aws",
        }
        if snapshot:
            values.update({
                "AS400_SNAPSHOT_RUN_ID": config.run_id,
                "AS400_SNAPSHOT_OUTPUT_DIR": "/tmp/quadringent-snapshot",
                "AS400_EVIDENCE_KEY": evidence_key(root, self._table_id, config.run_id),
                "AS400_PIPELINE_ID": self._table_id,
                "AS400_TABLE_ID": self._table_id,
            })
        else:
            stream = f"qual-{config.run_id}-{config.source.journal_library}.{config.source.journal_name}"
            values.update({
                "AS400_STREAM_KEY": stream.lower(),
                "AS400_READER_TIMEOUT_SECONDS": str(timeout_seconds),
                "AS400_RETRIEVE_TIMEOUT_MS": "10000",
                "AS400_RECEIPTED_SCANS": "true",
                "AS400_POLL_SECONDS": "1",
                "AS400_MIN_POLL_SECONDS": "1",
                "AS400_CHECKPOINT_TABLE" if config.storage.backend == "s3" else "AS400_CHECKPOINT_BUCKET": (
                    config.storage.checkpoint_location or ""
                ),
            })
        return values

    def _receipt_count(self, stdout: str, *, snapshot: bool) -> int | None:
        if len(stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            return None
        expected = "initial_copy_completed" if snapshot else "capture_finished"
        receipts: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
            if parsed.get("event") == expected:
                receipts.append(parsed)
        if len(receipts) != 1:
            return None
        receipt = receipts[0]
        if snapshot:
            if receipt.get("run_id") != self.config.run_id or receipt.get("table_id") != self._table_id:
                return None
            count = receipt.get("rows_copied")
        else:
            metrics = receipt.get("metrics")
            if not isinstance(metrics, dict):
                return None
            count = metrics.get("events_published")
        return count if type(count) is int and count >= 0 else None
