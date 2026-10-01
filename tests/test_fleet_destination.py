"""Plan de destination des treize voies brutes, fail-closed."""

from __future__ import annotations

import pytest

import site_fixture

from quadringent.fleet_destination import (
    FleetDestinationError,
    FleetDestinationPlan,
)


SITE = site_fixture.build_test_site()

TABLES = (
    "ADDRS1",
    "CAL001",
    "COST1",
    "CUSTOM1",
    "ORDER",
    "EXPENS",
    "DATE01",
    "SALE",
    "PLACE01",
    "PLACES",
    "CNTR",
    "PRODUCT",
    "HOLIDAYS",
)


def plan(site_overrides: dict | None = None, **overrides) -> FleetDestinationPlan:
    site = SITE if site_overrides is None else site_fixture.build_test_site(**site_overrides)
    arguments = {"tables": TABLES, "site": site, "pre_existing_lanes": ("SALE",)}
    arguments.update(overrides)
    return FleetDestinationPlan(**arguments)


def test_prefix_is_the_table_first_segment() -> None:
    subject = plan()
    assert subject.table_prefix("HOLIDAYS") == "ibmi/ledger/holidays/journal"
    assert subject.stage_url("HOLIDAYS") == (
        "s3://acme-000000000001-test-ibmi-raw/ibmi/ledger/holidays/journal/"
    )


def test_object_names_follow_the_schema_and_the_table() -> None:
    objects = plan().table_objects("CUSTOM1")
    assert objects.stage == "IBMI_TEST_CUSTOM1_EXTERNAL_STAGE"
    assert objects.raw_table == "QUADRINGENT_CUSTOM1_RAW"
    assert objects.pipe == "QUADRINGENT_CUSTOM1_PIPE"


def test_object_names_follow_the_site_destination_prefix() -> None:
    objects = plan(site_overrides={"destination_prefix": "CDC_FORGE"}).table_objects("CUSTOM1")
    assert objects.raw_table == "CDC_FORGE_CUSTOM1_RAW"
    assert objects.canonical_view == "CDC_FORGE_CUSTOM1_CANONICAL"
    assert objects.pipe == "CDC_FORGE_CUSTOM1_PIPE"
    assert objects.stage == "IBMI_TEST_CUSTOM1_EXTERNAL_STAGE"


def test_integration_is_widened_to_the_product_prefix_only() -> None:
    statement = plan().integration_statement()
    assert "STORAGE_ALLOWED_LOCATIONS" in statement
    assert "s3://acme-000000000001-test-ibmi-raw/ibmi/ledger/'" in statement
    # Aucun wildcard : Snowflake n'en accepte pas dans cette propriete.
    assert "*" not in statement.split("=")[1]


def test_pre_existing_lane_is_never_redefined() -> None:
    subject = plan()
    assert "SALE" in subject.pre_existing_lanes
    assert "SALE" not in subject.new_lanes()
    ddl = [
        statement
        for statement in subject.statements()
        if not statement.startswith("GRANT ")
    ]
    assert not any("QUADRINGENT_SALE_PIPE" in statement for statement in ddl)
    assert not any("IBMI_TEST_SALE_EXTERNAL_STAGE" in statement for statement in ddl)


def test_every_lane_is_measurable_by_the_verifier_role() -> None:
    """Chaque voie — héritée comprise — accorde MONITOR/SELECT au rôle
    vérificateur : sans grant, Snowflake masque l'objet à la sonde de
    livraison (régression INT : destination_configuration_missing)."""

    subject = plan()
    statements = subject.statements()
    role = subject.site.verifier_role_name
    for table in subject.tables:
        objects = subject.table_objects(table)
        assert (
            f'GRANT MONITOR ON PIPE "ACME_RAW"."IBMI_TEST"."{objects.pipe}" TO ROLE {role}'
            in statements
        )
        assert (
            f'GRANT SELECT ON TABLE "ACME_RAW"."IBMI_TEST"."{objects.raw_table}" TO ROLE {role}'
            in statements
        )
        assert (
            f'GRANT SELECT ON VIEW "ACME_RAW"."IBMI_TEST"."{objects.canonical_view}" TO ROLE {role}'
            in statements
        )
    # Les grants passent après les objets : un rôle absent n'interrompt
    # pas la création des voies.
    first_grant = next(
        index for index, s in enumerate(statements) if s.startswith("GRANT ")
    )
    last_create = max(
        index
        for index, s in enumerate(statements)
        if s.startswith(("CREATE ", "ALTER STORAGE"))
    )
    assert first_grant > last_create


