#!/usr/bin/env python3
"""Point d'entrée du Job Kubernetes éphémère de découverte de tables.

Lancé par ``KubernetesJobTableDiscoveryClient``
(``quadringent_control_plane.v2.executor.diagnostic_jobs``) avec l'image de
capture — la seule à embarquer le worker Java/JTOpen. Ouvre un
``PersistentJavaWorker`` le temps d'un seul appel ``discover``, imprime la
sortie brute (protocole ligne existant, voir
``quadringent.table_discovery``) sur stdout préfixée par
``quadringent_discover_result=``, puis quitte — aucune connexion IBM i
n'est gardée ouverte au-delà de ce Job.

Le mot de passe n'est **jamais** un argument de commande ni journalisé : il
est lu depuis la variable d'environnement ``ISERIES_PASSWORD``, posée par le
Secret Kubernetes éphémère référencé en ``envFrom`` du conteneur (voir
``executor/manifests.py::build_table_discovery_job``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from quadringent.java_worker import PersistentJavaWorker

RESULT_PREFIX = "quadringent_discover_result="


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} manquant")
    return value


def build_worker(args: argparse.Namespace) -> PersistentJavaWorker:
    return PersistentJavaWorker(
        java=os.environ.get("AS400_JAVA", "java"),
        classpath=_required_env("AS400_JAVA_CLASSPATH"),
        host=args.host,
        user=args.user,
        # ``discover`` n'exige ni schema ni table : le worker Java de
        # diagnostic (``DiagnosticWorker``) ne les lit jamais — aucune
        # valeur fictive à passer au constructeur.
        timeout_seconds=float(os.environ.get("AS400_JAVA_WORKER_TIMEOUT_SECONDS", "30")),
        # Jamais ``AS400_JAVA_WORKER_CLASS`` : l'image capture le fixe au worker
        # de capture, qui exige table, journal et fuseau.
        class_name=os.environ.get("AS400_DIAGNOSTIC_WORKER_CLASS", "io.quadringent.as400.DiagnosticWorker"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--libraries", default=None, help="CSV, défaut : bibliothèque courante du worker")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--search", default=None)
    return parser.parse_args(argv)



def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not os.environ.get("ISERIES_PASSWORD"):
        print("ISERIES_PASSWORD manquant (Secret non monté)", file=sys.stderr)
        return 2
    libraries = tuple(part.strip() for part in args.libraries.split(",") if part.strip()) if args.libraries else None
    worker = build_worker(args)
    try:
        raw_output = worker.discover(libraries=libraries, limit=args.limit, search=args.search)
    except Exception as error:  # noqa: BLE001 — jamais de mot de passe dans le message
        print(f"discover_error={error}", file=sys.stderr)
        return 1
    finally:
        worker.close()
    print(RESULT_PREFIX + json.dumps({"raw_output": raw_output}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
