from __future__ import annotations

import copy
from datetime import datetime, timezone
import io
import json
import unittest

from quadringent_control_plane.fleet import MANIFEST, FleetError
from quadringent_control_plane.fleet_sidecar import (
    HISTORY_PROGRESS_BUCKET,
    HISTORY_PROGRESS_PREFIX,
    build_fleet_ui_sidecar,
    generate_fleet_ui_sidecar,
    load_history_progress_documents,
    parse_fleet_ui_sidecar,
    parse_history_progress,
)


try:
    from tests.test_fleet_plan import catalog_payload
except ImportError:
    from test_fleet_plan import catalog_payload

GENERATED_AT = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def _chunk(
    worker: int,
    start: int,
    end: int,
    offset: int,
    rows: int,
    status: str,
    bytes_: int = 0,
) -> dict[str, object]:
    return {
        "worker": worker,
        "rrn_start": start,
        "rrn_end": end,
        "ordinal_offset": offset,
        "rows": rows,
        "bytes": bytes_,
        "status": status,
    }


def _progress_doc(
    table: str = "SALE",
    run_id: str = "run-1",
    updated_at: str = "2026-09-17T10:00:00Z",
    chunks: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if chunks is None:
        chunks = [
            _chunk(0, 1, 500, 0, 100, "published", 2048),
            _chunk(0, 501, 1000, 100, 90, "read_done"),
            _chunk(1, 1001, 1500, 190, 0, "pending"),
        ]
    published = [c for c in chunks if c["status"] == "published"]
    return {
        "kind": "history-progress",
        "run_id": run_id,
        "table": table,
        "updated_at": updated_at,
        "max_rrn": 2000,
        "chunk_rows": 500,
        "chunks": chunks,
        "totals": {
            "planned_rows": 1000,
            "published_rows": sum(int(c["rows"]) for c in published),
            "published_bytes": sum(int(c["bytes"]) for c in published),
            "published_objects": len(published),
        },
    }


def _table(sidecar: dict[str, object], name: str = "SALE") -> dict[str, object]:
    return next(t for t in sidecar["fleet"]["tables"] if t["name"] == name)


class ParseHistoryProgressTests(unittest.TestCase):
    def test_valid_document_projects_counts_and_running_status(self) -> None:
        progress = parse_history_progress(_progress_doc())
        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["run_id"], "run-1")
        self.assertEqual(progress["published_rows"], 100)
        self.assertEqual(progress["published_bytes"], 2048)
        self.assertEqual(progress["planned_rows"], 1000)
        self.assertEqual(progress["chunks_published"], 1)
        self.assertEqual(progress["chunks_read_done"], 1)
        self.assertEqual(progress["chunks_pending"], 1)

    def test_all_published_is_complete_any_failed_is_failed(self) -> None:
        done = parse_history_progress(
            _progress_doc(chunks=[_chunk(0, 1, 500, 0, 100, "published")])
        )
        self.assertEqual(done["status"], "complete")
        failed = parse_history_progress(
            _progress_doc(
                chunks=[
                    _chunk(0, 1, 500, 0, 100, "published"),
                    _chunk(1, 501, 1000, 100, 0, "failed"),
                ]
            )
        )
        self.assertEqual(failed["status"], "failed")

    def test_fail_closed_on_inconsistent_or_out_of_scope_documents(self) -> None:
        cases = []
        doc = _progress_doc()
        doc["totals"]["published_rows"] = 999  # ≠ somme des tranches
        cases.append(doc)
        doc = _progress_doc()
        doc["table"] = "UNKNOWN"
        cases.append(doc)
        doc = _progress_doc()
        doc["chunks"][0]["status"] = "guessed"
        cases.append(doc)
        doc = _progress_doc()
        doc["chunks"].append(dict(doc["chunks"][0]))  # doublon de tranche
        doc["totals"]["published_rows"] = 200
        cases.append(doc)
        doc = _progress_doc()
        doc["chunks"][0]["rrn_end"] = 999999  # hors borne max_rrn
        cases.append(doc)
        doc = _progress_doc()
        doc["totals"]["published_bytes"] = 1  # ≠ somme des octets publiés
        cases.append(doc)
        doc = _progress_doc()
        doc["chunks"][0]["bytes"] = -1  # volume négatif
        cases.append(doc)
        doc = _progress_doc()
        doc["chunks"][0]["bytes"] = "2048"  # non entier
        cases.append(doc)
        doc = _progress_doc()
        del doc["chunks"][0]["bytes"]  # schéma fermé : clé absente refusée
        cases.append(doc)
        doc = _progress_doc()
        doc["totals"]["published_bytes"] = "2048"  # total non entier
        cases.append(doc)
        for case in cases:
            with self.subTest(case=json.dumps(case)[:80]):
                with self.assertRaises(FleetError):
                    parse_history_progress(case)


