#!/usr/bin/env python3
"""Mesure la livraison Snowflake de la flotte et publie la preuve console dédiée.

Le lecteur de flotte réécrit ``fleet/console-snapshot.json`` toutes les
~10 s : ce script ne le modifie jamais. Il lit ce document, mesure les
voies déclarées par requêtes en lecture seule, attache un bloc
``destination-proof-v1`` et publie le résultat sous
``fleet/console-proof.json`` — une clé, un écrivain, une écriture
conditionnelle (``IfMatch`` sur l'ETag existant, création sinon).

Le mode par défaut est un dry-run borné : mesure et preuve sont imprimées,
rien n'est écrit. ``--execute`` publie.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if __name__ == "__main__" and _SCRIPT_DIRECTORY in sys.path:
    sys.path.remove(_SCRIPT_DIRECTORY)
    # Le répertoire du script reste importable, mais en dernière priorité :
    # les paquets de src/ gardent l'antériorité (anti-masquage) et les
    # modules frères comme quadringent_autonomous_verify restent trouvables.
    sys.path.append(_SCRIPT_DIRECTORY)
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.fleet_certify_probe import (  # noqa: E402
    CERTIFY_MAX_AGE_SECONDS,
    certify_document,
    measure_table_certification,
    parse_certify_document,
)
from quadringent.fleet_load_ledger import (  # noqa: E402
    FleetLoadLedger,
    fleet_ledger_etag,
    journal_listing,
    load_fleet_ledger,
    manifest_receipt,
    missing_events as _ledger_missing_events,
    refresh_table_ledger,
    save_fleet_ledger,
    unexpected_rows as _ledger_unexpected_rows,
)
from quadringent.fleet_load_probe import (  # noqa: E402
    attach_fleet_destination_proof,
    fleet_console_proof_key,
    fleet_console_proof_s3_uri,
    fleet_console_snapshot_key,
    measure_fleet_load,
)
from quadringent.site_config import current as current_site  # noqa: E402
from quadringent.snowflake_autonomous import publish_autonomous_proof  # noqa: E402
from quadringent_control_plane.model import SourceDescriptor  # noqa: E402
from quadringent_control_plane.projection import project_console_document  # noqa: E402
from quadringent_autonomous_verify import (  # noqa: E402
    _connect_snowflake,
    _publication_client,
)

_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_MAX_PROOF_BYTES = 1024 * 1024


def read_fleet_snapshot(storage, *, site) -> dict[str, object]:
    """Lit le document console de flotte, borné et validé — jamais modifié."""

    key = fleet_console_snapshot_key(site)
    response = storage.get_object(Bucket=site.raw_bucket, Key=key)
    body = response["Body"]
    try:
        size = response.get("ContentLength")
        if type(size) is not int or not 0 < size <= _MAX_SNAPSHOT_BYTES:
            raise ValueError("fleet console snapshot size is invalid")
        payload = body.read(_MAX_SNAPSHOT_BYTES + 1)
        if len(payload) != size:
            raise ValueError("fleet console snapshot length changed")
        document = json.loads(payload)
    finally:
        body.close()
    if (
        not isinstance(document, dict)
        or document.get("format_version") != "as400-console-v1"
    ):
        raise ValueError("fleet console snapshot format is incompatible")
    return document


def _proof_etag(storage, *, site) -> str | None:
    """ETag de la preuve déjà publiée, ou None — une version lisible ou rien."""

    try:
        response = storage.head_object(
            Bucket=site.raw_bucket, Key=fleet_console_proof_key(site)
        )
    except Exception as error:
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    etag = response.get("ETag")
    if not isinstance(etag, str) or not etag.strip():
        raise ValueError("existing fleet proof version is unreadable")
    return etag


def refresh_load_ledger(storage, measurement, *, site, manifest_budget=None):
    """Avance le registre de publication sur le relevé courant.

    Le registre cumule les événements déclarés par les manifestes de lots
    — la population de même portée que le brut chargé. La lecture du
    registre, l'avance bornée par table et la réécriture conditionnelle
    restent dans ce seul script : un écrivain, toujours.
    """

    ledger = load_fleet_ledger(storage, site)
    if ledger is None:
        ledger = FleetLoadLedger()
    loaded_by_table = {
        entry.table: entry.file_rows
        for entry in measurement.tables
        if entry.file_rows is not None
    }
    # Le budget de lectures de manifestes est global au cycle : les voies
    # se le partagent dans l'ordre déclaré — l'amorçage avance sans jamais
    # dépasser le coût S3 alloué à un relevé.
    remaining = manifest_budget
    report = {}
    for entry in measurement.tables:
        listing = journal_listing(storage, site, entry.table)
        kwargs = {} if remaining is None else {"budget": remaining}
        outcome = refresh_table_ledger(
            ledger.table(entry.table),
            listing,
            fetch=lambda key: manifest_receipt(storage, site, key),
            loaded_file_rows=loaded_by_table.get(entry.table),
            **kwargs,
        )
        report[entry.table] = outcome
        if remaining is not None:
            remaining -= outcome["manifests_read"]
    return ledger, report


def read_fleet_proof(storage, *, site) -> dict[str, object] | None:
    """La preuve console déjà publiée — ``None`` si absente ou illisible.

    Lecture support du carry-forward de certification : les mesures
    profondes encore jeunes sont reprises telles quelles au lieu d'être
    refaites à chaque cycle.
    """

    try:
        response = storage.get_object(
            Bucket=site.raw_bucket, Key=fleet_console_proof_key(site)
        )
        body = response["Body"]
        try:
            document = json.loads(body.read(_MAX_PROOF_BYTES + 1))
        finally:
            body.close()
    except Exception:
        return None
    return document if isinstance(document, dict) else None


def collect_certify_documents(
    storage, cursor, measurement, ledger, *, site, now
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """Mesure profonde par voie, embarquée dans ``certify.tables``.

    La certification est une mesure à TTL — scan complet figé par
    ``AT(TIMESTAMP=>T)`` — relue par le pilote de progression depuis la
    preuve console : même clé, même écrivain, aucun droit supplémentaire.
    Une mesure encore jeune (``CERTIFY_MAX_AGE_SECONDS``) est reprise de la
    preuve courante ; une voie dont la mesure courante est incomplète n'est
    pas tentée ; un échec sur une voie ne masque jamais les autres.
    """

    previous = read_fleet_proof(storage, site=site)
    carried: dict[str, object] = {}
    if isinstance(previous, dict):
        block = previous.get("certify")
        if isinstance(block, dict) and isinstance(block.get("tables"), dict):
            carried = block["tables"]
    docs: dict[str, dict[str, object]] = {}
    report: dict[str, str] = {}
    for entry in measurement.tables:
        prior = parse_certify_document(carried.get(entry.table))
        if prior is not None:
            measured_at = _parse_instant(prior.measured_at)
            if (
                measured_at is not None
                and (now - measured_at).total_seconds() < CERTIFY_MAX_AGE_SECONDS
            ):
                docs[entry.table] = carried[entry.table]
                report[entry.table] = "fresh"
                continue
        if entry.error is not None or entry.file_rows is None:
            report[entry.table] = "measure_incomplete"
            continue
        table_ledger = ledger.tables.get(entry.table)
        if table_ledger is None:
            report[entry.table] = "ledger_missing"
            continue
        missing = _ledger_missing_events(table_ledger, entry.file_rows)
        unexpected = _ledger_unexpected_rows(table_ledger, entry.file_rows)
        if missing is None or unexpected is None:
            report[entry.table] = "ledger_incomplete"
            continue
        certification = measure_table_certification(
            cursor,
            site,
            entry.table,
            missing=missing,
            extra=unexpected,
            pending_files=entry.pending_files,
            end=now,
        )
        if certification is None:
            report[entry.table] = "measure_failed"
            continue
        docs[entry.table] = certify_document(certification)
        report[entry.table] = "measured"
    return docs, report


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def prepare_fleet_proof(
    storage, cursor, *, site, observed_at, manifest_budget=None
):
    """Lit, mesure, avance le registre, attache et valide — sans publier."""

    document = read_fleet_snapshot(storage, site=site)
    measurement = measure_fleet_load(cursor, site)
    ledger, _ledger_report = refresh_load_ledger(
        storage, measurement, site=site, manifest_budget=manifest_budget
    )
    combined = attach_fleet_destination_proof(
        document,
        measurement,
        site=site,
        load_ledger=ledger,
        observed_at=observed_at,
    )
    flux = document.get("flux")
    flux_id = flux.get("id") if isinstance(flux, dict) else None
    if not isinstance(flux_id, str) or not flux_id:
        raise ValueError("fleet console snapshot identity is missing")
    # La mesure profonde est embarquée dans la même preuve : une voie en
    # échec laisse sa mesure absente, jamais une valeur fabriquée.
    certify_docs: dict[str, dict[str, object]] = {}
    certify_report: dict[str, str] = {}
    try:
        certify_docs, certify_report = collect_certify_documents(
            storage,
            cursor,
            measurement,
            ledger,
            site=site,
            now=observed_at,
        )
    except Exception:
        certify_report = {"_cycle": "failed"}
    if certify_docs:
        combined["certify"] = {
            "observed_at": observed_at.isoformat(),
            "tables": certify_docs,
        }
    # Le document publié doit rester consommable par la projection du site :
    # une preuve qui échouerait ici ne partirait jamais vers la console.
    project_console_document(
        combined,
        SourceDescriptor(
            flux_id, "live", site.environment, fleet_console_proof_s3_uri(site)
        ),
        now=observed_at,
    )
    payload = (
        json.dumps(combined, sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n"
    )
    if len(payload) > _MAX_PROOF_BYTES:
        raise ValueError("combined fleet proof exceeds the read budget")
    return combined, measurement, payload, ledger, certify_report


def publish_prepared_fleet_proof(storage, payload, etag, *, site):
    """Publication conditionnelle sur la clé dédiée — jamais sur le snapshot."""

    if not isinstance(payload, bytes) or not payload:
        raise ValueError("fleet proof payload must be non-empty bytes")
    if etag is not None and (not isinstance(etag, str) or not etag.strip()):
        raise ValueError("invalid conditional proof publication")
    return publish_autonomous_proof(
        storage,
        fleet_console_proof_s3_uri(site),
        payload,
        expected=fleet_console_proof_s3_uri(site),
        expected_etag=etag,
        create_only=etag is None,
    )


def _status(combined, measurement) -> dict[str, object]:
    proof = combined["destination_proof"]
    return {
        "activation": proof["activation"]["state"],
        "blocker_code": proof["activation"]["blocker_code"],
        "load": proof["load"]["state"],
        "destination": proof["destination"]["state"],
        "reconciliation": proof["reconciliation"]["state"],
        "tables": len(measurement.tables),
        "measured_tables": sum(
            1 for entry in measurement.tables if entry.error is None
        ),
        "pipes_running": sum(
            1 for entry in measurement.tables if entry.pipe_state == "RUNNING"
        ),
        "loaded_events": proof["reconciliation"].get("loaded_event_count"),
        "captured_events": proof["reconciliation"].get("captured_event_count"),
        "unreceipted_events": (
            combined.get("destination", {}).get("ledger") or {}
        ).get("unreceipted_event_count"),
        "ledger_tables_complete": (
            combined.get("destination", {}).get("ledger") or {}
        ).get("tables_complete"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    site = current_site()
    snowflake_authentication = parser.add_mutually_exclusive_group()
    snowflake_authentication.add_argument(
        "--connection-name", default=site.snowflake_connection
    )
    snowflake_authentication.add_argument(
        "--snowflake-oidc-token-file",
        help="Use the dedicated site workload identity with a projected OIDC token file",
    )
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--aws-profile", default=site.aws_profile)
    authentication.add_argument(
        "--aws-default-credentials",
        action="store_true",
        help="Use the SDK credential chain without forcing a local SSO profile",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="publish the combined proof to fleet/console-proof.json; default is a bounded dry-run",
    )
    parser.add_argument(
        "--manifest-budget",
        type=int,
        default=None,
        help="cap on new manifest reads this cycle; default keeps the module bound",
    )
    args = parser.parse_args(argv)
    if args.manifest_budget is not None and args.manifest_budget < 0:
        parser.error("--manifest-budget must be a non-negative integer")
    publication_status = "not_attempted"
    try:
        import boto3
        import snowflake.connector

        profile = None if args.aws_default_credentials else args.aws_profile
        storage = _publication_client(boto3, profile)
        connection = _connect_snowflake(
            snowflake.connector,
            None if args.snowflake_oidc_token_file else args.connection_name,
            oidc_token_file=args.snowflake_oidc_token_file,
        )
        try:
            cursor = connection.cursor()
            try:
                observed = datetime.now(UTC)
                (
                    combined,
                    measurement,
                    payload,
                    ledger,
                    certify_report,
                ) = prepare_fleet_proof(
                    storage,
                    cursor,
                    site=site,
                    observed_at=observed,
                    manifest_budget=args.manifest_budget,
                )
            finally:
                cursor.close()
        finally:
            connection.close()
        if args.execute:
            publication_status = "unknown"
            # Le registre n'est persisté que sur exécution : un dry-run
            # n'écrit rien — ni preuve, ni état de mesure.
            save_fleet_ledger(
                storage,
                site,
                ledger,
                expected_etag=fleet_ledger_etag(storage, site),
            )
            etag = _proof_etag(storage, site=site)
            publish_prepared_fleet_proof(storage, payload, etag, site=site)
            publication_status = "confirmed"
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "publication_status": publication_status,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3
    print(
        json.dumps(
            {
                "status": "observed" if args.execute else "dry_run",
                "published": bool(args.execute),
                "publication_status": publication_status,
                "proof_key": fleet_console_proof_key(site),
                "snapshot_key": fleet_console_snapshot_key(site),
                "certify": certify_report,
                **_status(combined, measurement),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
