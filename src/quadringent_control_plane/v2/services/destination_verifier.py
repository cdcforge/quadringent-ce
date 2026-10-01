"""Vérificateur Snowflake réel pour ``POST /v2/destinations/{id}/verify``.

Même discipline que ``source_probe.py`` (sonde IBM i) : ce module déclare le
port (``DestinationVerifierProtocol``) et les structures de résultat, sans
jamais improviser la connexion ailleurs — ``DestinationsService.verify`` le
consomme, injecté depuis ``create_v2_app`` (port ``destination_verifier``,
jamais branché par défaut ; sans lui, la route répond honnêtement
``verified: "unknown"``, comme ``sources.test`` sans sonde).

L'implémentation réelle (``SnowflakeKeyPairVerifier``) réutilise le
connecteur ``snowflake-connector-python`` déjà en dépendance, avec la même
connexion par paire de clés (JWT) que
``scripts/quadringent_destination_loader.py::_connect_snowflake``. Elle
vérifie, dans l'ordre : la connexion, le rôle courant, le warehouse, la base,
les schémas déclarés, puis les droits nécessaires au chargeur (créer/écrire
les tables historique et miroir, et le MERGE) — prouvés par une création de
table réelle, aussitôt détruite, plutôt que par une simple lecture de
``SHOW GRANTS`` (un rôle propriétaire de ses tables n'a besoin d'aucun GRANT
INSERT/SELECT/UPDATE/DELETE distinct, voir
``services/destinations.py::_build_setup_script``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from ...connections import _SNOWFLAKE_IDENTIFIER

# Convention figée par services/destinations.py::_build_setup_script — jamais
# une valeur redécouverte séparément.
DECLARED_DATABASE = "QUADRINGENT"
DECLARED_HISTORY_SCHEMA = "RAW"
DECLARED_MIRROR_SCHEMA = "CURATED"


class DestinationVerifierError(RuntimeError):
    """Erreur de vérification réduite à un code sûr, jamais de détail brut de connexion."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VerificationOutcome:
    """Résultat d'une étape (connexion, rôle, warehouse, base, schéma, droits)."""

    ok: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class DestinationVerificationResult:
    connection: VerificationOutcome
    role: VerificationOutcome
    warehouse: VerificationOutcome
    database: VerificationOutcome
    schema: VerificationOutcome
    load_privileges: VerificationOutcome

    def verified(self) -> bool:
        return all(
            outcome.ok
            for outcome in (
                self.connection,
                self.role,
                self.warehouse,
                self.database,
                self.schema,
                self.load_privileges,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "verified": self.verified(),
            "connection": self.connection.to_dict(),
            "role": self.role.to_dict(),
            "warehouse": self.warehouse.to_dict(),
            "database": self.database.to_dict(),
            "schema": self.schema.to_dict(),
            "load_privileges": self.load_privileges.to_dict(),
        }


@dataclass(frozen=True)
class DestinationVerifierRequest:
    snowflake_account: str
    service_user: str
    service_role: str
    warehouse: str
    private_key_pem: str
    destination_database: str = DECLARED_DATABASE
    destination_schema: str | None = None


def validated_destination_scope(database: object, schema: object) -> tuple[str, str | None]:
    """Même contrat d'identifiants que connections v1 et la configuration du site."""
    for name, value in (("destination_database", database), ("destination_schema", schema)):
        if name == "destination_schema" and value is None:
            continue
        if not isinstance(value, str) or _SNOWFLAKE_IDENTIFIER.fullmatch(value) is None:
            raise DestinationVerifierError("invalid_destination_scope", f"{name} invalide")
    return str(database).upper(), schema.upper() if isinstance(schema, str) else None


class DestinationVerifierProtocol(Protocol):
    """Vérificateur réel injecté — jamais de secret journalisé, jamais improvisé ici."""

    def verify(self, request: DestinationVerifierRequest) -> DestinationVerificationResult: ...


def warehouse_name_for(service_role: str) -> str:
    """Nom du warehouse dédié — même convention que
    ``services/destinations.py::_build_setup_script`` (``QDT_WH_<suffixe du
    rôle>``, aussi dupliquée par
    ``scripts/quadringent_destination_loader.py::_snowflake_role_to_warehouse``),
    jamais une valeur redécouverte séparément."""

    prefix = "QDT_ROLE_"
    if not service_role.startswith(prefix):
        raise DestinationVerifierError(
            "role_convention_mismatch",
            f"rôle de service hors convention {prefix}* : {service_role!r}",
        )
    return f"QDT_WH_{service_role[len(prefix):]}"


def _connection_refused_result(detail: str) -> DestinationVerificationResult:
    unverified = VerificationOutcome(False, "non vérifié : connexion refusée")
    return DestinationVerificationResult(
        connection=VerificationOutcome(False, detail),
        role=unverified,
        warehouse=unverified,
        database=unverified,
        schema=unverified,
        load_privileges=unverified,
    )


class SnowflakeKeyPairVerifier:
    """Vérificateur réel — connexion JWT par paire de clés
    (``snowflake-connector-python``). Import différé : jamais requis hors
    production/qualification, comme
    ``scripts/quadringent_destination_loader.py::_connect_snowflake``."""

    def __init__(self, *, login_timeout: float = 15.0, network_timeout: float = 15.0) -> None:
        self._login_timeout = login_timeout
        self._network_timeout = network_timeout

    def verify(self, request: DestinationVerifierRequest) -> DestinationVerificationResult:
        try:
            database, output_schema = validated_destination_scope(
                request.destination_database, request.destination_schema
            )
        except DestinationVerifierError:
            return _connection_refused_result("périmètre Snowflake déclaré invalide")
        schemas = (output_schema,) if output_schema is not None else (DECLARED_HISTORY_SCHEMA, DECLARED_MIRROR_SCHEMA)
        try:
            connection = self._connect(request)
        except Exception as error:  # jamais de détail réseau brut, seulement le type d'erreur
            return _connection_refused_result(
                f"connexion Snowflake refusée sur le compte {request.snowflake_account} "
                f"({type(error).__name__}) — vérifier la clé/le rôle déclarés"
            )
        try:
            cursor = connection.cursor()
        except Exception:
            return _connection_refused_result(
                "session Snowflake ouverte mais curseur refusé — vérifier le rôle déclaré"
            )
        try:
            connection_outcome = VerificationOutcome(True, f"session ouverte sur {request.snowflake_account}")
            role_outcome = _check_role(cursor, request.service_role)
            warehouse_outcome = _check_warehouse(cursor, request.warehouse)
            database_outcome = _check_database(cursor, database)
            schema_outcome = _check_schemas(
                cursor, database, schemas
            )
            privileges_outcome = _check_load_privileges(
                cursor, database, schemas
            )
            return DestinationVerificationResult(
                connection=connection_outcome,
                role=role_outcome,
                warehouse=warehouse_outcome,
                database=database_outcome,
                schema=schema_outcome,
                load_privileges=privileges_outcome,
            )
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
            connection.close()

    def _connect(self, request: DestinationVerifierRequest) -> Any:
        import snowflake.connector  # import différé : jamais requis hors production/qualification
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(request.private_key_pem.encode("utf-8"), password=None)
        der = key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        return snowflake.connector.connect(
            account=request.snowflake_account,
            user=request.service_user,
            role=request.service_role,
            private_key=der,
            warehouse=request.warehouse,
            login_timeout=self._login_timeout,
            network_timeout=self._network_timeout,
            session_parameters={"QUERY_TAG": "QUADRINGENT_DESTINATION_VERIFY"},
        )


def _fetch_scalar(cursor: Any) -> str | None:
    rows = cursor.fetchall()
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        return str(next(iter(row.values())))
    return str(row[0])


def _check_role(cursor: Any, expected_role: str) -> VerificationOutcome:
    try:
        cursor.execute("SELECT CURRENT_ROLE()")
        actual = _fetch_scalar(cursor)
    except Exception:
        return VerificationOutcome(False, "requête CURRENT_ROLE() refusée")
    if actual is None:
        return VerificationOutcome(False, "aucun rôle courant renvoyé")
    if actual.upper() != expected_role.upper():
        return VerificationOutcome(
            False, f"rôle courant {actual} différent du rôle de service déclaré {expected_role}"
        )
    return VerificationOutcome(True, f"rôle {actual} actif")


def _check_warehouse(cursor: Any, expected_warehouse: str) -> VerificationOutcome:
    try:
        cursor.execute("SELECT CURRENT_WAREHOUSE()")
        actual = _fetch_scalar(cursor)
    except Exception:
        return VerificationOutcome(False, "requête CURRENT_WAREHOUSE() refusée")
    if actual is None or actual.upper() != expected_warehouse.upper():
        return VerificationOutcome(
            False, f"warehouse courant {actual!r} différent du warehouse déclaré {expected_warehouse}"
        )
    return VerificationOutcome(True, f"warehouse {actual} actif")


def _check_database(cursor: Any, database: str) -> VerificationOutcome:
    try:
        cursor.execute(f"USE DATABASE {database}")
    except Exception:
        return VerificationOutcome(False, f"base {database} inaccessible — vérifier le GRANT USAGE")
    return VerificationOutcome(True, f"base {database} accessible")


def _check_schemas(cursor: Any, database: str, schemas: tuple[str, ...]) -> VerificationOutcome:
    inaccessible: list[str] = []
    for schema in schemas:
        try:
            cursor.execute(f"USE SCHEMA {database}.{schema}")
        except Exception:
            inaccessible.append(schema)
    if inaccessible:
        return VerificationOutcome(
            False,
            f"schéma(s) inaccessible(s) : {', '.join(f'{database}.{s}' for s in inaccessible)} — "
            "vérifier le GRANT USAGE",
        )
    return VerificationOutcome(
        True, f"schémas accessibles : {', '.join(f'{database}.{s}' for s in schemas)}"
    )


def _check_load_privileges(cursor: Any, database: str, schemas: tuple[str, ...]) -> VerificationOutcome:
    """Prouve ``CREATE TABLE`` (donc écriture historique/miroir + MERGE, le
    rôle étant propriétaire de ses tables) par une création réelle,
    aussitôt détruite — même stratégie que
    ``scripts/quadringent_preflight.py::_stage_privilege_proven``."""

    missing: list[str] = []
    cleanup_failed: list[str] = []
    # CREATE sans adoption : un nom occupé échoue et ne sera jamais supprimé.
    probe_table = "QDT_VERIFY_" + uuid4().hex.upper()
    for schema in schemas:
        qualified = f"{database}.{schema}.{probe_table}"
        try:
            cursor.execute(f"CREATE TABLE {qualified} (PROBE_COLUMN NUMBER)")
        except Exception:
            missing.append(schema)
            continue
        try:
            cursor.execute(f"DROP TABLE {qualified}")
        except Exception:
            cleanup_failed.append(qualified)
    if cleanup_failed:
        return VerificationOutcome(
            False,
            f"retrait de la sonde refusé sur : {', '.join(cleanup_failed)} — "
            "vérification incomplète ; retirer uniquement ces tables de sonde",
        )
    if missing:
        return VerificationOutcome(
            False,
            f"CREATE TABLE refusé sur : {', '.join(f'{database}.{s}' for s in missing)} — "
            "accorder CREATE TABLE au rôle de service (historique/miroir/MERGE en dépendent)",
        )
    return VerificationOutcome(
        True,
        f"CREATE TABLE confirmé sur {', '.join(f'{database}.{s}' for s in schemas)} "
        "(historique, miroir, MERGE couverts : le rôle est propriétaire de ses tables)",
    )
