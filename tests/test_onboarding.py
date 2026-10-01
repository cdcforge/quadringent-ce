from __future__ import annotations

import unittest

import site_fixture

from quadringent_control_plane.fleet import MANIFEST
from quadringent_control_plane.onboarding import (
    DEFAULTS,
    FLEET_TABLES,
    PROVISIONED_STAGES,
    evaluate_onboarding,
    stage_for_table,
    KEYED_TABLES,
)


def spec(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "step": "source",
        "ibmi_host": "ibmi.acme.invalid",
        "ibmi_user": "CDCAPP",
        "tls": True,
        "allow_plaintext": False,
        "secret_ref_name": "acme-test-ibmi",
        "secret_ref_key": "ISERIES_PASSWORD",
        "schema": "LEDGER",
        "table": "SALE",
        "journal_library": "DEMOLIB",
        "journal_name": "TRNJRN",
        "snowflake_database": "ACME_RAW",
        "snowflake_schema": "IBMI_TEST",
        "snowflake_stage": "IBMI_TEST_SALE_EXTERNAL_STAGE",
        "connectivity": "ok",
        "pilot": "pass",
    }
    payload.update(overrides)
    return payload


class OnboardingEvaluationTests(unittest.TestCase):
    def test_client_claims_cannot_certify_runtime_or_activation(self) -> None:
        for step in ("permissions", "pilot", "verdict", "activate"):
            with self.subTest(step=step):
                result = evaluate_onboarding(spec(step=step, connectivity="ok", pilot="pass"))
                self.assertEqual(result["proven"], [])
                self.assertTrue(result["declared"])
                self.assertIn("Connectivité IBM i non observée", result["unproven"])
                self.assertNotEqual(result["status"], "ready")
                if step == "activate":
                    self.assertEqual(result["status"], "blocked")

    def test_client_cannot_inject_server_evidence(self) -> None:
        result = evaluate_onboarding(spec(step="activate", proven=["runtime verified"],
            evidence={"status": "pass"}, runtime_verified=True))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["proven"], [])

    def test_nominal_source_step_points_to_permissions(self) -> None:
        result = evaluate_onboarding(spec(step="source"))

        self.assertEqual(result["status"], "ok")
        self.assertIn("sans vérification", result["next_action"])
        self.assertEqual(result["defaults"]["replica_count_at_rest"], 0)
        self.assertEqual(result["defaults"]["tls"], True)

    def test_loading_success_and_error_states_are_explicit(self) -> None:
        missing = evaluate_onboarding({"step": "source"})
        self.assertEqual(missing["status"], "error")
        self.assertIn("L'hôte IBM i est obligatoire", missing["errors"])

        connected = evaluate_onboarding(spec(step="permissions", connectivity="ok"))
        self.assertEqual(connected["status"], "ok")
        self.assertEqual(connected["proven"], [])

        failed = evaluate_onboarding(spec(step="permissions", connectivity="error"))
        self.assertEqual(failed["status"], "error")
        self.assertTrue(any("Échec de connectivité déclaré" in item for item in failed["blocked"]))

    def test_plaintext_is_blocked(self) -> None:
        result = evaluate_onboarding(spec(tls=False, allow_plaintext=True))
        self.assertIn("TLS est obligatoire ; le plaintext IBM i est interdit", result["blocked"])
        self.assertEqual(result["status"], "error")

    def test_tls_ca_file_default_is_the_image_bundle(self) -> None:
        self.assertEqual(DEFAULTS["tls_ca_file"], "/app/certs/ibmi-test-ca.pem")
        result = evaluate_onboarding(spec(step="source"))
        self.assertEqual(result["defaults"]["tls_ca_file"], "/app/certs/ibmi-test-ca.pem")

    def test_empty_tls_ca_file_is_blocked(self) -> None:
        result = evaluate_onboarding(spec(step="source", tls_ca_file=""))
        self.assertEqual(result["status"], "error")
        self.assertIn("Le fichier CA TLS IBM i est obligatoire", result["blocked"])

    def test_password_payload_is_rejected_without_echo(self) -> None:
        result = evaluate_onboarding({"step": "source", "password": "super-secret", "ibmi_host": "ibmi.acme.invalid"})
        self.assertEqual(result["status"], "error")
        self.assertIn("Un secret ne doit jamais être envoyé au control plane", result["blocked"])
        serialized = str(result)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("password", serialized.lower())

    def test_as400_alpha_destination_is_blocked(self) -> None:
        result = evaluate_onboarding(
            spec(
                step="destination",
                snowflake_schema="POPSINK_ALPHA",
                snowflake_stage="POPSINK_ALPHA_STAGE",
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertTrue(any("IBMI_TEST" in item or "POPSINK" in item for item in result["blocked"]))

    def test_a_table_outside_the_prepared_perimeter_is_rejected(self) -> None:
        result = evaluate_onboarding(spec(step="journal", table="INCONNUE"))
        self.assertIn(
            "Au moins une table déclarée est hors du périmètre préparé", result["errors"]
        )

    def test_a_prepared_table_is_accepted(self) -> None:
        result = evaluate_onboarding(spec(step="journal", table="CNTR"))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["status"], "ok")

    def test_unobserved_connectivity_is_never_proven(self) -> None:
        result = evaluate_onboarding(spec(step="permissions", connectivity="unknown"))
        self.assertIn("Connectivité IBM i non observée", result["unproven"])
        self.assertNotIn("Connectivité IBM i observée", result["proven"])

    def test_verdict_lists_proven_unproven_and_next_safe_action(self) -> None:
        result = evaluate_onboarding(spec(step="verdict", connectivity="ok", pilot="unobserved"))
        self.assertEqual(result["status"], "review")
        self.assertIn("Journal LEDGER sélectionné pour 1 table(s)", result["declared"])
        self.assertEqual(result["verification_scope"], "configuration_only")
        self.assertTrue(any("pilote" in item.lower() for item in result["unproven"]))
        self.assertIn("n'est pas observé", result["next_action"])

    def test_activate_stays_blocked_even_when_client_reports_success(self) -> None:
        blocked = evaluate_onboarding(spec(step="activate", pilot="unobserved"))
        self.assertEqual(blocked["status"], "blocked")
        claimed = evaluate_onboarding(spec(step="activate", connectivity="ok", pilot="pass"))
        self.assertEqual(claimed["status"], "blocked")
        self.assertTrue(claimed["blocked"])
        self.assertIn("ne vérifie ni ne lance de Job", claimed["next_action"])
        self.assertEqual(DEFAULTS["replica_count_at_rest"], 0)

    def test_failed_pilot_blocks_activation(self) -> None:
        result = evaluate_onboarding(spec(step="activate", pilot="fail"))
        self.assertEqual(result["status"], "error")
        self.assertTrue(any("Échec du pilote déclaré" in item for item in result["blocked"]))


if __name__ == "__main__":
    unittest.main()


class OnboardingFleetSelectionTests(unittest.TestCase):
    """La sélection des treize tables est évaluée, pas supposée."""

    def test_the_full_fleet_selection_is_declared_once(self) -> None:
        result = evaluate_onboarding(spec(step="journal", tables=list(MANIFEST)))

        self.assertEqual(result["errors"], [])
        self.assertEqual(result["blocked"], [])
        self.assertIn("Journal LEDGER sélectionné pour 13 table(s)", result["declared"])

    def test_an_unknown_table_is_refused_without_echoing_it(self) -> None:
        result = evaluate_onboarding(spec(step="journal", tables=["SALE", "SECRET_TABLE"]))

        self.assertIn("Au moins une table déclarée est hors du périmètre préparé", result["errors"])
        self.assertNotIn("SECRET_TABLE", str(result))

    def test_a_duplicate_table_is_refused(self) -> None:
        result = evaluate_onboarding(spec(step="journal", tables=["SALE", "sale"]))

        self.assertIn("La sélection contient une table en double", result["errors"])

    def test_an_empty_selection_is_refused(self) -> None:
        result = evaluate_onboarding(spec(step="journal", tables=[]))

        self.assertIn("Sélectionnez les tables à copier", result["errors"])

    def test_a_malformed_selection_is_refused(self) -> None:
        for value in ("SALE", {"table": "SALE"}, 13, [1, None]):
            with self.subTest(value=value):
                result = evaluate_onboarding(spec(step="journal", tables=value))
                self.assertEqual(result["errors"], ["Sélection de tables illisible"])

    def test_a_single_unreadable_entry_does_not_repeat_the_reason(self) -> None:
        result = evaluate_onboarding(spec(step="journal", tables=["SALE", None, 3]))

        self.assertEqual(result["errors"], ["Sélection de tables illisible"])

    def test_one_missing_destination_is_reported_per_selection(self) -> None:
        result = evaluate_onboarding(spec(step="destination", tables=list(MANIFEST)))

        self.assertIn(
            "Destination Snowflake non provisionnée pour 12 des 13 tables sélectionnées",
            result["blocked"],
        )
        self.assertEqual(result["status"], "error")

    def test_the_sale_only_selection_keeps_its_destination(self) -> None:
        result = evaluate_onboarding(spec(step="destination", tables=["SALE"]))

        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["status"], "ok")

    def test_an_unprovisioned_stage_is_refused(self) -> None:
        result = evaluate_onboarding(spec(step="destination", snowflake_stage="IBMI_TEST_CNTR_EXTERNAL_STAGE"))

        self.assertIn("Cette zone de dépôt n'est pas provisionnée en TEST", result["blocked"])

    def test_activation_requires_the_prepared_perimeter(self) -> None:
        partial = evaluate_onboarding(spec(step="activate", tables=["SALE", "CNTR"]))
        complete = evaluate_onboarding(spec(step="activate", tables=list(MANIFEST)))

        self.assertIn(
            "L'activation exige la sélection des 13 tables préparées ; 2 déclarée(s)",
            partial["blocked"],
        )
        self.assertNotIn(
            "L'activation exige la sélection des 13 tables préparées ; 13 déclarée(s)",
            complete["blocked"],
        )

    def test_a_legacy_single_table_payload_is_not_treated_as_a_fleet_selection(self) -> None:
        result = evaluate_onboarding(spec(step="activate", table="SALE"))

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(
            any("tables préparées" in item for item in result["blocked"])
        )

    def test_the_fleet_table_list_matches_the_prepared_manifest(self) -> None:
        from quadringent_control_plane.fleet import MANIFEST as FLEET_MANIFEST

        self.assertEqual(FLEET_TABLES, FLEET_MANIFEST)
        self.assertEqual(len(FLEET_TABLES), 13)
        self.assertEqual(stage_for_table("SALE"), "IBMI_TEST_SALE_EXTERNAL_STAGE")
        self.assertIn(stage_for_table("SALE"), PROVISIONED_STAGES)


