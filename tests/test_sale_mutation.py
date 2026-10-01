from __future__ import annotations

import site_fixture

import json
import io
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from quadringent.contract import ChangeEvent
from quadringent.sale_mutation import (
    SALE_KEY_COLUMNS,
    build_sale_contract_events,
    evaluate_sale_mutation_window,
    load_sale_jsonl,
)


SITE = site_fixture.build_test_site()

class EvntMutationContractTests(unittest.TestCase):
    def test_equal_update_counts_do_not_certify_after_before_inversion(self) -> None:
        events = list(build_sale_contract_events(site=SITE))
        before, after = events[1:3]
        events[1] = replace(after, position=before.position)
        events[2] = replace(before, position=after.position)
        report = evaluate_sale_mutation_window(events, site=SITE)
        self.assertEqual(report["status"], "unobserved")
        check = next(c for c in report["checks"] if c["id"] == "update_image_order")
        self.assertEqual(check["status"], "unobserved")

    def test_missing_update_image_is_not_a_complete_ordered_window(self) -> None:
        for missing in ("u_before", "u_after"):
            with self.subTest(missing=missing):
                events = tuple(e for e in build_sale_contract_events(site=SITE) if e.operation != missing)
                report = evaluate_sale_mutation_window(events, site=SITE)
                check = next(c for c in report["checks"] if c["id"] == "update_image_order")
                self.assertEqual(check["status"], "unobserved")

    def test_cli_accepts_exact_byte_limit_and_rejects_one_more_byte(self) -> None:
        root = Path(__file__).parents[1]
        record = json.dumps(build_sale_contract_events(site=SITE)[0].to_record()).encode()
        boundary = record + b" " * (32 * 1024 * 1024 - len(record))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "window.jsonl"
            for extra in (b"", b" "):
                payload = boundary + extra
                path.write_bytes(payload)
                for source in ("-", str(path)):
                    with self.subTest(extra_bytes=len(extra), source_kind=source == "-"):
                        result = subprocess.run(
                            [sys.executable, str(root / "scripts/quadringent_sale_mutation_gate.py"),
                             "--input", source],
                            input=payload if source == "-" else None,
                            capture_output=True, check=False, timeout=15,
                        )
                        self.assertEqual(result.returncode, 3 if extra else 2)
                        if extra:
                            self.assertEqual(result.stdout, b"")
                            self.assertEqual(json.loads(result.stderr), {
                                "status": "invalid", "error_type": "ValueError",
                            })
                        else:
                            self.assertEqual(result.stderr, b"")
                            self.assertEqual(json.loads(result.stdout)["verdict"],
                                             "MUTATION_WINDOW_INCOMPLETE")

    def test_cli_stops_reading_oversized_input_before_materializing_the_rest(self) -> None:
        root = Path(__file__).parents[1]
        main = runpy.run_path(str(root / "scripts/quadringent_sale_mutation_gate.py"))["main"]

        class ObservedStream(io.BytesIO):
            consumed = 0

            def read(self, size: int = -1) -> bytes:
                result = super().read(size)
                self.consumed += len(result)
                return result

        for source in ("-", "oversized.jsonl"):
            with self.subTest(source=source):
                stream = ObservedStream(b"x" * (32 * 1024 * 1024 + 4096))
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("sys.stdin", SimpleNamespace(buffer=stream)), \
                     patch.object(Path, "open", return_value=stream), \
                     redirect_stdout(stdout), redirect_stderr(stderr):
                    result = main(["--input", source])
                self.assertEqual(result, 3)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(json.loads(stderr.getvalue()), {
                    "status": "invalid", "error_type": "ValueError",
                })
                self.assertEqual(stream.consumed, 32 * 1024 * 1024 + 1)

    def test_fixture_proves_technical_cud_null_additive_schema_and_replay(self) -> None:
        events = build_sale_contract_events(site=SITE)

        report = evaluate_sale_mutation_window(events, site=SITE)

        self.assertEqual(report["schema_version"], "quadringent-proof-mutation-v1")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["operation_counts"], {
            "c": 2,
            "u": 0,
            "u_before": 1,
            "u_after": 1,
            "d": 1,
        })
        self.assertEqual(report["unique_event_count"], 5)
        self.assertEqual(report["final_row_count"], 1)
        self.assertEqual(report["replay_row_count"], 1)
        self.assertEqual({check["status"] for check in report["checks"]}, {"pass"})

    def test_exact_replay_is_deduplicated_without_changing_the_snapshot(self) -> None:
        events = build_sale_contract_events(site=SITE)

        report = evaluate_sale_mutation_window(events + (events[0],), site=SITE)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["input_event_count"], 6)
        self.assertEqual(report["unique_event_count"], 5)
        self.assertEqual(report["duplicate_replay_count"], 1)
        self.assertEqual(report["final_row_count"], report["replay_row_count"])

    def test_missing_delete_or_schema_evolution_is_incomplete_never_green(self) -> None:
        events = build_sale_contract_events(site=SITE)

        report = evaluate_sale_mutation_window(
            tuple(event for event in events if event.operation != "d")
        , site=SITE)

        self.assertEqual(report["status"], "unobserved")
        alerts = {check["id"]: check["status"] for check in report["checks"]}
        self.assertEqual(alerts["operation_coverage"], "unobserved")

        same_schema = tuple(
            ChangeEvent(
                source_system=event.source_system,
                journal=event.journal,
                library=event.library,
                table=event.table,
                operation=event.operation,
                position=event.position,
                commit_timestamp=event.commit_timestamp,
                schema_version="sha256:sale-v1",
                before=event.before,
                after=event.after,
            )
            for event in events
        )
        report = evaluate_sale_mutation_window(same_schema, site=SITE)
        checks = {check["id"]: check["status"] for check in report["checks"]}
        self.assertEqual(report["status"], "unobserved")
        self.assertEqual(checks["additive_schema"], "unobserved")

        unpaired = tuple(event for event in events if event.operation != "u_after")
        report = evaluate_sale_mutation_window(unpaired, site=SITE)
        checks = {check["id"]: check["status"] for check in report["checks"]}
        self.assertEqual(report["status"], "unobserved")
        self.assertEqual(checks["update_image_balance"], "unobserved")

    def test_null_business_key_is_a_breach_and_never_exposes_key_values(self) -> None:
        events = list(build_sale_contract_events(site=SITE))
        after = dict(events[0].after or {})
        after[SALE_KEY_COLUMNS[0]] = None
        events[0] = ChangeEvent(
            source_system=events[0].source_system,
            journal=events[0].journal,
            library=events[0].library,
            table=events[0].table,
            operation=events[0].operation,
            position=events[0].position,
            commit_timestamp=events[0].commit_timestamp,
            schema_version=events[0].schema_version,
            before=events[0].before,
            after=after,
        )

        report = evaluate_sale_mutation_window(tuple(events), site=SITE)

        self.assertEqual(report["status"], "breach")
        self.assertEqual(
            next(check for check in report["checks"] if check["id"] == "business_keys")["status"],
            "breach",
        )
        rendered = json.dumps(report)
        self.assertNotIn("route-secret", rendered)
        self.assertNotIn("parcel-secret", rendered)

    def test_non_scalar_nonfinite_or_boolean_business_key_is_a_clean_breach(self) -> None:
        for invalid_value in ({"nested": "value"}, ["value"], {"value"}, float("nan"), True):
            with self.subTest(value_type=type(invalid_value).__name__):
                events = list(build_sale_contract_events(site=SITE))
                after = dict(events[0].after or {})
                after[SALE_KEY_COLUMNS[0]] = invalid_value
                events[0] = ChangeEvent(
                    source_system=events[0].source_system,
                    journal=events[0].journal,
                    library=events[0].library,
                    table=events[0].table,
                    operation=events[0].operation,
                    position=events[0].position,
                    commit_timestamp=events[0].commit_timestamp,
                    schema_version=events[0].schema_version,
                    before=events[0].before,
                    after=after,
                )

                report = evaluate_sale_mutation_window(tuple(events), site=SITE)

                self.assertEqual(report["status"], "breach")
                self.assertEqual(
                    next(
                        check
                        for check in report["checks"]
                        if check["id"] == "business_keys"
                    )["status"],
                    "breach",
                )

    def test_receiver_rotation_without_a_chain_is_incomplete_not_a_false_breach(self) -> None:
        events = list(build_sale_contract_events(site=SITE))
        last = events[-1]
        events[-1] = ChangeEvent(
            source_system=last.source_system,
            journal=last.journal,
            library=last.library,
            table=last.table,
            operation=last.operation,
            position=type(last.position)("SIM0002", 1),
            commit_timestamp=last.commit_timestamp,
            schema_version=last.schema_version,
            before=last.before,
            after=last.after,
        )

        report = evaluate_sale_mutation_window(tuple(events), site=SITE)
        checks = {check["id"]: check["status"] for check in report["checks"]}

        self.assertEqual(report["status"], "unobserved")
        self.assertEqual(checks["receiver_order"], "unobserved")
        self.assertEqual(checks["position_order"], "unobserved")
        self.assertEqual(checks["idempotent_replay"], "unobserved")

    def test_jsonl_loader_rejects_forged_identity_and_invalid_images(self) -> None:
        record = build_sale_contract_events(site=SITE)[0].to_record()
        missing = dict(record)
        missing.pop("event_id")
        with self.assertRaisesRegex(ValueError, "event identity mismatch"):
            load_sale_jsonl((json.dumps(missing) + "\n").encode())

        forged = dict(record)
        forged["event_id"] = "forged"
        with self.assertRaisesRegex(ValueError, "event identity mismatch"):
            load_sale_jsonl((json.dumps(forged) + "\n").encode())

        invalid = dict(record)
        invalid["after"] = "not-an-object"
        with self.assertRaisesRegex(ValueError, "row images must be objects"):
            load_sale_jsonl((json.dumps(invalid) + "\n").encode())

    def test_jsonl_loader_accepts_a_bounded_canary_above_ten_megabytes(self) -> None:
        event = build_sale_contract_events(site=SITE)[0]
        padded = ChangeEvent(
            source_system=event.source_system,
            journal=event.journal,
            library=event.library,
            table=event.table,
            operation=event.operation,
            position=event.position,
            commit_timestamp=event.commit_timestamp,
            schema_version=event.schema_version,
            before=event.before,
            after={**(event.after or {}), "EVPAD": "x" * (10 * 1024 * 1024)},
        )
        payload = (json.dumps(padded.to_record()) + "\n").encode()
        self.assertGreater(len(payload), 10 * 1024 * 1024)

        loaded = load_sale_jsonl(payload)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].event_id, padded.event_id)

    def test_jsonl_loader_rejects_a_payload_above_thirty_two_megabytes(self) -> None:
        payload = b"x" * ((32 * 1024 * 1024) + 1)

        with self.assertRaisesRegex(ValueError, "exceeds the size limit"):
            load_sale_jsonl(payload)

    def test_cli_is_truthful_for_fixture_and_incomplete_live_window(self) -> None:
        root = Path(__file__).parents[1]
        fixture = subprocess.run(
            [sys.executable, str(root / "scripts/quadringent_sale_mutation_gate.py")],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(fixture.returncode, 2, fixture.stderr)
        self.assertEqual(json.loads(fixture.stdout)["verdict"], "CONTRACT_PASS_LIVE_PENDING")

        events = build_sale_contract_events(site=SITE)[:-1]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "window.jsonl"
            source.write_text(
                "".join(json.dumps(event.to_record()) + "\n" for event in events),
                encoding="utf-8",
            )
            live = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/quadringent_sale_mutation_gate.py"),
                    "--input",
                    str(source),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(live.returncode, 2, live.stderr)
        self.assertEqual(json.loads(live.stdout)["status"], "unobserved")

        streamed = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/quadringent_sale_mutation_gate.py"),
                "--input",
                "-",
            ],
            cwd=root,
            input="".join(json.dumps(event.to_record()) + "\n" for event in events),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(streamed.returncode, 2, streamed.stderr)
        self.assertEqual(json.loads(streamed.stdout)["status"], "unobserved")


if __name__ == "__main__":
    unittest.main()
