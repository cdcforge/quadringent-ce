from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.fleet_capture import (
    FleetRoutingError,
    FleetTableRouter,
    FleetWindowCoordinator,
    parse_fleet_tables,
    split_window_batch,
    table_checkpoint_key,
    table_object_prefix,
)
from quadringent.object_store import (
    FileObjectStore,
    RawFirstCaptureCoordinator,
    read_raw_batch,
)
from quadringent.raw import _batch_id


TABLES = ("ADDRS1", "CNTR", "SALE")
RECEIVER = "DEMOJRN0100"


def event(
    table: str,
    sequence: int,
    *,
    journal: str = "DEMOJRN",
    receiver: str = RECEIVER,
) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi",
        journal=journal,
        library="SALES",
        table=table,
        operation="c",
        position=JournalPosition(receiver, sequence),
        commit_timestamp="2026-09-14T10:00:00Z",
        schema_version="as400-raw-v1",
        before=None,
        after={"ID": str(sequence)},
    )


def window_batch(
    events: list[ChangeEvent],
    *,
    end: JournalPosition | None = None,
) -> tuple[bytes, bytes]:
    """Construit une fenêtre certifiée exactement comme le lecteur Java."""

    unique: list[ChangeEvent] = []
    seen: set[str] = set()
    for item in events:
        if item.event_id not in seen:
            seen.add(item.event_id)
            unique.append(item)
    payload = b"".join(
        json.dumps(item.to_record(), sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for item in unique
    )
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    watermark = end or (unique[-1].position if unique else JournalPosition(RECEIVER, 0))
    event_ids = [item.event_id for item in unique]
    batch_id = _batch_id("as400-raw-v1", event_ids, watermark, payload_sha256)
    manifest = {
        "batch_id": batch_id,
        "format_version": "as400-raw-v1",
        "event_count": len(unique),
        "event_ids": event_ids,
        "high_watermark": {"receiver": watermark.receiver, "sequence": watermark.sequence},
        "payload_sha256": payload_sha256,
    }
    return (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        payload,
    )


@pytest.fixture()
def router(tmp_path: Path) -> FleetTableRouter:
    def store_factory(prefix: str) -> FileObjectStore:
        store = FileObjectStore(tmp_path / prefix)
        stores[prefix] = store
        return store

    def checkpoint_factory(stream_key: str) -> JsonCheckpointStore:
        checkpoint = JsonCheckpointStore(
            tmp_path / "checkpoints" / (stream_key.replace("/", "_") + ".json")
        )
        checkpoints[stream_key] = checkpoint
        return checkpoint

    stores: dict[str, FileObjectStore] = {}
    checkpoints: dict[str, JsonCheckpointStore] = {}
    router = FleetTableRouter(
        root="as400/sales",
        tables=TABLES,
        store_factory=store_factory,
        checkpoint_factory=checkpoint_factory,
    )
    router.stores = stores  # type: ignore[attr-defined]
    router.checkpoints = checkpoints  # type: ignore[attr-defined]
    return router


def table_store(router: FleetTableRouter, table: str) -> FileObjectStore:
    return router.stores[table_object_prefix("as400/sales", table)]  # type: ignore[attr-defined]


def table_cursor(
    router: FleetTableRouter,
    table: str,
    *,
    root: str = "as400/sales",
) -> JournalPosition | None:
    key = table_checkpoint_key(root, table)
    checkpoints = getattr(router, "checkpoints", None)
    if checkpoints is not None and key in checkpoints:
        return checkpoints[key].load()
    return router._checkpoint_factory(key).load()  # noqa: SLF001 - inspection de test


def test_table_prefixes_are_stable_and_lowercase() -> None:
    assert table_object_prefix("as400/sales", "CNTR") == "as400/sales/cntr/journal"
    assert table_checkpoint_key("as400/sales/", "cntr") == "as400/sales/cntr/journal"


def test_unsafe_roots_and_tables_are_refused() -> None:
    with pytest.raises(ValueError):
        table_object_prefix("", "CNTR")
    with pytest.raises(ValueError):
        table_object_prefix("a/../b", "CNTR")
    with pytest.raises(ValueError):
        table_object_prefix("root", "CNTR;DROP")
    with pytest.raises(ValueError):
        parse_fleet_tables([])
    with pytest.raises(ValueError):
        parse_fleet_tables(["CNTR", "cntr"])
    with pytest.raises(ValueError):
        parse_fleet_tables("CNTR")


def test_split_keeps_the_original_line_bytes() -> None:
    events = [event("CNTR", 1), event("SALE", 2), event("CNTR", 3)]
    manifest, payload = window_batch(events)

    windows = split_window_batch(manifest, payload, tables=TABLES)

    by_table = {window.table: window for window in windows}
    assert set(by_table) == {"CNTR", "SALE"}
    pays_lines = payload.split(b"\n")[:1] + payload.split(b"\n")[2:3]
    assert by_table["CNTR"].payload == pays_lines[0] + b"\n" + pays_lines[1] + b"\n"
    assert by_table["CNTR"].event_count == 2
    assert by_table["SALE"].event_count == 1


def test_split_output_is_readable_by_the_repository_reader() -> None:
    manifest, payload = window_batch([event("CNTR", 1), event("SALE", 2)])

    for window in split_window_batch(manifest, payload, tables=TABLES):
        batch = read_raw_batch(window.manifest, window.payload)
        assert batch.manifest.batch_id == window.batch_id
        assert all(item.table == window.table for item in batch.events)


def test_split_refuses_a_table_outside_the_manifest() -> None:
    manifest, payload = window_batch([event("CNTR", 1), event("ORDER", 2)])

    with pytest.raises(FleetRoutingError):
        split_window_batch(manifest, payload, tables=TABLES)


def test_split_refuses_a_forged_event_identity() -> None:
    """Un manifeste recalculé ne doit pas blanchir une identité falsifiée."""

    original = event("CNTR", 1)
    forged_payload = json.dumps(
        {**original.to_record(), "event_id": "0" * 64},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    forged_hash = hashlib.sha256(forged_payload).hexdigest()
    forged_manifest = json.dumps(
        {
            "batch_id": _batch_id("as400-raw-v1", [original.event_id], original.position, forged_hash),
            "format_version": "as400-raw-v1",
            "event_count": 1,
            "event_ids": [original.event_id],
            "high_watermark": {"receiver": original.position.receiver, "sequence": original.position.sequence},
            "payload_sha256": forged_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    with pytest.raises(FleetRoutingError):
        split_window_batch(forged_manifest, forged_payload, tables=TABLES)


def test_split_refuses_a_watermark_that_is_not_the_window_end() -> None:
    manifest, payload = window_batch([event("CNTR", 1)])

    with pytest.raises(FleetRoutingError):
        split_window_batch(
            manifest,
            payload,
            tables=TABLES,
            expected_end=JournalPosition(RECEIVER, 99),
        )


def test_router_publishes_only_the_tables_with_events(router: FleetTableRouter) -> None:
    manifest, payload = window_batch([event("CNTR", 1), event("SALE", 2), event("CNTR", 3)])
    end = JournalPosition(RECEIVER, 3)

    report = router.route_window(end=end, manifest_content=manifest, payload=payload)

    assert report.routed_tables == 3
    assert report.published_tables == 3
    assert report.routed_events == 3
    pays = next(window for window in split_window_batch(manifest, payload, tables=TABLES) if window.table == "CNTR")
    assert table_store(router, "CNTR").get(pays.payload_key) == pays.payload
    assert table_store(router, "CNTR").get(pays.manifest_key) == pays.manifest
    assert table_cursor(router, "CNTR") == end
    assert list(Path(table_store(router, "ADDRS1").root).glob("batch-*")) == []


def test_a_table_without_event_still_advances_its_own_cursor(router: FleetTableRouter) -> None:
    manifest, payload = window_batch([event("CNTR", 1)], end=JournalPosition(RECEIVER, 5))

    router.route_window(end=JournalPosition(RECEIVER, 5), manifest_content=manifest, payload=payload)

    assert table_cursor(router, "SALE") == JournalPosition(RECEIVER, 5)
    assert list(Path(table_store(router, "SALE").root).glob("batch-*")) == []


def test_an_empty_scan_covers_every_table(router: FleetTableRouter) -> None:
    report = router.route_window(end=JournalPosition(RECEIVER, 7))

    assert report.published_tables == len(TABLES)
    for table in TABLES:
        assert table_cursor(router, table) == JournalPosition(RECEIVER, 7)


def test_replaying_the_same_window_is_idempotent(router: FleetTableRouter) -> None:
    manifest, payload = window_batch([event("CNTR", 1)], end=JournalPosition(RECEIVER, 4))
    end = JournalPosition(RECEIVER, 4)

    first = router.route_window(end=end, manifest_content=manifest, payload=payload)
    second = router.route_window(end=end, manifest_content=manifest, payload=payload)

    assert first.published_tables == len(TABLES)
    assert second.reused_tables == len(TABLES)
    assert second.published_tables == 0


def test_a_covered_window_replayed_with_other_bytes_is_refused(router: FleetTableRouter) -> None:
    manifest, payload = window_batch([event("CNTR", 1)], end=JournalPosition(RECEIVER, 4))
    end = JournalPosition(RECEIVER, 4)
    router.route_window(end=end, manifest_content=manifest, payload=payload)

    other_manifest, other_payload = window_batch([event("CNTR", 1), event("SALE", 4)], end=end)
    # La fenêtre est republiée sous un autre batch_id : la table déjà couverte
    # pointe un objet absent, ce qui doit échouer au lieu de passer inaperçu.
    with pytest.raises(FleetRoutingError):
        router.route_window(end=end, manifest_content=other_manifest, payload=other_payload)


def test_a_table_cursor_ahead_of_the_window_is_refused(router: FleetTableRouter) -> None:
    router.route_window(end=JournalPosition(RECEIVER, 9))

    manifest, payload = window_batch([event("CNTR", 1)], end=JournalPosition(RECEIVER, 4))
    with pytest.raises(FleetRoutingError):
        router.route_window(end=JournalPosition(RECEIVER, 4), manifest_content=manifest, payload=payload)


def test_receiver_rotation_is_recorded_per_table(router: FleetTableRouter) -> None:
    manifest, payload = window_batch([event("CNTR", 1)], end=JournalPosition(RECEIVER, 4))
    router.route_window(end=JournalPosition(RECEIVER, 4), manifest_content=manifest, payload=payload)

    rotated_manifest, rotated_payload = window_batch(
        [event("CNTR", 1, receiver="DEMOJRN0200")], end=JournalPosition("DEMOJRN0200", 1)
    )
    router.route_window(
        end=JournalPosition("DEMOJRN0200", 1),
        manifest_content=rotated_manifest,
        payload=rotated_payload,
    )

    assert table_cursor(router, "CNTR") == JournalPosition("DEMOJRN0200", 1)


def test_coordinator_routes_before_advancing_the_journal_cursor(tmp_path: Path) -> None:
    journal_store = FileObjectStore(tmp_path / "journal")
    journal_checkpoint = JsonCheckpointStore(tmp_path / "journal.json")
    inner = RawFirstCaptureCoordinator(journal_store, journal_checkpoint)
    router = FleetTableRouter(
        root="fleet",
        tables=TABLES,
        store_factory=lambda prefix: FileObjectStore(tmp_path / prefix),
        checkpoint_factory=lambda key: JsonCheckpointStore(
            tmp_path / "checkpoints" / (key.replace("/", "_") + ".json")
        ),
    )
    coordinator = FleetWindowCoordinator(inner, router)
    manifest, payload = window_batch([event("CNTR", 4)], end=JournalPosition(RECEIVER, 4))

    coordinator.capture_receipted_window_result(
        start=JournalPosition(RECEIVER, 4),
        end=JournalPosition(RECEIVER, 4),
        previous=None,
        manifest_content=manifest,
        payload=payload,
    )

    assert journal_checkpoint.load() == JournalPosition(RECEIVER, 4)
    assert table_cursor(router, "CNTR", root="fleet") == JournalPosition(RECEIVER, 4)
    assert table_cursor(router, "SALE", root="fleet") == JournalPosition(RECEIVER, 4)


def test_the_capture_service_publishes_every_table_through_one_read(tmp_path: Path) -> None:
    """Une seule fenêtre lue, treize couvertures, un seul curseur journal."""

    from datetime import UTC, datetime

    from quadringent.continuous import (
        CapturedWindow,
        ContinuousCaptureService,
        ReceiverSnapshot,
    )
    from test_continuous import FakeCatalog, FakeRunner

    journal_store = FileObjectStore(tmp_path / "journal")
    journal_checkpoint = JsonCheckpointStore(tmp_path / "journal.json")
    journal_checkpoint.commit(JournalPosition(RECEIVER, 9))
    stores: dict[str, FileObjectStore] = {}
    checkpoints: dict[str, JsonCheckpointStore] = {}
    router = FleetTableRouter(
        root="fleet",
        tables=TABLES,
        store_factory=lambda prefix: stores.setdefault(prefix, FileObjectStore(tmp_path / prefix)),
        checkpoint_factory=lambda key: checkpoints.setdefault(
            key, JsonCheckpointStore(tmp_path / "checkpoints" / (key.replace("/", "_") + ".json"))
        ),
    )
    coordinator = FleetWindowCoordinator(
        RawFirstCaptureCoordinator(journal_store, journal_checkpoint), router
    )
    end = JournalPosition(RECEIVER, 19)
    manifest, payload = window_batch([event("CNTR", 12), event("SALE", 15)], end=end)
    service = ContinuousCaptureService(
        FakeCatalog([ReceiverSnapshot("QGPL", RECEIVER, 1, 20)]),
        FakeRunner(CapturedWindow(end, manifest, payload)),
        coordinator,
        journal_checkpoint,
        max_entries=10,
        receipted_scans=True,
        utc_now=lambda: datetime(2026, 9, 14, 12, tzinfo=UTC),
    )

    result = service.run_once()

    assert result.status == "published"
    assert journal_checkpoint.load() == end
    for table in TABLES:
        assert checkpoints[table_checkpoint_key("fleet", table)].load() == end
    pays_store = stores[table_object_prefix("fleet", "CNTR")]
    published = sorted(item.name for item in Path(pays_store.root).glob("batch-*"))
    assert len(published) == 2
    payload_key = next(name for name in published if name.endswith(".jsonl"))
    batch = read_raw_batch(
        (Path(pays_store.root) / next(name for name in published if name.endswith(".manifest.json"))).read_bytes(),
        (Path(pays_store.root) / payload_key).read_bytes(),
    )
    assert [item.table for item in batch.events] == ["CNTR"]
    assert batch.manifest.high_watermark == end
    assert list(Path(stores[table_object_prefix("fleet", "ADDRS1")].root).glob("batch-*")) == []


def test_coordinator_refuses_an_incomplete_inner_contract() -> None:
    with pytest.raises(ValueError):
        FleetWindowCoordinator(object(), _NoopRouter())


class _NoopRouter(FleetTableRouter):
    def __init__(self) -> None:  # noqa: D107 - doublure minimale
        self.tables = TABLES


def test_route_window_refuses_a_half_supplied_raw() -> None:
    class _Router(FleetTableRouter):
        def __init__(self) -> None:
            self.tables = TABLES
            self.root = "fleet"

    with pytest.raises(ValueError):
        _Router().route_window(end=JournalPosition(RECEIVER, 1), manifest_content=b"{}")


def test_fleet_mode_is_absent_without_environment() -> None:
    from quadringent.fleet_capture import fleet_mode_from_environment

    assert fleet_mode_from_environment({}) is None
    assert fleet_mode_from_environment({"AS400_FLEET_TABLES": "  "}) is None


def test_fleet_mode_requires_both_variables() -> None:
    from quadringent.fleet_capture import fleet_mode_from_environment

    with pytest.raises(ValueError):
        fleet_mode_from_environment({"AS400_FLEET_TABLES": "CNTR,SALE"})
    with pytest.raises(ValueError):
        fleet_mode_from_environment({"AS400_FLEET_TABLE_ROOT": "as400/sales"})


def test_fleet_mode_refuses_a_single_table_or_an_absolute_root() -> None:
    from quadringent.fleet_capture import fleet_mode_from_environment

    with pytest.raises(ValueError):
        fleet_mode_from_environment(
            {"AS400_FLEET_TABLE_ROOT": "root", "AS400_FLEET_TABLES": "CNTR"}
        )
    with pytest.raises(ValueError):
        fleet_mode_from_environment(
            {"AS400_FLEET_TABLE_ROOT": "/absolute", "AS400_FLEET_TABLES": "CNTR,SALE"}
        )


def test_fleet_mode_normalizes_the_declared_manifest() -> None:
    from quadringent.fleet_capture import fleet_mode_from_environment

    mode = fleet_mode_from_environment(
        {"AS400_FLEET_TABLE_ROOT": " as400/sales/ ", "AS400_FLEET_TABLES": "cntr, sale"}
    )

    assert mode is not None
    assert mode.table_root == "as400/sales"
    assert mode.tables == ("CNTR", "SALE")


def test_the_declared_fleet_manifest_matches_the_plan_manifest() -> None:
    """Le manifeste du plan et le runtime de capture ne doivent pas diverger."""

    from quadringent.fleet_capture import fleet_mode_from_environment

    from quadringent_control_plane.fleet import MANIFEST

    assert parse_fleet_tables(MANIFEST) == MANIFEST
    mode = fleet_mode_from_environment(
        {"AS400_FLEET_TABLE_ROOT": "as400/sales", "AS400_FLEET_TABLES": ",".join(MANIFEST)}
    )
    assert mode is not None
    assert mode.tables == MANIFEST
    assert len(mode.tables) == 13
