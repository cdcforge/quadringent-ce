"""CLI ``qualification run`` / ``qualification report``.

Invocation directe (le paquet n'est pas dans le wheel d'exécution, voir
``pyproject.toml``) :

    python -m quadringent_qualification.cli run --config path/to/run.yaml --steps all
    python -m quadringent_qualification.cli report --run-json run/<run_id>/report.json

``run`` construit les adaptateurs réels via :func:`build_real_adapters` ; le
runner doit disposer de Docker, d'une identité cloud et des secrets référencés
par la configuration. ``--offline-fake`` utilise des adaptateurs en mémoire
pour vérifier le câblage sans système externe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .adapters import CaptureRunner, SourceDriver, StorageBackend, WarehouseLoader
from .config import ConfigError, RunConfig, load_config
from .orchestrator import Orchestrator, RunReport
from .report import render_markdown, to_json, to_markdown


class AdapterWiringError(RuntimeError):
    """Levée si la configuration ou les dépendances du runner réel manquent."""


def build_real_adapters(config: RunConfig) -> tuple[SourceDriver, CaptureRunner, StorageBackend, WarehouseLoader]:
    """Compose les quatre adaptateurs réels sans effectuer de mutation source.

    Imports différés : un essai ``--offline-fake`` ne requiert ni les SDK
    cloud ni le connecteur Snowflake du runner de qualification.
    """
    try:
        from .real_capture import DockerCaptureRunner
        from .real_source import DockerIbmiSourceDriver
        from .real_storage import CloudQualificationStorage
        from .real_warehouse import SnowflakeQualificationWarehouse

        source = DockerIbmiSourceDriver(config)
        capture = DockerCaptureRunner(config)
        storage = CloudQualificationStorage(config.storage)
        warehouse = SnowflakeQualificationWarehouse(config)
    except Exception:
        # Les exceptions d'un SDK ou d'un fichier secret peuvent contenir
        # des identifiants ; la CLI n'en recopie jamais le texte.
        raise AdapterWiringError("adaptateur réel indisponible ou configuration incomplète") from None
    return source, capture, storage, warehouse


def _build_offline_fake_adapters(config: RunConfig) -> tuple[SourceDriver, CaptureRunner, StorageBackend, WarehouseLoader]:
    # Import tardif : ce module de fakes vit sous tests/ et ne doit pas être un
    # import obligatoire pour un usage CLI normal (adaptateurs réels).
    root = Path(__file__).resolve().parents[2]
    tests_dir = root / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    from qualification.fakes import (  # type: ignore[import-not-found]
        FakeCaptureRunner,
        FakeSourceDriver,
        FakeStorageBackend,
        FakeWarehouseLoader,
    )

    return (
        FakeSourceDriver(primary_key=config.table.primary_key),
        FakeCaptureRunner(),
        FakeStorageBackend(),
        FakeWarehouseLoader(),
    )


def _default_out_dir(config: RunConfig) -> Path:
    return Path("run") / config.run_id


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config, env=os.environ)
    except ConfigError as error:
        print(f"configuration invalide : {error}", file=sys.stderr)
        return 2

    try:
        steps = config.resolved_steps(args.steps)
    except ConfigError as error:
        print(f"étapes invalides : {error}", file=sys.stderr)
        return 2

    try:
        if args.offline_fake:
            source, capture, storage, warehouse = _build_offline_fake_adapters(config)
        else:
            source, capture, storage, warehouse = build_real_adapters(config)
    except AdapterWiringError as error:
        print(str(error), file=sys.stderr)
        return 3

    orchestrator = Orchestrator(config, source=source, capture=capture, storage=storage, warehouse=warehouse,
                                execution_mode="offline_fake" if args.offline_fake else "real")
    report = orchestrator.run(steps)

    out_dir = Path(args.out_dir) if args.out_dir else _default_out_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(to_json(report), encoding="utf-8")
    (out_dir / "report.md").write_text(to_markdown(report), encoding="utf-8")
    print(f"run {config.run_id} — étapes sélectionnées : {report.status} ; "
          f"qualification produit complète : NOT_VALIDATED — rapport écrit sous {out_dir}")
    return 0 if report.status == "PASS" else 1


def cmd_report(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.run_json).read_text(encoding="utf-8"))
    markdown = render_markdown(data)
    if args.out_markdown:
        Path(args.out_markdown).write_text(markdown, encoding="utf-8")
        print(f"résumé Markdown écrit sous {args.out_markdown}")
    else:
        print(markdown)
    return 0 if data.get("status") == "PASS" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qualification")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="exécute un run de qualification")
    run_parser.add_argument("--config", required=True, help="fichier de configuration YAML ou JSON")
    run_parser.add_argument("--steps", default="all", help="'all' ou une liste séparée par des virgules")
    run_parser.add_argument("--out-dir", default=None, help="répertoire de sortie (défaut : run/<run_id>)")
    run_parser.add_argument("--offline-fake", action="store_true",
                             help="utilise des adaptateurs en mémoire pour vérifier le câblage sans système externe")
    run_parser.set_defaults(func=cmd_run)

    report_parser = subparsers.add_parser("report", help="régénère le résumé Markdown d'un rapport JSON existant")
    report_parser.add_argument("--run-json", required=True, help="fichier report.json produit par 'run'")
    report_parser.add_argument("--out-markdown", default=None, help="fichier de sortie (défaut : stdout)")
    report_parser.set_defaults(func=cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
