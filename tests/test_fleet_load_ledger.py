from __future__ import annotations

from io import BytesIO
import json
import unittest

import site_fixture

from quadringent.fleet_load_ledger import (
    FleetLoadLedger,
    ManifestReceipt,
    TableLedger,
    fleet_load_ledger_key,
    journal_listing,
    ledger_state,
    load_fleet_ledger,
    manifest_event_count,
    manifest_receipt,
    missing_events,
    snapshot_declared,
    parse_fleet_ledger,
    published_events,
    refresh_table_ledger,
    serialize_fleet_ledger,
    unexpected_rows,
)


SITE = site_fixture.build_test_site()


def _storage(objects: dict[str, bytes]):
    """Stockage de test : get/list bornés au dictionnaire donné."""

    class Storage:
        def get_object(self, **kwargs):
            key = kwargs["Key"]
            if key not in objects:
                error = RuntimeError("absent")
                error.response = {"Error": {"Code": "NoSuchKey"}}
                raise error
            payload = objects[key]
            requested = kwargs.get("Range")
            if requested:
                spec = requested.removeprefix("bytes=")
                if spec.startswith("-"):
                    length = int(spec[1:])
                    start = max(0, len(payload) - length)
                    sliced = payload[start:]
                else:
                    start, end = spec.split("-")
                    start = int(start)
                    sliced = payload[start : int(end) + 1]
                return {
                    "Body": BytesIO(sliced),
                    "ContentRange": f"bytes {start}-{start + len(sliced) - 1}/{len(payload)}",
                }
            return {
                "Body": BytesIO(payload),
                "ContentRange": f"bytes 0-{len(payload) - 1}/{len(payload)}",
            }

        def list_objects_v2(self, **kwargs):
            prefix = kwargs.get("Prefix", "")
            keys = [
                {"Key": key}
                for key in sorted(objects)
                if key.startswith(prefix)
            ]
            return {"Contents": keys, "IsTruncated": False}

    return Storage()


def _manifest(
    events: int,
    *,
    receiver: str = "DEMOJRN4115",
    sequence: int | None = None,
) -> bytes:
    document = {
        "batch_id": "x" * 32,
        "event_count": events,
        "event_ids": ["e" * 64] * events,
        "journal_receiver": receiver,
    }
    if sequence is not None:
        document["high_watermark"] = {
            "receiver": receiver,
            "sequence": sequence,
        }
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _key(table: str, batch: str) -> str:
    return f"as400/sales/{table.lower()}/journal/batch-{batch}.jsonl"


class ParseFleetLedgerTests(unittest.TestCase):
    def test_roundtrip_preserves_every_population(self) -> None:
        ledger = FleetLoadLedger()
        entry = ledger.table("SALE")
        entry.receipted[_key("SALE", "a")] = 5
        entry.unreceipted[_key("SALE", "gone")] = 40
        entry.pending = [_key("SALE", "new")]
        entry.baselined = True
        entry.complete = True

        restored = parse_fleet_ledger(json.loads(serialize_fleet_ledger(ledger)))

        self.assertEqual(restored.tables["SALE"].receipted, {_key("SALE", "a"): 5})
        self.assertEqual(restored.tables["SALE"].unreceipted, {_key("SALE", "gone"): 40})
        self.assertEqual(restored.tables["SALE"].pending, [_key("SALE", "new")])
        self.assertTrue(restored.tables["SALE"].baselined)

    def test_corrupt_or_foreign_documents_are_refused_never_reset(self) -> None:
        for document in (
            None,
            "text",
            {},
            {"schema_version": "fleet-load-ledger-v1", "tables": {}},
            {"schema_version": "fleet-load-ledger-v2", "tables": []},
            {
                "schema_version": "fleet-load-ledger-v2",
                "tables": {"SALE": {"receipted": {"bad.txt": 3}}},
            },
            {
                "schema_version": "fleet-load-ledger-v2",
                "tables": {"SALE": {"receipted": {_key("SALE", "a"): -1}}},
            },
            {
                "schema_version": "fleet-load-ledger-v2",
                "tables": {
                    "SALE": {
                        "receipted": {_key("SALE", "a"): 3},
                        "unreceipted": {_key("SALE", "a"): 3},
                        "pending": [],
                    }
                },
            },
        ):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    parse_fleet_ledger(document)


