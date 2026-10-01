"""Rejeu quand la capture franchit une rotation de receveur.

Le journal tourne vite — mesuré le 17/09, environ sept receveurs par jour — donc
tôt ou tard les événements d'une table viennent de plusieurs receveurs. Le rejeu
refusait ce cas, ce qui rendait la table injouable dès le premier franchissement.

Le refus avait une raison solide : **les séquences ne sont pas globalement
croissantes**. Mesure sur la source, 14/09 : `DEMOJRN4042` finissait à
901 994 124 et `DEMOJRN4043` commençait à **1**. Trier par séquence sans
distinguer les receveurs ferait donc passer un événement ancien pour récent.

La correction dérive le **rang du receveur dans la chaîne** de l'horodatage de
son plus ancien événement : un journal écrit dans un receveur à la fois, donc
ces horodatages croissent le long de la chaîne et ne dépendent pas du numéro de
séquence.

Vérifié sur Snowflake avec un cas construit : séquence 900 000 000 (ancienne)
puis séquence 1 (récente, après redémarrage) — le rejeu choisit bien la récente.
Et deux receveurs partageant le même horodatage minimal sont refusés, parce que
le rang serait alors arbitraire.
"""

from __future__ import annotations

import site_fixture

import unittest

from quadringent.snowflake_business import SnowflakeBusinessMergePlan


SITE = site_fixture.build_test_site()

def _plan() -> SnowflakeBusinessMergePlan:
    return SnowflakeBusinessMergePlan(
        scope=SITE.snowflake_scope, 
        canonical_table="QUADRINGENT_CNTR_CANONICAL",
        target_table="QUADRINGENT_CNTR_ROLLUP",
        source_library="SALES",
        source_table="CNTR",
        key_columns=("CLE",),
    )


class ReceiverRotationTests(unittest.TestCase):
    def test_the_rank_is_derived_from_the_oldest_event_time(self) -> None:
        """Le rang doit venir du temps, pas du numéro de séquence."""

        merge = _plan()._merge_statement()
        self.assertIn("DENSE_RANK()", merge)
        self.assertIn("ORDER BY MIN(COMMIT_TIMESTAMP)", merge)
        self.assertIn("AS RANG_RECEVEUR", merge)

    def test_the_rank_comes_before_the_sequence_in_the_ordering(self) -> None:
        """Sans cela, une séquence ancienne passerait pour récente."""

        merge = _plan()._merge_statement()
        position_snapshot = merge.index("IS_SNAPSHOT ASC")
        position_rank = merge.index("RANG_RECEVEUR DESC", position_snapshot)
        position_sequence = merge.index("JOURNAL_SEQUENCE DESC", position_snapshot)
        self.assertLess(position_rank, position_sequence)

    def test_the_rank_travels_with_every_event(self) -> None:
        """Le rang doit être joint avant le tri, pas après."""

        merge = _plan()._merge_statement()
        self.assertIn("events_with_rank", merge)
        self.assertEqual(merge.count("FROM events_with_rank"), 4)
        # Le rang est joint aux événements, jamais recalculé dans le tri.
        self.assertNotIn("LEFT JOIN rangs ON rangs.JOURNAL_RECEIVER = latest_operation", merge)

    def test_an_ambiguous_rank_is_refused(self) -> None:
        """Deux receveurs au même horodatage minimal rendraient le rang arbitraire."""

        validation = _plan()._validation_statement()
        self.assertIn("PLUS_ANCIEN", validation)
        self.assertIn("RECEVEURS > 1", validation)

    def test_several_journal_receivers_are_no_longer_refused_outright(self) -> None:
        """C'est le cas normal après une rotation, il ne doit plus être refusé."""

        validation = _plan()._validation_statement()
        self.assertNotIn("JOURNAL_RECEIVER NOT LIKE 'SNAPSHOT:%') > 1", validation)

    def test_several_initial_images_are_still_refused(self) -> None:
        """Deux copies initiales rendraient le choix arbitraire entre elles."""

        validation = _plan()._validation_statement()
        self.assertIn("JOURNAL_RECEIVER LIKE 'SNAPSHOT:%') > 1", validation)
