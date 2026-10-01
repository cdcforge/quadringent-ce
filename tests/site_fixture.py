"""Site fictif partagé par la suite de tests.

Aucune valeur ici n'appartient à une installation réelle : le site « acme »
sert uniquement à vérifier que le produit résout son périmètre depuis la
configuration déclarée, jamais depuis des défauts du code. Le manifeste
repren­d les noms de tables utilisés par les jeux de données capturés de la
suite — ce sont des données d'entrée, pas une identité de déploiement.

Importer ce module renseigne les variables ``QUADRINGENT_*`` attendues par
``quadringent.site_config.from_environment`` — les tests peuvent ensuite
appeler ``current()``, ou ``install(build_test_site())`` pour une instance
modifiée.
"""

from __future__ import annotations

import os

TEST_SITE_ENV: dict[str, str] = {
    "QUADRINGENT_ENVIRONMENT": "test",
    "QUADRINGENT_SITE_ID": "acme",
    "QUADRINGENT_AWS_ACCOUNT_ID": "000000000001",
    "QUADRINGENT_AWS_REGION": "us-east-1",
    "QUADRINGENT_RAW_BUCKET": "acme-000000000001-test-ibmi-raw",
    "QUADRINGENT_RAW_PREFIX_ROOT": "ibmi/ledger",
    "QUADRINGENT_CHECKPOINT_TABLE": "cdc-checkpoints",
    "QUADRINGENT_SOURCE_SCHEMA": "LEDGER",
    "QUADRINGENT_PROOF_TABLE": "SALE",
    "QUADRINGENT_JOURNAL_NAME": "TRNJRN",
    "QUADRINGENT_DESTINATION_DATABASE": "ACME_RAW",
    "QUADRINGENT_DESTINATION_SCHEMA": "IBMI_TEST",
    "QUADRINGENT_DESTINATION_ID": "ibmi-test",
    "QUADRINGENT_FLEET_TABLES": (
        "ADDRS1,CAL001,COST1,CUSTOM1,ORDER,EXPENS,DATE01,SALE,"
        "PLACE01,PLACES,CNTR,PRODUCT,HOLIDAYS"
    ),
    "QUADRINGENT_KEYED_TABLES": "ADDRS1,CAL001,CUSTOM1,ORDER,SALE",
    "QUADRINGENT_PROVISIONED_STAGES": "SALE",
    "QUADRINGENT_PROOF_KEY_COLUMNS": "SDOM,SCOD,SSEQ,SORD,STYP,SDISC,SDATE",
    "QUADRINGENT_FORBIDDEN_FRAGMENTS": "POPSINK",
    "QUADRINGENT_IBMI_TLS_CA_FILE": "/app/certs/ibmi-test-ca.pem",
    "QUADRINGENT_SNOWFLAKE_ACCOUNT": "ACME-ACME_CORP",
    "QUADRINGENT_SNOWFLAKE_CONNECTION": "acme",
    "QUADRINGENT_AWS_PROFILE": "acme-test",
    "QUADRINGENT_IBMI_HOST": "ibmi.acme.invalid",
    "QUADRINGENT_IBMI_USER": "CDCAPP",
}


def _apply(environ: dict[str, str] | None = None) -> None:
    for key, value in (environ or TEST_SITE_ENV).items():
        os.environ[key] = value


def build_test_site(**overrides: object):
    """SiteConfig du site fictif ; ``overrides`` remplace des champs."""

    from dataclasses import replace

    from quadringent.site_config import from_environment

    site = from_environment(TEST_SITE_ENV)
    if overrides:
        site = replace(site, **overrides)
    return site


def install_test_site(**overrides: object):
    """Installe le site fictif (ou une variante) comme configuration courante."""

    from quadringent.site_config import install

    return install(build_test_site(**overrides))


_apply()
