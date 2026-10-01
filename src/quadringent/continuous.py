from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Callable, Protocol, Sequence

from .contract import JournalPosition
from .object_store import CheckpointStore, RawFirstCaptureCoordinator
from .raw import read_raw_batch
from .source_gate import (
    IbmiUserDisabledError,
    SourceAuthenticationBlockedError,
    SourceConfigurationBlockedError,
    SourceUnavailablePausedError,
)

__all__ = ["IbmiUserDisabledError"]


class ReceiverPlanningError(ValueError):
    """Raised when the source metadata cannot prove a safe next window."""


class CaptureCircuitOpenError(RuntimeError):
    """Too many consecutive source errors; stop instead of retrying forever."""


class SqlWindowTimeout(RuntimeError):
    """A bounded journal window exceeded its deadline. Safe to retry smaller."""


class SqlWindowIncomplete(RuntimeError):
    """A bounded journal window did not certify a complete scan.

    Defined here rather than in sql_window so that java_worker can raise it
    without pulling in a module the RetrieveJournal deployments do not mount.
    """


@dataclass(frozen=True)
class ReceiverSnapshot:
    """One receiver from an explicitly ordered IBM i metadata snapshot."""

    receiver_library: str
    receiver: str
    first_sequence: int | None
    last_sequence: int | None
    status: str | None = None

    def __post_init__(self) -> None:
        if not self.receiver_library.strip() or not self.receiver.strip():
            raise ValueError("receiver identity must not be empty")
        if self.first_sequence is not None and self.first_sequence < 0:
            raise ValueError("receiver first sequence must be non-negative")
        if self.last_sequence is not None and self.last_sequence < 0:
            raise ValueError("receiver last sequence must be non-negative")
        if (
            self.first_sequence is not None
            and self.last_sequence is not None
            and self.last_sequence < self.first_sequence
        ):
            raise ValueError("receiver last sequence must cover its first sequence")


@dataclass(frozen=True)
class CaptureWindow:
    receiver_library: str
    start: JournalPosition
    end: JournalPosition
    rotated_from: JournalPosition | None = None
    sequence_reset: bool = False

    @property
    def rotated(self) -> bool:
        return self.rotated_from is not None


@dataclass(frozen=True)
class CapturedWindow:
    """Exact raw bytes plus the cursor reached by the source reader.

    An empty scan is represented by two ``None`` byte fields. It still carries
    ``scanned_to`` so the cursor can advance over a window containing no rows
    matching the configured table filter.
    """

    scanned_to: JournalPosition
    manifest: bytes | None = None
    payload: bytes | None = None

    def __post_init__(self) -> None:
        if (self.manifest is None) != (self.payload is None):
            raise ValueError("raw manifest and payload must be supplied together")


@dataclass(frozen=True)
class PollResult:
    status: str
    window: CaptureWindow | None
    event_count: int = 0


@dataclass
class CaptureMetrics:
    polls: int = 0
    idle_polls: int = 0
    empty_scans: int = 0
    batches_published: int = 0
    events_published: int = 0
    payload_bytes_published: int = 0
    manifest_bytes_published: int = 0
    receiver_rotations: int = 0
    errors: int = 0
    poll_ms: float = 0.0
    catalog_ms: float = 0.0
    capture_ms: float = 0.0
    publish_ms: float = 0.0
    checkpoint_ms: float = 0.0
    last_watermark: JournalPosition | None = None
    last_source_tail: JournalPosition | None = None
    last_lag_sequences: int | None = None
    # Fenêtre de séquences du receiver qui porte le tail. Sans elle, une console
    # peut afficher la position du curseur mais pas où elle tombe dans le
    # receiver courant — donc pas ce qu'il reste avant la prochaine rotation.
    last_receiver_first_sequence: int | None = None
    last_receiver_last_sequence: int | None = None
    last_poll: dict[str, object] | None = None

    def observe_poll(
        self,
        *,
        poll_ms: float,
        catalog_ms: float,
        capture_ms: float,
        publish_ms: float,
        checkpoint_ms: float,
        payload_bytes: int,
        manifest_bytes: int,
        source_tail: JournalPosition | None,
        lag_sequences: int | None,
        receivers: Sequence[ReceiverSnapshot] | None = None,
    ) -> None:
        self.poll_ms += poll_ms
        self.catalog_ms += catalog_ms
        self.capture_ms += capture_ms
        self.publish_ms += publish_ms
        self.checkpoint_ms += checkpoint_ms
        self.payload_bytes_published += payload_bytes
        self.manifest_bytes_published += manifest_bytes
        self.last_source_tail = source_tail
        self.last_lag_sequences = lag_sequences
        active = _receiver_holding(source_tail, receivers)
        if active is not None:
            self.last_receiver_first_sequence = active.first_sequence
            self.last_receiver_last_sequence = active.last_sequence
        self.last_poll = {
            "poll_ms": round(poll_ms, 3),
            "catalog_ms": round(catalog_ms, 3),
            "capture_ms": round(capture_ms, 3),
            "publish_ms": round(publish_ms, 3),
            "checkpoint_ms": round(checkpoint_ms, 3),
            "payload_bytes": payload_bytes,
            "manifest_bytes": manifest_bytes,
            "source_tail": _position_payload(source_tail),
            "lag_sequences": lag_sequences,
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "polls": self.polls,
            "idle_polls": self.idle_polls,
            "empty_scans": self.empty_scans,
            "batches_published": self.batches_published,
            "events_published": self.events_published,
            "payload_bytes_published": self.payload_bytes_published,
            "manifest_bytes_published": self.manifest_bytes_published,
            "receiver_rotations": self.receiver_rotations,
            "errors": self.errors,
            "poll_ms": round(self.poll_ms, 3),
            "catalog_ms": round(self.catalog_ms, 3),
            "capture_ms": round(self.capture_ms, 3),
            "publish_ms": round(self.publish_ms, 3),
            "checkpoint_ms": round(self.checkpoint_ms, 3),
            "last_watermark": _position_payload(self.last_watermark),
            "last_source_tail": _position_payload(self.last_source_tail),
            "last_lag_sequences": self.last_lag_sequences,
            "last_receiver_first_sequence": self.last_receiver_first_sequence,
            "last_receiver_last_sequence": self.last_receiver_last_sequence,
            "last_poll": self.last_poll,
        }


