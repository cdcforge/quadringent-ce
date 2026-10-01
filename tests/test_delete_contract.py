"""Contrat d'une suppression IBM i, de l'événement brut jusqu'au rejeu.

Le produit déclare l'opération `d` dans son contrat, la traduit depuis
`DELETE_ROW` côté décodeur Java, et la gère au rejeu. **Aucune suppression
réelle n'a été observée** pendant les fenêtres mesurées — les treize tables
sont en `*AFTER` — donc ce contrat est la seule preuve disponible, et il doit
tenir.

Trois propriétés sont fixées ici :

- une suppression porte une image **avant** et pas d'image après ;
- une suppression sans image avant est refusée, jamais acceptée en silence ;
- l'identité reste déterministe, donc une suppression rejouée ne peut pas être
  confondue avec une création.
"""

from __future__ import annotations

import hashlib
import unittest

from quadringent.contract import ChangeEvent, JournalPosition


def _delete(sequence: int = 500) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi",
        journal="DEMOJRN",
        library="SALES",
        table="ORDER",
        operation="d",
        position=JournalPosition(receiver="DEMOJRN4088", sequence=sequence),
        commit_timestamp="2026-09-17T00:00:00Z",
        schema_version="sha256:" + "0" * 64,
        before={"CLE": "A1", "VALEUR": "supprimee"},
        after=None,
    )


class DeleteContractTests(unittest.TestCase):
    def test_a_delete_carries_a_before_image_and_no_after(self) -> None:
        event = _delete()
        self.assertEqual(event.operation, "d")
        self.assertIsNotNone(event.before)
        self.assertIsNone(event.after)

    def test_a_delete_without_before_image_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ChangeEvent(
                source_system="ibmi",
                journal="DEMOJRN",
                library="SALES",
                table="ORDER",
                operation="d",
                position=JournalPosition(receiver="DEMOJRN4088", sequence=501),
                commit_timestamp="2026-09-17T00:00:00Z",
                schema_version="sha256:" + "0" * 64,
                before=None,
                after=None,
            )

    def test_a_delete_with_an_after_image_is_refused(self) -> None:
        """Une suppression n'a pas d'image après : l'accepter masquerait une perte."""

        with self.assertRaises(ValueError):
            ChangeEvent(
                source_system="ibmi",
                journal="DEMOJRN",
                library="SALES",
                table="ORDER",
                operation="d",
                position=JournalPosition(receiver="DEMOJRN4088", sequence=502),
                commit_timestamp="2026-09-17T00:00:00Z",
                schema_version="sha256:" + "0" * 64,
                before={"CLE": "A1"},
                after={"CLE": "A1"},
            )

    def test_the_identity_is_deterministic_for_a_delete(self) -> None:
        """Une suppression rejouée garde son identité, donc pas de doublon."""

        attendu = hashlib.sha256(b"ibmi|DEMOJRN|DEMOJRN4088|500").hexdigest()
        self.assertEqual(_delete(500).event_id, attendu)
        self.assertEqual(_delete(500).event_id, attendu)

    def test_a_delete_can_be_rebuilt_from_its_raw_record(self) -> None:
        """Le rejeu relit un enregistrement brut : il doit le reconstruire."""

        enregistrement = _delete().to_record()
        rebati = ChangeEvent.from_record(enregistrement)
        self.assertEqual(rebati.operation, "d")
        self.assertEqual(rebati.before, {"CLE": "A1", "VALEUR": "supprimee"})
        self.assertIsNone(rebati.after)
        self.assertEqual(rebati.event_id, _delete().event_id)
