from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from as400_snapshot_publish import snapshot_object_key


class SnapshotPublishTests(unittest.TestCase):
    def test_object_key_uses_table_not_hardcoded_cost1(self) -> None:
        # Disposition unique (quadringent.storage_layout), table en premier
        # segment — corrigé le 24 septembre 2026 : ce module publiait
        # auparavant sous "<racine>/snapshot/<table>/...", un ordre
        # différent de celui du lecteur/chargeur (jamais chargé en
        # conséquence).
        self.assertEqual(
            snapshot_object_key("as400/sales/cntr", "DATE01", "batch-ab.jsonl"),
            "as400/sales/cntr/date01/snapshot/batch-ab.jsonl",
        )

    def test_object_key_rejects_unsafe_table_names(self) -> None:
        with self.assertRaises(ValueError):
            snapshot_object_key("as400/sales/cntr", "../etc", "batch-ab.jsonl")
        with self.assertRaises(ValueError):
            snapshot_object_key("as400/sales/cntr", "SALE PRDEP", "batch-ab.jsonl")


class SnapshotPublishGcsTests(unittest.TestCase):
    def test_gcs_backend_writes_each_batch_once_below_prefix(self) -> None:
        import os
        import tempfile
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch

        import as400_snapshot_publish as publish
        from test_gcs_backend import FakeGcsClient

        client = FakeGcsClient()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "batch-a.jsonl").write_bytes(b"{}\n")
            Path(directory, "batch-a.manifest.json").write_bytes(b"{}")
            environment = {"QUADRINGENT_STORAGE_BACKEND": "gcs", "AS400_RAW_DIRECTORY": directory,
                           "AS400_RAW_BUCKET": "sandbox-raw", "AS400_RAW_PREFIX": "qualification/run-1",
                           "ISERIES_TABLE": "QDC_ORDERS"}
            with patch.dict(os.environ, environment, clear=True), \
                    patch("quadringent.gcs_backend._client", return_value=client), \
                    redirect_stdout(StringIO()):
                self.assertEqual(publish.main(), 0)
                self.assertEqual(publish.main(), 0)
        self.assertEqual(sorted(name for _, name in client.objects), [
            "qualification/run-1/qdc_orders/snapshot/batch-a.jsonl",
            "qualification/run-1/qdc_orders/snapshot/batch-a.manifest.json",
        ])
        self.assertEqual([write[2] for write in client.writes], [0, 0])


if __name__ == "__main__":
    unittest.main()
