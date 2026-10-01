"""Load S3-staged proof-lane JSONL into the declared Snowflake destination.

Capture and load stay decoupled: this module never reads IBM i and never
advances a journal checkpoint. It only consumes an already-published capture
snapshot plus a bounded Snowflake cursor. Every prefix, stage and identifier
guard compares against the site-declared values carried by ``site`` — nothing
is pinned to an installation literal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from .destination_proof import attach_snowflake_proof
from .site_config import SiteConfig
from .snowflake_loader import _stage_path, assert_declared_destination
from .snowflake_replay import (
    SnowflakeReplayConfig,
    execute_external_files_replay,
    execute_external_replay,
    execute_external_stage_replay,
)


class DestinationSyncError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def assert_dedicated_sale_object(
    object_key: str,
    *,
    required_prefix: str,
    forbidden_fragments: Sequence[str],
) -> None:
    """Refuse any object escaping the dedicated proof lane or its markers."""

    if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
        raise DestinationSyncError("invalid_object_key", "La clé d'objet staging est invalide")
    lowered = object_key.lower()
    if any(str(fragment).lower() in lowered for fragment in forbidden_fragments):
        raise DestinationSyncError("forbidden_prefix", "Un préfixe interdit est présent")
    if not (
        object_key == required_prefix or object_key.startswith(required_prefix + "/")
    ):
        raise DestinationSyncError(
            "prefix_forbidden",
            "La clé d'objet doit rester sous le préfixe dédié déclaré",
        )


def assert_dedicated_sale_stage(stage: str, *, expected: str) -> None:
    if stage != expected:
        raise DestinationSyncError(
            "stage_forbidden",
            "Le stage Snowflake doit être le stage dédié déclaré",
        )


def sale_stage_relative_key(
    object_key: str,
    *,
    required_prefix: str,
    forbidden_fragments: Sequence[str],
) -> str:
    """Strip the dedicated prefix so COPY stays relative to the stage URL."""

    assert_dedicated_sale_object(
        object_key,
        required_prefix=required_prefix,
        forbidden_fragments=forbidden_fragments,
    )
    prefix = required_prefix + "/"
    if not object_key.startswith(prefix):
        raise DestinationSyncError("invalid_object_key", "La clé d'objet staging est invalide")
    relative = object_key[len(prefix) :]
    if not relative or not relative.lower().endswith(".jsonl"):
        raise DestinationSyncError("invalid_object_key", "La clé d'objet staging est invalide")
    try:
        return _stage_path(relative)
    except ValueError:
        raise DestinationSyncError("invalid_object_key", "La clé d'objet staging est invalide") from None


def sync_captured_destination(
    cursor: Any,
    capture_document: Mapping[str, object],
    config: SnowflakeReplayConfig,
    *,
    run_tag: str,
    observed_at: datetime,
    site: SiteConfig,
    all_jsonl: bool = False,
    object_keys: Sequence[str] | None = None,
) -> dict[str, object]:
    """Execute one bounded replay and return a combined snapshot, or fail closed."""

    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    assert_declared_destination(
        site.snowflake_scope,
        config.database,
        config.schema,
        config.stage,
        config.raw_table,
        config.canonical_table,
    )
    if config.scope != site.snowflake_scope:
        raise DestinationSyncError(
            "scope_forbidden",
            "La configuration du rejeu doit cibler la destination déclarée",
        )
    assert_dedicated_sale_stage(config.stage, expected=site.proof_stage)
    relative_keys: tuple[str, ...] | None = None
    if object_keys is not None:
        if all_jsonl:
            raise DestinationSyncError("invalid_object_key", "La clé d'objet staging est invalide")
        relative_keys = tuple(
            sale_stage_relative_key(
                key,
                required_prefix=site.stream_prefix,
                forbidden_fragments=site.forbidden_fragments,
            )
            for key in object_keys
        )
        if not relative_keys:
            raise DestinationSyncError("invalid_object_key", "La clé d'objet staging est invalide")
    elif not all_jsonl:
        assert_dedicated_sale_object(
            config.object_key,
            required_prefix=site.stream_prefix,
            forbidden_fragments=site.forbidden_fragments,
        )
    try:
        if relative_keys is not None:
            metrics = execute_external_files_replay(cursor, config, relative_keys)
        elif all_jsonl:
            metrics = execute_external_stage_replay(cursor, config)
        else:
            metrics = execute_external_replay(cursor, config)
    except DestinationSyncError:
        raise
    except OSError:
        raise DestinationSyncError("store_unavailable", "Stockage ou stage indisponible") from None
    except Exception:
        raise DestinationSyncError("snowflake_unavailable", "Destination Snowflake indisponible") from None

    if metrics.get("status") != "PASS":
        raise DestinationSyncError(
            "unreconciled",
            "La réconciliation Snowflake n'est pas prouvée",
        )
    try:
        return attach_snowflake_proof(
            capture_document,
            metrics,
            run_tag=run_tag,
            observed_at=observed_at,
            site=site,
        )
    except ValueError:
        raise DestinationSyncError(
            "unreconciled",
            "La preuve destination est contradictoire ou incomplète",
        ) from None