class RefreshTableLedgerTests(unittest.TestCase):
    def test_budget_bounds_new_manifest_reads(self) -> None:
        entry = TableLedger()
        listing = {_key("SALE", str(i)) for i in range(5)}
        reads = []

        report = refresh_table_ledger(
            entry,
            listing,
            fetch=lambda key: reads.append(key) or ManifestReceipt(events=3),
            budget=2,
        )

        self.assertEqual(len(reads), 2)
        self.assertEqual(report["pending_manifests"], 3)
        self.assertEqual(len(entry.pending), 3)
        self.assertFalse(entry.complete)
        self.assertFalse(entry.baselined)

    def test_deleted_files_keep_their_receipt(self) -> None:
        entry = TableLedger(
            receipted={_key("SALE", "old"): 7}, baselined=True, complete=True
        )
        refresh_table_ledger(
            entry,
            set(),
            fetch=lambda key: ManifestReceipt(events=0),
            loaded_file_rows={},
        )

        self.assertEqual(entry.receipted, {_key("SALE", "old"): 7})
        self.assertTrue(entry.complete)

    def test_baseline_adopts_loaded_files_never_listed_once(self) -> None:
        entry = TableLedger()
        listing = {_key("SALE", "a")}
        loaded = {_key("SALE", "a"): 5, _key("SALE", "gone"): 40}

        refresh_table_ledger(
            entry,
            listing,
            fetch=lambda key: ManifestReceipt(events=5),
            loaded_file_rows=loaded,
        )

        self.assertTrue(entry.complete)
        self.assertTrue(entry.baselined)
        self.assertEqual(entry.unreceipted, {_key("SALE", "gone"): 40})

    def test_manifestless_files_become_unreceipted_not_poison(self) -> None:
        entry = TableLedger()
        listing = {_key("SALE", "a"), _key("SALE", "b")}
        loaded = {_key("SALE", "a"): 5}

        refresh_table_ledger(
            entry,
            listing,
            fetch=lambda key: (
                ManifestReceipt(events=5) if key.endswith("a.jsonl") else None
            ),
            loaded_file_rows=loaded,
        )

        self.assertEqual(entry.receipted, {_key("SALE", "a"): 5})
        self.assertEqual(entry.unreceipted, {_key("SALE", "b"): 0})
        self.assertTrue(entry.baselined)

    def test_young_unreceipted_gets_promoted_when_manifest_lands(self) -> None:
        entry = TableLedger()
        # Premier relevé : le manifeste n'est pas encore posé.
        refresh_table_ledger(
            entry, {_key("SALE", "a")}, fetch=lambda key: None, loaded_file_rows={}
        )
        self.assertEqual(entry.unreceipted, {_key("SALE", "a"): 0})
        # Le manifeste arrive : la re-vérification promeut le fichier.
        refresh_table_ledger(
            entry,
            {_key("SALE", "a")},
            fetch=lambda key: ManifestReceipt(events=9),
            loaded_file_rows={},
        )
        self.assertEqual(entry.receipted, {_key("SALE", "a"): 9})
        self.assertEqual(entry.unreceipted, {})


class ReconciliationMathTests(unittest.TestCase):
    def _entry(self, receipted=None, unreceipted=None, pending=(), baselined=True):
        return TableLedger(
            receipted=receipted or {},
            unreceipted=unreceipted or {},
            pending=list(pending),
            baselined=baselined,
            complete=not pending,
        )

    def test_published_is_declared_plus_measured_populations(self) -> None:
        entry = self._entry(
            receipted={_key("SALE", "a"): 5, _key("SALE", "b"): 3},
            unreceipted={_key("SALE", "gone"): 40},
            pending=[_key("SALE", "new")],
        )
        loaded = {_key("SALE", "new"): 7}

        self.assertEqual(published_events(entry, loaded), 55)

    def test_published_is_unknown_before_baseline(self) -> None:
        entry = self._entry(receipted={_key("SALE", "a"): 5}, baselined=False)
        self.assertIsNone(published_events(entry, {}))

    def test_missing_counts_declared_never_loaded(self) -> None:
        entry = self._entry({_key("SALE", "a"): 5, _key("SALE", "b"): 3})
        loaded = {_key("SALE", "a"): 5, _key("SALE", "b"): 1}

        self.assertEqual(missing_events(entry, loaded), 2)

    def test_unexpected_counts_rows_beyond_every_receipt(self) -> None:
        entry = self._entry(
            receipted={_key("SALE", "a"): 5},
            unreceipted={_key("SALE", "gone"): 40},
            pending=[_key("SALE", "new")],
        )
        loaded = {
            _key("SALE", "a"): 8,      # sur-livraison d'un fichier reçu : +3
            _key("SALE", "gone"): 40,  # dans la base : 0
            _key("SALE", "new"): 7,    # en attente de manifeste : exclu
            _key("SALE", "alien"): 4,  # jamais vu nulle part : +4
        }

        self.assertEqual(unexpected_rows(entry, loaded), 7)

    def test_unexpected_is_zero_within_baseline(self) -> None:
        entry = self._entry(
            receipted={_key("SALE", "a"): 5},
            unreceipted={_key("SALE", "gone"): 40},
        )
        loaded = {_key("SALE", "a"): 5, _key("SALE", "gone"): 40}

        self.assertEqual(unexpected_rows(entry, loaded), 0)


