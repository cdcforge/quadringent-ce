"""Garde durable contre les rafales de sign-on IBM i.

Un spawn du worker JVM vaut une tentative d'authentification sur la source.
Sans garde, une erreur tue le JVM puis le poll suivant en respawn un : chaque
retry est un sign-on, et un redémarrage de pod remet le compteur à zéro —
exactement la boucle qui peut désactiver un profil (QMAXSIGN) ou marteler un
serveur en maintenance.

Ce module borne le nombre total de tentatives, durablement :

* au plus ``max_attempts`` concessions de connexion, comptées dans un état
  partagé — un pod qui redémarre hérite du compteur, il ne le réinitialise pas ;
* un échec d'authentification (profil désactivé, mot de passe faux ou expiré)
  bascule la garde en ``blocked`` : plus aucun sign-on tant qu'un opérateur
  n'a pas explicitement réarmé après intervention côté source ;
* les indisponibilités (hôte coupé, fenêtre de maintenance) basculent en
  ``paused`` avec ``retry_after`` : le process attend sans aucun sign-on, puis
  sonde une fois, puis prolonge la pause si la source est encore fermée ;
* une concession en cours porte un bail horodaté : deux pods ne peuvent pas
  doubler les tentatives, et un pod mort en vol libère le passage à l'expiration.

L'état vit dans la table DynamoDB des checkpoints (item
``source-gate#<host>#<user>``) — la garde est par compte source, pas par flux :
deux voies contre le même profil partagent le même budget. Une implémentation
fichier sert les tests et le POC hors ligne.
"""

from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
import tempfile
from typing import Any, Protocol


GATE_FORMAT_VERSION = "quadringent-source-gate-v1"

STATE_CLOSED = "closed"
STATE_PAUSED = "paused"
STATE_BLOCKED = "blocked"

REASON_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
REASON_AUTHENTICATION_BLOCKED = "AUTHENTICATION_BLOCKED"
REASON_CONFIGURATION_BLOCKED = "CONFIGURATION_BLOCKED"
REASON_RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
REASON_CONNECT_IN_FLIGHT = "CONNECT_IN_FLIGHT"
REASON_OPERATOR_PAUSE = "OPERATOR_PAUSE"

MAX_ATTEMPTS_HARD_LIMIT = 3
_CAS_RETRIES = 3


class ConnectFailureClass(str, Enum):
    """Famille d'un échec d'établissement de session — décide de la politique.

    ``AUTHENTICATION`` couvre profil désactivé, mot de passe incorrect ou
    expiré : rejouer le sign-on est interdit, la garde bloque jusqu'au reset
    opérateur. ``UNAVAILABLE`` couvre source coupée ou en maintenance : pause
    bornée puis sonde unique. ``UNKNOWN`` reste compté comme une tentative —
    un sign-on a eu lieu, le budget doit le payer.
    """

    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class SourceAuthenticationBlockedError(RuntimeError):
    """La garde est bloquée : aucun sign-on n'est tenté jusqu'au reset explicite."""


class SourceConfigurationBlockedError(RuntimeError):
    """La garde est bloquée par une erreur de configuration non rejouable.

    Distincte d'un refus d'authentification (``SourceAuthenticationBlockedError``) :
    la source répond, l'authentification a même réussi, mais un réglage
    déclaré contredit ce que la source mesure (ex. ``AS400_SOURCE_TIME_ZONE``
    incohérent avec le décalage UTC réel — ``SOURCE_CLOCK_MISMATCH``). Rejouer
    le sign-on tel quel produirait indéfiniment le même échec : la garde
    bloque jusqu'à correction de la configuration et reset opérateur
    explicite, sans jamais le présenter comme une coupure de la source.
    """


class SourceUnavailablePausedError(RuntimeError):
    """La garde est en pause : la source est joignable à nouveau après ``retry_after``.

    Lever cette erreur ne doit jamais déclencher un sign-on : l'appelant
    attend l'échéance ou s'arrête, puis laisse le prochain ``before_connect``
    accorder une sonde unique.
    """

    def __init__(self, message: str, *, retry_after: datetime, reason_code: str) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.reason_code = reason_code


class IbmiUserDisabledError(SourceAuthenticationBlockedError):
    """IBM i profile is disabled; retrying the sign-on is forbidden."""


