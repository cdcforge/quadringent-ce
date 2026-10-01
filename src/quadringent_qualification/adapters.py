"""Interfaces des adaptateurs branchés par l'orchestrateur.

Chaque adaptateur isole un effet de bord (IBM i, Docker, objet cloud,
entrepôt) derrière un ``Protocol`` minimal. Les tests du paquet n'utilisent
que des « fakes » en mémoire (voir ``tests/qualification/fakes.py``) : aucun
test de ce dépôt ne contacte un système réel. ``real_source``,
``real_source``, ``real_capture``, ``real_storage`` et ``real_warehouse``
fournissent les adaptateurs concrets, branchés par la CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Mapping, Protocol, Sequence


_RECEIVER_IDENTIFIER = re.compile(r"[A-Za-z0-9_$#@]{1,128}\Z")


@dataclass(frozen=True)
class SourceResult:
    """Résultat générique d'un appel au pilote de source."""

    exit_code: int
    checks: tuple[str, ...] = ()

    def parsed(self, prefix: str) -> list[dict[str, Any]]:
        """Décode les lignes ``PREFIX=<json>`` produites par le pilote de source.

        Reprend le protocole ligne texte du pilote Java de qualification :
        chaque ligne pertinente commence par ``prefix + "="``
        suivi d'un objet JSON.
        """
        import json

        return [
            json.loads(line.split("=", 1)[1])
            for line in self.checks
            if line.startswith(prefix + "=")
        ]


class SourceDriver(Protocol):
    """Pilote de la source IBM i : DML whitelistée, position du journal, dump, rotation."""

    def execute(self, statements: Sequence[str]) -> SourceResult:
        """Exécute des instructions DML whitelistées (INSERT/UPDATE/DELETE)."""
        ...

    def tail(self) -> SourceResult:
        """Receivers avec bibliothèque et séquence, dans l'ordre d'attachement."""
        ...

    def dump(self) -> SourceResult:
        """Relit toutes les lignes de la table cible, telles que vues côté source."""
        ...

    def row_positions(self, starting: tuple[str, int]) -> SourceResult:
        """Chaîne ordonnée ``SRC_RECEIVER`` et positions ``SRC_ROWPOS``.

        Chaque position porte ``JOURNAL_RECEIVER_NAME`` et
        ``SEQUENCE_NUMBER``. Un numéro de séquence seul ne certifie pas une
        rotation du receiver.
        """
        ...

    def rotate(self) -> SourceResult:
        """Force la rotation du receiver de journal courant."""
        ...


@dataclass(frozen=True)
class CaptureBoundary:
    """Position lue avant copie, puis départ exact du lecteur continu.

    La copie prouve ``last_sequence`` ; le lecteur démarre à la position
    suivante, inclusive. La bibliothèque du receiver n'est pas forcément
    celle du journal et doit venir de la même lecture source.
    """

    receiver_library: str
    receiver_name: str
    next_sequence: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.receiver_library, str)
                or _RECEIVER_IDENTIFIER.fullmatch(self.receiver_library) is None
                or not isinstance(self.receiver_name, str)
                or _RECEIVER_IDENTIFIER.fullmatch(self.receiver_name) is None
                or type(self.next_sequence) is not int or self.next_sequence < 1
                or not isinstance(self.observed_at, datetime)
                or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None):
            raise ValueError("qualification capture boundary is invalid")

    @property
    def last_sequence(self) -> int:
        return self.next_sequence - 1

    @property
    def capture_start(self) -> tuple[str, int]:
        return (self.receiver_name, self.next_sequence)


class CaptureRunner(Protocol):
    """Exécute l'image de capture pour une fenêtre bornée.

    ``bootstrap`` conserve toute la frontière lue avant l'instantané lorsque
    ``label == 'snapshot'``. Pour la capture continue, sa position reste une
    frontière de secours ; le checkpoint durable du lecteur a priorité.
    """

    def run(self, *, label: str, max_seconds: int, bootstrap: CaptureBoundary | None,
            env: Mapping[str, str]) -> "CaptureResult":
        ...


