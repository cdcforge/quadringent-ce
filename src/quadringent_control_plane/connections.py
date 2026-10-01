"""Persistance des liaisons (sites) déclarées par l'opérateur.

Une liaison décrit l'intention de raccorder un site IBM i à une destination
Snowflake — elle est créée bien avant que le moindre runtime Kubernetes ne
soit provisionné pour elle (`fleet_composition` reste le seul point qui
lance des Jobs). Le mot de passe IBM i n'est ni accepté ni persisté ici :
`ConnectionsStore.create` n'a structurellement aucun paramètre pour un
secret en clair, seule une référence Kubernetes (nom + clé) — le même
contrat que ``onboarding.py``.

Le magasin est adossé à :class:`AtomicJsonStateStore` (un seul fichier
``connections.json`` sous ``--fleet-state-dir``), écrit atomiquement à
chaque création — jamais de verrou distribué : un seul process control
plane écrit ce fichier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
import uuid
from typing import Sequence

from .fleet_runtime_store import AtomicJsonStateStore, AtomicJsonStateStoreError


class ConnectionsError(ValueError):
    """Une liaison ne peut pas être créée, lue, ou le magasin est hors contrat."""


class ConnectionsStorageError(ConnectionsError):
    """Le stockage est indisponible ; modifier le formulaire n'y remédie pas."""


CONNECTIONS_STATE_FILE = "connections.json"
FORMAT_VERSION = "quadringent-connections-v1"

# Une liaison tout juste créée par cette route n'a encore aucun Job, aucun
# Pod, aucun lecteur — ce n'est qu'une intention persistée. Le nom le dit
# sans détour : jamais "pending"/"active", qui laisseraient croire à un
# runtime déjà en vol. La seule route de création existe à ce jour : aucune
# transition vers un autre état n'est encore implémentée.
LIFECYCLE_DECLARED_NOT_IN_SERVICE = "declared_not_in_service"
LIFECYCLE_STATES = frozenset({LIFECYCLE_DECLARED_NOT_IN_SERVICE})

_DISPLAY_NAME = re.compile(r"^\S(?:.{0,118}\S)?$", re.UNICODE)
_SITE_ID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$")
_TABLE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_CLIENT_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IBMI_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
# Même forme que ``QUADRINGENT_IBMI_USER`` dans site_config : un compte
# refusé ici serait de toute façon refusé à la mise en service.
_IBMI_USER = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
_SNOWFLAKE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_SNOWFLAKE_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_CONNECTION_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_TABLES = 64


@dataclass(frozen=True)
class ConnectionRecord:
    """Une liaison persistée — jamais de mot de passe, seulement une référence."""

    connection_id: str
    site_id: str
    display_name: str
    ibmi_host: str
    ibmi_user: str
    snowflake_account: str
    destination_database: str
    destination_schema: str
    tables: tuple[str, ...]
    secret_ref_name: str
    secret_ref_key: str
    lifecycle_state: str
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "connection_id": self.connection_id,
            "site_id": self.site_id,
            "display_name": self.display_name,
            "ibmi_host": self.ibmi_host,
            "ibmi_user": self.ibmi_user,
            "snowflake_account": self.snowflake_account,
            "destination_database": self.destination_database,
            "destination_schema": self.destination_schema,
            "tables": list(self.tables),
            "secret_ref_name": self.secret_ref_name,
            "secret_ref_key": self.secret_ref_key,
            "lifecycle_state": self.lifecycle_state,
            "created_at": self.created_at,
        }