@dataclass(frozen=True)
class GateDecision:
    """Ce que ``before_connect`` a tranché, pour l'observabilité de l'appelant."""

    granted: bool
    reason_code: str
    retry_after: datetime | None = None


@dataclass(frozen=True)
class SourceGatePolicy:
    """Bornes de la garde. ``max_attempts`` ne dépasse jamais 3 : c'est la loi
    produit — au-delà, chaque tentative supplémentaire est un sign-on de trop
    sur un profil potentiellement fragilisé."""

    max_attempts: int = 3
    lease_seconds: int = 120
    pause_seconds: int = 900
    pause_max_seconds: int = 14_400

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= MAX_ATTEMPTS_HARD_LIMIT:
            raise ValueError("max_attempts must be between 1 and 3")
        if type(self.lease_seconds) is not int or not 30 <= self.lease_seconds <= 600:
            raise ValueError("lease_seconds must be between 30 and 600")
        if type(self.pause_seconds) is not int or not 60 <= self.pause_seconds <= 86_400:
            raise ValueError("pause_seconds must be between 60 and 86400")
        if type(self.pause_max_seconds) is not int or self.pause_max_seconds < self.pause_seconds:
            raise ValueError("pause_max_seconds must cover pause_seconds")

    def pause_delay(self, *, attempts_used: int) -> timedelta:
        """Recul borné : double par tentative au-delà du budget, plafonné."""

        extra = max(0, attempts_used - self.max_attempts)
        seconds = self.pause_seconds * (2 ** min(extra, 10))
        return timedelta(seconds=min(seconds, self.pause_max_seconds))