class KeyDisclosureTests(unittest.TestCase):
    """Une table sans clé doit être annoncée, pas découverte après la copie.

    Mesure du 17/09 sur le catalogue réel : huit tables sur treize n'ont aucune
    clé unique dans IBM i. Sans clé, la copie reste possible mais aucune table
    d'état ne peut être construite.
    """

    def evaluate(self, tables: list[str]) -> dict:
        return evaluate_onboarding({
            "step": "destination",
            "tables": tables,
            "ibmi_host": "ibmi.acme.invalid",
            "ibmi_user": "API",
            "snowflake_database": "ACME_RAW",
            "snowflake_schema": "IBMI_TEST",
            "stage": "IBMI_TEST_ORDER_EXTERNAL_STAGE",
        })

    def test_a_keyed_selection_is_declared_as_such(self) -> None:
        verdict = self.evaluate(["CUSTOM1", "ADDRS1"])
        declared = " ".join(verdict.get("declared", []))
        self.assertIn("2 des 2", declared)
        self.assertFalse(
            any("sans clé" in item for item in verdict.get("unproven", [])),
            "aucune table sans clé ne devait être signalée",
        )

    def test_an_unkeyed_table_is_declared_as_a_limitation(self) -> None:
        verdict = self.evaluate(["CNTR", "ORDER"])
        declared = " ".join(verdict.get("declared", []))
        unproven = " ".join(verdict.get("unproven", []))
        self.assertIn("1 des 2", declared)
        self.assertIn("sans clé", unproven)

    def test_an_absent_key_never_blocks_the_copy(self) -> None:
        """Copier reste possible sans clé : c'est une limite dite, pas un refus.

        Le verdict peut bloquer pour une autre raison (zone de dépôt non
        provisionnée), mais jamais à cause de la clé.
        """

        verdict = self.evaluate(["CNTR"])
        for item in verdict.get("blocked", []):
            self.assertNotIn("clé", item.lower(), f"la clé ne doit pas bloquer : {item}")
        self.assertIn("sans clé", " ".join(verdict.get("unproven", [])))

    def test_the_five_keyed_tables_are_the_ones_from_the_catalogue(self) -> None:
        """La liste vient du catalogue, elle n'est pas supposée."""

        self.assertEqual(
            set(KEYED_TABLES), {"ADDRS1", "CAL001", "CUSTOM1", "ORDER", "SALE"}
        )
        self.assertEqual(len(KEYED_TABLES), 5)


class SiteSecretPairTest(unittest.TestCase):
    """La référence au Secret IBM i est un couple indissociable : nom et clé
    sont déclarés ensemble ou absents ensemble."""

    def test_secret_name_without_key_is_refused(self) -> None:
        from quadringent.site_config import SiteConfigurationError

        for overrides in (
            {"ibmi_password_secret": "acme-ibmi"},
            {"ibmi_password_key": "ISERIES_PASSWORD"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(
                SiteConfigurationError
            ):
                site_fixture.build_test_site(**overrides)

    def test_secret_pair_is_accepted(self) -> None:
        site = site_fixture.build_test_site(
            ibmi_password_secret="acme-ibmi", ibmi_password_key="ISERIES_PASSWORD"
        )
        self.assertEqual("acme-ibmi", site.ibmi_password_secret)
        self.assertEqual("ISERIES_PASSWORD", site.ibmi_password_key)
