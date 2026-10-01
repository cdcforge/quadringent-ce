"""Fournisseurs DEV concrets des runtimes de flotte.

Le checkpoint de préparation provient du catalogue de receivers IBM i relevé au
moment de l'appel, en lecture seule et dans l'ordre rendu par IBM i. Un état
persisté, un catalogue mis en cache (`CachedReceiverCatalog`) ou le checkpoint
de cutover du plan ne sont pas admissibles : seul un relevé frais l'est.

Aucun lancement de lecteur ni d'orchestrateur historique n'est simulé ici ; les
lanceurs concrets restent à raccorder (voir
`docs/product/end-to-end-completion-contract.md`).
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .fleet import JournalCheckpoint


class CheckpointUnavailable(RuntimeError):
    """La queue du journal ne peut pas être établie avec certitude."""


@runtime_checkable
class ReceiverTail(Protocol):
    """Vue minimale d'un receiver : identité et dernière séquence écrite."""

    receiver: str
    last_sequence: int | None


class ReceiverCatalog(Protocol):
    """Catalogue de receivers ordonné par IBM i ; jamais retrié localement."""

    def snapshot(self) -> Sequence[ReceiverTail]:
        """Relève la chaîne de receivers au moment de l'appel."""


class ReceiverTailCheckpointProvider:
    """Relève la queue du journal courant au moment de l'appel.

    La queue est le dernier receiver de la chaîne ordonnée et sa dernière
    séquence écrite. Une chaîne vide, un receiver de queue sans dernière
    séquence, ou un relevé en échec rendent le checkpoint indisponible : le
    runtime de préparation refuse alors de démarrer plutôt que d'inventer une
    position.
    """

    def __init__(self, catalog: ReceiverCatalog) -> None:
        if catalog is None or not callable(getattr(catalog, "snapshot", None)):
            raise ValueError("catalog must expose a snapshot() call")
        self._catalog = catalog

    def current(self, plan: object) -> JournalCheckpoint:
        receivers = tuple(self._catalog.snapshot())
        if not receivers:
            raise CheckpointUnavailable("receiver metadata is empty")
        tail = receivers[-1]
        receiver = getattr(tail, "receiver", None)
        sequence = getattr(tail, "last_sequence", None)
        if not isinstance(receiver, str) or not receiver.strip():
            raise CheckpointUnavailable("tail receiver identity is unavailable")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise CheckpointUnavailable("tail receiver sequence is unavailable")
        return JournalCheckpoint(receiver=receiver, sequence=sequence)