def _receiver_holding(
    position: JournalPosition | None,
    receivers: Sequence[ReceiverSnapshot] | None,
) -> ReceiverSnapshot | None:
    if position is None or not receivers:
        return None
    for receiver in receivers:
        if receiver.receiver == position.receiver:
            return receiver
    return None


def _position_payload(position: JournalPosition | None) -> dict[str, object] | None:
    if position is None:
        return None
    return {"receiver": position.receiver, "sequence": position.sequence}


def finite_tail_bootstrap(
    receivers: Sequence[ReceiverSnapshot],
    max_entries: int,
) -> JournalPosition:
    """Start inside already-written sequences of the newest receiver.

    ``__TAIL__`` must not mean “wait at last_sequence for the next CNTR row”.
    The first window starts ``max_entries`` back (clamped to
    ``first_sequence``). It may now include ``last_sequence`` even on an
    ATTACHED receiver: the live-tail rule was removed on both read paths,
    see docs/decisions/2026-09-23-queue-vivante.md.
    """

    if max_entries < 1:
        raise ValueError("max_entries must be positive")
    if not receivers:
        raise ReceiverPlanningError("receiver metadata is empty")
    tail = receivers[-1]
    if tail.first_sequence is None or tail.last_sequence is None:
        raise ReceiverPlanningError("tail receiver has incomplete sequence bounds")
    start = max(tail.first_sequence, tail.last_sequence - max_entries + 1)
    return JournalPosition(tail.receiver, start)


def _source_tail(receivers: Sequence[ReceiverSnapshot]) -> JournalPosition | None:
    if not receivers or receivers[-1].last_sequence is None:
        return None
    newest = receivers[-1]
    return JournalPosition(newest.receiver, newest.last_sequence)


def _effective_lag(
    cursor: JournalPosition | None,
    source_tail: JournalPosition | None,
    receivers: Sequence[ReceiverSnapshot],
) -> int | None:
    """Lag for loop decisions: plain difference, then the receiver chain.

    Keeps the previous same-receiver behaviour bit for bit, and stops blanking
    the lag once the reader is a rotation or more behind.
    """

    if source_tail is None:
        return None
    direct = _same_receiver_lag(cursor, source_tail)
    if direct is not None:
        return direct
    return chain_lag(cursor, receivers)


def _planned_max_entries(
    lag_sequences: int | None,
    *,
    max_entries: int,
    catch_up_max_entries: int,
) -> int:
    if (
        lag_sequences is not None
        and lag_sequences > max_entries
        and catch_up_max_entries > max_entries
    ):
        return catch_up_max_entries
    return max_entries


def _same_receiver_lag(
    cursor: JournalPosition | None,
    source_tail: JournalPosition | None,
) -> int | None:
    if cursor is None or source_tail is None or cursor.receiver != source_tail.receiver:
        return None
    lag = source_tail.sequence - cursor.sequence
    return lag if lag >= 0 else None


class ReceiverCatalog(Protocol):
    def snapshot(self, required_receiver: str | None = None) -> Sequence[ReceiverSnapshot]:
        """Return receivers oldest-to-newest in IBM i metadata order.

        ``required_receiver`` demands coverage of that receiver (typically the
        durable checkpoint's): implementations may page or anchor deeper than
        the newest window rather than declare it absent from a bounded view.
        """


class WindowRunner(Protocol):
    def capture(self, window: CaptureWindow) -> CapturedWindow:
        """Read one bounded window without advancing the durable checkpoint."""


