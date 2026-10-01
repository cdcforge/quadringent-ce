from __future__ import annotations

from pathlib import Path
import re
import unittest

from quadringent_research.multi_object_capture import (
    ac2_claim_from_groups,
    gate_multi_tables,
    group_events,
    parse_multi_stdout,
    parse_tables,
    row_to_event,
)
from as400_ac2_gate import extract_multi_tables


class MultiObjectCaptureTests(unittest.TestCase):
    def test_gate_requires_addrs1_then_custom1(self) -> None:
        self.assertEqual(gate_multi_tables(["ADDRS1", "CUSTOM1"]), ["ADDRS1", "CUSTOM1"])
        with self.assertRaisesRegex(ValueError, "ac2 multi pair"):
            gate_multi_tables(["CUSTOM1", "ADDRS1"])
        with self.assertRaisesRegex(ValueError, "ac2 multi pair"):
            gate_multi_tables(["ADDRS1", "CAL001"])

    def test_parse_tables_rejects_single(self) -> None:
        with self.assertRaisesRegex(ValueError, ">=2"):
            parse_tables("ADDRS1")

    def test_parse_multi_stdout_splits_two_table_summaries(self) -> None:
        stdout = "\n".join(
            [
                '{"event":"retrieve_start","object_names":"ADDRS1,CUSTOM1"}',
                '{"event":"sql_row","table":"ADDRS1","sequence":"210000001","type":"PT","hex_sha256":"aa"}',
                '{"event":"sql_row","table":"CUSTOM1","sequence":"210000002","type":"UP","hex_sha256":"bb"}',
                '{"decoded":"2","elapsed_ms":"1800","event":"retrieve_summary","events_per_sec":"1.111","object_names":"ADDRS1,CUSTOM1"}',
                '{"decoded":"1","elapsed_ms":"1800","event":"retrieve_summary","table":"ADDRS1","events_per_sec":"0.556"}',
                '{"decoded":"1","elapsed_ms":"1800","event":"retrieve_summary","table":"CUSTOM1","events_per_sec":"0.556"}',
            ]
        )
        parsed = parse_multi_stdout(stdout)
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["summaries"]["ADDRS1"]["decoded"], 1)
        self.assertEqual(parsed["summaries"]["CUSTOM1"]["elapsed_ms"], 1800)
        self.assertFalse(parsed["timeout"])

    def test_row_to_event_and_ac2_claim(self) -> None:
        rows = [
            {"table": "ADDRS1", "sequence": "210000001", "type": "PT", "hex_sha256": "aa", "timestamp": "2026-08-26"},
            {"table": "CUSTOM1", "sequence": "210000002", "type": "UP", "hex_sha256": "bb", "timestamp": "2026-08-26"},
        ]
        events = [
            row_to_event(row, journal="DEMOJRN", library="SALES", receiver="DEMOJRN3769")
            for row in rows
        ]
        grouped = group_events(events, ["ADDRS1", "CUSTOM1"])
        summaries = {
            "ADDRS1": {"decoded": 1, "elapsed_ms": 1800},
            "CUSTOM1": {"decoded": 1, "elapsed_ms": 1800},
        }
        self.assertTrue(ac2_claim_from_groups(grouped, summaries))
        self.assertFalse(ac2_claim_from_groups({"ADDRS1": grouped["ADDRS1"]}, summaries))

    def test_extrait_un_manifeste_multi_table_synthetique(self) -> None:
        text = 'AS400_MULTI_TABLES\n  value: "ADDRS1,CUSTOM1"'
        self.assertEqual(extract_multi_tables(text), ["ADDRS1", "CUSTOM1"])

    def test_refuse_un_manifeste_sans_tables(self) -> None:
        with self.assertRaises(ValueError):
            extract_multi_tables("aucune déclaration")

    def test_java_helper_uses_union_all_object_filtered_sql(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "java/src/main/java/io/quadringent/as400/MultiObjectDisplayJournal.java"
        ).read_text(encoding="utf-8")
        self.assertIn("UNION ALL", source)
        self.assertIn("OBJECT_NAME => '%s'", source)
        self.assertNotIn("WHERE TRIM(OBJECT) IN", source)