class ConnectionsStore:
    """Magasin des liaisons — un fichier ``connections.json`` par process."""

    def __init__(self, store: AtomicJsonStateStore) -> None:
        self._store = store
        self._lock = threading.Lock()

    def create(
        self,
        *,
        site_id: str,
        display_name: str,
        ibmi_host: str,
        ibmi_user: str,
        snowflake_account: str,
        destination_database: str,
        destination_schema: str,
        tables: Sequence[str],
        secret_ref_name: str,
        secret_ref_key: str,
        now: str | None = None,
    ) -> ConnectionRecord:
        """Valide puis persiste une nouvelle liaison ; jamais de secret en clair.

        La signature n'accepte structurellement aucun mot de passe : un
        appelant ne peut pas en faire transiter un, même par erreur — c'est
        la garantie la plus forte que ce module peut offrir, avant même la
        validation des champs.
        """

        record = ConnectionRecord(
            connection_id=uuid.uuid4().hex,
            site_id=_validated(site_id, _SITE_ID, "site_id"),
            display_name=_validated(display_name, _DISPLAY_NAME, "display_name"),
            ibmi_host=_validated(ibmi_host, _IBMI_HOST, "ibmi_host"),
            ibmi_user=_validated(ibmi_user, _IBMI_USER, "ibmi_user"),
            snowflake_account=_validated(snowflake_account, _SNOWFLAKE_ACCOUNT, "snowflake_account"),
            destination_database=_validated(
                destination_database, _SNOWFLAKE_IDENTIFIER, "destination_database"
            ),
            destination_schema=_validated(
                destination_schema, _SNOWFLAKE_IDENTIFIER, "destination_schema"
            ),
            tables=_validated_tables(tables),
            secret_ref_name=_validated(secret_ref_name, _K8S_NAME, "secret_ref_name"),
            secret_ref_key=_validated(secret_ref_key, _CLIENT_ALIAS, "secret_ref_key"),
            lifecycle_state=LIFECYCLE_DECLARED_NOT_IN_SERVICE,
            created_at=now if now is not None else datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            document = self._load_document()
            document["connections"][record.connection_id] = record.to_dict()
            try:
                self._store.save(document)
            except (OSError, AtomicJsonStateStoreError) as error:
                raise ConnectionsStorageError("écriture du magasin de liaisons impossible") from error
        return record

    def get(self, connection_id: str) -> ConnectionRecord | None:
        if not isinstance(connection_id, str) or _CONNECTION_ID.fullmatch(connection_id) is None:
            return None
        document = self._load_document()
        raw = document["connections"].get(connection_id)
        return None if raw is None else _parse_record(raw)

    def list(self) -> tuple[ConnectionRecord, ...]:
        document = self._load_document()
        # Ordre stable et déterministe pour l'API : par date de création,
        # puis par identifiant pour départager un timestamp identique.
        records = [_parse_record(raw) for raw in document["connections"].values()]
        records.sort(key=lambda record: (record.created_at, record.connection_id))
        return tuple(records)

    def _load_document(self) -> dict[str, object]:
        try:
            raw = self._store.load()
        except (OSError, AtomicJsonStateStoreError) as error:
            raise ConnectionsStorageError("lecture du magasin de liaisons impossible") from error
        if raw is None:
            return {"format_version": FORMAT_VERSION, "connections": {}}
        if raw.get("format_version") != FORMAT_VERSION or type(raw.get("connections")) is not dict:
            raise ConnectionsStorageError("magasin de liaisons hors contrat")
        return {"format_version": FORMAT_VERSION, "connections": dict(raw["connections"])}


def _validated(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ConnectionsError(f"{field_name} invalide")
    return value


def _validated_tables(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or isinstance(values, str):
        raise ConnectionsError("tables doit être une liste d'identifiants")
    if not values:
        raise ConnectionsError("tables ne doit pas être vide")
    if len(values) > _MAX_TABLES:
        raise ConnectionsError("tables dépasse la borne du manifeste")
    tables = tuple(values)
    if any(not isinstance(table, str) or _TABLE.fullmatch(table) is None for table in tables):
        raise ConnectionsError("tables contient un identifiant invalide")
    if len(set(tables)) != len(tables):
        raise ConnectionsError("tables contient un doublon")
    return tables


def connections_state_path(state_directory: str | Path) -> Path:
    """Chemin du fichier ``connections.json`` sous un répertoire d'état donné."""

    return Path(state_directory) / CONNECTIONS_STATE_FILE


def _parse_record(raw: object) -> ConnectionRecord:
    if type(raw) is not dict:
        raise ConnectionsError("liaison persistée hors contrat")
    try:
        tables = raw["tables"]
        if not isinstance(tables, list):
            raise ConnectionsError("liaison persistée hors contrat")
        record = ConnectionRecord(
            connection_id=_validated(raw["connection_id"], _CONNECTION_ID, "connection_id"),
            site_id=_validated(raw["site_id"], _SITE_ID, "site_id"),
            display_name=_validated(raw["display_name"], _DISPLAY_NAME, "display_name"),
            ibmi_host=_validated(raw["ibmi_host"], _IBMI_HOST, "ibmi_host"),
            ibmi_user=_validated(raw["ibmi_user"], _IBMI_USER, "ibmi_user"),
            snowflake_account=_validated(raw["snowflake_account"], _SNOWFLAKE_ACCOUNT, "snowflake_account"),
            destination_database=_validated(
                raw["destination_database"], _SNOWFLAKE_IDENTIFIER, "destination_database"
            ),
            destination_schema=_validated(
                raw["destination_schema"], _SNOWFLAKE_IDENTIFIER, "destination_schema"
            ),
            tables=_validated_tables(tables),
            secret_ref_name=_validated(raw["secret_ref_name"], _K8S_NAME, "secret_ref_name"),
            secret_ref_key=_validated(raw["secret_ref_key"], _CLIENT_ALIAS, "secret_ref_key"),
            lifecycle_state=raw["lifecycle_state"],
            created_at=raw["created_at"],
        )
    except KeyError as error:
        raise ConnectionsError("liaison persistée incomplète") from error
    if record.lifecycle_state not in LIFECYCLE_STATES:
        raise ConnectionsError("état de cycle de vie de liaison invalide")
    if not isinstance(record.created_at, str) or not record.created_at.strip():
        raise ConnectionsError("date de création de liaison invalide")
    return record
