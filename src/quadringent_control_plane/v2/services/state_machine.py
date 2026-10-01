"""Machine à états déclarée du pipeline (``declared_state``).

Table-driven, exactement selon le contrat §1.2/§1.3
(``docs/plans/2026-09-23-control-plane-v2-contract.md``). ``declared_state``
est piloté par des actions et persisté ; il reste distinct de
l'``observed_state`` dérivé des preuves (``PipelineProjection.status`` dans
``model.py``/``repository.py``), qui n'est jamais muté directement par cette
machine — seule la couche de projection v1 continue de le calculer.

Aucune notion de confirmation n'est modélisée ici (« pending_confirmation »,
tâche 7 du contrat, est hors périmètre de ce chantier) : ``transition``
répond uniquement à la question « cet événement est-il permis dans cet
état ? », sans jugement sur le risque de l'action.
"""

from __future__ import annotations

DECLARED_STATES = frozenset({"not_started", "copying", "live", "paused", "attention", "stopped"})

# Terminal : aucune transition sortante, quel que soit l'événement (cf.
# contrat §1.2 — « stopped ne redevient jamais copying »).
TERMINAL_STATES = frozenset({"stopped"})


class StateMachineError(ValueError):
    """Erreur de base de la machine à états déclarée."""


class UnknownStateError(StateMachineError):
    """``current`` n'est pas un ``declared_state`` connu."""


class ForbiddenTransitionError(StateMachineError):
    """Cette transition n'est listée nulle part au contrat §1.2/§1.3."""

    def __init__(self, current: str, event: str) -> None:
        super().__init__(f"transition interdite : {current!r} --{event}--> ?")
        self.current = current
        self.event = event


# (état courant, événement) -> état suivant — reprise exhaustive du contrat.
_ALLOWED_TRANSITIONS: dict[tuple[str, str], str] = {
    ("not_started", "start"): "copying",
    ("copying", "bootstrap_completed"): "live",
    ("copying", "pause"): "paused",
    ("live", "pause"): "paused",
    ("paused", "resume_copying"): "copying",
    ("paused", "resume_live"): "live",
    ("copying", "attention"): "attention",
    ("live", "attention"): "attention",
    ("paused", "attention"): "attention",
    ("attention", "resume"): "copying",
    ("copying", "remove"): "stopped",
    ("live", "remove"): "stopped",
    ("paused", "remove"): "stopped",
    ("attention", "remove"): "stopped",
    ("copying", "restart_initial_copy"): "copying",
    ("live", "restart_initial_copy"): "copying",
    ("paused", "restart_initial_copy"): "copying",
    ("attention", "restart_initial_copy"): "copying",
}

# Ensemble de tous les événements connus, pour distinguer un événement mal
# orthographié (bug appelant) d'une transition simplement interdite dans cet
# état.
KNOWN_EVENTS = frozenset(event for _state, event in _ALLOWED_TRANSITIONS)


def transition(current: str, event: str) -> str:
    """Retourne le ``declared_state`` suivant, ou lève une erreur fermée.

    ``stopped`` est terminal par construction : aucune entrée de
    ``_ALLOWED_TRANSITIONS`` ne part de ``stopped``, donc tout événement y
    échoue avec ``ForbiddenTransitionError`` — sans cas particulier à
    maintenir.
    """

    if current not in DECLARED_STATES:
        raise UnknownStateError(f"declared_state inconnu : {current!r}")
    key = (current, event)
    next_state = _ALLOWED_TRANSITIONS.get(key)
    if next_state is None:
        raise ForbiddenTransitionError(current, event)
    return next_state


def allowed_events(current: str) -> frozenset[str]:
    """Événements permis depuis ``current`` — utile pour exposer les actions."""

    if current not in DECLARED_STATES:
        raise UnknownStateError(f"declared_state inconnu : {current!r}")
    return frozenset(event for (state, event) in _ALLOWED_TRANSITIONS if state == current)
