#!/usr/bin/env python3
"""Provision the declared fleet raw lanes in the declared destination.

Dry-run is the default. Execution only creates site-declared objects: the product prefix
in the storage integration, then one stage, one raw table and one auto-ingest
pipe per new lane. Pre-existing lanes are verified, never rewritten.

Verification reads back Snowflake: each stage must cover its table prefix, each
raw table must exist, and each pipe must carry an SQS channel and a definition
that names exactly its own stage and raw table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.fleet_destination import FleetDestinationPlan
from quadringent.site_config import current as current_site


def _site():
    return current_site()


def _dependency() -> object:
    try:
        import snowflake.connector  # noqa: PLC0415
    except ImportError:
        print("ERROR: snowflake-connector-python is required", file=sys.stderr)
        raise SystemExit(1)
    return snowflake.connector


def _connect(connection_name: str) -> object:
    if not isinstance(connection_name, str) or not connection_name.strip():
        raise SystemExit(
            "ERROR: --connection-name or QUADRINGENT_SNOWFLAKE_CONNECTION is required"
        )
    connector = _dependency()
    return connector.connect(
        connection_name=connection_name,
        login_timeout=20,
        network_timeout=60,
        session_parameters={"QUERY_TAG": f"QUADRINGENT_FLEET_DESTINATION_SETUP_{_site().fleet_environment}"},
    )


def _rows_by_name(cursor: object, sql: str, key: str) -> dict[str, dict[str, object]]:
    cursor.execute(sql)
    columns = [str(column[0]).lower() for column in cursor.description]
    result: dict[str, dict[str, object]] = {}
    for row in cursor.fetchall():
        record = dict(zip(columns, row))
        name = str(record.get(key, ""))
        if not name:
            raise RuntimeError(f"readback without {key}: {sql}")
        result[name] = record
    return result


def _pipe_status(cursor: object, database: str, schema: str, pipe: str) -> str:
    cursor.execute(f"SELECT SYSTEM$PIPE_STATUS('{database}.{schema}.{pipe}') AS status")
    payload = cursor.fetchall()[0][0]
    try:
        document = json.loads(str(payload))
    except (TypeError, ValueError):
        return "UNREADABLE"
    state = document.get("executionState")
    return str(state) if state else "UNKNOWN"


def _existing_pipes(cursor: object, plan: FleetDestinationPlan) -> set[str]:
    """Noms des tuyaux déjà présents avant toute écriture."""

    cursor.execute(f'SHOW PIPES IN SCHEMA "{plan.database}"."{plan.schema}"')
    columns = [str(column[0]).lower() for column in cursor.description]
    index = columns.index("name")
    return {str(row[index]) for row in cursor.fetchall()}


def verify(cursor: object, plan: FleetDestinationPlan) -> list[dict[str, object]]:
    """Constater par relecture l'effet réel, table par table."""

    stages = _rows_by_name(cursor, f'SHOW STAGES IN SCHEMA "{plan.database}"."{plan.schema}"', "name")
    cursor.execute(
        "SELECT TABLE_NAME FROM "
        f"{plan.database}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA = '{plan.schema}'"
    )
    tables = {str(row[0]) for row in cursor.fetchall()}
    pipes = _rows_by_name(cursor, f'SHOW PIPES IN SCHEMA "{plan.database}"."{plan.schema}"', "name")
    views: set[str] = set()
    cursor.execute(
        "SELECT TABLE_NAME FROM "
        f"{plan.database}.INFORMATION_SCHEMA.VIEWS "
        f"WHERE TABLE_SCHEMA = '{plan.schema}'"
    )
    views = {str(row[0]) for row in cursor.fetchall()}

    lanes: list[dict[str, object]] = []
    for objects in plan.objects():
        stage = stages.get(objects.stage)
        if stage is None:
            raise RuntimeError(f"missing stage for a fleet lane: {objects.stage}")
        stage_url = str(stage.get("url", ""))
        if not plan.covers(stage_url, objects.table):
            raise RuntimeError(f"stage does not cover its table prefix: {objects.stage}")
        if objects.raw_table not in tables:
            raise RuntimeError(f"missing raw table for a fleet lane: {objects.raw_table}")
        # Sans vue canonique, le rejeu metier n'a rien a lire : le chargement
        # brut est necessaire mais pas suffisant.
        if objects.canonical_view not in views:
            raise RuntimeError(f"missing canonical view for a fleet lane: {objects.canonical_view}")
        pipe = pipes.get(objects.pipe)
        if pipe is None:
            raise RuntimeError(f"missing pipe for a fleet lane: {objects.pipe}")
        if str(pipe.get("pattern", "")) != ".*[.]jsonl$":
            raise RuntimeError(f"pipe pattern is not the JSONL lane: {objects.pipe}")
        channel = str(pipe.get("notification_channel", ""))
        if not channel.startswith("arn:aws:sqs:"):
            raise RuntimeError(f"pipe has no SQS notification channel: {objects.pipe}")
        definition = str(pipe.get("definition", ""))
        for expected in (objects.raw_table, objects.stage):
            if expected not in definition:
                raise RuntimeError(f"pipe definition lost its own target: {objects.pipe}")
        lanes.append(
            {
                "table": objects.table,
                "prefix": plan.table_prefix(objects.table),
                "stage": objects.stage,
                "stage_url": stage_url,
                "raw_table": objects.raw_table,
                "canonical_view": objects.canonical_view,
                "pipe": objects.pipe,
                "notification_channel": channel,
                "execution_state": _pipe_status(cursor, plan.database, plan.schema, objects.pipe),
                "pre_existing": objects.table in plan.pre_existing_lanes,
            }
        )
    return lanes