def test_every_new_lane_gets_its_three_objects_in_order() -> None:
    statements = plan().statements()
    for table in plan().new_lanes():
        objects = plan().table_objects(table)
        stage_index = next(
            index
            for index, statement in enumerate(statements)
            if f'"{objects.stage}"' in statement and "CREATE STAGE" in statement
        )
        table_index = next(
            index
            for index, statement in enumerate(statements)
            if f'"{objects.raw_table}"' in statement and "CREATE TABLE" in statement
        )
        pipe_index = next(
            index
            for index, statement in enumerate(statements)
            if f'"{objects.pipe}"' in statement and "CREATE OR ALTER PIPE" in statement
        )
        assert stage_index < table_index < pipe_index


def test_pipes_carry_no_warehouse() -> None:
    for table in plan().new_lanes():
        statement = plan().create_pipe_statement(table)
        # Le parametre WAREHOUSE n'existe pas pour un tuyau : le refuser ici
        # evite une installation qui echouerait au trentieme objet.
        assert "WAREHOUSE = " not in statement


def test_creation_never_pretends_to_pause() -> None:
    """Le CREATE ne peut pas naitre en pause : Snowflake ignore la clause.

    Mesure du 17/09 : un tuyau cree avec `PIPE_EXECUTION_PAUSED = TRUE` est
    RUNNING immediatement. La creation ne doit donc pas pretendre le contraire.
    """

    subject = plan()
    for table in subject.new_lanes():
        assert "PIPE_EXECUTION_PAUSED" not in subject.create_pipe_statement(table)


def test_pause_only_touches_the_pipes_that_were_created() -> None:
    """Une reinstallation ne doit jamais suspendre un flux deja en service.

    Mesure du 17/09 : reinstaller la flotte a suspendu CNTR et ORDER alors
    qu'ils chargeaient. La pause est donc restreinte aux tuyaux absents avant
    l'execution.
    """

    subject = plan()
    # Sans restriction, la pause couvre les voies nouvelles (comportement du
    # premier deploiement).
    assert len(subject.pause_new_pipes_statements()) == len(subject.new_lanes())
    # Avec restriction, seuls les tuyaux demandes sont suspendus.
    limited = subject.pause_new_pipes_statements(("ORDER",))
    assert len(limited) == 1
    assert "QUADRINGENT_ORDER_PIPE" in limited[0]
    # La voie preexistante ne peut pas etre suspendue par erreur.
    assert len(subject.pause_new_pipes_statements(subject.new_lanes())) == len(
        subject.new_lanes()
    )
    assert not any("QUADRINGENT_SALE_PIPE" in statement for statement in subject.pause_new_pipes_statements())


def test_pause_accepts_an_empty_selection() -> None:
    """Une reinstallation ne cree aucun tuyau : rien a suspendre.

    Mesure du 17/09 : la premiere version levait une erreur sur une liste
    vide, ce qui faisait echouer une reinstallation pourtant sans effet.
    """

    assert plan().pause_new_pipes_statements(()) == ()


def test_pause_refuses_a_table_outside_the_fleet() -> None:
    with pytest.raises(FleetDestinationError):
        plan().pause_new_pipes_statements(("INCONNUE",))


def test_installation_never_resumes_a_pipe() -> None:
    statements = plan().statements()
    assert not any("PIPE_EXECUTION_PAUSED = FALSE" in statement for statement in statements)
    assert len(plan().resume_statements()) == len(plan().new_lanes())