def plan_next_window(
    checkpoint: JournalPosition | None,
    receivers: Sequence[ReceiverSnapshot],
    *,
    max_entries: int,
    bootstrap: JournalPosition | None = None,
) -> CaptureWindow | None:
    """Plan one bounded window from an explicit receiver chain.

    Receiver order is supplied by the IBM i metadata adapter. This function
    never sorts receiver names and refuses a sequence gap before crossing a
    receiver boundary. A missing current receiver is a hard stop rather than
    an invitation to silently skip data.
    """

    if max_entries < 1:
        raise ValueError("max_entries must be positive")
    if not receivers:
        raise ReceiverPlanningError("receiver metadata is empty")

    receiver_index = {item.receiver: index for index, item in enumerate(receivers)}
    if len(receiver_index) != len(receivers):
        raise ReceiverPlanningError("receiver metadata contains a duplicate")

    if checkpoint is None:
        if bootstrap is None:
            raise ReceiverPlanningError("an explicit bootstrap position is required")
        index = receiver_index.get(bootstrap.receiver)
        if index is None:
            raise ReceiverPlanningError("bootstrap receiver is not in metadata")
        current = receivers[index]
        start = bootstrap
        if current.last_sequence is None:
            return None
        if current.first_sequence is not None and start.sequence < current.first_sequence:
            raise ReceiverPlanningError("bootstrap position precedes receiver start")
        if start.sequence > current.last_sequence:
            return None
        # Un bootstrap explicite n'est jamais reculé, même s'il vaut
        # last_sequence : reculer ici retraverserait des entrées déjà
        # couvertes par un snapshot initial qui s'arrête exactement à ce
        # point. Seul finite_tail_bootstrap (le mode ``__TAIL__``) calcule un
        # recul borné, et il le fait avant d'appeler cette fonction.
        return _window(current, start, max_entries=max_entries)

    index = receiver_index.get(checkpoint.receiver)
    if index is None:
        raise ReceiverPlanningError("checkpoint receiver is missing from metadata")
    current = receivers[index]
    if current.first_sequence is None or current.last_sequence is None:
        raise ReceiverPlanningError("checkpoint receiver has incomplete sequence bounds")

    next_sequence = checkpoint.sequence + 1
    if next_sequence <= current.last_sequence:
        if next_sequence < current.first_sequence:
            raise ReceiverPlanningError("checkpoint precedes current receiver start")
        return _window(
            current,
            JournalPosition(current.receiver, next_sequence),
            max_entries=max_entries,
        )

    if checkpoint.sequence != current.last_sequence:
        raise ReceiverPlanningError("checkpoint is beyond the current receiver")
    if index == len(receivers) - 1:
        return None

    following = receivers[index + 1]
    if following.first_sequence is None:
        return None
    if following.first_sequence > current.last_sequence + 1:
        # A forward jump means a receiver between the two chain neighbours was
        # purged: those entries are unreachable and the hole is unrecoverable.
        # Stay fail-closed until an operator re-anchors the checkpoint.
        raise ReceiverPlanningError("receiver sequence gap requires operator review")
    if following.last_sequence is None:
        return None
    # A backward step is a journal sequence reset (CHGJRN SEQOPT(*RESET),
    # typical of weekend maintenance). The receiver chain is intact, so the
    # window crosses by attach order and flags the discontinuity for audit.
    sequence_reset = following.first_sequence <= current.last_sequence
    return _window(
        following,
        JournalPosition(following.receiver, following.first_sequence),
        max_entries=max_entries,
        rotated_from=checkpoint,
        sequence_reset=sequence_reset,
    )


def _window(
    receiver: ReceiverSnapshot,
    start: JournalPosition,
    *,
    max_entries: int,
    rotated_from: JournalPosition | None = None,
    sequence_reset: bool = False,
) -> CaptureWindow:
    if receiver.last_sequence is None:
        raise ReceiverPlanningError("receiver has no last sequence")
    # La règle de queue vivante (ne jamais lire last_sequence d'un receiver
    # ATTACHED) a été retirée pour les deux chemins de lecture : voir
    # docs/decisions/2026-09-23-queue-vivante.md. Une fenêtre peut donc
    # atteindre last_sequence, y compris juste après une transition de
    # receiver.
    end_sequence = min(start.sequence + max_entries - 1, receiver.last_sequence)
    if end_sequence < start.sequence:
        raise ReceiverPlanningError("planned window is empty")
    return CaptureWindow(
        receiver_library=receiver.receiver_library,
        start=start,
        end=JournalPosition(receiver.receiver, end_sequence),
        rotated_from=rotated_from,
        sequence_reset=sequence_reset,
    )