def _empty_record(now: datetime) -> dict[str, Any]:
    return {
        "format_version": GATE_FORMAT_VERSION,
        "state": STATE_CLOSED,
        "attempts_used": 0,
        "lease_until": None,
        "retry_after": None,
        "reason_code": None,
        "opened_at": None,
        "last_error_head": None,
        "updated_at": _iso(now),
        "reset_by": None,
        "reset_at": None,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fresh(moment: datetime | None, now: datetime) -> bool:
    return moment is not None and moment > now


class SourceGate(Protocol):
    """Autorité durable sur le droit de tenter un sign-on IBM i."""

    def before_connect(self, *, now: datetime) -> None:
        """Accorde une tentative ou lève sans qu'aucun sign-on n'ait eu lieu."""

    def record_connect_success(self, *, now: datetime) -> None:
        """Referme la garde après un ``worker_ready``."""

    def record_connect_failure(
        self, failure_class: ConnectFailureClass, *, now: datetime, error_head: str | None = None
    ) -> dict[str, Any] | None:
        """Comptabilise l'échec ; rend le record écrit pour décision d'erreur."""

    def reset(self, *, actor: str, now: datetime) -> dict[str, Any]:
        """Réarmement opérateur, audité : ``blocked``/``paused`` → ``closed``."""

    def pause_until(self, *, until: datetime, actor: str, now: datetime) -> dict[str, Any]:
        """Pause déclarée (fenêtre de maintenance connue) sans consommer de tentative."""

    def state(self) -> dict[str, Any]:
        """Lecture du record courant pour diagnostic."""


class _GateCore:
    """Transitions pures du record — partagées par les deux implémentations.

    Les écritures passent par le callback CAS de l'implémentation ; en cas de
    conflit l'état est relu puis re-tranché, jamais écrasé à l'aveugle.
    """

    def __init__(self, policy: SourceGatePolicy) -> None:
        self.policy = policy
        self._lease: str | None = None
        self._pending_pause: dict[str, Any] | None = None

    # -- transitions ------------------------------------------------------

    def before_connect(self, record: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """Nouveau record à écrire pour accorder, ou décision de refus levée."""

        state = record.get("state")
        retry_after = _parse_iso(record.get("retry_after"))
        lease_until = _parse_iso(record.get("lease_until"))
        attempts_used = _attempts(record)

        if state == STATE_BLOCKED:
            if record.get("reason_code") == REASON_CONFIGURATION_BLOCKED:
                raise SourceConfigurationBlockedError(
                    "configuration source bloquée — corriger la configuration "
                    "déclarée (ex. AS400_SOURCE_TIME_ZONE) puis intervention "
                    "opérateur requise avant toute nouvelle tentative de connexion IBM i"
                )
            raise SourceAuthenticationBlockedError(
                "authentification source bloquée — intervention opérateur requise "
                "avant toute nouvelle tentative de connexion IBM i"
            )
        if _fresh(lease_until, now):
            raise SourceUnavailablePausedError(
                "une tentative de connexion est déjà en cours",
                retry_after=lease_until,
                reason_code=REASON_CONNECT_IN_FLIGHT,
            )
        if state == STATE_PAUSED:
            if _fresh(retry_after, now):
                raise SourceUnavailablePausedError(
                    "source en pause — aucune tentative avant l'échéance déclarée",
                    retry_after=retry_after,
                    reason_code=str(record.get("reason_code") or REASON_SOURCE_UNAVAILABLE),
                )
            # Pause expirée : une sonde unique est accordée. La tentative est
            # comptée — un crash en vol ne rend pas son sign-on gratuit.
            return self._grant(record, now)
        if attempts_used >= self.policy.max_attempts:
            paused = dict(record)
            paused["state"] = STATE_PAUSED
            paused["reason_code"] = REASON_RETRY_BUDGET_EXHAUSTED
            paused["opened_at"] = paused.get("opened_at") or _iso(now)
            paused["retry_after"] = _iso(now + self.policy.pause_delay(attempts_used=attempts_used))
            paused["lease_until"] = None
            paused["updated_at"] = _iso(now)
            # La pause est d'abord persistée par l'appelant, puis levée : sinon
            # chaque lecteur recalculerait un retry_after différent.
            self._pending_pause = paused
            return paused
        return self._grant(record, now)

    def on_success(self, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        updated = _empty_record(now)
        for key in ("reset_by", "reset_at"):
            updated[key] = record.get(key)
        return updated

    def on_failure(
        self,
        record: dict[str, Any],
        failure_class: ConnectFailureClass,
        now: datetime,
        error_head: str | None,
    ) -> dict[str, Any]:
        updated = dict(record)
        updated["lease_until"] = None
        updated["last_error_head"] = error_head
        updated["updated_at"] = _iso(now)
        if failure_class is ConnectFailureClass.AUTHENTICATION:
            updated["state"] = STATE_BLOCKED
            updated["reason_code"] = REASON_AUTHENTICATION_BLOCKED
            updated["opened_at"] = updated.get("opened_at") or _iso(now)
            updated["retry_after"] = None
            return updated
        if failure_class is ConnectFailureClass.CONFIGURATION:
            # Erreur de configuration non rejouable (ex. SOURCE_CLOCK_MISMATCH) :
            # rejouer le sign-on produirait indéfiniment le même échec — même
            # blocage durable qu'un refus d'authentification, code distinct.
            updated["state"] = STATE_BLOCKED
            updated["reason_code"] = REASON_CONFIGURATION_BLOCKED
            updated["opened_at"] = updated.get("opened_at") or _iso(now)
            updated["retry_after"] = None
            return updated
        attempts_used = _attempts(updated)
        if updated.get("state") == STATE_PAUSED or attempts_used >= self.policy.max_attempts:
            updated["state"] = STATE_PAUSED
            updated["reason_code"] = (
                REASON_RETRY_BUDGET_EXHAUSTED
                if attempts_used >= self.policy.max_attempts
                else REASON_SOURCE_UNAVAILABLE
            )
            updated["opened_at"] = updated.get("opened_at") or _iso(now)
            updated["retry_after"] = _iso(now + self.policy.pause_delay(attempts_used=attempts_used))
        else:
            updated["state"] = STATE_CLOSED
            updated["retry_after"] = None
        return updated

    def on_reset(self, record: dict[str, Any], *, actor: str, now: datetime) -> dict[str, Any]:
        updated = _empty_record(now)
        updated["reset_by"] = actor
        updated["reset_at"] = _iso(now)
        return updated

    def on_pause(self, record: dict[str, Any], *, until: datetime, actor: str, now: datetime) -> dict[str, Any]:
        updated = dict(record)
        updated["state"] = STATE_PAUSED
        updated["reason_code"] = REASON_OPERATOR_PAUSE
        updated["retry_after"] = _iso(until)
        updated["lease_until"] = None
        updated["opened_at"] = updated.get("opened_at") or _iso(now)
        updated["updated_at"] = _iso(now)
        updated["reset_by"] = actor
        updated["reset_at"] = _iso(now)
        return updated

    # -- aides internes -----------------------------------------------------

    def _grant(self, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        lease_until = now + timedelta(seconds=self.policy.lease_seconds)
        self._lease = _iso(lease_until)
        updated = dict(record)
        updated["attempts_used"] = _attempts(record) + 1
        updated["lease_until"] = self._lease
        updated["updated_at"] = _iso(now)
        return updated

    def _raise_paused(self, record: dict[str, Any]) -> None:
        retry_after = _parse_iso(record.get("retry_after")) or datetime.now(timezone.utc)
        raise SourceUnavailablePausedError(
            "budget de tentatives de connexion épuisé — source mise en pause",
            retry_after=retry_after,
            reason_code=str(record.get("reason_code") or REASON_RETRY_BUDGET_EXHAUSTED),
        )


def _attempts(record: dict[str, Any]) -> int:
    raw = record.get("attempts_used")
    return raw if type(raw) is int and raw >= 0 else 0


class FileSourceGate:
    """Garde sur fichier — tests et POC hors ligne. Mêmes transitions que la
    version DynamoDB, sérialisées par un verrou de fichier."""

    def __init__(self, path: str | Path, *, policy: SourceGatePolicy | None = None) -> None:
        self.path = Path(path)
        self._core = _GateCore(policy or SourceGatePolicy())

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_record(datetime.now(timezone.utc))
        record = json.loads(self.path.read_text(encoding="utf-8"))
        if record.get("format_version") != GATE_FORMAT_VERSION:
            raise ValueError("source gate record format unknown")
        return record

    def _locked(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                record = self._read()
                outcome = fn(record)
                if isinstance(outcome, dict):
                    self._write(outcome)
                return outcome
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary_path = Path(temporary_name)
        try:
            with open(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                import os

                os.fsync(handle.fileno())
            temporary_path.replace(self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def before_connect(self, *, now: datetime) -> None:
        self._locked(lambda record: self._core.before_connect(record, now))
        self._raise_pending_pause()

    def _raise_pending_pause(self) -> None:
        paused = self._core._pending_pause
        self._core._pending_pause = None
        if paused is not None:
            self._core._raise_paused(paused)

    def record_connect_success(self, *, now: datetime) -> None:
        def apply(record):
            if self._core._lease is not None and record.get("lease_until") != self._core._lease:
                return record
            return self._core.on_success(record, now)

        self._locked(apply)
        self._core._lease = None

    def record_connect_failure(
        self, failure_class: ConnectFailureClass, *, now: datetime, error_head: str | None = None
    ) -> dict[str, Any] | None:
        def apply(record):
            if self._core._lease is not None and record.get("lease_until") != self._core._lease:
                return record
            return self._core.on_failure(record, failure_class, now, error_head)

        written = self._locked(apply)
        self._core._lease = None
        return written

    def reset(self, *, actor: str, now: datetime) -> dict[str, Any]:
        return self._locked(lambda record: self._core.on_reset(record, actor=actor, now=now))

    def pause_until(self, *, until: datetime, actor: str, now: datetime) -> dict[str, Any]:
        return self._locked(lambda record: self._core.on_pause(record, until=until, actor=actor, now=now))

    def state(self) -> dict[str, Any]:
        return self._read()


class _RevisionedSourceGate:
    """Transitions de garde sur un record versionné : ``_read`` rend le record
    et sa ``revision``, ``_cas_write`` n'écrit que depuis cette révision."""

    def __init__(self, policy: SourceGatePolicy | None) -> None:
        self._core = _GateCore(policy or SourceGatePolicy())

    def _read(self) -> dict[str, Any]:
        raise NotImplementedError

    def _cas_write(self, record: dict[str, Any], expected_revision: int) -> bool:
        raise NotImplementedError

    def _transact(self, fn) -> Any:
        for _ in range(_CAS_RETRIES):
            record = self._read()
            revision = int(record.pop("revision", 0))
            outcome = fn(record)
            if not isinstance(outcome, dict):
                return outcome
            if self._cas_write(outcome, revision):
                return outcome
        raise RuntimeError("source gate compare-and-set conflict")

    def before_connect(self, *, now: datetime) -> None:
        # La décision de refus lève depuis la transition ; la concession est
        # l'écriture CAS elle-même — une tentative accordée puis perdue en vol
        # reste comptée dans attempts_used.
        self._transact(lambda record: self._core.before_connect(record, now))
        paused = self._core._pending_pause
        self._core._pending_pause = None
        if paused is not None:
            self._core._raise_paused(paused)

    def record_connect_success(self, *, now: datetime) -> None:
        def apply(record):
            if self._core._lease is not None and record.get("lease_until") != self._core._lease:
                return None  # un autre détenteur a pris le bail : son état prime
            return self._core.on_success(record, now)

        self._transact(apply)
        self._core._lease = None

    def record_connect_failure(
        self, failure_class: ConnectFailureClass, *, now: datetime, error_head: str | None = None
    ) -> dict[str, Any] | None:
        def apply(record):
            if self._core._lease is not None and record.get("lease_until") != self._core._lease:
                return None
            return self._core.on_failure(record, failure_class, now, error_head)

        written = self._transact(apply)
        self._core._lease = None
        return written

    def reset(self, *, actor: str, now: datetime) -> dict[str, Any]:
        return self._transact(lambda record: self._core.on_reset(record, actor=actor, now=now))

    def pause_until(self, *, until: datetime, actor: str, now: datetime) -> dict[str, Any]:
        return self._transact(
            lambda record: self._core.on_pause(record, until=until, actor=actor, now=now)
        )

    def state(self) -> dict[str, Any]:
        record = self._read()
        record.pop("revision", None)
        return record


class DynamoDbSourceGate(_RevisionedSourceGate):
    """Garde dans la table des checkpoints — item ``<gate_key>`` à côté des
    positions de journal. Un seul item, des écritures conditionnelles : deux
    pods ne peuvent pas doubler le budget."""

    def __init__(
        self,
        table_name: str,
        gate_key: str,
        *,
        policy: SourceGatePolicy | None = None,
        client: Any | None = None,
    ) -> None:
        if not table_name.strip() or table_name.startswith("-"):
            raise ValueError("invalid DynamoDB table name")
        if not gate_key.strip() or len(gate_key) > 256:
            raise ValueError("invalid source gate key")
        super().__init__(policy)
        self.table_name = table_name
        self.gate_key = gate_key
        if client is None:
            import boto3

            client = boto3.client("dynamodb")
        self.client = client

    @staticmethod
    def key_for(host: str, user: str) -> str:
        """Une garde par compte source : toutes les voies contre le même profil
        IBM i partagent le même budget de tentatives."""

        clean_host = host.strip()
        clean_user = user.strip()
        if not clean_host or not clean_user:
            raise ValueError("source gate requires host and user")
        return f"source-gate#{clean_host}#{clean_user}"

    def _read(self) -> dict[str, Any]:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"stream_id": {"S": self.gate_key}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return _empty_record(datetime.now(timezone.utc))
        record: dict[str, Any] = {
            "format_version": GATE_FORMAT_VERSION,
            "state": item.get("state", {}).get("S", STATE_CLOSED),
            "attempts_used": int(item.get("attempts_used", {}).get("N", "0")),
            "lease_until": item.get("lease_until", {}).get("S"),
            "retry_after": item.get("retry_after", {}).get("S"),
            "reason_code": item.get("reason_code", {}).get("S"),
            "opened_at": item.get("opened_at", {}).get("S"),
            "last_error_head": item.get("last_error_head", {}).get("S"),
            "updated_at": item.get("updated_at", {}).get("S"),
            "reset_by": item.get("reset_by", {}).get("S"),
            "reset_at": item.get("reset_at", {}).get("S"),
        }
        revision = item.get("revision", {}).get("N")
        record["revision"] = int(revision) if revision is not None else 0
        return record

    def _cas_write(self, record: dict[str, Any], expected_revision: int) -> bool:
        revision = expected_revision + 1
        item: dict[str, Any] = {
            "stream_id": {"S": self.gate_key},
            "format_version": {"S": GATE_FORMAT_VERSION},
            "revision": {"N": str(revision)},
            "state": {"S": str(record.get("state") or STATE_CLOSED)},
            "attempts_used": {"N": str(_attempts(record))},
        }
        for name in ("lease_until", "retry_after", "reason_code", "opened_at",
                     "last_error_head", "updated_at", "reset_by", "reset_at"):
            value = record.get(name)
            if value is not None:
                item[name] = {"S": str(value)}
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(revision) OR revision = :expected",
                ExpressionAttributeValues={":expected": {"N": str(expected_revision)}},
            )
            return True
        except Exception as error:
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
            if code == "ConditionalCheckFailedException":
                return False
            raise

