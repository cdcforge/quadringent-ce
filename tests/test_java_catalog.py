from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from quadringent.java_catalog import (
    CachedReceiverCatalog,
    JavaReceiverCatalog,
    parse_catalog_output,
    parse_tail_output,
)
from quadringent.continuous import ReceiverSnapshot


CATALOG_OUTPUT = """as400-receiver-catalog-v1
receiver\tQGPL\tR2\tONLINE\t100\t110
receiver\tQGPL\tR10\tONLINE\t111\t130
"""


class ReceiverCatalogParserTests(unittest.TestCase):
    def test_parser_preserves_source_order_and_normalizes_empty_bounds(self) -> None:
        receivers = parse_catalog_output(CATALOG_OUTPUT)

        self.assertEqual([receiver.receiver for receiver in receivers], ["R2", "R10"])
        self.assertEqual(receivers[0].first_sequence, 100)
        self.assertEqual(receivers[1].last_sequence, 130)

        empty = parse_catalog_output(
            "as400-receiver-catalog-v1\nreceiver\tQGPL\tR11\t-\t-\t-\n"
        )
        self.assertIsNone(empty[0].first_sequence)
        self.assertIsNone(empty[0].last_sequence)
        self.assertIsNone(empty[0].status)

    def test_parser_rejects_bad_header_malformed_rows_and_duplicates(self) -> None:
        with self.assertRaises(ValueError):
            parse_catalog_output("wrong-header\n")
        with self.assertRaises(ValueError):
            parse_catalog_output("as400-receiver-catalog-v1\nreceiver\tQGPL\tR2\n")
        with self.assertRaises(ValueError):
            parse_catalog_output(
                "as400-receiver-catalog-v1\n"
                "receiver\tQGPL\tR2\tONLINE\t100\t110\n"
                "receiver\tQGPL\tR2\tONLINE\t100\t110\n"
            )


