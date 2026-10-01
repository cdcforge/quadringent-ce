"""Service Source v2 : CRUD read/create/test — secret jamais en clair.

Le secret IBM i est chiffré (Fernet) avant toute écriture et n'est jamais
redéchiffré vers l'appelant HTTP : la lecture ne renvoie que
``secret_set: true``. ``test`` ne sonde aucun réseau réel dans ce
chantier — il valide que la source existe et que son secret déchiffre
correctement (fail-closed si corrompu), sans jamais exposer sa valeur ;
le sondage IBM i réel reste un exécuteur à brancher ultérieurement, comme
les actions de pipeline (cf. ``state_machine.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import SecretBox
from . import validation
from .source_probe import SourceProbeProtocol, SourceProbeRequest


class SourceNotFoundError(LookupError):
    """Aucune source pour cet identifiant — 404 ``not_found``."""


class SourceValidationError(ValueError):
    """Un champ du corps de création/test est hors contrat."""


@dataclass(frozen=True)
class SourceRecord:
    id: str
    display_name: str
    ibmi_host: str
    ibmi_user: str
    secret_set: bool
    tls_fingerprint: str | None
    detected_timezone: str | None
    detected_version: str | None
    created_at: str
    updated_at: str
    paused_at: str | None = None
    # 0013_source_tls_pin : décision de confiance TLS retenue — jamais le
    # PEM lui-même dans cette vue (pas un secret, mais pas utile à
    # afficher ; voir ``SourcesService._pinned_pem`` pour l'exécuteur).
    tls_trust: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "ibmi_host": self.ibmi_host,
            "ibmi_user": self.ibmi_user,
            "secret_set": self.secret_set,
            "tls_fingerprint": self.tls_fingerprint,
            "tls_trust": self.tls_trust,
            "detected_timezone": self.detected_timezone,
            "detected_version": self.detected_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "paused": self.paused_at is not None,
            "paused_at": self.paused_at,
        }


class SourcesService:
    """Façade Postgres/SQLite (SQLAlchemy Core) pour la ressource Source."""

    def __init__(self, engine: Engine, secret_box: SecretBox, *, org_id: str) -> None:
        self._engine = engine
        self._secret_box = secret_box
        self._org_id = org_id

    def create(
        self,
        *,
        display_name: object,
        ibmi_host: object,
        ibmi_user: object,
        secret_value: object,
        now: datetime | None = None,
    ) -> SourceRecord:
        try:
            display_name = validation.validated_display_name(display_name)
            ibmi_host = validation.validated_ibmi_host(ibmi_host)
            ibmi_user = validation.validated_ibmi_user(ibmi_user)
        except validation.ValidationError as error:
            raise SourceValidationError(str(error)) from error
        if not isinstance(secret_value, str) or not secret_value:
            raise SourceValidationError("secret invalide ou vide")

        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        source_id = uuid.uuid4().hex
        ciphertext = self._secret_box.encrypt(secret_value)
        with self._engine.begin() as connection:
            connection.execute(
                insert(v2_schema.sources),
                {
                    "id": source_id,
                    "org_id": self._org_id,
                    "display_name": display_name,
                    "ibmi_host": ibmi_host,
                    "ibmi_user": ibmi_user,
                    "secret_ciphertext": ciphertext,
                    "tls_fingerprint": None,
                    "detected_timezone": None,
                    "detected_version": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
        return self.get(source_id)

    def plan_create(
        self,
        *,
        display_name: object,
        ibmi_host: object,
        ibmi_user: object,
    ) -> dict[str, object]:
        """Valide sans persister — support de ``dry_run`` à la création."""

        try:
            display_name = validation.validated_display_name(display_name)
            ibmi_host = validation.validated_ibmi_host(ibmi_host)
            ibmi_user = validation.validated_ibmi_user(ibmi_user)
        except validation.ValidationError as error:
            raise SourceValidationError(str(error)) from error
        return {
            "would_create": {
                "display_name": display_name,
                "ibmi_host": ibmi_host,
                "ibmi_user": ibmi_user,
                "secret_set": True,
            }
        }

    def get(self, source_id: str) -> SourceRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(select(v2_schema.sources).where(v2_schema.sources.c.id == source_id))
                .mappings()
                .first()
            )
        if row is None:
            raise SourceNotFoundError(source_id)
        return _to_record(row)

    def list(self) -> tuple[SourceRecord, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(v2_schema.sources).order_by(
                        v2_schema.sources.c.created_at, v2_schema.sources.c.id
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_record(row) for row in rows)

    def test(
        self,
        source_id: str,
        *,
        probe: SourceProbeProtocol | None = None,
        tls_trust: str | None = None,
        pinned_fingerprint: str | None = None,
        pinned_pem: str | None = None,
    ) -> dict[str, object]:
        """Re-teste réseau/TLS/authentification/horloge (design §2.1).

        Sans sonde injectée, se comporte comme avant ce chantier — seul le
        déchiffrement du secret est vérifié (fail-closed si corrompu),
        `reachable: "unknown"` — le sondage réel reste optionnel tant que le
        control plane n'est pas câblé à un exécuteur IBM i (`executor_unavailable`
        n'est jamais renvoyé ici : « non testé » n'est pas une erreur).
        Avec une sonde, le résultat complet (réseau, TLS, authentification,
        version, fuseau) est renvoyé et les champs détectés sont persistés.

        ``tls_trust``/``pinned_fingerprint``/``pinned_pem`` portent une
        décision d'épinglage explicite de l'opérateur pour *cette* tentative
        (``tls: {trust: "pinned", fingerprint, certificate_pem}`` côté
        route) ; sans eux, la confiance déjà persistée pour la source est
        réutilisée (test répété après un premier épinglage), avec
        ``"system"`` par défaut. Le PEM n'est jamais persisté sur la seule
        foi de la requête : seule une sonde qui confirme l'empreinte
        (``result.tls.ok`` avec ``trust == "pinned"``) déclenche l'écriture
        — jamais une valeur devinée ou non vérifiée.
        """

        record = self.get(source_id)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        v2_schema.sources.c.secret_ciphertext,
                        v2_schema.sources.c.tls_trust,
                        v2_schema.sources.c.tls_pinned_pem,
                    ).where(v2_schema.sources.c.id == source_id)
                )
                .mappings()
                .one()
            )
        # Échoue fermé si le chiffré est corrompu/clé rotée — jamais exposé.
        secret_value = self._secret_box.decrypt(row["secret_ciphertext"])
        if probe is None:
            return {"source_id": record.id, "reachable": "unknown", "secret_set": True}

        effective_trust = tls_trust or row["tls_trust"] or "system"
        effective_pem = pinned_pem if pinned_pem else (row["tls_pinned_pem"] if effective_trust == "pinned" else None)

        result = probe.probe(
            SourceProbeRequest(
                ibmi_host=record.ibmi_host,
                ibmi_user=record.ibmi_user,
                secret_value=secret_value,
                tls_trust=effective_trust,
                pinned_fingerprint=pinned_fingerprint,
                pinned_pem=effective_pem,
            )
        )
        values: dict[str, object] = {}
        if result.reachable():
            values.update(
                tls_fingerprint=result.tls_fingerprint,
                detected_timezone=result.detected_timezone,
                detected_version=result.ibmi_version,
            )
        # La décision de confiance elle-même n'est jamais posée depuis la
        # requête : seule la mesure de la sonde (``result.tls_trust``, gagée
        # par une poignée de main TLS réussie) est retenue — un épinglage
        # persiste le PEM correspondant, jamais avant que l'empreinte n'ait
        # été confirmée.
        if result.tls.ok and result.tls_trust:
            values["tls_trust"] = result.tls_trust
            values["tls_pinned_pem"] = effective_pem if result.tls_trust == "pinned" else None
        if values:
            values["updated_at"] = datetime.now(timezone.utc)
            with self._engine.begin() as connection:
                connection.execute(
                    update(v2_schema.sources).where(v2_schema.sources.c.id == source_id).values(**values)
                )
        return {"source_id": record.id, **result.to_dict()}

    def pause(self, source_id: str, *, now: datetime | None = None) -> SourceRecord:
        """Marque la source en pause (intention opérateur — ne touche pas les pipelines)."""

        self.get(source_id)  # 404 si inconnue
        paused_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.sources).where(v2_schema.sources.c.id == source_id).values(
                    paused_at=paused_at, updated_at=paused_at
                )
            )
        return self.get(source_id)

    def resume(self, source_id: str, *, now: datetime | None = None) -> SourceRecord:
        self.get(source_id)
        updated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.sources).where(v2_schema.sources.c.id == source_id).values(
                    paused_at=None, updated_at=updated_at
                )
            )
        return self.get(source_id)


def _to_record(row: object) -> SourceRecord:
    return SourceRecord(
        id=row["id"],
        display_name=row["display_name"],
        ibmi_host=row["ibmi_host"],
        ibmi_user=row["ibmi_user"],
        secret_set=bool(row["secret_ciphertext"]),
        tls_fingerprint=row["tls_fingerprint"],
        tls_trust=row["tls_trust"],
        detected_timezone=row["detected_timezone"],
        detected_version=row["detected_version"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        paused_at=_iso(row["paused_at"]) if row["paused_at"] is not None else None,
    )


def _iso(value: object) -> str:
    if isinstance(value, str):
        return value
    return value.isoformat()
