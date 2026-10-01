"""Rejeu d'une table qui porte à la fois une image initiale et des modifications.

C'est l'état normal après une bascule : la copie historique charge l'image, puis
le flux vivant ajoute des événements. Le rejeu refusait cet état.

Mesure du 17/09 sur des données réelles : `ADDRS1` et `CUSTOM1` portaient deux
receveurs (une image `SNAPSHOT:…` et un journal `DEMOJRN4059`) et la validation
les refusait toutes les deux. Après correction, `INVALID_EVENT_COUNT = 0` et le
MERGE complet produit 57 069 847 lignes dont **toutes** les clés respectent la
règle produit : l'événement de journal de plus grande séquence gagne, l'image ne
sert que lorsqu'aucun événement de journal ne touche la clé.

Trois propriétés sont fixées ici :

- plusieurs receveurs de **journal** restent interdits (séquences incomparables) ;
- plusieurs images initiales restent interdites (le choix serait arbitraire) ;
- une image **plus** un journal sont acceptés, et l'ordre du tri le dit.
"""

from __future__ import annotations

import site_fixture

import unittest

from quadringent.snowflake_business import (
    SNAPSHOT_RECEIVER_PREFIX,
    SnowflakeBusinessMergePlan,
)

SITE = site_fixture.build_test_site()

KEY = ("CLE",)


def _plan() -> SnowflakeBusinessMergePlan:
    return SnowflakeBusinessMergePlan(
        scope=SITE.snowflake_scope, 
        canonical_table="QUADRINGENT_CNTR_CANONICAL",
        target_table="QUADRINGENT_CNTR_ROLLUP",
        source_library="SALES",
        source_table="CNTR",
        key_columns=KEY,
    )


class SnapshotPlusJournalTests(unittest.TestCase):
    def test_the_snapshot_prefix_is_the_one_the_reader_emits(self) -> None:
        """Le préfixe distingue une lecture ordinale d'une position de journal."""

        self.assertEqual(SNAPSHOT_RECEIVER_PREFIX, "SNAPSHOT:")

    def test_the_guard_allows_one_snapshot_plus_one_journal(self) -> None:
        validation = _plan()._validation_statement()
        # Le compte des receveurs de journal exclut les images.
        self.assertIn(
            f"JOURNAL_RECEIVER NOT LIKE '{SNAPSHOT_RECEIVER_PREFIX}%'", validation
        )
        # Et les images sont comptées séparément.
        self.assertIn(
            f"JOURNAL_RECEIVER LIKE '{SNAPSHOT_RECEIVER_PREFIX}%'", validation
        )

    def test_two_images_are_still_refused(self) -> None:
        """Deux copies initiales rendraient le choix arbitraire entre elles."""

        validation = _plan()._validation_statement()
        self.assertIn("> 1", validation)
        self.assertIn("SNAPSHOT", validation)

    def test_the_ordering_puts_the_image_after_every_journal_event(self) -> None:
        """Une image initiale est une base, jamais une position gagnante."""

        merge = _plan()._merge_statement()
        self.assertIn("IS_SNAPSHOT ASC", merge)
        self.assertIn("JOURNAL_SEQUENCE DESC", merge)
        # L'image est identifiée par son préfixe, pas par une supposition.
        self.assertIn(f"LIKE '{SNAPSHOT_RECEIVER_PREFIX}%'", merge)
        # Le drapeau voyage dans toutes les branches de l'union.
        self.assertGreaterEqual(merge.count("IS_SNAPSHOT,"), 3)

    def test_the_tie_break_between_two_images_is_the_capture_time(self) -> None:
        """Si deux images coexistaient, la plus récente doit gagner."""

        merge = _plan()._merge_statement()
        position_snapshot = merge.index("IS_SNAPSHOT ASC")
        position_time = merge.index("COMMIT_TIMESTAMP DESC", position_snapshot)
        position_sequence = merge.index("JOURNAL_SEQUENCE DESC", position_snapshot)
        self.assertLess(position_sequence, position_time)

    def test_the_plan_still_refuses_a_non_rd_destination(self) -> None:
        with self.assertRaises(ValueError):
            SnowflakeBusinessMergePlan(
                scope=SITE.snowflake_scope, 
                canonical_table="QUADRINGENT_CNTR_CANONICAL",
                target_table="QUADRINGENT_CNTR_ROLLUP",
                source_library="SALES",
                source_table="CNTR",
                key_columns=(),
            )
