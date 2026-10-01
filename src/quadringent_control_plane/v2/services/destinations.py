"""Service Destination v2 : génération de la paire de clés + script SQL.

La création génère côté serveur une paire de clés RSA 2048 (PKCS8) : la clé
publique alimente le script SQL Snowflake (``RSA_PUBLIC_KEY``), la clé
privée est chiffrée (Fernet) avant stockage et n'est retournée qu'une seule
fois à l'appelant, jamais relisible ensuite — même discipline que
``secret_ref`` dans ``connections.py``. Aucun identifiant admin Snowflake
n'est demandé ni transmis : le script généré est destiné à être exécuté
manuellement par un administrateur Snowflake, qui garde le contrôle de ses
propres identifiants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import SecretBox
from . import validation
from .destination_verifier import (
    DestinationVerifierProtocol,
    DestinationVerifierRequest,
    warehouse_name_for,
    validated_destination_scope,
    DestinationVerifierError,
)

VERIFICATION_STATE_NOT_VERIFIED = "declared_not_verified"
VERIFICATION_STATE_VERIFIED = "verified"
VERIFICATION_STATE_FAILED = "failed"

# Voie de matérialisation historique/miroir (docs/decisions/2026-09-23-miroir-
# snowflake.md) : "copy_merge" garde le script inchangé (compatibilité
# ascendante) ; "streaming" ajoute les droits Snowpipe Streaming pour le
# rôle applicatif. "mirror_option" ne compte que sous "streaming" :
# "loader_merge" (option B, par défaut, aucun droit supplémentaire) ou
# "task_driven" (option A, exige EXECUTE TASK — mode expert).
_DESTINATION_MODES = ("copy_merge", "streaming")
_MIRROR_OPTIONS = ("loader_merge", "task_driven")


class DestinationNotFoundError(LookupError):
    """Aucune destination pour cet identifiant — 404 ``not_found``."""


class DestinationValidationError(ValueError):
    """Un champ du corps de création est hors contrat."""


@dataclass(frozen=True)
class DestinationRecord:
    id: str
    snowflake_account: str
    verification_state: str
    created_at: str
    paused_at: str | None = None
    # Identité de service persistée depuis 0011_dest_service_identity — le
    # rôle/utilisateur portés par setup_script en texte, aussi disponibles
    # ici pour le chargeur de destination (secrets_provisioner.py) et pour
    # le débogage. NULL pour une destination créée avant cette migration.
    service_user: str | None = None
    service_role: str | None = None
    destination_database: str = "QUADRINGENT"
    destination_schema: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "snowflake_account": self.snowflake_account,
            "destination_database": self.destination_database,
            "destination_schema": self.destination_schema,
            "verification_state": self.verification_state,
            "created_at": self.created_at,
            "paused": self.paused_at is not None,
            "paused_at": self.paused_at,
        }


class DestinationsService:
    """Façade Postgres/SQLite (SQLAlchemy Core) pour la ressource Destination."""

    def __init__(self, engine: Engine, secret_box: SecretBox, *, org_id: str) -> None:
        self._engine = engine
        self._secret_box = secret_box
        self._org_id = org_id

    def create(
        self,
        *,
        snowflake_account: object,
        destination_mode: object = "copy_merge",
        mirror_option: object = "loader_merge",
        destination_database: object = "QUADRINGENT",
        destination_schema: object = None,
        now: datetime | None = None,
    ) -> tuple[DestinationRecord, str]:
        """Crée la destination ; renvoie ``(record, private_key_pem)``.

        La clé privée n'est jamais relisible après cet appel — c'est
        l'unique occasion pour l'appelant de la récupérer (téléchargement).
        ``destination_mode``/``mirror_option`` ne pèsent que sur le contenu
        du script SQL généré (droits Snowpipe Streaming, EXECUTE TASK pour
        l'option A) — ils ne sont pas persistés en base, le script l'est.
        """

        try:
            snowflake_account = validation.validated_snowflake_account(snowflake_account)
            database, output_schema = validated_destination_scope(destination_database, destination_schema)
        except (validation.ValidationError, DestinationVerifierError) as error:
            raise DestinationValidationError(str(error)) from error
        if destination_mode not in _DESTINATION_MODES:
            raise DestinationValidationError(
                f"destination_mode doit valoir {' ou '.join(_DESTINATION_MODES)}"
            )
        if mirror_option not in _MIRROR_OPTIONS:
            raise DestinationValidationError(
                f"mirror_option doit valoir {' ou '.join(_MIRROR_OPTIONS)}"
            )

        private_key_pem, public_key_pem = _generate_rsa_key_pair()
        role_name = f"QDT_ROLE_{uuid.uuid4().hex[:8].upper()}"
        user_name = f"QDT_SVC_{uuid.uuid4().hex[:8].upper()}"
        script = _build_setup_script(
            snowflake_account=snowflake_account,
            role_name=role_name,
            user_name=user_name,
            public_key_pem=public_key_pem,
            destination_mode=destination_mode,
            mirror_option=mirror_option,
            destination_database=database,
            destination_schema=output_schema,
        )
        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        destination_id = uuid.uuid4().hex
        ciphertext = self._secret_box.encrypt(private_key_pem)
        with self._engine.begin() as connection:
            connection.execute(
                insert(v2_schema.destinations),
                {
                    "id": destination_id,
                    "org_id": self._org_id,
                    "snowflake_account": snowflake_account,
                    "destination_database": database,
                    "destination_schema": output_schema,
                    "key_pair_ciphertext": ciphertext,
                    "setup_script": script,
                    "verification_state": VERIFICATION_STATE_NOT_VERIFIED,
                    "created_at": created_at,
                    "service_user": user_name,
                    "service_role": role_name,
                },
            )
        return self.get(destination_id), private_key_pem

    def get(self, destination_id: str) -> DestinationRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.destinations).where(v2_schema.destinations.c.id == destination_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DestinationNotFoundError(destination_id)
        return _to_record(row)

    def list(self) -> tuple[DestinationRecord, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(v2_schema.destinations).order_by(
                        v2_schema.destinations.c.created_at, v2_schema.destinations.c.id
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_record(row) for row in rows)

    def setup_script(self, destination_id: str) -> str:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.destinations.c.setup_script).where(
                        v2_schema.destinations.c.id == destination_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DestinationNotFoundError(destination_id)
        return row["setup_script"]

    def verify(
        self, destination_id: str, *, verifier: DestinationVerifierProtocol | None = None
    ) -> dict[str, object]:
        """Vérifie la connexion par paire de clés, le rôle/warehouse/base/schémas
        déclarés, et les droits nécessaires au chargeur (§docstring du module
        ``destination_verifier``).

        Sans vérificateur injecté, répond honnêtement ``verified: "unknown"``
        — même discipline que ``SourcesService.test`` sans sonde : « non
        vérifié » n'est jamais une erreur, jamais un faux résultat vert.
        Avec un vérificateur, l'état persisté (``verification_state``)
        transitionne vers ``verified``/``failed`` selon le résultat complet.
        """

        record = self.get(destination_id)
        if verifier is None:
            return {"destination_id": record.id, "verified": "unknown"}
        if record.service_user is None or record.service_role is None:
            # Destination créée avant la migration 0011 (identité de service
            # non persistée) : impossible de se connecter sans improviser un
            # rôle/utilisateur — honnête plutôt qu'une supposition.
            return {
                "destination_id": record.id,
                "verified": "unknown",
                "detail": "identité de service absente (destination créée avant 0011) : régénérer la destination",
            }
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.destinations.c.key_pair_ciphertext).where(
                        v2_schema.destinations.c.id == destination_id
                    )
                )
                .mappings()
                .one()
            )
        # Échoue fermé si le chiffré est corrompu/clé rotée — jamais exposé.
        private_key_pem = self._secret_box.decrypt(row["key_pair_ciphertext"])
        result = verifier.verify(
            DestinationVerifierRequest(
                snowflake_account=record.snowflake_account,
                service_user=record.service_user,
                service_role=record.service_role,
                warehouse=warehouse_name_for(record.service_role),
                private_key_pem=private_key_pem,
                destination_database=record.destination_database,
                destination_schema=record.destination_schema,
            )
        )
        new_state = VERIFICATION_STATE_VERIFIED if result.verified() else VERIFICATION_STATE_FAILED
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.destinations)
                .where(v2_schema.destinations.c.id == destination_id)
                .values(verification_state=new_state)
            )
        return {"destination_id": record.id, **result.to_dict()}

    def pause(self, destination_id: str, *, now: datetime | None = None) -> DestinationRecord:
        self.get(destination_id)  # 404 si inconnue
        paused_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.destinations)
                .where(v2_schema.destinations.c.id == destination_id)
                .values(paused_at=paused_at)
            )
        return self.get(destination_id)

    def resume(self, destination_id: str) -> DestinationRecord:
        self.get(destination_id)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.destinations)
                .where(v2_schema.destinations.c.id == destination_id)
                .values(paused_at=None)
            )
        return self.get(destination_id)


def _generate_rsa_key_pair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def _public_key_body(public_key_pem: str) -> str:
    """Le contenu base64 sans les en-têtes PEM, tel qu'attendu par Snowflake."""

    lines = [line for line in public_key_pem.strip().splitlines() if "BEGIN" not in line and "END" not in line]
    return "\n".join(lines)


def _build_setup_script(
    *,
    snowflake_account: str,
    role_name: str,
    user_name: str,
    public_key_pem: str,
    destination_mode: str = "copy_merge",
    mirror_option: str = "loader_merge",
    destination_database: str = "QUADRINGENT",
    destination_schema: str | None = None,
) -> str:
    database, output_schema = validated_destination_scope(destination_database, destination_schema)
    history_schema = output_schema or "RAW"
    mirror_schema = output_schema or "CURATED"
    warehouse_name = f"QDT_WH_{role_name[9:]}"
    public_key_body = _public_key_body(public_key_pem)
    script = f"""-- Script de mise en service Quadringent (compte Snowflake : {snowflake_account})
-- Généré côté serveur ; à exécuter par un administrateur Snowflake.
-- Aucun identifiant admin Quadringent n'est requis ni transmis.

CREATE ROLE IF NOT EXISTS {role_name};

CREATE USER IF NOT EXISTS {user_name}
  TYPE = SERVICE
  RSA_PUBLIC_KEY = '{public_key_body}'
  DEFAULT_ROLE = {role_name};

GRANT ROLE {role_name} TO USER {user_name};

CREATE WAREHOUSE IF NOT EXISTS {warehouse_name}
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE;

GRANT USAGE ON WAREHOUSE {warehouse_name} TO ROLE {role_name};

CREATE DATABASE IF NOT EXISTS {database};
CREATE SCHEMA IF NOT EXISTS {database}.{history_schema};
CREATE SCHEMA IF NOT EXISTS {database}.{mirror_schema};

GRANT USAGE ON DATABASE {database} TO ROLE {role_name};
GRANT USAGE, CREATE TABLE ON SCHEMA {database}.{history_schema} TO ROLE {role_name};
GRANT USAGE ON SCHEMA {database}.{mirror_schema} TO ROLE {role_name};
"""
    if destination_mode == "streaming":
        # Snowpipe Streaming (SDK haute performance, pas d'objet PIPE) écrit
        # directement dans la table d'historique ; le rôle qui ouvre le
        # canal doit pouvoir la créer et y écrire. Le miroir vit dans
        # QUADRINGENT.CURATED, qui n'a jusqu'ici que USAGE : il lui faut
        # CREATE TABLE pour que le chargeur matérialise le miroir par le
        # MERGE loader-driven (option B, docs/decisions/2026-09-23-miroir-
        # snowflake.md). Le rôle est propriétaire des tables qu'il crée :
        # aucun GRANT INSERT/SELECT/UPDATE/DELETE distinct n'est requis.
        script += f"""
-- Snowpipe Streaming (historique) + MERGE miroir loader-driven (option B) :
-- le rôle crée et possède l'historique et le miroir dans {database}.{mirror_schema}.
GRANT CREATE TABLE ON SCHEMA {database}.{mirror_schema} TO ROLE {role_name};
"""
        if mirror_option == "task_driven":
            script += f"""
-- Option A (miroir piloté par tâche planifiée, mode expert) : le rôle doit
-- pouvoir exécuter la tâche dont il est propriétaire.
GRANT EXECUTE TASK ON ACCOUNT TO ROLE {role_name};
"""
    # Rôle lecteur : les tables répliquées appartiennent au rôle de service,
    # personne d'autre ne peut les lire sans ce rôle (constaté en
    # qualification). Lecture seule, tables actuelles et futures.
    reader_role = f"QDT_READ_{role_name[len('QDT_ROLE_'):]}"
    script += f"""
-- Accès en lecture pour les utilisateurs du client (à leur accorder :
--   GRANT ROLE {reader_role} TO ROLE <rôle de vos analystes>;)
CREATE ROLE IF NOT EXISTS {reader_role};
GRANT USAGE ON DATABASE {database} TO ROLE {reader_role};
GRANT USAGE ON SCHEMA {database}.{mirror_schema} TO ROLE {reader_role};
GRANT SELECT ON ALL TABLES IN SCHEMA {database}.{mirror_schema} TO ROLE {reader_role};
GRANT SELECT ON FUTURE TABLES IN SCHEMA {database}.{mirror_schema} TO ROLE {reader_role};
"""
    return script


def _to_record(row: object) -> DestinationRecord:
    return DestinationRecord(
        id=row["id"],
        snowflake_account=row["snowflake_account"],
        verification_state=row["verification_state"],
        created_at=_iso(row["created_at"]),
        paused_at=_iso(row["paused_at"]) if row["paused_at"] is not None else None,
        service_user=row["service_user"],
        service_role=row["service_role"],
        destination_database=row["destination_database"],
        destination_schema=row["destination_schema"],
    )


def _iso(value: object) -> str:
    if isinstance(value, str):
        return value
    return value.isoformat()
