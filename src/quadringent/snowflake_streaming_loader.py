"""Chargeur Snowpipe Streaming (historique) + MERGE miroir dédupliqué.

Option B de la décision du 23/09/2026 (``docs/decisions/2026-09-23-miroir-
snowflake.md``) : l'historique est alimenté ligne à ligne par le SDK
Snowpipe Streaming haute performance, sur un canal nommé de façon stable
par flux, avec reprise au dernier jeton d'offset connu ; le miroir est
matérialisé par un ``MERGE`` émis par le chargeur juste après le flush de
l'historique, sur un warehouse XS auto-suspendu. Aucune tâche planifiée,
aucun droit ``EXECUTE TASK`` requis pour cette option (contrairement à
l'option A, voir :mod:`quadringent_control_plane.v2.services.destinations`).

Ce module ne consomme que des lots bruts déjà rendus durables (voir
``object_store.RawFirstCaptureCoordinator`` : brut d'abord, checkpoint
ensuite) — jamais un lot en cours d'écriture. Le client Snowpipe Streaming
est injecté (:class:`StreamingClient`) : un adaptateur SDK réel
(:class:`SnowpipeStreamingClientAdapter`) et un faux client de test
(:class:`FakeStreamingClient`) implémentent le même protocole minimal.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
import time
from typing import Any, Callable, Collection, Iterator, Protocol

from .contract import ChangeEvent, JournalPosition
from .raw import RawBatch
from .site_config import SnowflakeScope
from .snowflake_destination import TableDestinationPlan

_CHANNEL_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_MAX_CHANNEL_NAME_LENGTH = 250


def account_url_host(snowflake_account: str) -> str:
    """Hôte standard du compte Snowflake, dérivé de ``snowflake_account``.

    Le SDK Snowpipe Streaming (``snowflake.ingest.streaming``, profil JWT) refuse de
    démarrer sans ``host``/``url`` — ``account`` seul ne suffit pas, contrairement
    à ``snowflake-connector-python`` qui sait le dériver lui-même. Ce module
    fournit donc la même dérivation aux deux clients, pour ne jamais diverger.

    ``snowflake_account`` porte déjà l'identifiant combiné organisation-compte
    tel que stocké par ``destinations`` (ex. ``EXAMPLEORG-EXAMPLEACCOUNT``,
    voir ``_SNOWFLAKE_ACCOUNT`` dans ``connections.py``) — la forme standard
    de l'hôte est alors ``<org>-<compte>.snowflakecomputing.com`` : aucune
    recomposition n'est nécessaire au-delà de la casse (l'hôte est toujours en
    minuscules) et du remplacement des ``_`` par des ``-`` (interdits dans un
    nom d'hôte DNS, autorisés dans un identifiant de compte).
    """

    if not snowflake_account or not snowflake_account.strip():
        raise ValueError("snowflake_account must not be empty")
    normalized = snowflake_account.strip().lower().replace("_", "-")
    return f"{normalized}.snowflakecomputing.com"


def account_url(snowflake_account: str) -> str:
    """URL HTTPS complète du compte Snowflake — voir :func:`account_url_host`."""

    return f"https://{account_url_host(snowflake_account)}"


def channel_name_for(scope: SnowflakeScope, history_table: str, stream_id: str) -> str:
    """Nom de canal Snowpipe Streaming stable pour un flux donné.

    Déterministe à partir de la destination déclarée, de la table
    d'historique et de l'identifiant de flux (p. ex. bibliothèque/table
    source) : deux exécutions du même flux ouvrent le même canal et
    reprennent au jeton d'offset qu'il a déjà validé — ne jamais dériver ce
    nom d'un horodatage ou d'un identifiant de run.
    """

    if not stream_id or not stream_id.strip():
        raise ValueError("stream_id must not be empty")
    raw = f"{scope.database}_{scope.schema}_{history_table}_{stream_id}_HISTORY"
    name = _CHANNEL_NAME_UNSAFE.sub("_", raw).upper()
    if len(name) > _MAX_CHANNEL_NAME_LENGTH:
        raise ValueError(
            f"channel name exceeds {_MAX_CHANNEL_NAME_LENGTH} characters once sanitized: {name!r}"
        )
    return name


def encode_offset_token(position: JournalPosition) -> str:
    """Jeton d'offset Snowpipe Streaming = position de journal IBM i.

    Le compteur de séquence est complété à gauche par des zéros pour que
    deux jetons du même receveur restent comparables lexicographiquement,
    au cas où le SDK ou un outil d'inspection les trie en texte brut.
    """

    return f"{position.receiver}:{position.sequence:020d}"


def decode_offset_token(token: str | None) -> JournalPosition | None:
    if token is None:
        return None
    receiver, sep, sequence = token.partition(":")
    if not sep:
        raise ValueError(f"malformed streaming offset token: {token!r}")
    return JournalPosition(receiver=receiver, sequence=int(sequence))


class StreamingChannel(Protocol):
    """Canal Snowpipe Streaming ouvert — un flux, un canal, un jeton d'offset."""

    latest_committed_offset_token: str | None

    def append_rows(
        self, rows: list[dict[str, Any]], *, start_offset_token: str, end_offset_token: str
    ) -> None: ...

    def initiate_flush(self) -> None: ...

    def wait_for_commit(
        self, predicate: Callable[[str | None], bool], *, timeout_seconds: float
    ) -> None: ...

    def close(self) -> None: ...


class StreamingClient(Protocol):
    def open_channel(self, channel_name: str, *, offset_token: str | None = None) -> StreamingChannel: ...

    def close(self) -> None: ...


class StreamingCommitTimeoutError(RuntimeError):
    """Le canal n'a pas confirmé le jeton d'offset attendu avant l'expiration."""


# --- Faux client de test --------------------------------------------------


@dataclass
class FakeStreamingChannel:
    """Canal en mémoire : enregistre les lignes et le dernier jeton validé."""

    name: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    latest_committed_offset_token: str | None = None
    closed: bool = False
    fail_next_commit: bool = False

    def append_rows(
        self, rows: list[dict[str, Any]], *, start_offset_token: str, end_offset_token: str
    ) -> None:
        if self.closed:
            raise RuntimeError("channel is closed")
        self.rows.extend(rows)
        self._pending_end_token = end_offset_token

    def wait_for_commit(
        self, predicate: Callable[[str | None], bool], *, timeout_seconds: float
    ) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise StreamingCommitTimeoutError(
                f"canal {self.name} : jeton d'offset non confirmé avant {timeout_seconds}s"
            )
        pending = getattr(self, "_pending_end_token", None)
        if pending is not None:
            self.latest_committed_offset_token = pending
        if not predicate(self.latest_committed_offset_token):
            raise StreamingCommitTimeoutError(
                f"canal {self.name} : jeton d'offset non confirmé avant {timeout_seconds}s"
            )

    def initiate_flush(self) -> None:
        if self.closed:
            raise RuntimeError("channel is closed")

    def close(self) -> None:
        self.closed = True


class FakeStreamingClient:
    """Faux client Snowpipe Streaming : un canal en mémoire par nom, réutilisable.

    Rouvrir un canal déjà connu (même nom) reprend son ``latest_committed_offset_token``
    — reproduit la reprise réelle du SDK. Un nom de canal jamais vu démarre
    au jeton fourni (généralement ``None``).
    """

    def __init__(self) -> None:
        self.channels: dict[str, FakeStreamingChannel] = {}
        self.closed = False

    def open_channel(self, channel_name: str, *, offset_token: str | None = None) -> FakeStreamingChannel:
        existing = self.channels.get(channel_name)
        if existing is not None:
            return existing
        channel = FakeStreamingChannel(name=channel_name, latest_committed_offset_token=offset_token)
        self.channels[channel_name] = channel
        return channel

    def close(self) -> None:
        self.closed = True


# --- Adaptateur SDK réel ---------------------------------------------------


class SnowpipeStreamingChannelAdapter:
    """Enveloppe un canal du SDK Snowpipe Streaming haute performance."""

    def __init__(self, raw_channel: Any, *, latest_committed_offset_token: str | None) -> None:
        self._raw_channel = raw_channel
        self.latest_committed_offset_token = latest_committed_offset_token

    def append_rows(
        self, rows: list[dict[str, Any]], *, start_offset_token: str, end_offset_token: str
    ) -> None:
        self._raw_channel.append_rows(
            rows, start_offset_token=start_offset_token, end_offset_token=end_offset_token
        )

    def initiate_flush(self) -> None:
        self._raw_channel.initiate_flush()

    def wait_for_commit(
        self, predicate: Callable[[str | None], bool], *, timeout_seconds: float
    ) -> None:
        try:
            self._raw_channel.wait_for_commit(predicate, timeout_seconds=timeout_seconds)
        except Exception as error:  # le SDK lève sa propre exception de timeout
            raise StreamingCommitTimeoutError(str(error)) from error
        token = getattr(self._raw_channel, "latest_committed_offset_token", None)
        if token is not None:
            self.latest_committed_offset_token = token

    def close(self) -> None:
        self._raw_channel.close()


class SnowpipeStreamingClientAdapter:
    """Enveloppe ``snowflake.ingest.streaming.StreamingIngestClient``.

    L'import du SDK est différé au constructeur : les tests unitaires (qui
    n'utilisent que :class:`FakeStreamingClient`) n'exigent pas la
    dépendance ``snowpipe-streaming`` installée.
    """

    def __init__(
        self,
        *,
        client_name: str,
        database: str,
        schema: str,
        table: str,
        profile_json: str,
    ) -> None:
        from snowflake.ingest.streaming import StreamingIngestClient  # type: ignore[import-not-found]

        self._client = StreamingIngestClient.from_table(
            client_name=client_name,
            db_name=database,
            schema_name=schema,
            table_name=table,
            profile_json=profile_json,
        )

    def open_channel(
        self, channel_name: str, *, offset_token: str | None = None
    ) -> SnowpipeStreamingChannelAdapter:
        raw_channel, status = self._client.open_channel(channel_name, offset_token=offset_token)
        latest = getattr(status, "offset_token", None)
        return SnowpipeStreamingChannelAdapter(
            raw_channel, latest_committed_offset_token=latest if latest is not None else offset_token
        )

    def close(self) -> None:
        self._client.close()


# --- Chargeur historique ---------------------------------------------------


@dataclass(frozen=True)
class HistoryStreamingLoadResult:
    events_appended: int
    events_skipped_already_committed: int
    resumed_from: JournalPosition | None


LOADER_CYCLE_STAGES = ("discovery_raw", "open_channel", "append", "flush", "commit", "merge", "checkpoint")
LOADER_CYCLE_FORMAT = "quadringent-loader-cycle-v1"


def loader_table_tag(table_id: str) -> str:
    """Attribue le relevé à l'identité interne de table, sans la journaliser."""

    return hashlib.sha256(table_id.encode("utf-8")).hexdigest()


class StreamingCycleMetrics:
    """Durées cumulées d'un cycle de chargeur ; aucune mesure de latence métier.

    Les étapes non exécutées restent absentes. Les horloges injectables
    permettent de distinguer les attentes SDK/stockage/SQL sans sondes réseau.
    """

    def __init__(
        self, table_id: str, *, monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self.table_tag = loader_table_tag(table_id)
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._started = monotonic()
        self._started_at = self._utc_now()
        self.stages_ms: dict[str, float | None] = dict.fromkeys(LOADER_CYCLE_STAGES)
        self.failed_stage: str | None = None

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if name not in LOADER_CYCLE_STAGES:
            raise ValueError("étape du chargeur inconnue")
        started = self._monotonic()
        try:
            yield
        except BaseException:
            if self.failed_stage is None:
                self.failed_stage = name
            raise
        finally:
            elapsed = (self._monotonic() - started) * 1000
            self.stages_ms[name] = (self.stages_ms[name] or 0.0) + elapsed

    @property
    def has_work(self) -> bool:
        # Une fenêtre vide avance le checkpoint mais ne constitue pas une livraison.
        return self.stages_ms["append"] is not None or self.stages_ms["merge"] is not None

    def record(self, *, failed: bool = False) -> dict[str, Any]:
        return {
            "format": LOADER_CYCLE_FORMAT, "table_tag": self.table_tag,
            "started_at": self._started_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "finished_at": self._utc_now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cycle_ms": (self._monotonic() - self._started) * 1000,
            "stages_ms": dict(self.stages_ms), "status": "failed" if failed else "success",
            "failed_stage": (self.failed_stage or "prepare") if failed else None,
        }


def measure_loader_stage(metrics: StreamingCycleMetrics | None, name: str) -> AbstractContextManager[None]:
    """Le chemin SQL et les appels SDK isolés restent sans instrumentation."""

    return nullcontext() if metrics is None else metrics.stage(name)


class HistoryStreamingLoader:
    """Consomme un lot brut durable, dans l'ordre, et l'ajoute à l'historique.

    La reprise se fait au ``latest_committed_offset_token`` du canal, pas au
    checkpoint brut : un même lot rejoué après un redémarrage du chargeur ne
    duplique pas les lignes déjà confirmées par Snowflake sur ce canal.
    Quand le canal a perdu son jeton, l'appelant doit aussi fournir les
    ``EVENT_ID`` déjà présents dans l'historique, lus directement en SQL.
    """

    def __init__(
        self,
        *,
        plan: TableDestinationPlan,
        client: StreamingClient,
        stream_id: str,
        commit_timeout_seconds: float = 60.0,
        flush_each_batch: bool = False,
    ) -> None:
        self._plan = plan
        self._client = client
        self._channel_name = channel_name_for(plan.scope, plan.history_table, stream_id)
        self._channel = client.open_channel(self._channel_name)
        self._commit_timeout_seconds = commit_timeout_seconds
        self._flush_each_batch = flush_each_batch
        # Un canal recréé n'a plus son jeton Snowpipe. Les lignes déjà
        # présentes doivent alors être vérifiées par EVENT_ID tant que ce
        # processus vit, même après son premier nouveau lot validé.
        self._needs_history_lookup = self.resume_position() is None

    @property
    def channel_name(self) -> str:
        return self._channel_name

    def resume_position(self) -> JournalPosition | None:
        return decode_offset_token(self._channel.latest_committed_offset_token)

    @property
    def needs_history_lookup(self) -> bool:
        return self._needs_history_lookup

    def load_batch(
        self, batch: RawBatch, *, already_present_ids: Collection[str] = (),
        cycle_metrics: StreamingCycleMetrics | None = None,
    ) -> HistoryStreamingLoadResult:
        resume_from = self.resume_position()
        pending: list[ChangeEvent] = []
        skipped = 0
        for event in batch.events:
            if (
                event.event_id in already_present_ids
                or (
                    resume_from is not None
                    and event.position.receiver == resume_from.receiver
                    and event.position.sequence <= resume_from.sequence
                )
            ):
                skipped += 1
                continue
            pending.append(event)

        if not pending:
            return HistoryStreamingLoadResult(
                events_appended=0, events_skipped_already_committed=skipped, resumed_from=resume_from
            )

        rows = [_history_row(self._plan, event) for event in pending]
        start_token = encode_offset_token(pending[0].position)
        end_token = encode_offset_token(pending[-1].position)
        with measure_loader_stage(cycle_metrics, "append"):
            self._channel.append_rows(rows, start_offset_token=start_token, end_offset_token=end_token)
        if self._flush_each_batch:
            with measure_loader_stage(cycle_metrics, "flush"):
                self._channel.initiate_flush()
        with measure_loader_stage(cycle_metrics, "commit"):
            self._channel.wait_for_commit(
                lambda token: token == end_token, timeout_seconds=self._commit_timeout_seconds
            )
        return HistoryStreamingLoadResult(
            events_appended=len(pending), events_skipped_already_committed=skipped, resumed_from=resume_from
        )

    def close(self) -> None:
        self._channel.close()


def _history_row(plan: TableDestinationPlan, event: ChangeEvent) -> dict[str, Any]:
    source = event.after if event.after is not None else event.before
    row: dict[str, Any] = {
        "EVENT_ID": event.event_id,
        "OPERATION": event.operation,
        "JOURNAL_RECEIVER": event.position.receiver,
        "JOURNAL_SEQUENCE": event.position.sequence,
        "COMMIT_TIMESTAMP": event.commit_timestamp,
    }
    for name in plan.business_column_names:
        row[name] = None if source is None else source.get(name)
    return row


# --- MERGE miroir dédupliqué ------------------------------------------------


@dataclass(frozen=True)
class MirrorMergePlan:
    """MERGE SQL matérialisant le miroir depuis l'historique (option B).

    Déduplique d'abord par ``EVENT_ID`` (un jeton d'offset rejoué, ou un
    canal recréé sous un autre nom après incident, peut réinsérer une ligne
    d'historique identique), puis retient par clé métier l'événement à la
    plus haute séquence *dans la fenêtre courante*. Le chargeur suit l'ordre
    des fenêtres par les reçus, y compris lors d'une rotation qui remet la
    séquence à 1 ; il transmet leurs EVENT_ID à ce MERGE. ``u_before`` ne porte
    que l'état antérieur d'une mise à jour technique et ne doit jamais
    écraser l'état courant du miroir. Une suppression (``d``) matchée
    efface la ligne ; une suppression non matchée n'insère rien.
    """

    plan: TableDestinationPlan

    def merge_sql(self, *, event_ids: tuple[str, ...] | None = None) -> str:
        plan = self.plan
        event_filter = ""
        if event_ids is not None:
            unique_ids = tuple(dict.fromkeys(event_ids))
            if not unique_ids or len(unique_ids) > 10_000 or any(
                re.fullmatch(r"[0-9a-f]{64}", event_id) is None for event_id in unique_ids
            ):
                raise ValueError("invalid event IDs for mirror MERGE")
            event_filter = "WHERE EVENT_ID IN (" + ", ".join(f"'{event_id}'" for event_id in unique_ids) + ")"
        columns = plan.business_column_names
        keys = plan.key_columns
        non_key_columns = tuple(c for c in columns if c not in keys)
        history = plan.qualified_history_table
        mirror = plan.qualified_mirror_table

        technical_cols = ("EVENT_ID", "OPERATION", "JOURNAL_RECEIVER", "JOURNAL_SEQUENCE", "COMMIT_TIMESTAMP")
        select_cols = ", ".join(technical_cols + columns + ("INGESTED_AT",))
        key_partition = ", ".join(keys)
        join_on = " AND ".join(f"target.{k} = source.{k}" for k in keys)

        update_assignments = ", ".join(
            [f"{c} = source.{c}" for c in non_key_columns]
            + [
                "EVENT_ID = source.EVENT_ID",
                "JOURNAL_RECEIVER = source.JOURNAL_RECEIVER",
                "JOURNAL_SEQUENCE = source.JOURNAL_SEQUENCE",
                "COMMIT_TIMESTAMP = source.COMMIT_TIMESTAMP",
                "MIRROR_UPDATED_AT = CURRENT_TIMESTAMP()",
            ]
        )
        insert_columns = (
            "EVENT_ID",
            "JOURNAL_RECEIVER",
            "JOURNAL_SEQUENCE",
            "COMMIT_TIMESTAMP",
            "MIRROR_UPDATED_AT",
        ) + columns
        insert_values = ", ".join(
            [
                "source.EVENT_ID",
                "source.JOURNAL_RECEIVER",
                "source.JOURNAL_SEQUENCE",
                "source.COMMIT_TIMESTAMP",
                "CURRENT_TIMESTAMP()",
            ]
            + [f"source.{c}" for c in columns]
        )

        return f"""MERGE INTO {mirror} AS target
USING (
    SELECT {select_cols}
    FROM (
        SELECT {select_cols}
        FROM {history}
        {event_filter}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY EVENT_ID ORDER BY INGESTED_AT DESC) = 1
    )
    WHERE OPERATION != 'u_before'
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY {key_partition}
        ORDER BY JOURNAL_SEQUENCE DESC
    ) = 1
) AS source
ON {join_on}
WHEN MATCHED AND source.OPERATION = 'd' THEN DELETE
WHEN MATCHED THEN UPDATE SET {update_assignments}
WHEN NOT MATCHED AND source.OPERATION != 'd' THEN INSERT ({", ".join(insert_columns)})
VALUES ({insert_values})"""

    def execute(self, cursor: Any, *, event_ids: tuple[str, ...] | None = None) -> None:
        cursor.execute(self.merge_sql(event_ids=event_ids))


# --- Retard mesuré (historique + miroir) ------------------------------------


@dataclass(frozen=True)
class StreamingLagMetrics:
    """Retards mesurés, exposés tels quels à la couche d'observation v2
    (``quadringent_control_plane.v2.services.observation.PipelineObservation``
    .history_lag_seconds/.mirror_lag_seconds). ``None`` quand la table est
    encore vide (aucun événement chargé) — jamais 0, jamais dérivé d'une
    autre figure.
    """

    history_lag_seconds: float | None
    mirror_lag_seconds: float | None


@dataclass(frozen=True)
class LagQueryPlan:
    """Requêtes SQL mesurant le retard : écart entre le ``COMMIT_TIMESTAMP``
    IBM i le plus récent chargé et l'instant de la requête — la fraîcheur du
    dernier événement vu, pas une moyenne ni un débit.
    """

    plan: TableDestinationPlan

    def history_lag_sql(self) -> str:
        return (
            "SELECT DATEDIFF('millisecond', MAX(COMMIT_TIMESTAMP), SYSDATE()) / 1000.0 "
            f"FROM {self.plan.qualified_history_table}"
        )

    def mirror_lag_sql(self) -> str:
        return (
            "SELECT DATEDIFF('millisecond', MAX(COMMIT_TIMESTAMP), SYSDATE()) / 1000.0 "
            f"FROM {self.plan.qualified_mirror_table}"
        )

    def read(self, cursor: Any) -> StreamingLagMetrics:
        """Exécute les deux requêtes et renvoie les retards mesurés.

        Une table encore vide renvoie ``NULL`` côté Snowflake (``MAX`` sur
        zéro ligne) : traduit en ``None``, jamais en ``0``.
        """

        cursor.execute(self.history_lag_sql())
        history_row = cursor.fetchone()
        history_lag = _first_non_null(history_row)

        cursor.execute(self.mirror_lag_sql())
        mirror_row = cursor.fetchone()
        mirror_lag = _first_non_null(mirror_row)

        return StreamingLagMetrics(history_lag_seconds=history_lag, mirror_lag_seconds=mirror_lag)


def _first_non_null(row: Any) -> float | None:
    if row is None:
        return None
    value = row[0]
    if value is None:
        return None
    return float(value)