class ContinuousCaptureService:
    """Run the source-only continuous raw capture loop.

    The source runner only reads a bounded window. This service owns the
    ordering boundary: exact raw bytes are published before the checkpoint is
    committed, while empty scans advance the cursor because no matching row
    exists to publish. It does not load Snowflake.
    """

    def __init__(
        self,
        catalog: ReceiverCatalog,
        runner: WindowRunner,
        coordinator: RawFirstCaptureCoordinator,
        checkpoint_store: CheckpointStore,
        *,
        max_entries: int,
        catch_up_max_entries: int | None = None,
        bootstrap: JournalPosition | None = None,
        poll_seconds: float = 5.0,
        min_poll_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        receipted_scans: bool = False,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        proof_window_id: str | None = None,
        proof_window_count: int | None = None,
    ) -> None:
        if type(receipted_scans) is not bool:
            raise ValueError('receipted_scans must be a boolean')
        if proof_window_id is not None and not receipted_scans:
            raise ValueError('proof windows require receipted scans')
        if proof_window_count is not None and (
            type(proof_window_count) is not int or not 1 <= proof_window_count <= 128
            or proof_window_id is None
        ):
            raise ValueError('proof window count requires an initial window and a budget of 1..128')
        if poll_seconds < 0:
            raise ValueError("poll_seconds must be non-negative")
        if min_poll_seconds <= 0:
            raise ValueError("min_poll_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if catch_up_max_entries is None:
            catch_up_max_entries = max_entries
        if catch_up_max_entries < 1:
            raise ValueError("catch_up_max_entries must be positive")
        self.catalog = catalog
        self.runner = runner
        self.coordinator = coordinator
        self.checkpoint_store = checkpoint_store
        self.max_entries = max_entries
        self.catch_up_max_entries = max(max_entries, catch_up_max_entries)
        self.bootstrap = bootstrap
        self.poll_seconds = poll_seconds
        self.min_poll_seconds = min_poll_seconds
        self._idle_wait_seconds = min_poll_seconds
        self.sleep = sleep
        self.receipted_scans = receipted_scans
        self.utc_now = utc_now
        self.proof_window_id = proof_window_id
        self.proof_window_count = proof_window_count
        self.active_proof_window_id = proof_window_id
        self.proof_window_index = 1
        self._proof_chain_prepared = False
        self.closed_proof_window = None
        self.metrics = CaptureMetrics()

    @property
    def proof_windows_complete(self) -> bool:
        return (self.proof_window_count is not None
                and self.proof_window_index == self.proof_window_count
                and self.closed_proof_window is not None)

    def _advance_proof_window(self) -> None:
        from .proof_windows import close_eligible_window, prepare_successor_window, prepare_window_chain

        if not self._proof_chain_prepared:
            prepare_window_chain(self.coordinator.store, self.checkpoint_store,
                                 initial_window_id=self.proof_window_id,
                                 window_count=self.proof_window_count)
            self._proof_chain_prepared = True

        if self.active_proof_window_id is None:
            return

        while True:
            if self.closed_proof_window is None:
                self.closed_proof_window = close_eligible_window(
                    self.coordinator.store, self.checkpoint_store,
                    window_id=self.active_proof_window_id, now=self.utc_now(),
                )
            if self.closed_proof_window is None or self.proof_window_count is None or self.proof_windows_complete:
                return
            successor = prepare_successor_window(
                self.coordinator.store, self.checkpoint_store,
                predecessor_id=self.active_proof_window_id, now=self.utc_now(),
            )
            # Advance memory only after the durable link and intent succeeded.
            self.active_proof_window_id = successor['window_id']
            self.proof_window_index += 1
            self.closed_proof_window = None

    def _reset_idle_wait(self) -> None:
        # Toute activité (publication ou rattrapage) prouve que le tail bouge
        # encore : le prochain sommeil oisif repart au plancher plutôt que de
        # garder l'attente longue héritée d'une période calme précédente.
        self._idle_wait_seconds = self.min_poll_seconds

    def _sleep_idle(self) -> None:
        wait = min(self._idle_wait_seconds, self.poll_seconds) if self.poll_seconds > 0 else 0.0
        self.sleep(wait)
        if self.poll_seconds > 0:
            self._idle_wait_seconds = min(wait * 2, self.poll_seconds)

    def run_once(self) -> PollResult:
        self.metrics.polls += 1
        poll_started = time.perf_counter()
        try:
            self._advance_proof_window()
            if self.proof_windows_complete:
                return PollResult('proof_complete', None)
            checkpoint = self.checkpoint_store.load()
            catalog_started = time.perf_counter()
            receivers = self.catalog.snapshot(
                required_receiver=checkpoint.receiver if checkpoint is not None else None
            )
            if not receivers and checkpoint is not None:
                # Catalogue ancré vide = le receiver du checkpoint n'est plus
                # dans la chaîne (purgé côté source). Le planificateur reste
                # fail-closed : la reprise exige un ré-ancrage ou un bootstrap
                # explicite, jamais un saut silencieux qui perdrait le journal.
                raise ReceiverPlanningError(
                    "checkpoint receiver "
                    f"{checkpoint.receiver} is absent from the journal chain"
                    " (purged) — re-anchor or re-bootstrap required"
                )
            catalog_ms = (time.perf_counter() - catalog_started) * 1000
            source_tail = _source_tail(receivers)
            lag_sequences = _effective_lag(
                checkpoint if checkpoint is not None else self.bootstrap,
                source_tail,
                receivers,
            )
            plan = plan_next_window(
                checkpoint,
                receivers,
                max_entries=_planned_max_entries(
                    lag_sequences,
                    max_entries=self.max_entries,
                    catch_up_max_entries=self.catch_up_max_entries,
                ),
                bootstrap=self.bootstrap,
            )
            if plan is None:
                on_idle = getattr(self.catalog, "on_idle", None)
                if callable(on_idle):
                    on_idle()
                else:
                    invalidate = getattr(self.catalog, "invalidate", None)
                    if callable(invalidate):
                        invalidate()
                self.metrics.idle_polls += 1
                self.metrics.observe_poll(
                    poll_ms=(time.perf_counter() - poll_started) * 1000,
                    catalog_ms=catalog_ms,
                    capture_ms=0.0,
                    publish_ms=0.0,
                    checkpoint_ms=0.0,
                    payload_bytes=0,
                    manifest_bytes=0,
                    source_tail=source_tail,
                    lag_sequences=lag_sequences,
                    receivers=receivers,
                )
                self._advance_proof_window()
                return PollResult("idle", None)

            observe = getattr(self.runner, "observe_lag", None)
            if callable(observe):
                observe(lag_sequences)
            started = time.perf_counter()
            captured = self.runner.capture(plan)
            scan_completed_at = self.utc_now() if self.receipted_scans else None
            capture_ms = (time.perf_counter() - started) * 1000
            if captured.scanned_to != plan.end:
                raise ValueError("source runner did not reach the planned window end")

            if captured.manifest is None:
                checkpoint_started = time.perf_counter()
                publish_ms = 0.0
                if self.receipted_scans:
                    receipt_result = self.coordinator.capture_receipted_window_result(
                        start=plan.start, end=captured.scanned_to, previous=checkpoint,
                        manifest_content=captured.manifest, payload=captured.payload,
                        scan_completed_at=scan_completed_at,
                    )
                    publish_ms = receipt_result.publish_ms
                    checkpoint_ms = receipt_result.checkpoint_ms
                else:
                    self.coordinator.advance_without_raw(
                        captured.scanned_to,
                        previous=plan.rotated_from,
                    )
                    checkpoint_ms = (time.perf_counter() - checkpoint_started) * 1000
                self.metrics.empty_scans += 1
                if plan.rotated:
                    self.metrics.receiver_rotations += 1
                self.metrics.last_watermark = captured.scanned_to
                self.metrics.observe_poll(
                    poll_ms=(time.perf_counter() - poll_started) * 1000,
                    catalog_ms=catalog_ms,
                    capture_ms=capture_ms,
                    publish_ms=publish_ms,
                    checkpoint_ms=checkpoint_ms,
                    payload_bytes=0,
                    manifest_bytes=0,
                    source_tail=source_tail,
                    lag_sequences=_effective_lag(captured.scanned_to, source_tail, receivers),
                    receivers=receivers,
                )
                self._advance_proof_window()
                return PollResult("empty_scan", plan)

            batch = read_raw_batch(captured.manifest, captured.payload or b"")
            if batch.manifest.high_watermark != captured.scanned_to:
                raise ValueError("raw high watermark does not match scanned cursor")
            if self.receipted_scans:
                capture_result = self.coordinator.capture_receipted_window_result(
                    start=plan.start, end=captured.scanned_to, previous=checkpoint,
                    manifest_content=captured.manifest, payload=captured.payload,
                    scan_completed_at=scan_completed_at,
                )
                if plan.rotated:
                    self.metrics.receiver_rotations += 1
            elif plan.rotated:
                capture_result = self.coordinator.capture_raw_transition(
                    captured.manifest,
                    captured.payload or b"",
                    previous=plan.rotated_from,
                )
                self.metrics.receiver_rotations += 1
            else:
                capture_result = self.coordinator.capture_raw(
                    captured.manifest,
                    captured.payload or b"",
                    payload_key=f"batch-{batch.manifest.batch_id}.jsonl",
                    manifest_key=f"batch-{batch.manifest.batch_id}.manifest.json",
                )
            self.metrics.batches_published += 1
            self.metrics.events_published += len(batch.events)
            self.metrics.observe_poll(
                poll_ms=(time.perf_counter() - poll_started) * 1000,
                catalog_ms=catalog_ms,
                capture_ms=capture_ms,
                publish_ms=capture_result.publish_ms,
                checkpoint_ms=capture_result.checkpoint_ms,
                payload_bytes=capture_result.payload_bytes,
                manifest_bytes=capture_result.manifest_bytes,
                source_tail=source_tail,
                lag_sequences=_effective_lag(captured.scanned_to, source_tail, receivers),
                receivers=receivers,
            )
            self.metrics.last_watermark = captured.scanned_to
            self._advance_proof_window()
            return PollResult("published", plan, len(batch.events))
        except Exception:
            self.metrics.errors += 1
            raise

    def run(
        self,
        *,
        max_polls: int | None = None,
        max_consecutive_errors: int | None = None,
        stop: Callable[[], bool] | None = None,
        on_result: Callable[[PollResult, dict[str, object]], None] | None = None,
    ) -> dict[str, object]:
        """Run until stopped; ``max_polls`` keeps DEV probes bounded."""

        if max_polls is not None and max_polls < 1:
            raise ValueError("max_polls must be positive")
        if max_consecutive_errors is not None and max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be positive")
        completed = 0
        consecutive_errors = 0
        while max_polls is None or completed < max_polls:
            if stop is not None and stop():
                break
            try:
                result = self.run_once()
            except SourceUnavailablePausedError as pause:
                # Source coupée ou en maintenance : aucun sign-on tant que
                # l'échéance n'est pas atteinte, puis une sonde unique accordée
                # par la garde. L'attente n'est ni un poll ni une erreur de
                # capture — la garde a déjà compté la tentative qui l'a ouverte.
                invalidate = getattr(self.catalog, "invalidate", None)
                if callable(invalidate):
                    invalidate()
                if on_result is not None:
                    metrics = self.metrics.snapshot()
                    metrics["last_error_type"] = type(pause).__name__
                    metrics["source_pause"] = {
                        "retry_after": pause.retry_after.isoformat(),
                        "reason_code": pause.reason_code,
                    }
                    on_result(PollResult("source_paused", None), metrics)
                consecutive_errors = 0
                while True:
                    if stop is not None and stop():
                        return self.metrics.snapshot()
                    remaining = (pause.retry_after - self.utc_now()).total_seconds()
                    if remaining <= 0:
                        break
                    self.sleep(min(remaining, 60.0))
                continue
            except Exception as error:
                completed += 1
                consecutive_errors += 1
                invalidate = getattr(self.catalog, "invalidate", None)
                if callable(invalidate):
                    invalidate()
                if on_result is not None:
                    metrics = self.metrics.snapshot()
                    metrics["last_error_type"] = type(error).__name__
                    metrics["last_error_head"] = _safe_error_head(error)
                    on_result(PollResult("error", None), metrics)
                if (
                    isinstance(error, (SourceAuthenticationBlockedError, SourceConfigurationBlockedError))
                    or _is_ibmi_userid_disabled(error)
                ):
                    # Refus d'authentification ou erreur de configuration non
                    # rejouable : jamais retenté dans la boucle, jamais
                    # présenté comme une simple coupure — voir source_gate.py.
                    raise
                if (
                    max_consecutive_errors is not None
                    and consecutive_errors >= max_consecutive_errors
                ):
                    raise CaptureCircuitOpenError(
                        f"capture circuit opened after {consecutive_errors} "
                        "consecutive capture errors"
                    ) from error
                if max_polls is None or completed < max_polls:
                    self.sleep(self.poll_seconds)
                continue
            consecutive_errors = 0
            completed += 1
            if on_result is not None:
                on_result(result, self.metrics.snapshot())
            if self.proof_windows_complete:
                break
            if result.status == "published" or (
                result.status == "empty_scan"
                and self.metrics.last_lag_sequences is not None
                and self.metrics.last_lag_sequences > 0
            ):
                # Une fenêtre non vide ou un rattrapage en cours prouve que le
                # journal bouge : l'attente oisive repart au plancher.
                self._reset_idle_wait()
            if max_polls is None or completed < max_polls:
                if result.status == "idle" or (
                    result.status == "empty_scan"
                    and (self.metrics.last_lag_sequences is None or self.metrics.last_lag_sequences <= 0)
                ):
                    self._sleep_idle()
        return self.metrics.snapshot()


# Marqueurs dont la présence prouve un refus d'authentification — profil
# désactivé, mot de passe faux ou expiré : rejouer le sign-on verrouillerait
# le compte. ``SQLNonTransientConnectionException`` n'y figure volontairement
# pas : cette classe JDBC couvre aussi une source coupée ou en maintenance,
# qui relève de la pause bornée, jamais du blocage.
_IBMI_USER_DISABLED_MARKERS = (
    "useriddisabled",
    "user id is disabled",
    "as400securityexception",
    "connect_error=user_disabled",
    "connect_error=authentication_failed",
    "password expired",
)


def _is_ibmi_userid_disabled(error: BaseException) -> bool:
    """Fail closed on IBM i authentication refusal; never retry the sign-on.

    JTOpen/PersistentJournalWorker emit the exception class name only
    (``window_error=AS400SecurityException``), not the JDBC phrase. Match the
    explicit auth tokens — connection-level classes are *not* auth evidence.
    """

    if isinstance(error, SourceAuthenticationBlockedError):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _IBMI_USER_DISABLED_MARKERS)


