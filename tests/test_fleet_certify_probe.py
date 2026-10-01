"""Sonde de certification : mesure figée, document clos, jamais d'estimation."""

from __future__ import annotations

import site_fixture

from datetime import datetime, timezone
import unittest

from quadringent.fleet_certify_probe import (
    CERTIFY_FORMAT,
    TableCertification,
    certify_document,
    measure_table_certification,
    parse_certify_document,
)


SITE = site_fixture.build_test_site()
END = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
LAST_COMMIT = datetime(2026, 9, 20, 9, 59, 50, tzinfo=timezone.utc)
LAST_INGEST = datetime(2026, 9, 20, 9, 59, 56, tzinfo=timezone.utc)


class FakeCursor:
    """Curseur de test : réponses par nature de requête de certification."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.executed: list[str] = []
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params=None) -> None:
        self.executed.append(sql)
        result = self.responder(sql)
        if isinstance(result, Exception):
            raise result
        self._rows = list(result)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        pass


def measured_responder(sql: str):
    """Population saine : brut = dédupliqué, aucune opération inconnue."""

    if "QUALIFY ROW_NUMBER" in sql:
        return [(10, 777, LAST_INGEST, LAST_COMMIT, 36)]
    if "journal_entry_type" in sql:
        return [("SNAPSHOT_ROW", 6), ("ADD_ROW1", 4)]
    if "HASH_AGG(PAYLOAD:event_id" in sql:
        return [(10, 10, 777)]
    raise AssertionError(f"unexpected certify query: {sql}")


def measured(table: str = "SALE", **overrides) -> TableCertification:
    values = {
        "table": table,
        "measured_at": END.isoformat(),
        "window_start_utc": "2026-09-20T09:59:59+00:00",
        "window_end_utc": END.isoformat(),
        "source_count": 10,
        "target_count": 10,
        "missing": 0,
        "extra": 0,
        "duplicates": 0,
        "source_hash": "777",
        "target_hash": "777",
        "destination_freshness_seconds": 0.0,
        "latency_seconds": 6.0,
        "throughput_rows_per_second": 0.01,
        "operations": (("ADD_ROW1", 4), ("SNAPSHOT_ROW", 6)),
    }
    values.update(overrides)
    return TableCertification(**values)


class MeasureCertificationTest(unittest.TestCase):
    def test_measure_composes_frozen_window_proof(self) -> None:
        cursor = FakeCursor(measured_responder)
        cert = measure_table_certification(
            cursor, SITE, "SALE", missing=0, extra=0, pending_files=0, end=END
        )
        self.assertIsNotNone(cert)
        self.assertEqual(cert.table, "SALE")
        self.assertEqual(cert.window_end_utc, END.isoformat())
        self.assertEqual(cert.source_count, 10)
        self.assertEqual(cert.target_count, 10)
        self.assertEqual(cert.duplicates, 0)
        self.assertEqual(cert.source_hash, "777")
        self.assertEqual(cert.target_hash, "777")
        # Population appliquée complète : fraîcheur nulle, latence mesurée.
        self.assertEqual(cert.destination_freshness_seconds, 0.0)
        self.assertEqual(cert.latency_seconds, 6.0)
        self.assertEqual(cert.throughput_rows_per_second, 36 / 3600)
        self.assertEqual(
            cert.operations, (("ADD_ROW1", 4), ("SNAPSHOT_ROW", 6))
        )
        # Chaque requête est figée au même instant — pas de dérive entre
        # brut et dédupliqué.
        self.assertEqual(len(cursor.executed), 3)
        for sql in cursor.executed:
            self.assertIn("AT(TIMESTAMP => TO_TIMESTAMP_TZ", sql)

    def test_measure_counts_duplicates_from_identity(self) -> None:
        def responder(sql: str):
            if "QUALIFY ROW_NUMBER" in sql:
                return [(10, 777, LAST_INGEST, LAST_COMMIT, 36)]
            if "journal_entry_type" in sql:
                return [("ADD_ROW1", 12)]
            if "HASH_AGG(PAYLOAD:event_id" in sql:
                return [(12, 10, 999)]
            raise AssertionError(sql)

        cert = measure_table_certification(
            FakeCursor(responder),
            SITE,
            "SALE",
            missing=0,
            extra=0,
            pending_files=0,
            end=END,
        )
        self.assertIsNotNone(cert)
        self.assertEqual(cert.source_count, 12)
        self.assertEqual(cert.duplicates, 2)

    def test_measure_refuses_when_raw_query_fails(self) -> None:
        def responder(sql: str):
            if "HASH_AGG(PAYLOAD:event_id" in sql:
                return RuntimeError("boom")
            return [(1, 1, LAST_INGEST, LAST_COMMIT, 1)]

        cert = measure_table_certification(
            FakeCursor(responder),
            SITE,
            "SALE",
            missing=0,
            extra=0,
            pending_files=0,
            end=END,
        )
        self.assertIsNone(cert)

    def test_measure_freshness_tracks_backlog(self) -> None:
        # Livraison incomplète : la fraîcheur vaut l'âge du dernier commit
        # observé — borne inférieure honnête de l'arriéré.
        cert = measure_table_certification(
            FakeCursor(measured_responder),
            SITE,
            "SALE",
            missing=3,
            extra=0,
            pending_files=0,
            end=END,
        )
        self.assertIsNotNone(cert)
        self.assertEqual(cert.destination_freshness_seconds, 10.0)

    def test_measure_refuses_unknown_freshness(self) -> None:
        def responder(sql: str):
            if "QUALIFY ROW_NUMBER" in sql:
                return [(10, 777, None, None, 36)]
            if "journal_entry_type" in sql:
                return [("ADD_ROW1", 10)]
            if "HASH_AGG(PAYLOAD:event_id" in sql:
                return [(10, 10, 777)]
            raise AssertionError(sql)

        # Livraison incomplète et frontière temporelle non mesurée :
        # la fraîcheur serait infinie — la mesure est refusée.
        cert = measure_table_certification(
            FakeCursor(responder),
            SITE,
            "SALE",
            missing=1,
            extra=0,
            pending_files=0,
            end=END,
        )
        self.assertIsNone(cert)

    def test_measure_refuses_unknown_operations(self) -> None:
        def responder(sql: str):
            if "QUALIFY ROW_NUMBER" in sql:
                return [(10, 777, LAST_INGEST, LAST_COMMIT, 36)]
            if "journal_entry_type" in sql:
                return [(None, 10)]
            if "HASH_AGG(PAYLOAD:event_id" in sql:
                return [(10, 10, 777)]
            raise AssertionError(sql)

        cert = measure_table_certification(
            FakeCursor(responder),
            SITE,
            "SALE",
            missing=0,
            extra=0,
            pending_files=0,
            end=END,
        )
        self.assertIsNone(cert)

    def test_measure_rejects_invalid_ledger_counters(self) -> None:
        cursor = FakeCursor(measured_responder)
        with self.assertRaises(ValueError):
            measure_table_certification(
                cursor, SITE, "SALE", missing=-1, extra=0, pending_files=0
            )
        with self.assertRaises(ValueError):
            measure_table_certification(
                cursor, SITE, "SALE", missing=0, extra=True, pending_files=0
            )
        self.assertEqual(cursor.executed, [])

    def test_measure_rejects_naive_window_end(self) -> None:
        with self.assertRaises(ValueError):
            measure_table_certification(
                FakeCursor(measured_responder),
                SITE,
                "SALE",
                missing=0,
                extra=0,
                pending_files=0,
                end=datetime(2026, 9, 20, 10, 0, 0),
            )


class CertifyDocumentTest(unittest.TestCase):
    def test_document_round_trip(self) -> None:
        document = certify_document(measured())
        parsed = parse_certify_document(document)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, measured())
        self.assertEqual(document["format_version"], CERTIFY_FORMAT)

    def test_parse_rejects_extra_key(self) -> None:
        document = certify_document(measured())
        document["debug_sql"] = "select 1"
        self.assertIsNone(parse_certify_document(document))

    def test_parse_rejects_missing_key(self) -> None:
        document = certify_document(measured())
        del document["source_hash"]
        self.assertIsNone(parse_certify_document(document))

    def test_parse_rejects_wrong_format(self) -> None:
        document = certify_document(measured())
        document["format_version"] = "other"
        self.assertIsNone(parse_certify_document(document))

    def test_parse_rejects_unaligned_window(self) -> None:
        document = certify_document(measured())
        document["window"]["end_utc"] = "2026-09-20T10:00:00.5+00:00"
        self.assertIsNone(parse_certify_document(document))

    def test_parse_rejects_inverted_window(self) -> None:
        document = certify_document(measured())
        document["window"]["start_utc"] = document["window"]["end_utc"]
        self.assertIsNone(parse_certify_document(document))

    def test_parse_rejects_negative_count(self) -> None:
        document = certify_document(measured())
        document["missing"] = -1
        self.assertIsNone(parse_certify_document(document))

    def test_parse_rejects_unsafe_table_identifier(self) -> None:
        # Le manifeste est borné par le collecteur ; la sonde n'admet que
        # des identifiants de table sûrs.
        self.assertIsNone(
            parse_certify_document(certify_document(measured(table="BAD;T")))
        )

if __name__ == "__main__":
    unittest.main()