class JavaReceiverCatalogTests(unittest.TestCase):
    def test_native_jdbc_paths_pin_iso_date_format(self) -> None:
        source_root = Path(__file__).parents[1]
        catalog_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java"
        ).read_text()
        decoder_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/JournalSession.java"
        ).read_text()
        date_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/MultiFormatJournalDate.java"
        ).read_text()
        time_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/MultiFormatJournalTime.java"
        ).read_text()

        self.assertIn('setDateFormat("iso")', catalog_source)
        self.assertIn('setProperty("date format", "iso")', catalog_source)
        self.assertIn('setProperty("secure", Boolean.toString(settings.tls()))', catalog_source)
        self.assertIn('setSecure(settings.tls())', catalog_source)
        self.assertIn('tlsValue("AS400_TLS", true)', catalog_source)
        self.assertIn('AS400_ALLOW_PLAINTEXT', catalog_source)
        self.assertNotIn('setSecure(false)', catalog_source)
        self.assertIn('setDateFormat("iso")', decoder_source)
        self.assertIn('setProperty("date format", "iso")', decoder_source)
        self.assertIn('setProperty("secure", Boolean.toString(settings.tls()))', decoder_source)
        self.assertIn('setSecure(settings.tls())', decoder_source)
        self.assertIn('new SecureAS400(', decoder_source)
        self.assertIn('tlsValue("AS400_TLS", true)', decoder_source)
        self.assertIn('AS400_ALLOW_PLAINTEXT', decoder_source)
        self.assertIn("TlsTrust.install(settings.tls())", decoder_source)
        self.assertNotIn('setSecure(false)', decoder_source)
        # Les formats de date/heure vivent dans les decodeurs multi-format ;
        # JournalSession les choisit via les variables d'environnement epinglees.
        self.assertIn('new MultiFormatJournalDate(', decoder_source)
        self.assertIn('new AS400Date(', date_source)
        self.assertIn('dateFormat("AS400_JOURNAL_DATE_FORMAT", "ISO")', decoder_source)
        self.assertIn('dateSeparator("AS400_JOURNAL_DATE_SEPARATOR", \'-\')', decoder_source)
        self.assertIn('new MultiFormatJournalTime(', decoder_source)
        self.assertIn('new AS400Time(', time_source)
        self.assertIn('timeFormat("AS400_JOURNAL_TIME_FORMAT", "ISO")', decoder_source)
        self.assertIn('timeSeparator("AS400_JOURNAL_TIME_SEPARATOR", \'.\')', decoder_source)
        self.assertIn('journal row image incomplete', decoder_source)
        # *AFTER est supporte nativement : un delete sans image n'est rejete
        # que si le RRN de l'en-tete journal est absent ou inutilisable.
        self.assertIn('no row image and no usable RRN', decoder_source)
        self.assertIn('COUNT_OR_RRN', decoder_source)
        self.assertNotIn('require *BOTH row images', decoder_source)

    def test_catalog_can_list_journaled_objects_without_row_payloads(self) -> None:
        source_root = Path(__file__).parents[1]
        catalog_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java"
        ).read_text()

        self.assertIn("AS400_OBJECT_LIBRARY", catalog_source)
        self.assertIn("QSYS2.JOURNALED_OBJECTS", catalog_source)
        self.assertIn("QSYS2.SYSTABLESTAT", catalog_source)
        self.assertIn("JOURNAL_IMAGES", catalog_source)
        self.assertIn("NUMBER_ROWS", catalog_source)
        self.assertIn("as400-object-catalog-v1", catalog_source)

    def test_tls_trust_installs_the_ibm_i_ca_and_refuses_trust_all(self) -> None:
        source_root = Path(__file__).parents[1]
        trust = (
            source_root / "java/src/main/java/io/quadringent/as400/TlsTrust.java"
        ).read_text()
        catalog_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java"
        ).read_text()
        snapshot_source = (
            source_root
            / "java/src/main/java/io/quadringent/as400/ReadOnlyTableSnapshot.java"
        ).read_text()

        self.assertIn("AS400_TLS_CA_FILE", trust)
        # Depuis le correctif de chaîne de confiance TLS (2026-09-24) :
        # aucun chemin par défaut trompeur — sans la variable, TlsTrust
        # laisse le magasin système/JVM par défaut en place (suffisant pour
        # une autorité publique), jamais une valeur codée en dur qui peut
        # ne pas exister dans l'image (voir docker/Dockerfile).
        self.assertNotIn("/app/certs/ibmi-ca.pem", trust)
        self.assertIn("SSLContext.setDefault", trust)
        self.assertIn("TrustManagerFactory", trust)
        self.assertIn("PRIVATE KEY", trust)
        self.assertNotIn("TrustAll", trust)
        self.assertNotIn("setHostnameVerifier", trust)
        self.assertIn("TlsTrust.install(settings.tls())", catalog_source)
        self.assertIn("TlsTrust.install(settings.tls())", snapshot_source)
        dl_scan = (source_root / "java/src/main/java/io/quadringent/as400/JournalDlScan.java").read_text()
        multi = (
            source_root / "java/src/main/java/io/quadringent/as400/MultiObjectDisplayJournal.java"
        ).read_text()
        self.assertIn("TlsTrust.install(true)", dl_scan)
        self.assertIn('setProperty("secure", "true")', dl_scan)
        self.assertNotIn('setProperty("secure", "false")', dl_scan)
        self.assertIn("TlsTrust.install(true)", multi)
        self.assertIn('setProperty("secure", "true")', multi)
        self.assertNotIn('envOr("AS400_TLS", "false")', multi)

    def _product_chart(self):
        import yaml
        result = subprocess.run(["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo", "-f", "infra-values/values-int.yaml"], capture_output=True, text=True, check=True)
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_le_manifeste_produit_impose_tls(self) -> None:
        docs = self._product_chart()
        data = next(d["data"] for d in docs if d["kind"] == "ConfigMap" and "AS400_TLS" in d.get("data", {}))
        self.assertEqual(data["AS400_TLS"], "true")
        self.assertEqual(data["AS400_ALLOW_PLAINTEXT"], "false")

    def test_le_manifeste_produit_borne_les_ressources_du_lecteur(self) -> None:
        docs = self._product_chart()
        capture = next(d for d in docs if d["kind"] == "Deployment" and not d["metadata"]["name"].endswith("control-plane"))
        self.assertEqual(capture["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"], "50m")
        data = next(d["data"] for d in docs if d["kind"] == "ConfigMap" and "AS400_TLS" in d.get("data", {}))
        self.assertEqual(data["ISERIES_JOURNAL_BUFFER_SIZE"], "16000000")
        # Le produit utilise un délai borné plus court que l'ancien Job de campagne.
        self.assertGreater(int(data["AS400_READER_TIMEOUT_SECONDS"]), 0)
        self.assertLessEqual(int(data["AS400_READER_TIMEOUT_SECONDS"]), 300)
        self.assertIn("AS400_RECEIVER_CATALOG_CACHE_POLLS", data)
        self.assertIn("AS400_RECEIVER_CATALOG_CACHE_SECONDS", data)

    @patch("quadringent.java_catalog.subprocess.run")
    def test_snapshot_keeps_password_out_of_java_command(self, run: object) -> None:
        completed = subprocess.CompletedProcess(
            args=["java"],
            returncode=0,
            stdout=CATALOG_OUTPUT,
            stderr="",
        )
        run.return_value = completed  # type: ignore[attr-defined]
        catalog = JavaReceiverCatalog(
            java="java",
            classpath="/app/probe.jar:/app/lib/*",
            host="ibmi-dev",
            user="catalog-user",
            journal_library="QGPL",
            journal_name="DEMOJRN",
            limit=20,
            timeout_seconds=10,
        )

        with patch.dict(os.environ, {"ISERIES_PASSWORD": "unit-only-password"}, clear=False):
            receivers = catalog.snapshot()

        self.assertEqual(len(receivers), 2)
        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertNotIn("unit-only-password", command)
        environment = run.call_args.kwargs["env"]  # type: ignore[attr-defined]
        self.assertEqual(environment["ISERIES_PASSWORD"], "unit-only-password")

    @patch("quadringent.java_catalog.subprocess.run")
    def test_snapshot_forwards_optional_service_ports(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["java"],
            returncode=0,
            stdout=CATALOG_OUTPUT,
            stderr="",
        )  # type: ignore[attr-defined]
        catalog = JavaReceiverCatalog(
            java="java",
            classpath="classpath",
            host="ibmi-dev",
            user="catalog-user",
            journal_library="QGPL",
            journal_name="DEMOJRN",
            database_port=18471,
            signon_port=18476,
            command_port=18475,
        )

        catalog.snapshot()

        environment = run.call_args.kwargs["env"]  # type: ignore[attr-defined]
        self.assertEqual(environment["AS400_DATABASE_PORT"], "18471")
        self.assertEqual(environment["AS400_SIGNON_PORT"], "18476")
        self.assertEqual(environment["AS400_COMMAND_PORT"], "18475")

    @patch("quadringent.java_catalog.subprocess.run")
    def test_snapshot_forwards_the_pinned_ca_file(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["java"], returncode=0, stdout=CATALOG_OUTPUT, stderr=""
        )  # type: ignore[attr-defined]
        catalog = JavaReceiverCatalog(
            java="java",
            classpath="classpath",
            host="ibmi-dev",
            user="catalog-user",
            journal_library="QGPL",
            journal_name="DEMOJRN",
            ca_file="/tmp/qdt-ibmi-ca-abc123.pem",
        )

        catalog.snapshot()

        environment = run.call_args.kwargs["env"]  # type: ignore[attr-defined]
        self.assertEqual(environment["AS400_TLS_CA_FILE"], "/tmp/qdt-ibmi-ca-abc123.pem")

    @patch("quadringent.java_catalog.subprocess.run")
    def test_snapshot_without_a_pinned_ca_never_leaks_an_inherited_env_value(self, run: object) -> None:
        """Objectif A : sans épinglage pour CETTE source, le sous-processus
        doit utiliser le magasin de confiance système/JVM par défaut —
        jamais une valeur AS400_TLS_CA_FILE héritée du process appelant
        (qui pourrait être celle d'une AUTRE source déjà sondée)."""

        run.return_value = subprocess.CompletedProcess(
            args=["java"], returncode=0, stdout=CATALOG_OUTPUT, stderr=""
        )  # type: ignore[attr-defined]
        catalog = JavaReceiverCatalog(
            java="java",
            classpath="classpath",
            host="ibmi-dev",
            user="catalog-user",
            journal_library="QGPL",
            journal_name="DEMOJRN",
        )

        with patch.dict(os.environ, {"AS400_TLS_CA_FILE": "/some/other/source-ca.pem"}, clear=False):
            catalog.snapshot()

        environment = run.call_args.kwargs["env"]  # type: ignore[attr-defined]
        self.assertNotIn("AS400_TLS_CA_FILE", environment)

    @patch("quadringent.java_catalog.subprocess.run")
    def test_nonzero_catalog_process_is_a_safe_error(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["java"],
            returncode=1,
            stdout="",
            stderr="credential or endpoint details",
        )  # type: ignore[attr-defined]
        catalog = JavaReceiverCatalog(
            java="java",
            classpath="classpath",
            host="ibmi-dev",
            user="catalog-user",
            journal_library="QGPL",
            journal_name="DEMOJRN",
            limit=20,
            timeout_seconds=10,
        )

        with self.assertRaises(RuntimeError) as context:
            catalog.snapshot()

        self.assertEqual(str(context.exception), "IBM i receiver catalog failed")

    def test_catalog_cache_reuses_snapshot_for_a_few_polls(self) -> None:
        class CountingCatalog:
            def __init__(self) -> None:
                self.calls = 0
                self.payload = (ReceiverSnapshot("QGPL", "R2", 100, 110),)

            def snapshot(self, required_receiver=None) -> tuple[ReceiverSnapshot, ...]:
                self.calls += 1
                self.required = required_receiver
                return self.payload

        inner = CountingCatalog()
        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=3,
            ttl_seconds=10,
            clock=lambda: 0.0,
        )

        first = catalog.snapshot()
        second = catalog.snapshot()
        third = catalog.snapshot()
        fourth = catalog.snapshot()

        self.assertEqual(inner.calls, 2)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertIs(first, third)
        self.assertEqual(fourth, first)
        self.assertEqual(inner.calls, 2)

    def test_catalog_cache_expires_by_time_even_with_remaining_polls(self) -> None:
        class CountingCatalog:
            def __init__(self) -> None:
                self.calls = 0
                self.payload = (ReceiverSnapshot("QGPL", "R2", 100, 110),)

            def snapshot(self, required_receiver=None) -> tuple[ReceiverSnapshot, ...]:
                self.calls += 1
                self.required = required_receiver
                return self.payload

        now = {"t": 0.0}
        inner = CountingCatalog()
        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=10,
            ttl_seconds=2,
            clock=lambda: now["t"],
        )

        catalog.snapshot()
        now["t"] = 2.0
        catalog.snapshot()

        self.assertEqual(inner.calls, 2)

    def test_catalog_cache_invalidate_forces_refresh(self) -> None:
        class CountingCatalog:
            def __init__(self) -> None:
                self.calls = 0
                self.payload = (ReceiverSnapshot("QGPL", "R2", 100, 110),)

            def snapshot(self, required_receiver=None) -> tuple[ReceiverSnapshot, ...]:
                self.calls += 1
                self.required = required_receiver
                return self.payload

        inner = CountingCatalog()
        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=10,
            ttl_seconds=10,
            clock=lambda: 0.0,
        )

        catalog.snapshot()
        catalog.invalidate()
        catalog.snapshot()

        self.assertEqual(inner.calls, 2)