class LoadHistoryProgressTests(unittest.TestCase):
    class _FakeS3:
        def __init__(self, objects: dict[str, tuple[object, bytes]]) -> None:
            self.objects = objects

        def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["Bucket"] == HISTORY_PROGRESS_BUCKET
            assert kwargs["Prefix"] == HISTORY_PROGRESS_PREFIX
            contents = [
                {"Key": key, "LastModified": stamp}
                for key, (stamp, _body) in self.objects.items()
            ]
            return {"Contents": contents, "IsTruncated": False}

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            _stamp, body = self.objects[Key]
            return {"Body": io.BytesIO(body), "ContentLength": len(body)}

    def _client(self, objects: dict[str, tuple[object, bytes]]) -> "LoadHistoryProgressTests._FakeS3":
        return self._FakeS3(objects)

    def test_latest_document_per_table_wins(self) -> None:
        older = _progress_doc(run_id="run-old", updated_at="2026-09-17T09:00:00Z")
        newer = _progress_doc(run_id="run-new", updated_at="2026-09-17T10:00:00Z")
        objects = {
            f"{HISTORY_PROGRESS_PREFIX}run-old.json": (
                datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc),
                json.dumps(older).encode(),
            ),
            f"{HISTORY_PROGRESS_PREFIX}run-new.json": (
                datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
                json.dumps(newer).encode(),
            ),
        }
        documents = load_history_progress_documents(self._client(objects))
        self.assertEqual(documents["SALE"]["run_id"], "run-new")

    def test_invalid_or_oversized_documents_are_explicit_not_absent(self) -> None:
        broken = _progress_doc()
        broken["totals"]["published_rows"] = 42  # incohérent
        objects = {
            f"{HISTORY_PROGRESS_PREFIX}run-bad.json": (
                datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
                json.dumps(broken).encode(),
            ),
        }
        documents = load_history_progress_documents(self._client(objects))
        self.assertEqual(documents["SALE"]["status"], "invalid")

        oversized = {
            f"{HISTORY_PROGRESS_PREFIX}run-big.json": (
                datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
                b"{" + b" " * (300 * 1024),
            ),
        }
        documents = load_history_progress_documents(self._client(oversized))
        # Un document illisible n'est attribuable à aucune table : non observé,
        # jamais projeté sous un nom d'emprunt.
        self.assertNotIn("run-big", documents)
        self.assertNotIn("SALE", documents)

    def test_bounded_listing_refuses_unbounded_inventory(self) -> None:
        objects = {
            f"{HISTORY_PROGRESS_PREFIX}r{i}.json": (
                datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
                json.dumps(_progress_doc(run_id=f"r{i}")).encode(),
            )
            for i in range(3)
        }
        with self.assertRaises(FleetError):
            load_history_progress_documents(self._client(objects), max_keys=2)


class SidecarProgressTests(unittest.TestCase):
    def test_observed_progress_fills_copied_rows_and_bytes(self) -> None:
        sidecar = generate_fleet_ui_sidecar(
            catalog_payload(),
            generated_at=GENERATED_AT,
            history_progress={"SALE": _progress_doc()},
        )
        table = _table(sidecar)
        self.assertEqual(table["copied_rows"], 100)
        # Les octets copiés viennent des lots publiés mesurés, jamais estimés.
        self.assertEqual(table["copied_bytes"], 2048)
        self.assertEqual(table["history_progress"]["status"], "running")
        self.assertEqual(table["history_progress"]["published_rows"], 100)
        self.assertEqual(table["history_progress"]["published_bytes"], 2048)
        other = _table(sidecar, "CNTR")
        self.assertIsNone(other["copied_rows"])
        self.assertIsNone(other["copied_bytes"])
        self.assertIsNone(other["history_progress"])
        parse_fleet_ui_sidecar(sidecar)

    def test_absent_or_invalid_progress_stays_explicitly_unknown(self) -> None:
        sidecar = generate_fleet_ui_sidecar(
            catalog_payload(),
            generated_at=GENERATED_AT,
            history_progress={"SALE": {"kind": "history-progress", "table": "SALE"}},
        )
        table = _table(sidecar)
        self.assertIsNone(table["copied_rows"])
        self.assertIsNone(table["copied_bytes"])
        self.assertEqual(table["history_progress"]["status"], "invalid")
        self.assertIsNone(table["history_progress"]["published_rows"])
        self.assertIsNone(table["history_progress"]["published_bytes"])
        parse_fleet_ui_sidecar(sidecar)

        sidecar = generate_fleet_ui_sidecar(catalog_payload(), generated_at=GENERATED_AT)
        for table in sidecar["fleet"]["tables"]:
            self.assertIsNone(table["copied_rows"])
            self.assertIsNone(table["copied_bytes"])
            self.assertIsNone(table["history_progress"])

    def test_parse_rejects_copied_rows_without_progress_proof(self) -> None:
        sidecar = generate_fleet_ui_sidecar(
            catalog_payload(),
            generated_at=GENERATED_AT,
            history_progress={"SALE": _progress_doc()},
        )
        forged = copy.deepcopy(sidecar)
        _table(forged)["history_progress"] = None
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(forged)

        mismatched = copy.deepcopy(sidecar)
        _table(mismatched)["copied_rows"] = 55
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(mismatched)

        forged_bytes = copy.deepcopy(sidecar)
        _table(forged_bytes)["copied_bytes"] = 4096  # ≠ published_bytes de la preuve
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(forged_bytes)

        dropped_bytes = copy.deepcopy(sidecar)
        _table(dropped_bytes)["copied_bytes"] = None
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(dropped_bytes)

        negative_bytes = copy.deepcopy(sidecar)
        _table(negative_bytes)["copied_bytes"] = -1
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(negative_bytes)

        forged_progress = copy.deepcopy(sidecar)
        _table(forged_progress)["history_progress"]["published_bytes"] = 1
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(forged_progress)


if __name__ == "__main__":
    unittest.main()