def _pause_everything(cursor: object, plan: FleetDestinationPlan) -> list[str]:
    failures: list[str] = []
    for statement in plan.pause_statements():
        try:
            cursor.execute(statement)
        except Exception as error:  # noqa: BLE001 - reprise bornée, jamais masquée
            failures.append(type(error).__name__)
    return failures


def _run(
    plan: FleetDestinationPlan,
    connection_name: str,
    rollback: bool,
    resume: bool,
) -> int:
    site = plan.site
    confirmation = site.confirmation_token('FLEET_DESTINATION')
    rollback_confirmation = f'ROLLBACK_{confirmation}'
    resume_confirmation = f'RESUME_{confirmation}'
    connection = _connect(connection_name)
    try:
        cursor = connection.cursor()
        if resume:
            failures: list[str] = []
            for statement in plan.resume_statements():
                try:
                    cursor.execute(statement)
                except Exception as error:  # noqa: BLE001 - reprise bornée, jamais masquée
                    failures.append(type(error).__name__)
            lanes = [] if failures else verify(cursor, plan)
            print(
                json.dumps(
                    {
                        "status": "RESUMED" if not failures else "ERROR",
                        "environment": site.environment,
                        "operation": "resume",
                        "resume_failures": failures,
                        "lanes": lanes,
                        "confirmation": resume_confirmation,
                    },
                    sort_keys=True,
                )
            )
            return 0 if not failures else 1
        if rollback:
            failures = _pause_everything(cursor, plan)
            print(
                json.dumps(
                    {
                        "status": "PAUSED" if not failures else "ERROR",
                        "environment": site.environment,
                        "operation": "rollback",
                        "pause_failures": failures,
                        "confirmation": rollback_confirmation,
                    },
                    sort_keys=True,
                )
            )
            return 0 if not failures else 1
        # Une réinstallation ne doit pas suspendre un flux déjà en service :
        # seuls les tuyaux absents avant cette exécution sont créés puis
        # suspendus.
        before = _existing_pipes(cursor, plan)
        created = tuple(
            table
            for table in plan.new_lanes()
            if plan.table_objects(table).pipe not in before
        )
        statements = plan.statements() + plan.pause_new_pipes_statements(created)
        for index, statement in enumerate(statements):
            try:
                cursor.execute(statement)
            except Exception as error:  # noqa: BLE001 - l'index est la preuve utile
                _pause_everything(cursor, plan)
                print(
                    json.dumps(
                        {
                            "status": "ERROR",
                            "environment": site.environment,
                            "failed_statement_index": index,
                            "statement_count": len(statements),
                            "error_type": type(error).__name__,
                            "error_code": getattr(error, "errno", None),
                        },
                        sort_keys=True,
                    )
                )
                return 1
        # Best effort : un warehouse deja suspendu ne doit pas faire echouer
        # une installation dont tous les objets sont, eux, constates.
        suspend_error: str | None = None
        try:
            suspend = plan.suspend_warehouse_statement()
            if suspend is not None:
                cursor.execute(suspend)
        except Exception as error:  # noqa: BLE001 - etat deja atteint attendu
            suspend_error = type(error).__name__
        lanes = verify(cursor, plan)
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "status": "APPLIED",
                "environment": site.environment,
                "operation": "install",
                "statement_count": len(plan.statements()),
                "created_lanes": list(created),
                "table_count": len(plan.tables),
                "new_lanes": list(plan.new_lanes()),
                "pre_existing_lanes": list(plan.pre_existing_lanes),
                "verification": "READBACK_OK",
                "warehouse_suspend": suspend_error or "APPLIED",
                "lanes": lanes,
                "confirmation": confirmation,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    site = _site()
    parser.add_argument("--connection-name", default=site.snowflake_connection)
    parser.add_argument("--tables", default=",".join(site.fleet_tables))
    parser.add_argument(
        "--pre-existing-lanes", default=",".join(site.provisioned_stages)
    )
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    tables = tuple(item.strip() for item in args.tables.split(",") if item.strip())
    lanes = tuple(
        item.strip() for item in args.pre_existing_lanes.split(",") if item.strip()
    )
    try:
        plan = FleetDestinationPlan(tables=tables, pre_existing_lanes=lanes, site=site)
    except ValueError as error:
        print(
            json.dumps(
                {"status": "ERROR", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    confirmation = site.confirmation_token('FLEET_DESTINATION')
    rollback_confirmation = f'ROLLBACK_{confirmation}'
    resume_confirmation = f'RESUME_{confirmation}'
    if args.rollback and args.resume:
        print("ERROR: --rollback and --resume are exclusive", file=sys.stderr)
        return 2
    if args.rollback and args.execute and args.confirm != rollback_confirmation:
        print("ERROR: exact site rollback confirmation is required", file=sys.stderr)
        return 2
    if args.resume and args.execute and args.confirm != resume_confirmation:
        print("ERROR: exact site resume confirmation is required", file=sys.stderr)
        return 2
    if not args.rollback and not args.resume and args.execute and args.confirm != confirmation:
        print("ERROR: exact site confirmation is required", file=sys.stderr)
        return 2

    if args.verify_only:
        connection = _connect(args.connection_name)
        try:
            document = {
                "status": "VERIFIED",
                "environment": site.environment,
                "operation": "verify",
                "lanes": verify(connection.cursor(), plan),
            }
        except Exception as error:  # noqa: BLE001 - diagnostic borné
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "operation": "verify",
                        "error_type": type(error).__name__,
                        "error_code": getattr(error, "errno", None),
                    },
                    sort_keys=True,
                )
            )
            return 1
        finally:
            connection.close()
        return _emit(document, args.report)

    if not args.execute:
        operation = "rollback" if args.rollback else ("resume" if args.resume else "install")
        return _emit(
            {
                "status": "DRY_RUN",
                "environment": site.environment,
                "operation": operation,
                "table_count": len(plan.tables),
                "new_lanes": list(plan.new_lanes()),
                "pre_existing_lanes": list(plan.pre_existing_lanes),
                "statement_count": len(
                    plan.pause_statements()
                    if args.rollback
                    else (plan.resume_statements() if args.resume else plan.statements())
                ),
                "integration_prefix": f"s3://{plan.bucket}/{plan.product_prefix}/",
                "confirmation": (
                    rollback_confirmation
                    if args.rollback
                    else (resume_confirmation if args.resume else confirmation)
                ),
            },
            args.report,
        )

    return _run(plan, args.connection_name, args.rollback, args.resume)


def _emit(document: dict[str, object], report: str) -> int:
    payload = json.dumps(document, sort_keys=True)
    if report:
        Path(report).write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
