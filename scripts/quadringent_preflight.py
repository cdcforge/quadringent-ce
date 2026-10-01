#!/usr/bin/env python3
"""Diagnostic pré-vol Quadringent : vérifie que le site déclaré est joignable.

Le client doit voir les échecs d'installation avant que la capture ne démarre,
pas comme des erreurs obscures au premier poll. Chaque contrôle est borné,
rapporte une ligne actionnable et le verdict global est fail-closed : le code
de sortie ne vaut 0 que si tous les contrôles requis passent.

Contrôles : joignabilité TCP IBM i (ports journal/DRDA), sonde d'écriture et
de lecture S3 sous le préfixe brut déclaré, accès à la table DynamoDB de
checkpoints, destination Snowflake lorsque des identifiants sont configurés,
et capacité du ServiceAccount du pod à créer des Jobs batch (informatif : le
lancement de flotte s'appuie sur l'identité du control plane).

Aucun secret n'est affiché : les messages ne portent que des métadonnées
publiques (hôte, ports, bucket, table, namespace).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if __name__ == "__main__" and _SCRIPT_DIRECTORY in sys.path:
    sys.path.remove(_SCRIPT_DIRECTORY)
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.site_config import (  # noqa: E402
    SiteConfig,
    SiteConfigurationError,
    current as current_site,
)
from quadringent.object_store import S3ObjectStore  # noqa: E402
from quadringent_control_plane.k8s_jobs import (  # noqa: E402
    SERVICE_ACCOUNT_ROOT,
    JobsApiError,
    JobsResponse,
    ServiceAccountContext,
    https_transport,
)

STATUSES = frozenset({"OK", "FAIL", "SKIP"})

# Ports JTOpen résolus comme le lecteur Java : les surcharges
# AS400_*_PORT priment, sinon les défauts TLS/plaintext du driver.
_TLS_PORTS = (("database", 9471), ("commande", 9475), ("signon", 9476))
_PLAIN_PORTS = (("database", 8471), ("commande", 8475), ("signon", 8476))
_PORT_ENV = {
    "database": "AS400_DATABASE_PORT",
    "commande": "AS400_COMMAND_PORT",
    "signon": "AS400_SIGNON_PORT",
}

# Sonde bornée : contenu déterministe pour que put_once reste idempotent
# entre deux exécutions du diagnostic.
_S3_PROBE_KEY = "preflight/sonde-preflight.json"
_S3_PROBE_CONTENT = (
    b'{"format_version":"quadringent-preflight-sonde-v1","role":"connectivity"}\n'
)

_DDB_PROBE_STREAM_ID = "quadringent-preflight-sonde"
_SNOWFLAKE_PROBE_STAGE = "QUADRINGENT_PREFLIGHT_SONDE"

_SSAR_PATH = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"


@dataclass(frozen=True)
class CheckResult:
    """Verdict d'un contrôle : statut borné et message actionnable."""

    name: str
    label: str
    status: str
    message: str
    required: bool = True
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"statut de contrôle invalide : {self.status}")

    def as_record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Providers:
    """Dépendances réelles injectées — les tests substituent des faussaires."""

    opener: Callable[..., Any]
    session: Any
    aws_error: str | None
    snowflake_connector: Any
    sa_root: str | Path
    transport_factory: Callable[..., JobsResponse]
    clock: Callable[[], float]


def _error_code(error: BaseException) -> str:
    """Code d'erreur AWS normalisé, sans texte distant ni secret."""

    return str(
        getattr(error, "response", {}).get("Error", {}).get("Code", "")
    )


def _aws_config(timeout_seconds: float) -> Any:
    """Configuration botocore bornée ; absente si botocore n'est pas là."""

    try:
        from botocore.config import Config
    except ImportError:
        return None
    return Config(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"max_attempts": 1, "mode": "standard"},
    )


def _ibmi_ports(environ: Mapping[str, str]) -> tuple[tuple[str, int], ...]:
    """Ports journal/DRDA attendus : défauts TLS, surcharges déclarées."""

    tls = environ.get("AS400_TLS", "true").strip().lower() != "false"
    defaults = dict(_TLS_PORTS if tls else _PLAIN_PORTS)
    resolved = []
    for service in ("database", "commande", "signon"):
        raw = environ.get(_PORT_ENV[service], "").strip()
        if raw:
            try:
                port = int(raw)
            except ValueError:
                raise ValueError(f"{_PORT_ENV[service]} doit être un entier") from None
            if not 1 <= port <= 65535:
                raise ValueError(f"{_PORT_ENV[service]} doit rester entre 1 et 65535")
        else:
            port = defaults[service]
        resolved.append((service, port))
    return tuple(resolved)


