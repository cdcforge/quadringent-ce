from __future__ import annotations

import pytest

from quadringent.continuous import ReceiverSnapshot

from quadringent_control_plane.fleet import FleetError, JournalCheckpoint
from quadringent_control_plane.fleet_prepare_runtime import (
    CODE_CHECKPOINT_UNAVAILABLE,
    PHASE_PREPARED,
    PHASE_PREPARE_FAILED,
    PrepareRuntime,
)
from quadringent_control_plane.fleet_providers import (
    CheckpointUnavailable,
    ReceiverTailCheckpointProvider,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore

from test_fleet_prepare_runtime import FakeLauncher, make_plan


class StaticCatalog:
    """Catalogue figé : la chaîne est rendue dans l'ordre IBM i, jamais retriée."""

    def __init__(self, receivers: list[object], *, error: BaseException | None = None) -> None:
        self._receivers = receivers
        self._error = error
        self.calls = 0

    def snapshot(self) -> list[object]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._receivers)


def snapshot(receiver: str, last: int | None, *, first: int | None = 1) -> ReceiverSnapshot:
    return ReceiverSnapshot(
        receiver_library="JRNLIB1",
        receiver=receiver,
        status="ATTACHED",
        first_sequence=first,
        last_sequence=last,
    )


def test_tail_follows_ibmi_order_not_lexical_order() -> None:
    catalog = StaticCatalog([snapshot("DEMOJRN0099", 900), snapshot("DEMOJRN0100", 120)])
    provider = ReceiverTailCheckpointProvider(catalog)

    checkpoint = provider.current(make_plan())

    assert checkpoint == JournalCheckpoint(receiver="DEMOJRN0100", sequence=120)
    assert catalog.calls == 1


def test_last_written_sequence_of_current_receiver_is_the_tail() -> None:
    catalog = StaticCatalog([snapshot("DEMOJRN0099", 50), snapshot("DEMOJRN0100", 250)])

    assert ReceiverTailCheckpointProvider(catalog).current(make_plan()) == JournalCheckpoint(
        receiver="DEMOJRN0100", sequence=250
    )


def test_empty_receiver_metadata_is_unavailable() -> None:
    provider = ReceiverTailCheckpointProvider(StaticCatalog([]))

    with pytest.raises(CheckpointUnavailable):
        provider.current(make_plan())


def test_tail_receiver_without_last_sequence_is_unavailable() -> None:
    provider = ReceiverTailCheckpointProvider(StaticCatalog([snapshot("DEMOJRN0100", None)]))

    with pytest.raises(CheckpointUnavailable):
        provider.current(make_plan())


def test_non_integer_tail_sequence_is_unavailable() -> None:
    class UntypedTail:
        receiver = "DEMOJRN0100"
        last_sequence = "250"

    provider = ReceiverTailCheckpointProvider(StaticCatalog([UntypedTail()]))

    with pytest.raises(CheckpointUnavailable):
        provider.current(make_plan())


def test_catalog_without_snapshot_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReceiverTailCheckpointProvider(object())  # type: ignore[arg-type]


def test_prepare_persists_the_fresh_tail_checkpoint(tmp_path) -> None:
    catalog = StaticCatalog([snapshot("DEMOJRN0099", 50), snapshot("DEMOJRN0100", 250)])
    provider = ReceiverTailCheckpointProvider(catalog)

    store = AtomicJsonStateStore(tmp_path / "prepare.json")
    launcher = FakeLauncher(store, [])
    outcome = PrepareRuntime(make_plan(), store, provider, launcher).prepare()

    assert outcome.phase == PHASE_PREPARED
    assert outcome.checkpoint == JournalCheckpoint(receiver="DEMOJRN0100", sequence=250)
    assert launcher.calls[0].checkpoint == outcome.checkpoint
    assert store.load()["checkpoint"] == {"receiver": "DEMOJRN0100", "sequence": 250}


def test_catalog_failure_fails_prepare_closed(tmp_path) -> None:
    catalog = StaticCatalog([], error=RuntimeError("JDBC host=192.0.2.10 user=CDCUSER"))
    provider = ReceiverTailCheckpointProvider(catalog)

    store = AtomicJsonStateStore(tmp_path / "prepare.json")
    launcher = FakeLauncher(store, [])
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()

    assert captured.value.code == CODE_CHECKPOINT_UNAVAILABLE
    assert "192.0.2.10" not in captured.value.safe_message
    assert launcher.calls == []
    persisted = store.load()
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["error_code"] == CODE_CHECKPOINT_UNAVAILABLE
    assert persisted["needs_recovery"] is False