class TailProbeParserTests(unittest.TestCase):
    def test_parses_a_single_attached_row(self) -> None:
        snapshot = parse_tail_output(
            "tail receiver=R10 library=QGPL first_sequence=111 last_sequence=140 status=ATTACHED\n"
        )

        assert snapshot is not None
        self.assertEqual(snapshot.receiver, "R10")
        self.assertEqual(snapshot.receiver_library, "QGPL")
        self.assertEqual(snapshot.first_sequence, 111)
        self.assertEqual(snapshot.last_sequence, 140)
        self.assertEqual(snapshot.status, "ATTACHED")

    def test_empty_output_means_no_attached_receiver(self) -> None:
        self.assertIsNone(parse_tail_output(""))
        self.assertIsNone(parse_tail_output("\n\n"))

    def test_rejects_malformed_or_multi_row_output(self) -> None:
        with self.assertRaises(ValueError):
            parse_tail_output("not-a-tail-row\n")
        with self.assertRaises(ValueError):
            parse_tail_output("tail receiver=R10 library=QGPL\n")
        with self.assertRaises(ValueError):
            parse_tail_output(
                "tail receiver=R10 library=QGPL first_sequence=1 last_sequence=2 status=ATTACHED\n"
                "tail receiver=R11 library=QGPL first_sequence=3 last_sequence=4 status=ATTACHED\n"
            )