def check_ibmi_tcp(
    site: SiteConfig,
    environ: Mapping[str, str],
    timeout_seconds: float,
    *,
    opener: Callable[..., Any] = socket.create_connection,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Joignabilité TCP des services journaux IBM i — aucun secret requis."""

    started = clock()
    name, label = "ibmi_tcp", "IBM i (TCP)"
    host = site.ibmi_host
    try:
        ports = _ibmi_ports(environ)
    except ValueError as error:
        return CheckResult(name, label, "FAIL", str(error), duration_ms=_elapsed(clock, started))
    unreachable = []
    for service, port in ports:
        try:
            connection = opener((host, port), timeout=timeout_seconds)
        except (OSError, TimeoutError):
            unreachable.append(f"{service}:{port}")
            continue
        try:
            connection.close()
        except (OSError, AttributeError):
            pass
    if unreachable:
        tried = ", ".join(f"{service} {port}" for service, port in ports)
        refused = ", ".join(unreachable)
        return CheckResult(
            name,
            label,
            "FAIL",
            f"le cluster n'atteint pas l'IBM i sur {host} ({refused} parmi {tried}) "
            "— vérifier le routage VPC et les pare-feux",
            duration_ms=_elapsed(clock, started),
        )
    joined = ", ".join(str(port) for _, port in ports)
    return CheckResult(
        name, label, "OK", f"IBM i joignable sur {host} (ports {joined})",
        duration_ms=_elapsed(clock, started),
    )


def _aws_failure(label: str, error: BaseException, *, target: str) -> CheckResult:
    """Réduit une erreur AWS à un message actionnable, sans texte distant."""

    code = _error_code(error)
    type_name = type(error).__name__
    if code in {"AccessDenied", "AccessDeniedException", "403", "UnauthorizedOperation"}:
        message = f"{target} — accès refusé, vérifier le rôle IAM du pod"
    elif code in {"NoSuchBucket"}:
        message = f"{target} — bucket inexistant, vérifier site.rawBucket"
    elif code in {"ResourceNotFoundException", "ResourceNotFound"}:
        message = f"{target} — ressource inexistante, vérifier site.checkpointTable"
    elif code in {"InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken"}:
        message = f"{target} — identifiants AWS invalides ou expirés"
    elif type_name in {
        "NoCredentialsError", "EndpointConnectionError", "ConnectTimeoutError",
        "ReadTimeoutError", "SSLError", "ProxyConnectionError",
    }:
        message = f"{target} — endpoint AWS ou identifiants introuvables ({type_name})"
    else:
        message = f"{target} — échec inattendu ({code or type_name})"
    return CheckResult("", label, "FAIL", message)


def check_s3(
    site: SiteConfig,
    session: Any,
    timeout_seconds: float,
    *,
    aws_error: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Sonde d'écriture puis de relecture sous le préfixe brut de preuve."""

    started = clock()
    name, label = "s3_sonde", "S3 (sonde écriture/lecture)"
    target = f"s3://{site.raw_bucket}/{site.stream_prefix}"
    if session is None:
        detail = aws_error or "session AWS indisponible"
        return CheckResult(name, label, "FAIL", f"{target} — {detail}")
    config = _aws_config(timeout_seconds)
    try:
        identity = session.client("sts", config=config).get_caller_identity()
    except Exception as error:  # borné : tout échec AWS devient un FAIL lisible
        return _with_duration(
            _aws_failure(label, error, target=f"{target} — identité AWS illisible"),
            clock, started, name,
        )
    account = identity.get("Account")
    if account != site.aws_account_id:
        return CheckResult(
            name,
            label,
            "FAIL",
            f"{target} — l'identité AWS appartient au compte {account}, "
            f"le site déclare {site.aws_account_id} — vérifier le rôle assumé",
            duration_ms=_elapsed(clock, started),
        )
    store = S3ObjectStore(site.raw_bucket, site.stream_prefix, client=session.client("s3", config=config))
    try:
        store.put_once(_S3_PROBE_KEY, _S3_PROBE_CONTENT)
        read_back = store.get(_S3_PROBE_KEY)
    except ValueError:
        return CheckResult(
            name, label, "FAIL",
            f"{target} — une sonde divergente existe déjà, inspecter le préfixe",
            duration_ms=_elapsed(clock, started),
        )
    except Exception as error:
        return _with_duration(_aws_failure(label, error, target=target), clock, started, name)
    if read_back != _S3_PROBE_CONTENT:
        return CheckResult(
            name, label, "FAIL",
            f"{target} — la sonde relue diffère de l'écriture, vérifier le stockage",
            duration_ms=_elapsed(clock, started),
        )
    return CheckResult(
        name, label, "OK", f"sonde écrite et relue sous {target}",
        duration_ms=_elapsed(clock, started),
    )


def check_dynamodb(
    site: SiteConfig,
    session: Any,
    timeout_seconds: float,
    *,
    aws_error: str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Table de checkpoints : existence, état ACTIVE et lecture confirmée.

    La sonde reste en lecture seule : la politique du site n'accorde ni
    PutItem hors flux ni DeleteItem — écrire laisserait un résidu que le
    diagnostic ne pourrait pas retirer.
    """

    started = clock()
    name, label = "dynamodb_checkpoints", "DynamoDB (checkpoints)"
    target = f"table {site.checkpoint_table}"
    if session is None:
        detail = aws_error or "session AWS indisponible"
        return CheckResult(name, label, "FAIL", f"{target} — {detail}")
    client = session.client("dynamodb", config=_aws_config(timeout_seconds))
    try:
        description = client.describe_table(TableName=site.checkpoint_table)
    except Exception as error:
        return _with_duration(_aws_failure(label, error, target=target), clock, started, name)
    status = str(description.get("Table", {}).get("TableStatus", ""))
    if status != "ACTIVE":
        return CheckResult(
            name, label, "FAIL",
            f"{target} — état {status or 'inconnu'}, attendre ACTIVE ou recréer la table",
            duration_ms=_elapsed(clock, started),
        )
    try:
        client.get_item(
            TableName=site.checkpoint_table,
            Key={"stream_id": {"S": _DDB_PROBE_STREAM_ID}},
            ConsistentRead=True,
        )
    except Exception as error:
        return _with_duration(_aws_failure(label, error, target=target), clock, started, name)
    return CheckResult(
        name, label, "OK", f"{target} accessible (ACTIVE, lecture confirmée)",
        duration_ms=_elapsed(clock, started),
    )


def check_snowflake(
    site: SiteConfig,
    *,
    connector: Any,
    connection_name: str | None,
    oidc_token_file: str | None,
    timeout_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Destination Snowflake : session, base/schéma déclarés, CREATE STAGE."""

    started = clock()
    name, label = "snowflake_destination", "Snowflake (destination)"
    oidc = bool(oidc_token_file)
    if not oidc and not (connection_name or "").strip():
        return CheckResult(
            name, label, "SKIP",
            "aucun identifiant Snowflake configuré — contrôle non exécuté",
            duration_ms=_elapsed(clock, started),
        )
    if connector is None:
        if oidc:
            return CheckResult(
                name, label, "FAIL",
                "snowflake-connector-python absent de l'image alors que "
                "preflight.snowflakeOidc est activé — pointer preflight.image "
                "vers l'image vérificateur",
                duration_ms=_elapsed(clock, started),
            )
        return CheckResult(
            name, label, "SKIP",
            "snowflake-connector-python absent de l'image — contrôle non exécuté "
            "(utiliser preflight.image pour pointer l'image vérificateur)",
            duration_ms=_elapsed(clock, started),
        )
    options: dict[str, Any]
    if oidc:
        if not Path(oidc_token_file).is_absolute() or not Path(oidc_token_file).is_file():
            return CheckResult(
                name, label, "FAIL",
                f"jeton OIDC attendu mais absent ({oidc_token_file}) — "
                "vérifier le montage preflight.snowflakeOidc",
                duration_ms=_elapsed(clock, started),
            )
        options = {
            "account": site.snowflake_account,
            "authenticator": "WORKLOAD_IDENTITY",
            "workload_identity_provider": "OIDC",
            "token_file_path": oidc_token_file,
            "role": site.verifier_role_name,
            "database": site.destination_database,
            "schema": site.destination_schema,
        }
    else:
        options = {"connection_name": connection_name.strip()}
    try:
        connection = connector.connect(
            **options,
            warehouse=site.warehouse_name,
            login_timeout=15,
            network_timeout=timeout_seconds,
            session_parameters={"QUERY_TAG": "QUADRINGENT_PREFLIGHT"},
        )
    except Exception as error:
        return CheckResult(
            name, label, "FAIL",
            f"connexion Snowflake refusée sur le compte {site.snowflake_account} "
            f"({type(error).__name__}) — vérifier l'identité workload ou l'alias",
            duration_ms=_elapsed(clock, started),
        )
    try:
        try:
            cursor = connection.cursor()
        except Exception:
            return CheckResult(
                name, label, "FAIL",
                "session Snowflake ouverte mais curseur refusé — "
                "vérifier le rôle déclaré",
                duration_ms=_elapsed(clock, started),
            )
        try:
            try:
                role = _current_role(cursor)
            except Exception:
                return CheckResult(
                    name, label, "FAIL",
                    "session Snowflake ouverte mais requête refusée — "
                    "vérifier le rôle déclaré",
                    duration_ms=_elapsed(clock, started),
                )
            database = site.destination_database
            schema = site.destination_schema
            try:
                cursor.execute(f"USE DATABASE {database}")
                cursor.execute(f"USE SCHEMA {database}.{schema}")
            except Exception:
                return CheckResult(
                    name, label, "FAIL",
                    f"base {database} ou schéma {schema} inaccessible — "
                    "vérifier la destination déclarée et les grants USAGE",
                    duration_ms=_elapsed(clock, started),
                )
            if not _stage_privilege_granted(cursor, database, schema, role):
                if not _stage_privilege_proven(cursor, database, schema):
                    return CheckResult(
                        name, label, "FAIL",
                        f"le rôle {role or 'courant'} n'a pas CREATE STAGE sur "
                        f"{database}.{schema} — accorder le privilège au rôle du pipeline",
                        duration_ms=_elapsed(clock, started),
                    )
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
    finally:
        connection.close()
    return CheckResult(
        name, label, "OK",
        f"destination {site.destination_namespace} accessible, CREATE STAGE confirmé",
        duration_ms=_elapsed(clock, started),
    )


def _current_role(cursor: Any) -> str | None:
    cursor.execute("SELECT CURRENT_ROLE()")
    rows = cursor.fetchall()
    if not rows:
        return None
    row = rows[0]
    return str(row[0]) if not isinstance(row, Mapping) else str(row.get("CURRENT_ROLE()"))


def _grant_rows(cursor: Any, database: str, schema: str) -> list[dict[str, str]]:
    """Grants visibles sur le schéma déclaré, normalisés en majuscules."""

    cursor.execute(f"SHOW GRANTS ON SCHEMA {database}.{schema}")
    names = [str(column[0]).upper() for column in (cursor.description or ())]
    rows = []
    for row in cursor.fetchall() or ():
        if isinstance(row, Mapping):
            rows.append({str(key).upper(): str(value) for key, value in row.items()})
        else:
            rows.append({names[index]: str(value) for index, value in enumerate(row) if index < len(names)})
    return rows


def _stage_privilege_granted(cursor: Any, database: str, schema: str, role: str | None) -> bool:
    """Vrai si un grant CREATE STAGE/OWNERSHIP est visible pour le rôle."""

    try:
        rows = _grant_rows(cursor, database, schema)
    except Exception:
        return False
    principals = {principal for principal in (role, "PUBLIC") if principal}
    for row in rows:
        privilege = row.get("PRIVILEGE", "").upper()
        grantee = (row.get("GRANTEE_NAME") or row.get("GRANTED_TO") or "").upper()
        if privilege in {"CREATE STAGE", "OWNERSHIP"} and grantee in {p.upper() for p in principals}:
            return True
    return False


def _stage_privilege_proven(cursor: Any, database: str, schema: str) -> bool:
    """Prouve le privilège par une création réelle, immédiatement détruite."""

    qualified = f"{database}.{schema}.{_SNOWFLAKE_PROBE_STAGE}"
    try:
        cursor.execute(f"CREATE STAGE IF NOT EXISTS {qualified}")
    except Exception:
        return False
    try:
        cursor.execute(f"DROP STAGE IF EXISTS {qualified}")
    except Exception:
        pass  # le créateur détient OWNERSHIP ; un reliquat resterait explicite
    return True


def check_kubernetes(
    environ: Mapping[str, str],
    timeout_seconds: float,
    *,
    sa_root: str | Path = SERVICE_ACCOUNT_ROOT,
    transport_factory: Callable[..., Any] = https_transport,
    clock: Callable[[], float] = time.monotonic,
) -> CheckResult:
    """Le ServiceAccount du pod peut-il créer des Jobs batch/v1 ?

    Contrôle informatif (``required=False``) : le pod pré-vol s'exécute sous
    l'identité de capture, alors que le lancement de flotte s'appuie sur le
    ServiceAccount du control plane — un refus n'empêche pas la capture.
    """

    started = clock()
    name, label = "kubernetes_jobs", "Kubernetes (Jobs batch)"
    host = environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    if not host:
        return CheckResult(
            name, label, "SKIP",
            "hors cluster Kubernetes — contrôle RBAC non exécuté",
            required=False, duration_ms=_elapsed(clock, started),
        )
    try:
        context = ServiceAccountContext.load(sa_root)
    except JobsApiError:
        return CheckResult(
            name, label, "SKIP",
            "aucun jeton de ServiceAccount monté — contrôle RBAC non exécuté",
            required=False, duration_ms=_elapsed(clock, started),
        )
    raw_port = environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or environ.get(
        "KUBERNETES_SERVICE_PORT"
    ) or "443"
    try:
        port = int(raw_port)
    except ValueError:
        return CheckResult(
            name, label, "FAIL",
            "port de l'API Kubernetes illisible — vérifier l'environnement du pod",
            required=False, duration_ms=_elapsed(clock, started),
        )
    body = json.dumps(
        {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "namespace": context.namespace,
                    "verb": "create",
                    "group": "batch",
                    "resource": "jobs",
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        transport = transport_factory(
            context, host=host, port=port, timeout_seconds=timeout_seconds
        )
        response = transport("POST", _SSAR_PATH, body, "application/json")
    except JobsApiError:
        return CheckResult(
            name, label, "FAIL",
            "API Kubernetes injoignable — vérifier l'endpoint du cluster",
            required=False, duration_ms=_elapsed(clock, started),
        )
    if response.status in (401, 403):
        return CheckResult(
            name, label, "FAIL",
            "l'API Kubernetes refuse l'identité du pod — vérifier le ServiceAccount",
            required=False, duration_ms=_elapsed(clock, started),
        )
    if response.status not in (200, 201):
        return CheckResult(
            name, label, "FAIL",
            f"réponse inattendue de l'API Kubernetes ({response.status})",
            required=False, duration_ms=_elapsed(clock, started),
        )
    allowed = (
        isinstance(response.body, Mapping)
        and isinstance(response.body.get("status"), Mapping)
        and response.body["status"].get("allowed") is True
    )
    if not allowed:
        return CheckResult(
            name, label, "FAIL",
            f"le ServiceAccount du pod ne peut pas créer de Jobs batch dans "
            f"{context.namespace} — le lancement de flotte s'appuie sur "
            "quadringent-control-plane (contrôle informatif, non bloquant)",
            required=False, duration_ms=_elapsed(clock, started),
        )
    return CheckResult(
        name, label, "OK",
        f"le ServiceAccount peut créer des Jobs batch dans {context.namespace}",
        required=False, duration_ms=_elapsed(clock, started),
    )


def _elapsed(clock: Callable[[], float], started: float) -> int:
    return max(0, round((clock() - started) * 1000))


def _with_duration(result: CheckResult, clock: Callable[[], float], started: float, name: str) -> CheckResult:
    """Ré-attache nom et durée à un verdict construit par un helper."""

    return CheckResult(
        name,
        result.label,
        result.status,
        result.message,
        required=result.required,
        duration_ms=_elapsed(clock, started),
    )


def _guarded(
    check: Callable[..., CheckResult],
    *args: Any,
    clock: Callable[[], float] = time.monotonic,
    **kwargs: Any,
) -> CheckResult:
    """Un contrôle qui lève devient un FAIL lisible, jamais une trace brute."""

    started = clock()
    try:
        return check(*args, clock=clock, **kwargs)
    except Exception as error:
        return CheckResult(
            getattr(check, "__name__", "controle"),
            "contrôle interne",
            "FAIL",
            f"erreur interne du diagnostic ({type(error).__name__})",
            duration_ms=_elapsed(clock, started),
        )


def run_preflight(
    site: SiteConfig,
    args: argparse.Namespace,
    environ: Mapping[str, str],
    providers: Providers,
) -> list[CheckResult]:
    """Exécute les cinq contrôles dans l'ordre de lecture du rapport."""

    return [
        _guarded(
            check_ibmi_tcp, site, environ, args.timeout_seconds,
            opener=providers.opener, clock=providers.clock,
        ),
        _guarded(
            check_s3, site, providers.session, args.timeout_seconds,
            aws_error=providers.aws_error, clock=providers.clock,
        ),
        _guarded(
            check_dynamodb, site, providers.session, args.timeout_seconds,
            aws_error=providers.aws_error, clock=providers.clock,
        ),
        _guarded(
            check_snowflake, site,
            connector=providers.snowflake_connector,
            connection_name=args.connection_name,
            oidc_token_file=args.snowflake_oidc_token_file,
            timeout_seconds=args.timeout_seconds,
            clock=providers.clock,
        ),
        _guarded(
            check_kubernetes, environ, args.timeout_seconds,
            sa_root=providers.sa_root,
            transport_factory=providers.transport_factory,
            clock=providers.clock,
        ),
    ]


def overall_status(checks: Sequence[CheckResult]) -> str:
    """Fail-closed : un seul contrôle requis en échec suffit."""

    return (
        "FAIL"
        if any(check.status == "FAIL" and check.required for check in checks)
        else "OK"
    )


def build_report(site: SiteConfig, checks: Sequence[CheckResult]) -> dict[str, object]:
    return {
        "format_version": "quadringent-preflight-v1",
        "site": site.site_id,
        "environment": site.environment,
        "status": overall_status(checks),
        "checks": [check.as_record() for check in checks],
    }


def print_checklist(report: Mapping[str, object], out: Any = None) -> None:
    """Checklist lisible : une ligne par contrôle, verdict global borné."""

    out = sys.stdout if out is None else out
    print(
        f"Pré-vol Quadringent — site {report['site']} ({report['environment']})",
        file=out,
    )
    checks = report["checks"]
    for check in checks:
        suffix = " (non requis)" if not check["required"] else ""
        print(f"[{check['status']:<4}] {check['name']:<22} {check['message']}{suffix}", file=out)
    failures = [check for check in checks if check["status"] == "FAIL" and check["required"]]
    skipped = [check for check in checks if check["status"] == "SKIP"]
    verdict = "OK" if report["status"] == "OK" else "ÉCHEC"
    print(
        f"Résultat : {verdict} — {len(failures)} contrôle(s) requis en échec, "
        f"{len(skipped)} ignoré(s)",
        file=out,
    )


def _aws_session(site: SiteConfig, args: argparse.Namespace, environ: Mapping[str, str]) -> tuple[Any, str | None]:
    """Session boto3 sur le compte du site ; (None, raison) si indisponible."""

    try:
        import boto3
    except ImportError:
        return None, "boto3 absent de l'image"
    options = {"region_name": site.aws_region}
    profile = None if args.aws_default_credentials else args.aws_profile
    if profile:
        options["profile_name"] = profile
    try:
        return boto3.Session(**options), None
    except Exception as error:
        return None, f"session AWS indisponible ({type(error).__name__})"


def _snowflake_connector() -> Any:
    """Connecteur optionnel : absent des images légères, présent du vérificateur."""

    try:
        import snowflake.connector
    except ImportError:
        return None
    return snowflake.connector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=3.0,
        help="budget réseau par contrôle (0,5 à 30 s, défaut 3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="n'émettre que le rapport JSON, sans la checklist lisible",
    )
    snowflake_auth = parser.add_mutually_exclusive_group()
    snowflake_auth.add_argument("--connection-name", default=None)
    snowflake_auth.add_argument(
        "--snowflake-oidc-token-file",
        default=None,
        help="jeton OIDC projeté pour l'identité de charge Snowflake",
    )
    aws_auth = parser.add_mutually_exclusive_group()
    aws_auth.add_argument("--aws-profile", default=None)
    aws_auth.add_argument("--aws-default-credentials", action="store_true")
    args = parser.parse_args(argv)
    if not 0.5 <= args.timeout_seconds <= 30.0:
        parser.error("--timeout-seconds doit rester entre 0,5 et 30")
    try:
        site = current_site()
    except SiteConfigurationError as error:
        print(f"Configuration de site invalide : {error}", file=sys.stderr)
        return 2
    if args.connection_name is None:
        args.connection_name = site.snowflake_connection
    if args.aws_profile is None:
        args.aws_profile = site.aws_profile

    session, aws_error = _aws_session(site, args, os.environ)
    providers = Providers(
        opener=socket.create_connection,
        session=session,
        aws_error=aws_error,
        snowflake_connector=_snowflake_connector(),
        sa_root=SERVICE_ACCOUNT_ROOT,
        transport_factory=https_transport,
        clock=time.monotonic,
    )
    checks = run_preflight(site, args, os.environ, providers)
    report = build_report(site, checks)
    if not args.json:
        print_checklist(report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