def test_pause_covers_every_created_pipe_and_the_warehouse() -> None:
    statements = plan().pause_statements()
    assert len(statements) == len(plan().new_lanes()) + 1
    assert statements[-1].endswith("SUSPEND")


def test_a_legacy_larger_stage_covers_its_table_lane() -> None:
    subject = plan()
    assert subject.covers(
        "s3://acme-000000000001-test-ibmi-raw/ibmi/ledger/sale/", "SALE"
    )
    assert not subject.covers(
        "s3://acme-000000000001-test-ibmi-raw/ibmi/ledger/sale/", "ORDER"
    )


@pytest.mark.parametrize(
    "site_overrides",
    (
        {"destination_schema": "POPSINK_ALPHA"},
        {"destination_database": "PROD_RAW"},
        {"raw_prefix_root": "../as400"},
        {"raw_bucket": "Not-A-Bucket"},
    ),
)
def test_unsafe_destination_is_refused(site_overrides: dict) -> None:
    with pytest.raises((FleetDestinationError, ValueError)):
        plan(site_overrides=site_overrides)


def test_pause_refuses_a_lane_outside_the_fleet() -> None:
    with pytest.raises((FleetDestinationError, ValueError)):
        plan(pre_existing_lanes=("ZZTOP",))


def test_single_lane_is_refused() -> None:
    with pytest.raises(FleetDestinationError):
        FleetDestinationPlan(tables=("ORDER",), site=SITE)


def test_the_thirteen_lanes_stay_in_the_declared_schema() -> None:
    subject = plan()
    assert (
        f"{subject.database}.{subject.schema}"
        == f"{SITE.destination_database}.{SITE.destination_schema}"
    )
    assert len(subject.objects()) == 13


def test_every_lane_gets_a_canonical_view() -> None:
    """Sans vue canonique, le rejeu metier n'a rien a lire.

    Mesure du 17/09 : le provisionnement creait zone, table brute et tuyau,
    mais aucune vue canonique, et aucune table hors SALE n'en avait. Le
    chargement brut etait donc complet et le rejeu impossible.
    """

    subject = plan()
    for table in subject.tables:
        objects = subject.table_objects(table)
        assert objects.canonical_view == f"QUADRINGENT_{table}_CANONICAL"
        statement = subject.create_canonical_view_statement(table)
        assert f'"{objects.canonical_view}"' in statement
        assert "CREATE OR REPLACE VIEW" in statement
        assert f"FROM {subject._q(objects.raw_table)}" in statement
        # La vue expose les colonnes attendues par le rejeu metier.
        for column in (
            "EVENT_ID", "JOURNAL_RECEIVER", "JOURNAL_SEQUENCE", "OPERATION",
            "PAYLOAD", "SOURCE_FILE", "SOURCE_ROW_NUMBER", "INGESTED_AT",
        ):
            assert column in statement, f"colonne manquante dans la vue {table}: {column}"


def test_the_canonical_view_is_idempotent_by_event_identity() -> None:
    """Un lot recharge ne doit pas produire deux lignes techniques."""

    statement = plan().create_canonical_view_statement("CNTR")
    assert "PARTITION BY EVENT_ID" in statement
    assert "QUALIFY ROW_NUMBER()" in statement


def test_the_installation_creates_the_view_between_table_and_pipe() -> None:
    """L'ordre compte : la vue lit la table brute, le tuyau alimente la table."""

    subject = plan()
    statements = subject.statements()
    table_name = subject.new_lanes()[0]
    objects = subject.table_objects(table_name)
    raw_index = next(
        index for index, statement in enumerate(statements)
        if f'"{objects.raw_table}"' in statement and "CREATE TABLE" in statement
    )
    view_index = next(
        index for index, statement in enumerate(statements)
        if f'"{objects.canonical_view}"' in statement and "CREATE OR REPLACE VIEW" in statement
    )
    pipe_index = next(
        index for index, statement in enumerate(statements)
        if f'"{objects.pipe}"' in statement and "CREATE OR ALTER PIPE" in statement
    )
    assert raw_index < view_index < pipe_index
