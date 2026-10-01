"""Magasin des liaisons : création, lecture, liste — jamais de mot de passe."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from quadringent_control_plane.connections import (
    CONNECTIONS_STATE_FILE,
    LIFECYCLE_DECLARED_NOT_IN_SERVICE,
    ConnectionsError,
    ConnectionsStore,
    connections_state_path,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore


def _fields(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "site_id": "acme",
        "display_name": "Site Acme (test)",
        "ibmi_host": "ibmi.acme.invalid",
        "ibmi_user": "CDCAPP",
        "snowflake_account": "ACME-ACME_CORP",
        "destination_database": "ACME_RAW",
        "destination_schema": "IBMI_TEST",
        "tables": ["SALE", "CNTR"],
        "secret_ref_name": "acme-test-ibmi",
        "secret_ref_key": "ISERIES_PASSWORD",
    }
    payload.update(overrides)
    return payload


class ConnectionsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = connections_state_path(self.directory.name)
        self.assertEqual(path.name, CONNECTIONS_STATE_FILE)
        self.store = ConnectionsStore(AtomicJsonStateStore(path))

    def test_create_persists_a_declared_not_in_service_connection(self) -> None:
        record = self.store.create(**_fields())
        self.assertEqual(record.lifecycle_state, LIFECYCLE_DECLARED_NOT_IN_SERVICE)
        self.assertEqual(record.site_id, "acme")
        self.assertEqual(record.tables, ("SALE", "CNTR"))
        self.assertTrue(record.created_at)

    def test_une_ecriture_impossible_devient_un_refus_de_stockage(self):
        from unittest.mock import patch
        from quadringent_control_plane.connections import ConnectionsStorageError
        with patch.object(AtomicJsonStateStore, "save", side_effect=OSError("disk unavailable")):
            with self.assertRaises(ConnectionsStorageError):
                self.store.create(**_fields())

    def test_created_connection_is_readable_by_id(self) -> None:
        created = self.store.create(**_fields())
        fetched = self.store.get(created.connection_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.to_dict(), created.to_dict())

    def test_unknown_connection_id_returns_none(self) -> None:
        self.assertIsNone(self.store.get("0" * 32))

    def test_malformed_connection_id_returns_none_without_raising(self) -> None:
        self.assertIsNone(self.store.get("not-a-valid-id"))

    def test_list_returns_every_created_connection(self) -> None:
        first = self.store.create(**_fields(display_name="Premier site"))
        second = self.store.create(**_fields(site_id="beta", display_name="Second site"))
        listed_ids = {record.connection_id for record in self.store.list()}
        self.assertEqual(listed_ids, {first.connection_id, second.connection_id})

    def test_list_on_an_empty_store_is_empty(self) -> None:
        self.assertEqual(self.store.list(), ())

    def test_connection_never_persists_a_password_field(self) -> None:
        # La signature de create() n'a structurellement aucun paramètre de mot
        # de passe : un appel avec une clé en trop est un TypeError, jamais un
        # champ silencieusement accepté puis écrit sur disque.
        with self.assertRaises(TypeError):
            self.store.create(**_fields(), ibmi_password="hunter2")  # type: ignore[call-arg]
        raw_document = Path(connections_state_path(self.directory.name)).read_text(
            encoding="utf-8"
        ) if connections_state_path(self.directory.name).exists() else ""
        self.assertNotIn("hunter2", raw_document)

    def test_create_rejects_an_invalid_site_id(self) -> None:
        with self.assertRaises(ConnectionsError):
            self.store.create(**_fields(site_id="Not Valid!"))

    def test_create_rejects_an_empty_display_name(self) -> None:
        with self.assertRaises(ConnectionsError):
            self.store.create(**_fields(display_name=""))

    def test_create_rejects_an_empty_table_selection(self) -> None:
        with self.assertRaises(ConnectionsError):
            self.store.create(**_fields(tables=[]))

    def test_create_rejects_duplicate_tables(self) -> None:
        with self.assertRaises(ConnectionsError):
            self.store.create(**_fields(tables=["SALE", "SALE"]))

    def test_create_rejects_a_malformed_table_name(self) -> None:
        with self.assertRaises(ConnectionsError):
            self.store.create(**_fields(tables=["sale-lower"]))

    def test_create_rejects_missing_secret_reference_key(self) -> None:
        with self.assertRaises(ConnectionsError):
            self.store.create(**_fields(secret_ref_key=""))

    def test_two_connections_for_different_sites_get_distinct_ids(self) -> None:
        first = self.store.create(**_fields(site_id="acme"))
        second = self.store.create(**_fields(site_id="beta"))
        self.assertNotEqual(first.connection_id, second.connection_id)

    def test_store_on_a_document_with_wrong_format_version_fails_closed(self) -> None:
        path = connections_state_path(self.directory.name)
        AtomicJsonStateStore(path).save({"format_version": "other", "connections": {}})
        with self.assertRaises(ConnectionsError):
            self.store.list()

    def test_store_on_a_corrupt_json_file_fails_closed(self) -> None:
        path = connections_state_path(self.directory.name)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ConnectionsError):
            self.store.list()



class ConnectionAccountTests(unittest.TestCase):
    """Le compte de lecture est aussi indispensable que l'adresse.

    Une liaison qui ne retient que l'hôte est inutilisable : la mise en
    service ne saurait pas avec quel compte se connecter, et l'information
    saisie par l'opérateur serait perdue en silence.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ConnectionsStore(
            AtomicJsonStateStore(connections_state_path(self.directory.name))
        )

    def test_le_compte_de_lecture_est_persiste_et_relu(self) -> None:
        self.store.create(**_fields(ibmi_user="CDCUSER"))
        reread = ConnectionsStore(
            AtomicJsonStateStore(connections_state_path(self.directory.name))
        )
        self.assertEqual(reread.list()[0].ibmi_user, "CDCUSER")

    def test_le_compte_est_publie_dans_la_reponse(self) -> None:
        record = self.store.create(**_fields(ibmi_user="CDCAPP"))
        self.assertEqual(record.to_dict()["ibmi_user"], "CDCAPP")

    def test_un_compte_hors_forme_est_refuse(self) -> None:
        for invalid in ("", "minuscules", "AVEC-TIRET", "A" * 31):
            with self.assertRaises(ConnectionsError):
                self.store.create(**_fields(ibmi_user=invalid))

if __name__ == "__main__":
    unittest.main()