@dataclass(frozen=True)
class CaptureResult:
    exit_code: int
    events: tuple[Mapping[str, Any], ...]
    log: str
    observed_count: int | None = None

    @property
    def event_count(self) -> int:
        """Compte reçu du produit, ou nombre de lignes réellement décodées."""
        if self.observed_count is None:
            return len(self.events)
        if type(self.observed_count) is not int or self.observed_count < 0:
            raise ValueError("compte de capture invalide")
        if self.events and len(self.events) != self.observed_count:
            raise ValueError("compte de capture incohérent")
        return self.observed_count


class StorageBackend(Protocol):
    """Lecture/écriture du brut durable (S3/GCS via les backends du produit)."""

    def list_objects(self, prefix: str) -> Sequence[str]:
        ...

    def read_lines(self, key: str) -> Sequence[str]:
        ...

    def read_bytes(self, key: str, max_bytes: int) -> bytes:
        """Lit exactement les octets d'un objet du run, sous une borne stricte."""
        ...

    def object_created_at(self, key: str) -> Any:
        """Horodatage de création de l'objet ``key`` (utilisé pour la fraîcheur)."""
        ...


@dataclass(frozen=True)
class RawReplayEvidence:
    """Observations des lots validés, y compris chaque référence rejouée.

    Les comptes de rejeu sont des occurrences au-delà de la première, les
    listes d'identifiants permettent de retrouver les événements concernés.
    """

    raw_rows: int
    raw_distinct_events: int
    replayed_identical: int
    replayed_divergent: int
    identical_event_ids: tuple[str, ...] = ()
    divergent_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        counts = (self.raw_rows, self.raw_distinct_events, self.replayed_identical, self.replayed_divergent)
        if (any(type(value) is not int or value < 0 for value in counts)
                or (self.raw_rows == 0) != (self.raw_distinct_events == 0)
                or self.raw_rows - self.raw_distinct_events != self.replayed_identical + self.replayed_divergent):
            raise ValueError("comptes de rejeu brut incohérents")
        for count, event_ids in ((self.replayed_identical, self.identical_event_ids),
                                 (self.replayed_divergent, self.divergent_event_ids)):
            if (any(not isinstance(value, str) or not value for value in event_ids)
                    or len(set(event_ids)) != len(event_ids)
                    or (count == 0) != (len(event_ids) == 0) or len(event_ids) > count):
                raise ValueError("identifiants de rejeu brut incohérents")

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_rows": self.raw_rows, "raw_distinct_events": self.raw_distinct_events,
            "replayed_identical": self.replayed_identical, "replayed_divergent": self.replayed_divergent,
            "identical_event_ids": list(self.identical_event_ids),
            "divergent_event_ids": list(self.divergent_event_ids),
        }


class WarehouseLoader(Protocol):
    """Chargement et lecture de l'entrepôt cible (Snowflake en pratique)."""

    def load(self, *, raw_prefix: str) -> None:
        """Charge le brut durable avant les lectures du rapprochement."""
        ...

    def fetch_events(self, *, schema: str) -> Sequence[Mapping[str, Any]]:
        """Relit les évènements canoniques déjà chargés (snapshot + journal)."""
        ...

    def fetch_raw_counts(self, *, schema: str) -> tuple[int, int]:
        """``(lignes brutes, évènements distincts)`` pour le contrôle de rejeu."""
        ...

    def fetch_raw_evidence(self, *, schema: str) -> RawReplayEvidence:
        """Mesure identités et contenus des événements des lots bruts validés."""
        ...

    def fetch_mirror_value(self, *, schema: str, row_key: int, column: str,
                           timeout_seconds: int) -> Any | None:
        """SELECT indépendant et borné de la valeur miroir, sans cache ni oracle."""
        ...

    def fetch_mirror_rows(self, *, schema: str) -> Sequence[Mapping[str, Any]]:
        """Relit les lignes du miroir réel, sans masquer les clés dupliquées."""
        ...