_SAFE_ERROR_HEAD = re.compile(r"[^A-Za-z0-9_.:/=+ -]")
_FORBIDDEN_ERROR_TOKENS = (
    "password",
    "token",
    "secret",
    "dsn",
    "email",
    "jdbc:",
    "authorization",
)


def _safe_error_head(error: BaseException) -> str:
    """Keep a short, non-secret diagnostic from a capture error."""

    head = str(error).replace("\n", " ").strip()[:80]
    lowered = head.lower()
    if any(token in lowered for token in _FORBIDDEN_ERROR_TOKENS):
        return type(error).__name__
    return _SAFE_ERROR_HEAD.sub("", head)


def journal_lag(
    *,
    tail_receiver: str,
    tail_sequence: int | None,
    processed_receiver: str,
    processed_sequence: int | None,
) -> dict[str, object]:
    """Distance between the last processed position and the journal tail.

    Sequence numbers are only comparable inside one receiver, so a tail on a
    different receiver yields ``comparable=False`` rather than a bogus number.
    A processed position ahead of the tail is a fail-closed error: the
    checkpoint must never be ahead of what the journal exposes.
    """

    if tail_sequence is None or processed_sequence is None:
        return {
            "comparable": False,
            "lag_sequences": None,
            "tail_receiver": tail_receiver,
            "processed_receiver": processed_receiver,
            "reason": "unknown journal position",
        }
    tail = int(tail_sequence)
    processed = int(processed_sequence)
    if str(tail_receiver).strip() != str(processed_receiver).strip():
        return {
            "comparable": False,
            "lag_sequences": None,
            "tail_receiver": tail_receiver,
            "processed_receiver": processed_receiver,
        }
    if processed > tail:
        raise ValueError("processed position is ahead of the journal tail")
    return {
        "comparable": True,
        "lag_sequences": tail - processed,
        "tail_receiver": tail_receiver,
        "processed_receiver": processed_receiver,
    }