class LedgerStorageTests(unittest.TestCase):
    def test_load_returns_none_on_absent_ledger(self) -> None:
        self.assertIsNone(load_fleet_ledger(_storage({}), SITE))

    def test_load_refuses_a_corrupt_ledger(self) -> None:
        storage = _storage({fleet_load_ledger_key(SITE): b"not-json"})

        with self.assertRaises(ValueError):
            load_fleet_ledger(storage, SITE)

    def test_manifest_count_reads_only_the_document_head(self) -> None:
        key = _key("SALE", "a")
        manifest = key[: -len(".jsonl")] + ".manifest.json"
        # Un manifeste de plusieurs Mo : la lecture bornée suffit.
        big = _manifest(11) + b" " * (6 * 1024 * 1024)
        storage = _storage({manifest: big})

        self.assertEqual(manifest_event_count(storage, SITE, key), 11)

    def test_manifest_missing_or_invalid_returns_none(self) -> None:
        storage = _storage({})
        self.assertIsNone(manifest_event_count(storage, SITE, _key("SALE", "a")))

    def test_manifest_receipt_reads_receiver_and_bound_from_tail(self) -> None:
        key = _key("SALE", "a")
        manifest = key[: -len(".jsonl")] + ".manifest.json"
        # Un gros manifeste : receiver et borne vivent après event_ids,
        # dans le suffixe borné — jamais dans l'en-tête.
        big = _manifest(11, receiver="SNAPSHOT:run-1", sequence=9_999) + b" " * (
            6 * 1024 * 1024
        )
        # Le padding casse le suffixe : on reconstruit un vrai document
        # large où le suffixe contient les champs terminaux.
        document = json.loads(big.rstrip())
        document["event_ids"] = ["e" * 64] * 200_000
        big = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        storage = _storage({manifest: big})

        receipt = manifest_receipt(storage, SITE, key)

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.events, 11)
        self.assertEqual(receipt.receiver, "SNAPSHOT:run-1")
        self.assertEqual(receipt.sequence, 9_999)

    def test_manifest_receipt_small_document_parses_bounds(self) -> None:
        key = _key("CNTR", "a")
        manifest = key[: -len(".jsonl")] + ".manifest.json"
        storage = _storage({manifest: _manifest(2, receiver="DEMOJRN9", sequence=42)})

        receipt = manifest_receipt(storage, SITE, key)

        self.assertEqual(
            receipt, ManifestReceipt(events=2, receiver="DEMOJRN9", sequence=42)
        )

    def test_snapshot_bounds_track_snapshot_receivers_only(self) -> None:
        entry = TableLedger()
        receipts = {
            _key("SALE", "a"): ManifestReceipt(
                events=3, receiver="SNAPSHOT:run-1", sequence=10
            ),
            _key("SALE", "b"): ManifestReceipt(
                events=4, receiver="SNAPSHOT:run-1", sequence=14
            ),
            _key("SALE", "c"): ManifestReceipt(
                events=2, receiver="DEMOJRN4115", sequence=700
            ),
            _key("SALE", "d"): ManifestReceipt(events=1),
        }
        listing = set(receipts)
        refresh_table_ledger(
            entry, listing, fetch=lambda key: receipts.get(key)
        )

        self.assertEqual(entry.snapshot_bounds, {"SNAPSHOT:run-1": 14})
        self.assertEqual(snapshot_declared(entry), 14)

    def test_snapshot_declared_is_none_without_snapshot_bounds(self) -> None:
        self.assertIsNone(snapshot_declared(TableLedger()))

    def test_snapshot_bounds_roundtrip(self) -> None:
        ledger = FleetLoadLedger()
        entry = ledger.table("SALE")
        entry.snapshot_bounds["SNAPSHOT:run-1"] = 1_234
        restored = parse_fleet_ledger(json.loads(serialize_fleet_ledger(ledger)))
        self.assertEqual(
            restored.tables["SALE"].snapshot_bounds, {"SNAPSHOT:run-1": 1_234}
        )

    def test_snapshot_bounds_refuse_foreign_receivers(self) -> None:
        document = {
            "schema_version": "fleet-load-ledger-v2",
            "tables": {
                "SALE": {
                    "receipted": {},
                    "unreceipted": {},
                    "pending": [],
                    "snapshot_bounds": {"DEMOJRN4115": 9},
                }
            },
        }
        with self.assertRaises(ValueError):
            parse_fleet_ledger(document)

    def test_journal_listing_collects_only_jsonl(self) -> None:
        prefix = f"{SITE.journal_prefix_for('SALE')}/"
        storage = _storage(
            {
                f"{prefix}batch-a.jsonl": b"x",
                f"{prefix}batch-a.manifest.json": b"{}",
                f"{prefix}notes.txt": b"x",
            }
        )

        self.assertEqual(
            journal_listing(storage, SITE, "SALE"), {f"{prefix}batch-a.jsonl"}
        )

    def test_ledger_state_reports_construction_progress(self) -> None:
        building = TableLedger(
            receipted={_key("SALE", "a"): 5}, pending=[_key("SALE", "b")]
        )
        done = TableLedger(
            receipted={_key("CNTR", "a"): 2},
            unreceipted={_key("CNTR", "gone"): 3},
            baselined=True,
            complete=True,
        )

        state = ledger_state([("SALE", building), ("CNTR", done)])

        self.assertEqual(state["tables_complete"], 1)
        self.assertEqual(state["tables_baselined"], 1)
        self.assertEqual(state["receipted_event_count"], 7)
        self.assertEqual(state["unreceipted_event_count"], 3)
        self.assertEqual(state["pending_file_count"], 1)


if __name__ == "__main__":
    unittest.main()
