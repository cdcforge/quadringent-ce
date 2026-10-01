#!/usr/bin/env python3
"""Génère un sidecar UI Quadringent à partir d'un catalogue metadata-only."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Le répertoire scripts/ contient aussi quadringent_control_plane.py : le retirer
# évite qu'il masque le package src/quadringent_control_plane.
_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if _SCRIPT_DIRECTORY in sys.path:
    sys.path.remove(_SCRIPT_DIRECTORY)
ROOT = Path(__file__).resolve().parents[1]
src = str(ROOT / "src")
if src not in sys.path:
    sys.path.insert(0, src)

from quadringent_control_plane.fleet import FleetError, MAX_CONCURRENCY  # noqa: E402
from quadringent_control_plane.fleet_sidecar import (  # noqa: E402
    generate_fleet_ui_sidecar_file,
    load_history_progress_documents,
)


def _s3_client(region: str) -> object:
    """Client S3 de lecture bornée — ``boto3`` reste une dépendance optionnelle."""

    import boto3  # noqa: PLC0415

    return boto3.client("s3", region_name=region)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Génère le sidecar UI Quadringent (metadata-only, sans ligne métier)"
    )
    parser.add_argument("--catalog", required=True, help="Catalogue metadata-only JSON")
    parser.add_argument("--output", required=True, help="Fichier sidecar JSON à écrire")
    parser.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY)
    parser.add_argument("--historical-byte-budget", type=int, default=None)
    parser.add_argument(
        "--history-progress",
        action="store_true",
        help=(
            "Relit les documents history-progress du site déclaré "
            "(bucket/préfixe issus de QUADRINGENT_*)"
        ),
    )
    try:
        arguments = parser.parse_args(argv)
        history_progress = None
        if arguments.history_progress:
            # Le périmètre de lecture (bucket, préfixe) est celui du site
            # déclaré ; la relecture est bornée par le contrat du module.
            from quadringent.site_config import current as current_site  # noqa: PLC0415

            history_progress = load_history_progress_documents(
                _s3_client(current_site().aws_region)
            )
        generate_fleet_ui_sidecar_file(
            arguments.catalog,
            arguments.output,
            max_concurrency=arguments.concurrency,
            historical_byte_budget=arguments.historical_byte_budget,
            history_progress=history_progress,
        )
    except FleetError as error:
        print(error.safe_message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
