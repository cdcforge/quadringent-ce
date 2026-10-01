"""Tâche 5 — machine à états déclarée du pipeline (``declared_state``).

Couvre exactement les transitions du contrat §1.2/§1.3
(``docs/plans/2026-09-23-control-plane-v2-contract.md``), y compris les
transitions explicitement interdites (ex. ``stopped`` ne redevient jamais
``copying``).
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2.services.state_machine import (
    DECLARED_STATES,
    ForbiddenTransitionError,
    UnknownStateError,
    transition,
)


# --- Transitions autorisées (§1.2/§1.3) -------------------------------------


@pytest.mark.parametrize(
    "current,event,expected",
    [
        ("not_started", "start", "copying"),
        ("copying", "bootstrap_completed", "live"),
        ("copying", "pause", "paused"),
        ("live", "pause", "paused"),
        ("paused", "resume_copying", "copying"),
        ("paused", "resume_live", "live"),
        ("copying", "attention", "attention"),
        ("live", "attention", "attention"),
        ("paused", "attention", "attention"),
        ("attention", "resume", "copying"),
        ("copying", "remove", "stopped"),
        ("live", "remove", "stopped"),
        ("paused", "remove", "stopped"),
        ("attention", "remove", "stopped"),
        ("copying", "restart_initial_copy", "copying"),
        ("live", "restart_initial_copy", "copying"),
        ("paused", "restart_initial_copy", "copying"),
        ("attention", "restart_initial_copy", "copying"),
    ],
)
def test_allowed_transitions(current: str, event: str, expected: str) -> None:
    assert transition(current, event) == expected


# --- Transitions interdites --------------------------------------------------


@pytest.mark.parametrize(
    "current,event",
    [
        ("stopped", "start"),
        ("stopped", "resume_copying"),
        ("stopped", "resume_live"),
        ("stopped", "resume"),
        ("stopped", "pause"),
        ("stopped", "remove"),
        ("stopped", "restart_initial_copy"),
        ("not_started", "pause"),
        ("not_started", "resume_copying"),
        ("not_started", "remove"),
        ("not_started", "attention"),
        ("live", "start"),
        ("copying", "start"),
        ("paused", "start"),
        ("paused", "bootstrap_completed"),
        ("attention", "pause"),
        ("attention", "bootstrap_completed"),
    ],
)
def test_forbidden_transitions_raise(current: str, event: str) -> None:
    with pytest.raises(ForbiddenTransitionError):
        transition(current, event)


def test_stopped_is_terminal_no_outgoing_transition_at_all() -> None:
    for event in {"start", "pause", "resume", "resume_copying", "resume_live", "remove",
                  "restart_initial_copy", "attention", "bootstrap_completed"}:
        with pytest.raises(ForbiddenTransitionError):
            transition("stopped", event)


def test_unknown_current_state_raises() -> None:
    with pytest.raises(UnknownStateError):
        transition("not-a-real-state", "start")


def test_declared_states_match_the_contract_vocabulary() -> None:
    assert DECLARED_STATES == frozenset(
        {"not_started", "copying", "live", "paused", "attention", "stopped"}
    )
