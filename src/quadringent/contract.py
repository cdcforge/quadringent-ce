from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True, order=True)
class JournalPosition:
    """A journal receiver and sequence pair supplied by the capture layer."""

    receiver: str
    sequence: int

    def __post_init__(self) -> None:
        if not self.receiver.strip():
            raise ValueError("journal receiver must not be empty")
        if self.sequence < 0:
            raise ValueError("journal sequence must be non-negative")


@dataclass(frozen=True)
class ChangeEvent:
    """Canonical event envelope shared by raw storage and downstream loaders.

    ``u`` is a logical update with both images. ``u_before`` and ``u_after``
    are the explicit technical IBM i journal entries emitted when ``*BOTH``
    is enabled. They are intentionally not collapsed at capture time: each
    journal entry has its own native position and must remain replayable.
    """

    source_system: str
    journal: str
    library: str
    table: str
    operation: str
    position: JournalPosition
    commit_timestamp: str
    schema_version: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def __post_init__(self) -> None:
        if (self.before is not None and not isinstance(self.before, dict)) or (
            self.after is not None and not isinstance(self.after, dict)
        ):
            raise ValueError("row images must be objects or null")
        if self.operation not in {"c", "u", "u_before", "u_after", "d"}:
            raise ValueError("operation must be one of c, u, u_before, u_after, d")
        if not all(
            value.strip()
            for value in (
                self.source_system,
                self.journal,
                self.library,
                self.table,
                self.commit_timestamp,
                self.schema_version,
            )
        ):
            raise ValueError("event identity and timestamp fields must not be empty")
        # Une creation n'a pas d'etat anterieur et une suppression n'a pas
        # d'etat posterieur. Accepter l'un ou l'autre en silence masquerait un
        # defaut de decodage : mesure du 17/09, aucune des 145 468 259 lignes
        # chargees ne presente ces formes, donc les refuser ne casse rien.
        if self.operation == "c":
            if self.after is None:
                raise ValueError("create event requires an after image")
            if self.before is not None:
                raise ValueError("create event must not carry a before image")
        if self.operation == "d":
            if self.before is None:
                raise ValueError("delete event requires a before image")
            if self.after is not None:
                raise ValueError("delete event must not carry an after image")
        if self.operation == "u" and (self.before is None or self.after is None):
            raise ValueError("logical update event requires before and after images")
        if self.operation == "u_before" and (self.before is None or self.after is not None):
            raise ValueError("technical before-image event requires only a before image")
        if self.operation == "u_after" and (self.after is None or self.before is not None):
            raise ValueError("technical after-image event requires only an after image")

    @property
    def event_id(self) -> str:
        identity = "|".join(
            (
                self.source_system,
                self.journal,
                self.position.receiver,
                str(self.position.sequence),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ChangeEvent":
        """Reconstruct an event from the canonical raw envelope."""

        return cls(
            source_system=str(record["source_system"]),
            journal=str(record["journal"]),
            library=str(record["library"]),
            table=str(record["table"]),
            operation=str(record["operation"]),
            position=JournalPosition(
                receiver=str(record["journal_receiver"]),
                sequence=int(record["journal_sequence"]),
            ),
            commit_timestamp=str(record["commit_timestamp"]),
            schema_version=str(record["schema_version"]),
            before=record.get("before"),
            after=record.get("after"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source_system": self.source_system,
            "journal": self.journal,
            "library": self.library,
            "table": self.table,
            "operation": self.operation,
            "journal_receiver": self.position.receiver,
            "journal_sequence": self.position.sequence,
            "commit_timestamp": self.commit_timestamp,
            "schema_version": self.schema_version,
            "before": self.before,
            "after": self.after,
        }


class EventIdentityConflict(ValueError):
    """Raised when one journal position is observed with different content."""


def deduplicate(events: list[ChangeEvent]) -> list[ChangeEvent]:
    """Keep the first exact replay and reject a conflicting event identity."""

    unique: dict[str, ChangeEvent] = {}
    for event in events:
        previous = unique.get(event.event_id)
        if previous is None:
            unique[event.event_id] = event
            continue
        if _canonical_json(previous.to_record()) != _canonical_json(event.to_record()):
            raise EventIdentityConflict(
                f"journal position reused with different content: {event.event_id}"
            )
    return list(unique.values())


class OffsetLedger:
    """In-memory proof model for raw-then-checkpoint ordering.

    The production adapter will persist this state in the transport/checkpoint
    system. Receiver rotation is intentionally not inferred from names here;
    a later phase must provide an explicit receiver ordering contract.
    """

    def __init__(self) -> None:
        self.observed: JournalPosition | None = None
        self.committed: JournalPosition | None = None

    def observe(self, position: JournalPosition) -> None:
        if self.observed is not None and position.receiver != self.observed.receiver:
            raise ValueError("receiver rotation requires explicit ordering")
        if self.observed is not None and position < self.observed:
            raise ValueError("observed journal position moved backwards")
        self.observed = position

    def commit_raw(self, position: JournalPosition) -> None:
        if self.committed is not None and position.receiver != self.committed.receiver:
            raise ValueError("receiver rotation requires explicit ordering")
        if self.observed is None or position > self.observed:
            raise ValueError("cannot commit an unobserved journal position")
        if self.committed is not None and position < self.committed:
            raise ValueError("committed journal position moved backwards")
        self.committed = position


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