class CachedReceiverCatalogTailProbeTests(unittest.TestCase):
    def _catalog(self, snapshot: tuple[ReceiverSnapshot, ...]):
        class CountingCatalog:
            def __init__(self, payload: tuple[ReceiverSnapshot, ...]) -> None:
                self.calls = 0
                self.payload = payload

            def snapshot(self, required_receiver=None) -> tuple[ReceiverSnapshot, ...]:
                self.calls += 1
                return self.payload

        return CountingCatalog(snapshot)

    def test_probe_advances_last_sequence_without_a_full_catalog_refetch(self) -> None:
        inner = self._catalog(
            (
                ReceiverSnapshot("QGPL", "R2", 100, 110),
                ReceiverSnapshot("QGPL", "R10", 111, 120, status="ATTACHED"),
            )
        )
        probes = [ReceiverSnapshot("QGPL", "R10", 111, 130, status="ATTACHED")]
        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=5,
            ttl_seconds=60,
            clock=lambda: 0.0,
            tail_probe=lambda: probes.pop(0) if probes else None,
        )

        first = catalog.snapshot()
        second = catalog.snapshot()

        self.assertEqual(inner.calls, 1)
        attached = [item for item in second if item.receiver == "R10"][0]
        self.assertEqual(attached.last_sequence, 130)
        self.assertNotEqual(first, second)

    def test_probe_showing_a_different_attached_receiver_forces_full_catalog(self) -> None:
        inner = self._catalog(
            (ReceiverSnapshot("QGPL", "R10", 111, 120, status="ATTACHED"),)
        )
        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=5,
            ttl_seconds=60,
            clock=lambda: 0.0,
            tail_probe=lambda: ReceiverSnapshot("QGPL", "R11", 121, 121, status="ATTACHED"),
        )

        catalog.snapshot()
        catalog.snapshot()

        self.assertEqual(inner.calls, 2)

    def test_probe_failure_falls_back_to_existing_cached_behaviour(self) -> None:
        inner = self._catalog(
            (ReceiverSnapshot("QGPL", "R10", 111, 120, status="ATTACHED"),)
        )

        def failing_probe() -> ReceiverSnapshot:
            raise RuntimeError("IBM i tail probe failed")

        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=5,
            ttl_seconds=60,
            clock=lambda: 0.0,
            tail_probe=failing_probe,
        )

        first = catalog.snapshot()
        second = catalog.snapshot()

        self.assertEqual(inner.calls, 1)
        self.assertEqual(first, second)

    def test_probe_cannot_move_last_sequence_backwards(self) -> None:
        inner = self._catalog(
            (ReceiverSnapshot("QGPL", "R10", 111, 140, status="ATTACHED"),)
        )
        catalog = CachedReceiverCatalog(
            inner,
            ttl_polls=5,
            ttl_seconds=60,
            clock=lambda: 0.0,
            tail_probe=lambda: ReceiverSnapshot("QGPL", "R10", 111, 130, status="ATTACHED"),
        )

        catalog.snapshot()
        second = catalog.snapshot()

        self.assertEqual(inner.calls, 1)
        attached = [item for item in second if item.receiver == "R10"][0]
        self.assertEqual(attached.last_sequence, 140)


if __name__ == "__main__":
    unittest.main()
