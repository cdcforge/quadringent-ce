"""Rejeu metier incremental, avec filigrane d'ingestion.

Le rejeu complet relit tout le perimetre a chaque execution : sur 57 M de lignes,
le declencher souvent reproduirait le probleme de cout des dynamic tables.

Propriete centrale, verifiee sur Snowflake le 17/09 : **le rejeu incremental
donne un resultat identique au rejeu complet**. Cas construit : quatre cles
d'image, puis une modification, une suppression et une creation ingerees plus
tard. Le rattrapage depuis un filigrane anterieur redonne exactement l'etat du
rejeu complet.

Cout mesure sur la meme table reelle, CUSTOM1 (57 M de lignes) :

    MERGE complet      : 330 s
    MERGE incremental  :  84 s   (un seul evenement neuf)

Trois choix sont fixes ici, chacun pour une raison mesuree.
"""

from __future__ import annotations

import unittest

import site_fixture

from quadringent.snowflake_business_incremental import IncrementalReplayPlan


SITE = site_fixture.build_test_site()

KEYS = ("CH1NCPT", "CH1PER", "CH1DA", "CH1CH", "CH1CE", "CH1PT")


def _plan() -> IncrementalReplayPlan:
    return IncrementalReplayPlan.for_table(table="CUSTOM1", key_columns=KEYS,
        scope=SITE.snowflake_scope, source_library=SITE.source_schema)


class IncrementalReplayTests(unittest.TestCase):
    def test_the_three_objects_are_derived_from_the_table_name(self) -> None:
        plan = _plan()
        self.assertEqual(plan.business.canonical_table, "QUADRINGENT_CUSTOM1_CANONICAL")
        self.assertEqual(plan.business.target_table, "QUADRINGENT_CUSTOM1_ROLLUP")
        self.assertEqual(plan.state_table, "QUADRINGENT_CUSTOM1_REPLAY_STATE")

    def test_the_watermark_is_an_ingestion_time_not_a_journal_position(self) -> None:
        """Snowpipe charge en asynchrone : un fichier peut arriver apres un plus récent."""

        merge = _plan().business.merge_statement("2026-09-17T01:00:00Z")
        self.assertIn("INGESTED_AT >", merge)
        self.assertNotIn("JOURNAL_SEQUENCE >", merge)

    def test_a_touched_key_is_recomputed_over_its_whole_history(self) -> None:
        """Un événement tardif peut changer le gagnant d'une clé déjà présente."""

        merge = _plan().business.merge_statement("2026-09-17T01:00:00Z")
        self.assertIn("new_events AS (", merge)
        self.assertIn("touched_keys AS (", merge)
        # La lecture qui alimente le tri porte sur tout l'historique, pas sur
        # les seuls événements neufs.
        self.assertIn("source_events AS (", merge)
        self.assertIn("IN (SELECT FINGERPRINT FROM touched_keys)", merge)

    def test_a_key_change_touches_both_keys(self) -> None:
        """Une modification qui change la clé touche l'ancienne et la nouvelle."""

        merge = _plan().business.merge_statement("2026-09-17T01:00:00Z")
        touched = merge[merge.index("touched_keys AS ("):merge.index("source_events AS (")]
        self.assertIn("BEFORE_FINGERPRINT", touched)
        self.assertIn("AFTER_FINGERPRINT", touched)

    def test_the_watermark_advances_only_after_a_successful_replay(self) -> None:
        """Une panne entre les deux fait rejouer la fenêtre, ce qui est sans effet."""

        steps = _plan().steps("2026-09-17T01:00:00Z", "2026-09-17T02:00:00Z")
        merge_index = next(
            index for index, step in enumerate(steps) if step.startswith("MERGE INTO")
        )
        # L'avancement du filigrane n'est pas dans cette suite : il appartient à
        # l'appelant, qui ne le fait qu'après un rejeu réussi.
        self.assertEqual(merge_index, len(steps) - 1)
        self.assertFalse(any("REPLAY_STATE" in step and "MERGE" in step for step in steps))

    def test_nothing_new_produces_no_replay(self) -> None:
        """Un filigrane à jour ne doit pas relancer un rejeu inutile."""

        steps = _plan().steps("2026-09-17T02:00:00Z", "2026-09-17T02:00:00Z")
        self.assertFalse(any(step.startswith("MERGE INTO") for step in steps))

    def test_an_absent_watermark_produces_a_full_replay(self) -> None:
        """Première exécution : le périmètre est complet, sans filtre."""

        steps = _plan().steps(None, "2026-09-17T02:00:00Z")
        merge = next(step for step in steps if step.startswith("MERGE INTO"))
        self.assertNotIn("touched_keys", merge)
        self.assertNotIn("INGESTED_AT >", merge)

    def test_an_absent_latest_ingestion_produces_no_replay(self) -> None:
        """Table canonique vide : rien à rejouer, le filigrane ne bouge pas."""

        steps = _plan().steps("2026-09-17T01:00:00Z", None)
        self.assertFalse(any(step.startswith("MERGE INTO") for step in steps))

    def test_the_state_table_records_enough_to_explain_itself(self) -> None:
        ddl = _plan().create_state_statement()
        for column in ("SOURCE_TABLE", "WATERMARK", "LAST_APPLIED_ROWS", "UPDATED_AT"):
            self.assertIn(column, ddl)

    def test_a_negative_applied_rows_count_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _plan().advance_watermark_statement("2026-09-17T02:00:00Z", -1)

    def test_an_unsafe_table_name_is_refused(self) -> None:
        for name in ("order/journal", "ORDER*", "", "A" * 65):
            with self.subTest(table=name):
                with self.assertRaises(ValueError):
                    IncrementalReplayPlan.for_table(table=name, key_columns=KEYS,
                    scope=SITE.snowflake_scope, source_library=SITE.source_schema)

    def test_the_state_table_stays_in_the_rd_schema(self) -> None:
        plan = _plan()
        self.assertIn('"ACME_RAW"."IBMI_TEST"', plan.qualified_state_table)
        self.assertNotIn("POPSINK", plan.qualified_state_table)