def lag_trend(
    samples: Sequence[int],
    *,
    floor_ratio: float = 4.0,
    tolerance: float = 0.25,
    floor_absolute: int = 1000,
) -> dict[str, object]:
    """Classify a lag series as bounded, diverging or catching up.

    A reader that keeps up does not hold a flat lag: it oscillates and
    periodically returns to a floor near zero. Judging on the least-squares
    slope alone therefore calls a healthy reader DIVERGING as soon as the last
    sample happens to be high — which is what the 2026-08-26 step 1 run
    produced on a series that returned to 1 four times.

    The verdict is based on whether the floor moves: the reader is diverging
    only when the minimum lag of the last third exceeds the minimum of the
    first third by more than ``floor_ratio``, with a positive slope.
    """

    values = [int(item) for item in samples]
    if len(values) < 2:
        return {
            "verdict": "INCONCLUSIVE",
            "diverging": False,
            "samples": len(values),
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "slope_per_sample": 0.0,
        }

    count = len(values)
    mean_x = (count - 1) / 2
    mean_y = sum(values) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    slope = (
        sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
        / denominator
        if denominator
        else 0.0
    )

    third = max(1, count // 3)
    floor_first = min(values[:third])
    floor_last = min(values[-third:])
    threshold = tolerance * max(abs(mean_y), 1.0)

    # A rising floor is divergence on its own: the reader never returns to the
    # tail. Requiring the slope test as well hid a 1 -> 1377824 climb, because
    # the slope threshold scales with the mean lag (step6, 2026-08-27).
    floor_rose = (
        floor_last > max(floor_first, 1) * floor_ratio
        and floor_last > floor_absolute
    )
    if floor_rose or (slope > threshold and floor_last > floor_first):
        verdict = "DIVERGING"
    elif slope < -threshold and floor_last < floor_first:
        verdict = "CATCHING_UP"
    else:
        verdict = "BOUNDED"

    return {
        "verdict": verdict,
        "diverging": verdict == "DIVERGING",
        "samples": count,
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "mean": round(mean_y, 1),
        "floor_first_third": floor_first,
        "floor_last_third": floor_last,
        "slope_per_sample": round(slope, 3),
    }


def window_backoff(
    *,
    max_entries: int,
    consecutive_timeouts: int,
    floor: int = 100,
    max_consecutive: int = 5,
) -> dict[str, object]:
    """Shrink the journal window after a timeout instead of ending the run.

    A window that cannot finish inside the SQL timeout will not finish on a
    retry of the same size either, so the width is halved per consecutive
    timeout. The checkpoint never advanced on a timed-out window, so retrying
    is safe. After ``max_consecutive`` timeouts the run stops fail-closed
    rather than looping on a journal it cannot read.
    """

    if max_entries < 1:
        raise ValueError("max_entries must be positive")
    timeouts = max(0, int(consecutive_timeouts))
    if timeouts > max_consecutive:
        return {
            "retry": False,
            "max_entries": max(floor, max_entries >> min(timeouts, 20)),
            "reason": "too many consecutive window timeouts",
        }
    width = max_entries
    for _ in range(timeouts):
        width = max(floor, width // 2)
    return {"retry": True, "max_entries": width, "reason": ""}


def budget_exhausted(
    *,
    started_at: float,
    now: float,
    max_seconds: int | None,
    window_seconds: int = 0,
) -> bool:
    """True when the next window would not finish inside the wall-clock budget.

    Used so an escalating soak exits its own loop and still emits the closing
    lag trend, instead of being killed by ``activeDeadlineSeconds``.
    """

    if max_seconds is None:
        return False
    if max_seconds < 0:
        raise ValueError("max_seconds must be non-negative")
    return (now - started_at) + max(window_seconds, 0) >= max_seconds


def adaptive_window(
    *,
    base_max_entries: int,
    lag_sequences: int | None,
    consecutive_timeouts: int,
    catch_up_threshold: int = 1000,
    catch_up_divisor: int = 10,
    ceiling: int = 10000,
    floor: int = 100,
    max_consecutive: int = 5,
) -> dict[str, object]:
    """Window width driven by both the lag and the recent timeout streak.

    A fixed width makes catch-up impossible: on the 2026-08-26 1800 s run the
    lag peaked at 468046 sequences while each poll could only advance 5000.
    Widening while behind shortens the recovery; narrowing after a timeout
    keeps the window inside the SQL deadline.

    Surviving takes precedence over catching up: a timeout streak narrows the
    window even when the lag is large, because a window that keeps timing out
    advances nothing at all. ``ceiling`` must stay at or below the Java-side
    ``AS400_BATCH_ENTRIES`` cap.
    """

    if base_max_entries < 1:
        raise ValueError("base_max_entries must be positive")
    if lag_sequences is not None and int(lag_sequences) < 0:
        raise ValueError("lag_sequences must be non-negative")
    if catch_up_divisor < 1:
        raise ValueError("catch_up_divisor must be positive")

    timeouts = max(0, int(consecutive_timeouts))
    if timeouts > 0:
        state = window_backoff(
            max_entries=base_max_entries,
            consecutive_timeouts=timeouts,
            floor=floor,
            max_consecutive=max_consecutive,
        )
        return {
            "max_entries": state["max_entries"],
            "retry": state["retry"],
            "mode": "backoff",
            "reason": state["reason"],
            "widened": False,
        }

    if lag_sequences is None or int(lag_sequences) <= catch_up_threshold:
        return {
            "max_entries": min(base_max_entries, ceiling),
            "retry": True,
            "mode": "tail",
            "reason": "",
            "widened": False,
        }

    behind = int(lag_sequences)
    widened = min(ceiling, max(base_max_entries, behind // catch_up_divisor))
    return {
        "max_entries": widened,
        "retry": True,
        "mode": "catch_up",
        "reason": "",
        "widened": widened > base_max_entries,
    }


def should_probe_emptiness(
    *,
    lag_sequences: int | None,
    window_sequences: int,
) -> bool:
    """Whether to run the DISPLAY_JOURNAL emptiness probe before an RJ window.

    RetrieveJournal hangs on an empty ATTACHED window, which is why the probe
    exists. But the probe is a full SQL scan of the window: at the measured
    531 seq/s it costs about 38 s on 20000 sequences, against 3 s for the RJ
    window itself, so paying it on every window caps RJ at the SQL scan rate.

    A window can only be empty if it reaches the journal tail. When the reader
    is far behind, the window is entirely inside the backlog and necessarily
    holds entries, so the probe buys nothing. The probe is therefore paid only
    when the window reaches the tail — and there the window is small, so the
    probe is cheap. An unknown lag keeps the protection.
    """

    if window_sequences < 1:
        raise ValueError("window_sequences must be positive")
    if lag_sequences is None:
        return True
    return int(lag_sequences) <= int(window_sequences)


def chain_lag(
    cursor: JournalPosition | None,
    receivers: Sequence[ReceiverSnapshot],
) -> int | None:
    """Cumulative distance from ``cursor`` to the tail of the receiver chain.

    ``_same_receiver_lag`` blanks the lag as soon as the cursor and the tail
    sit on different receivers, which is correct for a bare subtraction but
    unusable once the reader falls a rotation behind — exactly when the lag
    matters most, because that is when catch-up sizing and the emptiness-probe
    decision depend on it.

    The catalogue is ordered oldest-to-newest and carries both bounds per
    receiver, so the distance is the remainder of the cursor's receiver, plus
    every whole receiver after it, plus the consumed part of the tail
    receiver. Returns None fail-closed whenever the chain cannot be trusted:
    unknown receiver, missing bounds, or a cursor outside its own receiver.
    """

    if cursor is None or not receivers:
        return None
    if any(item.first_sequence is None or item.last_sequence is None for item in receivers):
        return None

    index = None
    for position, item in enumerate(receivers):
        if item.receiver == cursor.receiver:
            index = position
            break
    if index is None:
        return None

    here = receivers[index]
    if not (here.first_sequence <= cursor.sequence <= here.last_sequence):
        return None

    lag = here.last_sequence - cursor.sequence
    for item in receivers[index + 1 :]:
        lag += item.last_sequence - item.first_sequence + 1
    return lag


def should_log_poll(
    *,
    status: str,
    polls: int,
    seconds_since_last_log: float,
    interval_s: int,
    warmup_polls: int = 3,
) -> bool:
    """Whether a poll deserves a full metrics line.

    A 24 h soak at tail speed produces on the order of 110000 polls. Emitting
    the full metrics object for each one truncates the pod log and destroys the
    evidence. Polls that published or failed are always logged; quiet polls are
    summarised on an interval.
    """

    if interval_s < 0:
        raise ValueError("interval_s must be non-negative")
    if interval_s == 0:
        return True
    if status not in {"idle", "empty_scan"}:
        return True
    if polls <= warmup_polls:
        return True
    return seconds_since_last_log >= interval_s
